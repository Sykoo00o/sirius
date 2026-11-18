#!/usr/bin/env python3
"""Standalone HITL approval supervisor, driven by the schema.sql state model.

What this is
------------
One process that does three things on a loop:

  1. runs the orchestrator tick (expire stale intents, alert, heartbeat),
  2. reads pending tier-2 spend out of engine.transactions,
  3. posts a Telegram card per new intent with inline [Approve]/[Reject], and
     applies the human's tap back onto the row.

It targets the four-table model in supabase/schema.sql (agent_logs,
financial_ledger, transactions, hustle_metrics). That model has no idempotency
lookup for Telegram delivery (no alert_sent column, no provider-event key on
transactions), and its only server-side writers are the read helpers. So the
alert bookkeeping that prevents "spam the human every 20s for the same deal"
lives here, in process memory plus the append-only agent_logs table. The trade
is recorded in _AlertState below.

Relationship to engine/supervisor.py
-------------------------------------
Different concern, deliberately not merged. That module reconciles the agent
fleet (heartbeats, desired_state, docker restarts). This one gates spend and
drives the approval conversation. They may run in the same deployment or apart.

python-telegram-bot vs engine/telegram.py
------------------------------------------
The existing engine/telegram.py talks to the Bot API over httpx. This script
uses python-telegram-bot for the INBOUND callback routing (it ships the
UpdateQueue and callback parsing we would otherwise hand-roll) and httpx for the
OUTBOUND card send, matching engine/telegram.py's httpx wire format exactly
(same sendMessage payload, same 4000-char cap). Outbound staying on httpx keeps
one rendering path and one place that knows our HTML entity escaping. If you
would rather collapse outbound onto PTB's Application too, replace AlertSender
only; nothing else touches the transport.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# python-telegram-bot is required for the inbound callback loop. Import-guarded
# so a missing dependency is a one-line actionable message rather than a stack
# trace, and so the httpx-only pieces (card rendering) stay importable for tests.
# ---------------------------------------------------------------------------
try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CallbackQueryHandler,
        ContextTypes,
    )

    PTB_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the dep
    PTB_IMPORT_ERROR = exc
    InlineKeyboardButton = InlineKeyboardMarkup = Update = None  # type: ignore[assignment]
    ParseMode = ContextTypes = None  # type: ignore[assignment]
    Application = ApplicationBuilder = CallbackQueryHandler = None  # type: ignore[assignment]

try:  # psycopg is the DB driver; reuse the project's, never bundle a second one
    import psycopg
    from psycopg.rows import dict_row
except ImportError as exc:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    _PSYCOPG_IMPORT_ERROR: Exception | None = exc
else:
    _PSYCOPG_IMPORT_ERROR = None

log = logging.getLogger("ghost.supervisor")

# ---------------------------------------------------------------------------
# Constants and small helpers
# ---------------------------------------------------------------------------

AGENT_HANDLE = "supervisor"
TG_API = "https://api.telegram.org"

# Telegram truncates caption/text at these byte-ish limits; clamp before send so
# a long vendor description cannot make the whole send 400.
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
CALLBACK_DATA_LIMIT = 64

# Card actions. Short prefix + full UUID + verb fits well inside 64 bytes
# ("appr:11111111-1111-1111-1111-111111111111" is 46 bytes), so we carry the
# WHOLE id and resolve it exactly. The existing bot stores a 16-hex-char prefix
# (engine/telegram_bot.py:flat); that is fragile across 2^32 intents. Full UUID,
# exact match, no collision handling needed.
ACTION_APPROVE = "appr"
ACTION_REJECT = "rej"
_CALLBACK_RE = re.compile(rf"^({ACTION_APPROVE}|{ACTION_REJECT}):([0-9a-fA-F-]{{36}})$")

# Currency symbols for display only; unknown codes fall back to the ISO code.
_CCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£"}

# Per-tier emoji on the card, mirroring how much autonomy the request carries.
_TIER_EMOJI = {"tier1": "🟢", "tier2": "🟡", "tier3": "🔴"}

_CCY_CODE_RE = re.compile(r"[A-Za-z]{3}")
_HEXISH_RE = re.compile(r"^[0-9a-fA-F-]+$")


def env(name: str, default: str = "") -> str:
    """.env-shaped value cleaner: literal 'null'/'none'/quotes mean unset.

    A literal `null` in TELEGRAM_APPROVAL_BOT_TOKEN produces a confusing 401
    from Telegram; treat it as "not configured" so the loop stays alive.
    """
    raw = os.getenv(name, "").strip()
    if raw.lower() in {"", "null", "none", '""', "''", "undefined"}:
        return ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return raw


def fmt_minor(amount: int, currency: str = "USD") -> str:
    """Minor units (cents) to a human string, sign-preserving. No floats."""
    amount = int(amount)
    sign = "-" if amount < 0 else ""
    symbol = _CCY_SYMBOL.get(currency.upper(), f"{currency.upper()} ")
    return f"{sign}{symbol}{abs(amount) / 100:,.2f}"


def normalize_ccy(code: str) -> str:
    """Best-effort ISO-4217 from a currency symbol or code (for card display)."""
    code = (code or "").strip()
    if not code:
        return "USD"
    if _CCY_CODE_RE.fullmatch(code):
        return code.upper()
    return {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}.get(code, code.upper())


def short(tx_id: str) -> str:
    """First 8 hex chars, for human labels (never used as a DB key)."""
    return tx_id.replace("-", "")[:8]


def escape(value: object) -> str:
    """HTML-escape any value interpolated into an HTML-mode Telegram message."""
    return html.escape(str(value if value is not None else ""), quote=False)


def coerce_int(value: object) -> int:
    """Minor units arrive from PG as int (bigint); coerce defensively anyway."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _is_expired(tx: PendingTx, cutoff: datetime) -> bool:
    """True when expires_at is present and at/older than cutoff. Naive stamps are
    assumed UTC; a parse failure is treated as not-expired so a malformed stamp
    cannot silently drop a live intent."""
    if not tx.expires_at:
        return False
    try:
        exp = datetime.fromisoformat(tx.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp <= cutoff


def now_utc() -> datetime:
    return datetime.now(UTC)


def load_dotenv() -> None:
    """Load the repo .env into os.environ without adding a dependency.

    Existing modules lean on the runtime (docker compose env_file) rather than a
    loader; a human running `python supervisor.py` locally has no such runtime,
    so we do a minimal KEY=VALUE parse ourselves. An explicit value already in
    the environment always wins, so real env/container injection is never
    overridden by a stale file. Missing file is fine; a broken line is skipped.
    """
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().removeprefix("export ").strip()
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError as exc:
            log.debug("could not read %s: %s", path, exc)
        return


# ---------------------------------------------------------------------------
# Data model on the wire
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingTx:
    """A tier-2 intent awaiting a human tap, as read from engine.transactions."""

    transaction_id: str
    title: str
    amount_minor: int
    currency: str
    tier: str
    vendor: str | None
    agent_handle: str | None
    hustle: str | None
    rationale: str | None
    expected_roi_note: str | None
    source_url: str | None
    created_at: str | None
    expires_at: str | None

    @property
    def money(self) -> str:
        return fmt_minor(self.amount_minor, self.currency)

    def expires_label(self) -> str | None:
        if not self.expires_at:
            return None
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return self.expires_at
        secs = (exp - now_utc()).total_seconds()
        if secs <= 0:
            return "expiry reached"
        mins = int(secs // 60)
        return f"expires in {mins}m" if mins >= 1 else "expires in <1m"


def _s(row: dict[str, Any], key: str) -> str | None:
    val = row.get(key)
    return None if val is None else str(val)


def row_to_pending(row: dict[str, Any]) -> PendingTx:
    # created_at/expires_at arrive as datetimes (psycopg dict_row); stringify for
    # the wire so the dataclass has one obvious representation.
    created = row.get("created_at")
    expires = row.get("expires_at")
    return PendingTx(
        transaction_id=str(row["transaction_id"]),
        title=str(row.get("title") or "untitled intent"),
        amount_minor=coerce_int(row.get("amount_minor")),
        currency=normalize_ccy(str(row.get("currency") or "USD")),
        tier=str(row.get("tier") or "tier2"),
        vendor=_s(row, "vendor"),
        agent_handle=_s(row, "agent_handle"),
        hustle=_s(row, "hustle"),
        rationale=_s(row, "rationale"),
        expected_roi_note=_s(row, "expected_roi_note"),
        source_url=_s(row, "source_url"),
        created_at=created.isoformat() if isinstance(created, datetime) else _s(row, "created_at"),
        expires_at=expires.isoformat() if isinstance(expires, datetime) else _s(row, "expires_at"),
    )


# ---------------------------------------------------------------------------
# Card rendering (HTTP path and PTB path both call this -> one HTML source)
# ---------------------------------------------------------------------------


def approval_keyboard(tx_id: str) -> InlineKeyboardMarkup:
    """PTB reply_markup for [Approve]/[Reject]; callback_data carries the full UUID."""
    approve = InlineKeyboardButton("✅ Approve", callback_data=f"{ACTION_APPROVE}:{tx_id}")
    reject = InlineKeyboardButton("❌ Reject", callback_data=f"{ACTION_REJECT}:{tx_id}")
    return InlineKeyboardMarkup([[approve, reject]])


def approval_keyboard_json(tx_id: str) -> dict[str, Any]:
    """The same keyboard as a raw dict for httpx's reply_markup JSON field.

    Mirror of approval_keyboard(): changing one without the other lets the httpx
    and PTB render paths disagree on the buttons, the worst kind of drift because
    only one path is under load at a time. If you prefer a single path, drop the
    httpx sender (see AlertSender) and route everything through PTB.
    """
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve",
             "callback_data": f"{ACTION_APPROVE}:{tx_id}"[:CALLBACK_DATA_LIMIT]},
            {"text": "❌ Reject",
             "callback_data": f"{ACTION_REJECT}:{tx_id}"[:CALLBACK_DATA_LIMIT]},
        ]]
    }


def build_card_caption(tx: PendingTx) -> str:
    """HTML caption for sendPhoto and the message body alike."""
    emoji = _TIER_EMOJI.get(tx.tier, "🟡")
    lines = [f"{emoji} <b>Approval requested</b> · {escape(tx.tier)}"]
    if tx.vendor:
        lines.append(f"<b>{escape(tx.vendor)}</b> · {escape(tx.title)}")
    else:
        lines.append(f"<b>{escape(tx.title)}</b>")
    lines.append(f"<b>{escape(tx.money)}</b>")

    facts: list[str] = []
    if tx.agent_handle:
        facts.append(f"agent: <code>{escape(tx.agent_handle)}</code>")
    if tx.hustle:
        facts.append(f"hustle: <code>{escape(tx.hustle)}</code>")
    facts.append(f"id: <code>{escape(short(tx.transaction_id))}</code>")
    if (label := tx.expires_label()):
        facts.append(escape(label))
    lines.append(" · ".join(facts))

    if tx.rationale:
        lines.append(f"\n<i>Why:</i> {escape(tx.rationale)}")
    if tx.expected_roi_note:
        lines.append(f"<i>Expected ROI:</i> {escape(tx.expected_roi_note)}")
    if tx.source_url:
        # Anchor form keeps the raw URL out of the visible text; href is quoted
        # and attribute-escaped, so it cannot break out of the anchor.
        lines.append(f'<a href="{escape(tx.source_url)}">source</a>')
    return "\n".join(lines)[:CAPTION_LIMIT]


def send_payload(chat_id: str, tx: PendingTx) -> dict[str, Any]:
    """sendMessage body for the httpx path (mirrors engine/telegram.py exactly)."""
    return {
        "chat_id": chat_id,
        "text": build_card_caption(tx),
        "parse_mode": ParseMode.HTML if ParseMode else "HTML",
        "disable_web_page_preview": bool(tx.source_url),
        "reply_markup": json.dumps(approval_keyboard_json(tx.transaction_id)),
    }


def decision_text(action: str, tx: PendingTx, decider: str) -> str:
    """Body shown in-place once the card has been decided."""
    verb = "Approved ✅" if action == ACTION_APPROVE else "Rejected ❌"
    body = f"{verb} · <b>{escape(tx.title)}</b>\n{escape(tx.money)} · {escape(decider)}"
    return body[:TEXT_LIMIT]


# ---------------------------------------------------------------------------
# Decision parsing (callback_data -> structured), with a legacy decoder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    action: str  # ACTION_APPROVE | ACTION_REJECT
    transaction_id: str


def parse_decision(raw: str) -> Decision | None:
    """Parse our callback_data contract `appr|rej:<uuid>`. None if not a decision."""
    if m := _CALLBACK_RE.match(raw or ""):
        return Decision(action=m.group(1), transaction_id=m.group(2))
    return None


def parse_legacy_decision(data: str, resolver: Any) -> Decision | None:
    """Decode the legacy `decide:<16hex>:approve|reject` from engine/telegram_bot.py.

    The legacy bot sends a 16-char flattened UUID prefix, not the full id, so it
    must be resolved against the DB by `resolver(prefix) -> transaction_id`. If a
    second operator still runs that bot, this keeps its taps working on the
    schema.sql rows. resolver returns None when the prefix is ambiguous/unknown.
    """
    parts = (data or "").split(":")
    if len(parts) == 3 and parts[0] == "decide":
        prefix, verb = parts[1], parts[2]
        if not _HEXISH_RE.match(prefix):
            return None
        if (tx_id := resolver(prefix)) is not None:
            return Decision(ACTION_APPROVE if verb in {"approve", "a"} else ACTION_REJECT, tx_id)
    return None


# ---------------------------------------------------------------------------
# Database layer: plain psycopg3, direct against engine.transactions
# ---------------------------------------------------------------------------


class TxStore:
    """Thin data layer over the schema.sql `transactions` + `agent_logs` tables.

    Connections are opened per call. At a 20s tick and a single owner that is
    cheaper and simpler than a pool, and a transient pool outage cannot wedge the
    loop. Every write is parameterised; no string is concatenated into SQL.

    Identity is the FULL transaction_id (UUID), never a truncation, so an
    approve/reject resolves to exactly one row with no collision logic.
    """

    _PENDING_COLS = """
        transaction_id, title, amount_minor, currency, tier, vendor, agent_handle,
        hustle, rationale, expected_roi_note, source_url, created_at, expires_at
    """

    def __init__(self, database_url: str, statement_timeout_ms: int = 15000) -> None:
        self._dsn = database_url
        self._timeout = int(statement_timeout_ms)
        if psycopg is None:  # pragma: no cover
            raise RuntimeError(
                f"psycopg is required: {_PSYCOPG_IMPORT_ERROR!r}. Install the project deps."
            )

    def _connect(self) -> Any:
        return psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=True,
            options=f"-c statement_timeout={self._timeout}",
        )

    def read_pending(self) -> list[PendingTx]:
        """Tier-2 spend intents still awaiting a human, past-expiry included.

        Deliberately returns rows past `expires_at` and filters them HERE, not in
        SQL: PostgreSQL forbids evaluating a volatile predicate (now()) under a
        locking clause, so a `for update` with `expires_at > now()` in the WHERE
        silently DROPS due-soon rows instead of locking them, and the expiry sweep
        in `expire_due` would never see them to flip them to 'expired'. Confirmed
        against PG16: the predicate-removed query returns the expired row, the
        predicate-present one does not. So we lock the coarse set here and judge
        the clock in Python.

        `for update skip locked` makes concurrent supervisors safe (no double
        alert); we do not hold the lock across the Telegram send, which is fine at
        one owner because the in-memory alert ledger plus the atomic decision
        guard make the worst case a harmless duplicate card, not a double decision.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                # _PENDING_COLS is a module constant of literal identifiers, not
                # runtime/user data, so the f-string interpolates no value.
                f"select {self._PENDING_COLS} from engine.transactions "  # noqa: S608
                "where direction = 'spend' and approval_status = 'pending' "
                "order by created_at for update skip locked"
            )
            rows = cur.fetchall()
        cutoff = now_utc()
        out: list[PendingTx] = []
        for r in rows:
            tx = row_to_pending(r)
            if _is_expired(tx, cutoff):
                continue
            out.append(tx)
        return out

    def expire_due(self) -> list[str]:
        """Flip pending intents whose expires_at has passed to 'expired' + log."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update engine.transactions
                   set approval_status = 'expired'
                 where direction = 'spend'
                   and approval_status = 'pending'
                   and expires_at is not null
                   and expires_at <= now()
                returning transaction_id::text as id
                """
            )
            ids = [r["id"] for r in cur.fetchall()]
        for tx_id in ids:
            self.log(
                "warning", "approval.expired",
                f"intent {short(tx_id)} expired unanswered", tx_id,
            )
        return ids

    def apply_decision(self, tx_id: str, action: str, decider: str) -> str:
        """Atomically apply a human tap.

        The WHERE keeps the decision idempotent and race-safe: the UPDATE lands
        only on a still-pending row. rowcount 0 means a tap lost the race (two
        operators, a double tap, or a tap after expiry), which is the desired
        outcome, not an error. Returns 'decided', 'duplicate', or 'missing'.
        """
        new_status = "approved" if action == ACTION_APPROVE else "rejected"
        fulfill = "authorized" if action == ACTION_APPROVE else "cancelled"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select approval_status, title from engine.transactions "
                "where transaction_id = %s",
                (tx_id,),
            )
            row = cur.fetchone()
            if row is None:
                return "missing"
            if row["approval_status"] != "pending":
                return "duplicate"
            # No decision_source/alert columns exist on transactions in
            # schema.sql, so the audit trail of "decided via Telegram" lives in
            # the append-only agent_logs row below, not as a transactions column.
            cur.execute(
                """
                update engine.transactions
                   set approval_status = %s,
                       fulfillment_status = %s,
                       approved_by = %s,
                       approved_at = now()
                 where transaction_id = %s and approval_status = 'pending'
                """,
                (new_status, fulfill, decider[:200], tx_id),
            )
            if cur.rowcount != 1:
                return "duplicate"  # lost the race between SELECT and UPDATE
        self.log(
            "info", "approval.decided",
            f"intent {short(tx_id)} {new_status} by {decider}", tx_id, is_audit=True,
        )
        return "decided"

    def set_alert(self, tx_id: str, message_id: int | None, chat_id: str) -> None:
        """Stamp the delivered card's Telegram message id into agent_logs.context.

        transactions has no alert_message_id column in schema.sql, and adding one
        for a supervisory nicety is not justified, so the card/message correlation
        rides in the append-only log row that already records the alert. Best-effort:
        losing it only costs in-place card editing after a restart.
        """
        if message_id is None:
            return
        try:
            self.log("info", "approval.alert_sent",
                     f"alerted {short(tx_id)}", tx_id,
                     extra={"telegram_message_id": int(message_id), "chat_id": chat_id})
        except Exception as exc:  # non-fatal: only loses in-place edit on restart
            log.warning("could not record alert message for %s: %s", short(tx_id), exc)

    def get_alert(self, tx_id: str) -> int | None:
        """The Telegram message id for a card, recovered from agent_logs so a
        post-restart tap can still edit the original card in place."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    select context->>'telegram_message_id' as mid
                      from engine.agent_logs
                     where event = 'approval.alert_sent'
                       and context->>'transaction_id' = %s
                     order by created_at desc limit 1
                    """,
                    (tx_id,),
                )
                row = cur.fetchone()
                if row and row.get("mid") is not None:
                    return int(row["mid"])
        except Exception as exc:
            log.debug("could not recover alert message for %s: %s", short(tx_id), exc)
        return None

    def heartbeat(self) -> None:
        """Cheap liveness marker in agent_logs, at most one per tick.

        Best-effort by design: if the write fails the loop must still run, so we
        log the failure and continue rather than treating DB health as fatal here.
        """
        try:
            self.log("debug", "supervisor.tick", "orchestrator tick complete", is_audit=False)
        except Exception as exc:
            log.warning("heartbeat log write failed (continuing): %s", exc)

    def log(
        self, level: str, event: str, message: str, tx_id: str | None = None,
        is_audit: bool = False, extra: dict[str, Any] | None = None,
    ) -> None:
        """Append to engine.agent_logs. level is validated against log_level so a
        typo cannot abort the transaction with an enum cast error."""
        level = level if level in _LOG_LEVELS else "info"
        context: dict[str, Any] = dict(extra or {})
        if tx_id:
            context["transaction_id"] = tx_id
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into engine.agent_logs
                    (agent_handle, level, event, message, context, is_audit)
                values (%s, %s, %s, %s, %s::jsonb, %s)
                """,
                (AGENT_HANDLE, level, event, message[:2000], json.dumps(context), is_audit),
            )

    def find_id_by_prefix(self, prefix: str) -> str | None:
        """Resolve a flattened UUID prefix to a full transaction_id, or None if ambiguous."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select transaction_id::text as id from engine.transactions "
                "where replace(transaction_id::text, '-', '') LIKE %s limit 2",
                (prefix + "%",),
            )
            rows = cur.fetchall()
        return rows[0]["id"] if len(rows) == 1 else None


_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


# ---------------------------------------------------------------------------
# Alert dedupe state
# ---------------------------------------------------------------------------


@dataclass
class _AlertState:
    """Process-memory record of what we have already alerted, and how it failed.

    Why in memory: the schema.sql model has no place to store Telegram delivery
    state (no alert_sent, no provider key on transactions, and financial_ledger
    is append-only so we cannot stamp a row). Re-notifying on restart is the
    accepted trade, and is far safer than silently dropping a new intent.

    Why failures are remembered: an unconfigured bot that retried every 20s would
    emit thousands of identical warnings a day. failures maps transaction_id ->
    (consecutive_failures, monotonic_backoff_until); an intent in backoff is
    skipped until its clock passes. Success clears the entry entirely.
    """

    alerted: set[str] = field(default_factory=set)
    failures: dict[str, tuple[int, float]] = field(default_factory=dict)
    max_backoff: float = field(default=3600.0)

    def should_alert(self, tx_id: str) -> bool:
        if tx_id in self.alerted:
            return False
        entry = self.failures.get(tx_id)
        return not (entry and time.monotonic() < entry[1])

    def mark_alerted(self, tx_id: str) -> None:
        self.alerted.add(tx_id)
        self.failures.pop(tx_id, None)

    def record_failure(self, tx_id: str) -> None:
        prev, _ = self.failures.get(tx_id, (0, 0.0))
        attempts = prev + 1
        # Jittered exponential backoff capped at max_backoff. Jitter so a batch of
        # intents that all fail (e.g. bot down) recovers staggered, not in lockstep.
        # Jitter is decorrelation only, not a secret, so random is fine (matches
        # the sibling jitter in engine/telegram.py).
        delay = min(60.0 * (2 ** (attempts - 1)), self.max_backoff) + random.uniform(0, 5)  # noqa: S311
        self.failures[tx_id] = (attempts, time.monotonic() + delay)

    def forget(self, tx_id: str) -> None:
        self.alerted.discard(tx_id)
        self.failures.pop(tx_id, None)


# ---------------------------------------------------------------------------
# Outbound card sender (httpx), mirroring engine/telegram.py
# ---------------------------------------------------------------------------


class AlertSender:
    """Posts approval cards over httpx and edits them to a decided state.

    Kept on httpx (not PTB) so the wire payload matches engine/telegram.py and
    the entity escaping is defined once. Handles the three delivery outcomes that
    need different responses: 429 backs off using retry_after; 400 means OUR
    payload is bad (a malformed entity/anchor), so we downgrade to plain text
    once and retry, because losing the card is worse than losing its bolding.
    """

    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 20.0) -> None:
        self.token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout_s
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _client_http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def send_card(self, tx: PendingTx) -> tuple[bool, int | None]:
        """Send the card. Returns (ok, message_id). message_id set only on success."""
        if not self.configured:
            log.warning("telegram sender unconfigured; not sending card for %s",
                        short(tx.transaction_id))
            return False, None
        url = f"{TG_API}/bot{self.token}/sendMessage"
        payload = send_payload(self.chat_id, tx)
        client = await self._client_http()
        try:
            resp = await client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.warning("card send network error for %s: %s", short(tx.transaction_id), exc)
            return False, None

        if resp.status_code == 429:
            retry_after = float(_retry_after(resp.text))
            await asyncio.sleep(min(retry_after, 15.0))
            try:
                resp = await client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError):
                return False, None

        if resp.status_code == 200 and _ok(resp):
            return True, int(_result_message_id(resp) or 0) or None

        if resp.status_code == 400:
            # Likely a parse_html entity error. Retry once as plain text so the
            # human still sees the request even if our HTML was rejected.
            log.info("card 400 for %s; retrying plain text: %s",
                     short(tx.transaction_id), resp.text[:160])
            plain = dict(payload)
            plain.pop("parse_mode", None)
            plain["text"] = _strip_tags(build_card_caption(tx))[:TEXT_LIMIT]
            try:
                resp = await client.post(url, json=plain)
            except (httpx.TimeoutException, httpx.NetworkError):
                return False, None
            if resp.status_code == 200 and _ok(resp):
                return True, int(_result_message_id(resp) or 0) or None

        log.warning("card send failed for %s: http %s %s",
                    short(tx.transaction_id), resp.status_code, resp.text[:200])
        return False, None

    async def edit_decided(self, message_id: int | None, text: str) -> None:
        """Replace the card in place once decided. Best-effort: only loses visual."""
        if not self.configured or not message_id:
            return
        url = f"{TG_API}/bot{self.token}/editMessageText"
        payload = {
            "chat_id": self.chat_id, "message_id": message_id, "text": text,
            "parse_mode": ParseMode.HTML if ParseMode else "HTML",
        }
        try:
            await (await self._client_http()).post(url, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.debug("editMessageText failed (best-effort): %s", exc)


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


class Supervisor:
    def __init__(
        self, store: TxStore, sender: AlertSender, bot: Any,
        stop: asyncio.Event, *, owner_id: str, tick_seconds: int, dry_run: bool,
    ) -> None:
        self.store = store
        self.sender = sender
        self.bot = bot  # PTB Application, or None
        self.stop = stop
        self.owner_id = owner_id
        self.tick_seconds = int(tick_seconds)
        self.dry_run = dry_run
        self.state = _AlertState()
        # transaction_id -> full PendingTx for the most recent cards, so an
        # answer can rebuild the decided-body text without another DB read. Lost
        # on restart; the DB alert_message_id path in _on_callback covers that.
        self._recent: dict[str, PendingTx] = {}

    async def tick_once(self) -> None:
        """One orchestrator pass. Any internal failure must not end the process."""
        try:
            expired = self.store.expire_due()
            for tx_id in expired:
                self.state.forget(tx_id)

            pending = self.store.read_pending()
            live_ids = {t.transaction_id for t in pending}
            # Forget alerted-but-no-longer-pending ids so the set cannot grow
            # without bound over a long-lived process.
            for gone in self.state.alerted - live_ids:
                self.state.forget(gone)
            # Prune stale failure backoffs for intents no longer pending.
            for dead in [tid for tid in self.state.failures if tid not in live_ids]:
                self.state.forget(dead)

            for tx in pending:
                self._recent[tx.transaction_id] = tx
                if not self.state.should_alert(tx.transaction_id):
                    continue
                if self.dry_run:
                    log.info("[dry-run] would alert %s %s (%s)",
                             short(tx.transaction_id), tx.title, tx.money)
                    # Do NOT mark_alerted in dry-run: it is a rehearsal, and if
                    # dry-run flips off later the real card must still go out.
                    continue
                ok, message_id = await self.sender.send_card(tx)
                if ok:
                    self.state.mark_alerted(tx.transaction_id)
                    self.store.set_alert(tx.transaction_id, message_id, self.sender.chat_id)
                    log.info("alerted %s %s (%s)", short(tx.transaction_id), tx.title, tx.money)
                else:
                    self.state.record_failure(tx.transaction_id)

            self.store.heartbeat()
        except Exception as exc:
            log.exception("supervisor tick failed (continuing): %s", exc)

    async def loop(self) -> None:
        """Run tick_once until stop is set. Sleep is interruptible by shutdown."""
        log.info("orchestrator loop starting (tick=%ss, dry_run=%s, alerts=%s)",
                 self.tick_seconds, self.dry_run, self.sender.configured)
        while not self.stop.is_set():
            await self.tick_once()
            # Normal path: the wait times out after a full tick and we loop again.
            # Suppression is intentional, not a swallowed error.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=self.tick_seconds)
        log.info("orchestrator loop stopped")

    # ----- inbound callbacks (registered on the PTB Application) ------------

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle an inline Approve/Reject tap: apply it, edit the card, ack."""
        query = update.callback_query
        if query is None:
            return
        data = query.data or ""
        decider = f"telegram:{query.from_user.id if query.from_user else 'unknown'}"

        decision = parse_decision(data)
        if decision is None:
            decision = parse_legacy_decision(data, self.store.find_id_by_prefix)
        if decision is None:
            await _safe_answer(query, "Unrecognized button.")
            return

        # Enforce owner-only if TELEGRAM_OWNER_ID is set; a stray tap that guesses
        # a transaction UUID should not be able to authorize spend.
        if self.owner_id and str(query.from_user.id) != self.owner_id:
            log.warning("ignored tap from non-owner %s", query.from_user.id)
            await _safe_answer(query, "Only the owner can decide these.", show=True)
            return

        tx = self._recent.get(decision.transaction_id)
        try:
            outcome = await asyncio.to_thread(
                self.store.apply_decision, decision.transaction_id, decision.action, decider
            )
        except Exception as exc:
            log.exception("apply_decision failed: %s", exc)
            await _safe_answer(query, "Could not record that decision.", show=True)
            return

        if tx is not None:
            body = decision_text(decision.action, tx, decider)
        elif outcome == "missing":
            await _safe_answer(query, "Unknown request.", show=True)
            return
        else:
            verb = "Approved ✅" if decision.action == ACTION_APPROVE else "Rejected ❌"
            body = verb  # no card in memory (post-restart); short confirm suffices

        # Which card to blank out, in order of reliability: THIS tapper's card
        # (edit in place via the callback query), then the original card persisted
        # in agent_logs (covers a second operator tapping the same intent), then
        # the original card as seen from this update. Editing the right message
        # removes the now-dead buttons so a stale re-tap cannot re-hit the row.
        tapped_mid = query.message.message_id if query.message else None
        stored_mid = self.store.get_alert(decision.transaction_id)

        async def _blank(mid: int | None) -> None:
            if mid is None:
                return
            with contextlib.suppress(Exception):  # already edited, or too old to edit
                await self.sender.edit_decided(mid, "")

        if outcome == "decided":
            await self.sender.edit_decided(tapped_mid, body)
            await _blank(stored_mid)
            toast = "Approved ✅" if decision.action == ACTION_APPROVE else "Rejected ❌"
            await _safe_answer(query, toast)
        elif outcome == "duplicate":
            # The winner already edited its own card; blank the losers' buttons.
            await _blank(tapped_mid)
            await _blank(stored_mid)
            await _safe_answer(query, "Already decided.", show=True)
        else:  # missing
            await _safe_answer(query, "Unknown request.", show=True)


def _strip_tags(text: str) -> str:
    out = re.sub(r"<[^>]+>", "", text)
    # html.escape used quote=False so apostrophes survive verbatim; "&amp;" is
    # the only entity we emit, so decoding just that is sufficient (we never
    # introduce &lt;, &gt;, or &quot;).
    return out.replace("&amp;", "&")


async def _safe_answer(query: Any, text: str, show: bool = False) -> None:
    with contextlib.suppress(Exception):
        await query.answer(text=text, show_alert=show)


# ---------------------------------------------------------------------------
# http response helpers (kept tiny and total)
# ---------------------------------------------------------------------------


def _retry_after(text: str) -> float:
    try:
        body = json.loads(text)
        return float(((body.get("parameters") or {}).get("retry_after")) or 2)
    except Exception:
        return 2.0


def _ok(resp: httpx.Response) -> bool:
    try:
        return bool(resp.json().get("ok"))
    except Exception:
        return False


def _result_message_id(resp: httpx.Response) -> int | None:
    try:
        return int(resp.json()["result"]["message_id"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_application(token: str, handler: Any) -> Any:
    """Construct the PTB Application for the approval bot.

    Postgres is synchronous, so the handler offloads its DB work to a worker
    thread via asyncio.to_thread (see Supervisor.on_callback); PTB's own update
    concurrency stays off, so a slow decision cannot interleave with the tick.

    Persistence is OFF by default in 21.x, and we want it off: the default would
    write a pickle-backed jobqueue carrying the unmasked bot token to disk under
    $PWD. This process schedules no PTB Jobs, so the jobqueue has nothing to lose.
    """
    app = ApplicationBuilder().token(token).concurrent_updates(False).build()
    app.add_handler(CallbackQueryHandler(handler))
    return app


async def run() -> int:
    load_dotenv()

    database_url = env("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is unset; the supervisor has no state and cannot start.",
              file=sys.stderr)
        return 2
    if psycopg is None:
        print(f"psycopg is required and not importable: {_PSYCOPG_IMPORT_ERROR!r}", file=sys.stderr)
        return 2

    token = env("TELEGRAM_APPROVAL_BOT_TOKEN")
    chat_id = env("TELEGRAM_APPROVAL_CHAT_ID") or env("TELEGRAM_OWNER_ID")
    owner_id = env("TELEGRAM_OWNER_ID")
    dry_run = env("ENGINE_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}
    statement_timeout_ms = _safe_int(env("DB_STATEMENT_TIMEOUT_MS"), 15000)

    store = TxStore(database_url, statement_timeout_ms)
    sender = AlertSender(token, chat_id)

    app: Any = None
    # A token is set but the callback library is missing: fail with the exact fix,
    # not an ImportError from deep in the wiring.
    if token and PTB_IMPORT_ERROR is not None:
        print(f"python-telegram-bot is required for callback handling: {PTB_IMPORT_ERROR!r}\n"
              "Install it:  pip install 'python-telegram-bot[rate-limiter]~=21.6'\n"
              "Or unset TELEGRAM_APPROVAL_BOT_TOKEN to run alert-only with no inbound taps.",
              file=sys.stderr)
        return 2
    # Build a plain asyncio.Event for shutdown; set_signal_handlers uses the
    # running loop so call it after we are inside asyncio.run().
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()

    # The callback handler is a bound coroutine; PTB awaits it as an Application
    # coroutine callback. It shares `store` and `_recent` with the tick loop by
    # reference, so a tap and a tick never diverge on the same row.
    sup_holder: dict[str, Supervisor] = {}

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (sup := sup_holder.get("sup")) is not None:
            await sup.on_callback(update, context)

    # Build (no network yet) then connect with bounded retry. Constructing the
    # Application does not talk to Telegram; initialize() probes the token via
    # getMe, and on PTB 21.11 a *network* failure there surfaces as
    # telegram.error.NetworkError, NOT InvalidToken. A one-second blip at boot
    # must not take the whole supervisor down, so we log and retry, then give up
    # cleanly on shutdown. app stays None until it is actually connected, so the
    # finally-block teardown never trips on a half-built object.
    if token:
        app = build_application(token, handler)
        attempt = 0
        while not stop.is_set():
            try:
                await app.initialize()
                await app.start()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                backoff = min(60.0 * (2 ** min(attempt, 4)), 300.0)
                log.warning("telegram bot connect failed (#%d): %s; retrying in %.0fs",
                            attempt, type(exc).__name__, backoff)
                with contextlib.suppress(Exception):
                    await app.shutdown()
                app = build_application(token, handler)  # reset half-initialized state
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)

    sup = Supervisor(
        store, sender, app, stop,
        owner_id=owner_id, tick_seconds=_safe_int(env("SUPERVISOR_TICK_SECONDS"), 20),
        dry_run=dry_run,
    )
    sup_holder["sup"] = sup

    def _shutdown(signame: str) -> None:
        if not stop.is_set():
            log.info("received %s, shutting down", signame)
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, _shutdown, sig.name)

    log.info("supervisor up: dry_run=%s telegram=%s owner_filter=%s",
             dry_run, bool(token and chat_id), bool(owner_id))

    try:
        # Polling lives on app.updater; the orchestrator tick runs alongside it.
        if app is not None:
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES if Update else None)
        await sup.loop()
    finally:
        if app is not None:
            with contextlib.suppress(Exception):
                await app.updater.stop()
            with contextlib.suppress(Exception):
                await app.stop()
            with contextlib.suppress(Exception):
                await app.shutdown()
        await sender.aclose()
        log.info("supervisor stopped cleanly")
    return 0


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ghost X2 HITL approval supervisor")
    parser.add_argument("--once", action="store_true", help="run a single tick, then exit")
    parser.add_argument("--tick-seconds", type=int, default=None,
                        help="override SUPERVISOR_TICK_SECONDS")
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    load_dotenv()
    if args.tick_seconds:
        os.environ["SUPERVISOR_TICK_SECONDS"] = str(args.tick_seconds)

    if args.once:
        return asyncio.run(_run_once())
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


async def _run_once() -> int:
    database_url = env("DATABASE_URL")
    if not database_url or psycopg is None:
        print("DATABASE_URL/psycopg unavailable", file=sys.stderr)
        return 2
    store = TxStore(database_url, _safe_int(env("DB_STATEMENT_TIMEOUT_MS"), 15000))
    sender = AlertSender(env("TELEGRAM_APPROVAL_BOT_TOKEN"),
                         env("TELEGRAM_APPROVAL_CHAT_ID") or env("TELEGRAM_OWNER_ID"))
    stop = asyncio.Event()
    dry_run = env("ENGINE_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}
    sup = Supervisor(store, sender, None, stop, owner_id=env("TELEGRAM_OWNER_ID"),
                     tick_seconds=0, dry_run=dry_run)
    try:
        await sup.tick_once()
    finally:
        await sender.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
