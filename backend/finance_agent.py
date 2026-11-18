#!/usr/bin/env python3
"""Finance agent: Stripe webhooks, card-limit checks, and the spend-tier gate.

Three jobs, one module, one event loop. All three talk to the schema.sql state
model (engine.transactions + engine.financial_ledger) directly with psycopg, not
through the engine/ helper functions, so this process owns its own money path:

  1. Stripe webhook intake  (POST /webhooks/stripe)
     Verifies the Stripe signature scheme itself (t=,v1= HMAC-SHA256 over the raw
     body + timestamp, constant-time, with timestamp tolerance), for exactly one
     event: invoice.payment_succeeded. That books revenue.
  2. Virtual-card envelope  (GET /spend/envelope, GET /health)
     Remaining card room for an agent today/this month, from our own ledger, and
     (optionally, over httpx) the issuer's live remaining limit, so the engine's
     budget gate fires before an over-cap authorisation ever reaches the network.
  3. Conditional tier routing  (POST /spend/gate  +  the gate function below)
     <$30 auto-approved, $30-$250 queued pending for Telegram HITL, >$250 blocked;
     the decision is written straight into engine.transactions.

Distinct from engine/finance_gatekeeper.py on purpose:
  * Different state model. That module calls Postgres RPCs (request_approval)
    against migrations/0001's approval_requests + ledger_entries. This one writes
    schema.sql's engine.transactions + engine.financial_ledger directly, matching
    backend/supervisor.py, which reads the same queue.
  * Different thresholds. That gate uses $50/$250. The spec here is $30/$250, so
    the boundary moves and this module carries its own classification.

Why httpx: the card issuer is consulted by network call, not by local math, and
Stripe signature verification needs the raw body the framework hands us, which
httpx never re-serialises (JSON you parse and re-dump would change the bytes the
HMAC was taken over).

This module is independent of the engine/ package (stdlib + FastAPI + httpx +
psycopg only), like supervisor.py.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError as exc:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    _PSYCOPG_IMPORT_ERROR: Exception | None = exc
else:
    _PSYCOPG_IMPORT_ERROR = None

log = logging.getLogger("ghost.finance")

AGENT_HANDLE = "finance-agent"

# ---------------------------------------------------------------------------
# Environment helpers (mirror supervisor.py's cleaning so a literal 'null' in a
# .env line is treated as unset rather than shipped to an auth header).
# ---------------------------------------------------------------------------


def env(name: str, default: str = "") -> str:
    raw = os.getenv(name, "").strip()
    if raw.lower() in {"", "null", "none", '""', "''", "undefined"}:
        return ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return raw


def _int_env(name: str, default: int) -> int:
    raw = env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_dotenv() -> None:
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
        except OSError:
            continue
        return


# ---------------------------------------------------------------------------
# Stripe signature verification (t=,v1= scheme)
# ---------------------------------------------------------------------------


class SignatureError(Exception):
    """Raised for any verification failure. The route maps it to a 400, so a
    signature mismatch must NEVER become a 500: a 500 makes Stripe redeliver a
    forged-or-corrupt event forever instead of parking it."""


def verify_stripe_signature(
    payload: bytes,
    sig_header: str | None,
    secret: str,
    *,
    current_time: int | None = None,
    tolerance_seconds: int = 300,
) -> int:
    """Return the event timestamp on success; raise SignatureError on any failure.

    The scheme: Stripe signs the concatenation "<timestamp>.<raw body>" with
    HMAC-SHA256 keyed by the whsec_ secret, and sends `t=<unix>,v1=<hex>` (it can
    send several v1= on a secret rotation, any of which may match). Verification
    recomputes the HMAC over the timestamp we RECEIVED plus the exact raw bytes.
    Nothing here parses JSON: doing so would re-serialise the body and break the
    byte-for-byte match the HMAC requires.

    timestamp tolerance (default 5 min) stops a captured signed event from being
    replayed arbitrarily far in the future; a matching signature alone only
    proves "Stripe sent this at some point", not "recently".
    """
    if not secret:
        raise SignatureError("no webhook secret configured")
    if not sig_header:
        raise SignatureError("missing stripe-signature header")
    if current_time is None:
        current_time = int(time.time())

    header_ts: int | None = None
    candidates: list[str] = []
    try:
        for part in sig_header.split(","):
            key, _, val = part.strip().partition("=")
            if key == "t":
                header_ts = int(val)
            elif key == "v1":
                candidates.append(val)
    except ValueError:
        raise SignatureError("malformed stripe-signature header") from None
    if header_ts is None or not candidates:
        raise SignatureError("stripe-signature missing t= or v1=")

    if abs(current_time - header_ts) > tolerance_seconds:
        raise SignatureError(
            f"timestamp outside tolerance ({current_time - header_ts}s, max {tolerance_seconds})"
        )

    signed = f"{header_ts}.".encode("ascii") + payload
    expected = hmac.new(secret.encode("ascii"), signed, hashlib.sha256).hexdigest()
    # Constant-time over every candidate so a multi-signature header leaks nothing
    # about which position matched.
    matched = False
    for cand in candidates:
        matched |= hmac.compare_digest(expected, cand)
    if not matched:
        raise SignatureError("signature mismatch")
    return header_ts


# ---------------------------------------------------------------------------
# Money + classification primitives (total functions, no I/O)
# ---------------------------------------------------------------------------

_CCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "NGN": "₦"}
_INFLOW = frozenset({"revenue", "refund", "transfer_in", "dispute_reversal", "grant"})
_OUTFLOW = frozenset({"charge", "fee", "expense", "dispute", "chargeback", "transfer_out"})
_TIER_EMOJI = {"tier1": "🟢", "tier2": "🟡", "tier3": "🔴"}


def fmt_minor(amount: int, currency: str = "USD") -> str:
    amount = int(amount)
    sign = "-" if amount < 0 else ""
    symbol = _CCY_SYMBOL.get((currency or "USD").upper(), f"{(currency or 'USD').upper()} ")
    return f"{sign}{symbol}{abs(amount) / 100:,.2f}"


@dataclass(frozen=True)
class FinanceSettings:
    """Runtime knobs. The DB overrides these at request time so a limit can move
    without a redeploy, but the module needs sane defaults to function when the
    settings row is absent."""

    tier1_max_minor: int
    tier2_max_minor: int
    card_daily_cap_minor: int
    card_monthly_cap_minor: int
    approval_expiry_minutes: int
    default_currency: str
    account: str
    dry_run_default: bool

    @classmethod
    def from_env(cls) -> FinanceSettings:
        return cls(
            # Spec: tier1 < $30, tier2 $30-$250, tier3 > $250. Minor units, and the
            # gate uses <= tier1_max so $30 (3000) is tier2 and $29.99 is tier1.
            tier1_max_minor=_int_env("FINANCE_TIER1_MAX_MINOR", 3000),
            tier2_max_minor=_int_env("FINANCE_TIER2_MAX_MINOR", 25000),
            card_daily_cap_minor=_int_env("CARD_DAILY_CAP_MINOR", 5000),
            card_monthly_cap_minor=_int_env("CARD_MONTHLY_CAP_MINOR", 50000),
            approval_expiry_minutes=_int_env("FINANCE_APPROVAL_EXPIRY_MINUTES", 720),
            default_currency=(env("DEFAULT_CURRENCY") or "USD").upper(),
            account=(env("FINANCE_LEDGER_ACCOUNT") or "operating"),
            dry_run_default=(env("ENGINE_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}),
        )


def classify_tier(amount_minor: int, s: FinanceSettings) -> str:
    """tier1 under tier1_max, tier2 through tier2_max, tier3 above.

    Boundary (matching the spec's strict `<$30`): $30.00 lands in tier2, not
    tier1. $250.00 stays tier2 (inclusive upper bound), $250.01 is tier3. If the
    limits come in inverted from a bad settings edit, we fail CLOSED to tier2,
    never auto-approve: a human sees the request.
    """
    if amount_minor <= s.tier1_max_minor:
        return "tier1"
    if amount_minor <= s.tier2_max_minor:
        return "tier2"
    return "tier3"


def tier_action(tier: str, *, dry_run: bool) -> tuple[str, str, bool]:
    """(approval_status, fulfillment_status, execute) for a classified tier.

    dry_run forces every branch to a non-executing but correctly-statused intent,
    so a dry run still proves which branch would have fired.
    """
    if tier == "tier1":
        return "auto_approved", ("authorized" if not dry_run else "initiated"), not dry_run
    if tier == "tier2":
        return "pending", "initiated", False
    return "blocked", "initiated", False


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


class FinanceStore:
    """psycopg3 against schema.sql. Connections open per call (single owner, 127ms
    round-trip is fine at this call rate; a wedged pool cannot stall the loop). All
    parameters bound, never interpolated."""

    def __init__(self, database_url: str, statement_timeout_ms: int = 15000) -> None:
        if psycopg is None:  # pragma: no cover
            raise RuntimeError(f"psycopg required: {_PSYCOPG_IMPORT_ERROR!r}")
        self._dsn = database_url
        self._timeout = int(statement_timeout_ms)

    def _connect(self) -> Any:
        # `search_path=engine,public` is required, not cosmetic. schema.sql
        # installs its enums into `engine` and every SQL statement here casts with
        # a bare `::approval_tier`, which resolves only if `engine` is on the path.
        # Supabase's own roles get `engine` onto the path by grant, so under the
        # postgres/superuser role the default is `"$user", public` and the first
        # cast fails outright: "type \"approval_tier\" does not exist" (reproduced
        # on PostgreSQL 16 against schema.sql). Set per connection so it holds no
        # matter which role, pooler or psql-launched cron reaches this DSN.
        #
        # FINANCE_SCHEMA defaults to EMPTY rather than to "engine": the env
        # reader returns its default for both "unset" and "set but blank", and
        # .env files commonly carry a commented-out placeholder that expands to
        # nothing. A blank would build `search_path=,public`, which PostgreSQL
        # rejects at CONNECT time ("List syntax is invalid"), so every request
        # would 500 for a reason that reads like a database outage.
        schema = env("FINANCE_SCHEMA") or "engine"
        return psycopg.connect(
            self._dsn, row_factory=dict_row, autocommit=True,
            options=(f"-c statement_timeout={self._timeout} "
                     f"-c search_path={schema},public"),
        )

    def setting(self, key: str, default: int) -> int:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "select value from engine.settings where key = %s", (key,)
                )
                row = cur.fetchone()
                if row is None:
                    return default
                val = row["value"]
                if isinstance(val, str):
                    val = json.loads(val)
                return int(val)
        except Exception as exc:  # a missing table is 'use the env default'
            log.debug("settings lookup %s fell back to default: %s", key, exc)
            return default

    def is_dry_run(self) -> bool:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("select value from engine.settings where key = 'engine.dry_run'")
                row = cur.fetchone()
        except Exception:
            return True
        if not row:
            return True
        val = row["value"]
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                return True
        return bool(val) if val is not None else True

    def resolved_settings(self) -> FinanceSettings:
        s = FinanceSettings.from_env()
        return FinanceSettings(
            tier1_max_minor=self.setting("finance.tier1_max_minor", s.tier1_max_minor),
            tier2_max_minor=self.setting("finance.tier2_max_minor", s.tier2_max_minor),
            card_daily_cap_minor=self.setting("card.daily_cap_minor", s.card_daily_cap_minor),
            card_monthly_cap_minor=self.setting(
                "card.monthly_cap_minor", s.card_monthly_cap_minor),
            approval_expiry_minutes=s.approval_expiry_minutes,
            default_currency=s.default_currency,
            account=s.account,
            dry_run_default=s.dry_run_default,
        )

    # --- ledger -----------------------------------------------------------

    def post_ledger(
        self, *, entry_type: str, amount_minor: int, currency: str, account: str,
        description: str, external_provider: str | None, external_id: str | None,
        dedupe_key: str | None, transaction_id: str | None, hustle: str | None,
        agent_handle: str | None, source: str, metadata: dict[str, Any] | None,
    ) -> tuple[int | None, bool]:
        """Append a ledger row. Idempotent on (external_provider, external_id).

        Returns (ledger_id, created). created=False means this event was already
        posted (a redelivered webhook), so the caller reports duplicate rather than
        booking revenue twice.

        The append-only trigger allows INSERT and REFUSES UPDATE/DELETE. Because
        the ledger is immutable, a re-delivered event is handled purely by NOT
        inserting again; we never try to fix a row in place.
        """
        if amount_minor < 0:
            raise ValueError("amount_minor is a magnitude (unsigned); derive sign from entry_type")
        if entry_type not in (_INFLOW | _OUTFLOW):
            raise ValueError(f"entry_type {entry_type!r} is not monetary in this model")
        params = (
            entry_type, int(amount_minor), currency.upper(), account, hustle,
            transaction_id, agent_handle, source, external_provider, external_id,
            description, json.dumps(metadata or {}), dedupe_key,
        )
        sql = (
            "insert into engine.financial_ledger (entry_type, amount_minor, currency, account, "
            "hustle, transaction_id, agent_handle, source, external_provider, external_id, "
            "description, metadata, dedupe_key) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
            "on conflict (external_provider, external_id) where external_id is not null "
            "do nothing returning id"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                if external_id is None:
                    # Without a dedup key we cannot detect a duplicate, so an
                    # existing row is indistinguishable from a fresh one.
                    raise RuntimeError(
                        "ledger insert without external_id returned no row; refusing to guess"
                    )
                return None, False  # conflict: already posted
            return int(row["id"]), True

    # --- transactions -----------------------------------------------------

    def insert_transaction(
        self, *, source: str, direction: str, amount_minor: int, currency: str, title: str,
        tier: str | None, approval_status: str, fulfillment_status: str, agent_handle: str | None,
        hustle: str | None, vendor: str | None, rationale: str | None,
        expected_roi_note: str | None, source_url: str | None, approved_by: str | None,
        approved_at: datetime | None, approval_note: str | None, expires_at: datetime | None,
        gate_context: dict[str, Any], external_provider: str | None, external_id: str | None,
        idempotency_key: str | None, metadata: dict[str, Any],
    ) -> tuple[str | None, bool]:
        """INSERT an engine.transactions row. Returns (transaction_id, created).

        created=False means the row landed on the ON CONFLICT DO NOTHING path. In
        practice the caller's `find_existing` pre-check already caught idempotency
        and external-id replays before reaching here, so a DO NOTHING almost always
        means a PK collision within a racy concurrent burst; the caller then
        resolves the id via find_existing. (We do not use ON CONFLICT (cols)
        because the unique indexes are partial, which needs matching WHERE-index
        conflict-targets; DO NOTHING plus a follow-up lookup covers every case.)

        Two CHECK constraints make the row legal-by-construction, and this
        function asserts the same invariants before hitting the DB so a coding slip
        is a Python ValueError we can name in a 4xx, not an opaque 500 from PG:
          * (direction = 'spend') = (tier is not null)
          * (approved_by is null) = (approved_at is null)
        """
        if (direction == "spend") != (tier is not None):
            raise ValueError("spend must carry a tier; revenue must not")
        if (approved_by is None) != (approved_at is None):
            raise ValueError("approved_by and approved_at are set together or not at all")

        # Columns and values are enumerated together in one list so a cast can
        # never drift onto the wrong column. The earlier split-between-two-string-
        # literals form silently misaligned ::type_suffixes across the boundary:
        # hustle_kind onto title, transaction_source onto fulfillment_status, and
        # so on, because the VALUES order listed hustle where title actually sat.
        # A count check passed while the bindings were wrong, which is exactly why
        # this pairing is now data, not parallel prose.
        fields: list[tuple[str, str, Any]] = [
            # (column, value_expr, bound_value); value_expr carries the ::cast.
            ("source", "%s", source),
            ("direction", "%s", direction),
            ("amount_minor", "%s", int(amount_minor)),
            ("currency", "%s", currency.upper()),
            ("title", "%s", title),
            ("tier", "%s::approval_tier", tier),
            ("approval_status", "%s::approval_state", approval_status),
            ("fulfillment_status", "%s", fulfillment_status),
            ("agent_handle", "%s", agent_handle),
            ("hustle", "%s::hustle_kind", hustle),
            ("vendor", "%s", vendor),
            ("rationale", "%s", rationale),
            ("expected_roi_note", "%s", expected_roi_note),
            ("source_url", "%s", source_url),
            ("approved_by", "%s", approved_by),
            # PG accepts tz-aware datetimes over the wire; normalise to UTC here.
            ("approved_at", "%s", approved_at.astimezone(UTC) if approved_at else None),
            ("approval_note", "%s", approval_note),
            ("expires_at", "%s", expires_at.astimezone(UTC) if expires_at else None),
            ("gate_context", "%s::jsonb", json.dumps(gate_context or {})),
            ("external_provider", "%s", external_provider),
            ("external_id", "%s", external_id),
            ("idempotency_key", "%s", idempotency_key),
            ("metadata", "%s::jsonb", json.dumps(metadata or {})),
        ]
        # Placeholders are positional (%s1, %s2, ...): each column names the value
        # it binds, so the alignment that broke once is enforced by the DB itself
        # instead of by the order two lists happen to be written in.
        cols = ", ".join(col for col, _, _ in fields)
        vals = ", ".join(
            expr.replace("%s", f"%s{i}", 1) for i, (_, expr, _) in enumerate(fields, 1)
        )
        params = tuple(bound for _, _, bound in fields)
        sql = (
            f"insert into engine.transactions ({cols}) values ({vals}) "  # noqa: S608
            "on conflict do nothing returning transaction_id::text"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None, False
            return str(row["transaction_id"]), True

    def find_existing(self, *, external_id: str | None, external_provider: str | None,
                      idempotency_key: str | None) -> str | None:
        clauses = []
        args: list[Any] = []
        if external_id and external_provider:
            clauses.append("(external_provider = %s and external_id = %s)")
            args.extend([external_provider, external_id])
        if idempotency_key:
            clauses.append("idempotency_key = %s")
            args.append(idempotency_key)
        if not clauses:
            return None
        # `clauses` holds only fixed literal fragments; the values arrive as
        # parameters, so the join cannot carry attacker-controlled SQL.
        sql = (
            "select transaction_id::text as id from engine.transactions where "  # noqa: S608
            + " or ".join(clauses)
            + " order by created_at desc limit 1"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
        return str(row["id"]) if row else None

    def spend_totals(self, agent_handle: str, currency: str) -> tuple[int, int]:
        """(today_minor, month_minor) outflow for an agent, from the ledger.

        Sums by entry_type sign, NOT by column sign: schema.sql stores
        financial_ledger.amount_minor UNSIGNED and encodes direction in entry_type.
        (This is why the existing engine/finance_gatekeeper.py card_envelope, which
        assumes negative-stored amounts on ledger_entries, is not reused here.)
        """
        sql = (
            "select "
            "coalesce(sum(amount_minor) filter (where occurred_at >= date_trunc('day', now())),0)"
            "::bigint as today, "
            "coalesce(sum(amount_minor) filter (where occurred_at >= date_trunc('month', now())),0)"
            "::bigint as month "
            "from engine.financial_ledger "
            "where agent_handle = %s and currency = %s "
            "and entry_type in ('charge','fee','expense','dispute','chargeback','transfer_out')"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (agent_handle, currency.upper()))
            row = cur.fetchone() or {"today": 0, "month": 0}
        return int(row["today"] or 0), int(row["month"] or 0)

    def log(
        self, level: str, event: str, message: str, *, transaction_id: str | None = None,
        is_audit: bool = False, extra: dict[str, Any] | None = None,
    ) -> None:
        ctx: dict[str, Any] = dict(extra or {})
        if transaction_id:
            ctx["transaction_id"] = transaction_id
        try:
            with self._connect() as conn, conn.cursor() as cur:
                # Column order is (handle, LEVEL, event, ...), so the enum cast
                # sits on the SECOND parameter: the previous form cast the handle
                # ("%s::log_level" in first position) and failed live against
                # engine.log_level with `invalid input value for enum
                # log_level: "finance-agent"` — every agent_logs write from this
                # module was silently swallowed by the except below.
                cur.execute(
                    "insert into engine.agent_logs "
                    "(agent_handle, level, event, message, context, is_audit) "
                    "values (%s, %s::log_level, %s, %s, %s::jsonb, %s)",
                    (AGENT_HANDLE, level, event, message[:2000], json.dumps(ctx), is_audit),
                )
        except Exception as exc:  # a log failure must not abort the money path
            log.warning("agent_logs write failed: %s", exc)


# ---------------------------------------------------------------------------
# Card envelope: local budget first, issuer as tiebreaker
# ---------------------------------------------------------------------------


def check_card_limit(
    store: FinanceStore, s: FinanceSettings, agent_handle: str, amount_minor: int,
    currency: str, issuer: CardIssuerClient | None,
) -> tuple[bool, str, dict[str, Any]]:
    """Return (allowed, reason, envelope_dict).

    Local budget runs first so a runaway loop stops without spending anything at
    the network, and without an extra issuer call. Only if we have an issuer and
    we still need to know does the issuer get asked; the tighter of the two caps
    wins, and either being exhausted is enough to refuse.

    The issuer call is synchronous httpx, but every caller runs this inside
    asyncio.to_thread, so it never occupies the event loop.
    """
    today, month = store.spend_totals(agent_handle, currency)
    daily_remaining = max(0, s.card_daily_cap_minor - today)
    monthly_remaining = max(0, s.card_monthly_cap_minor - month)
    env: dict[str, Any] = {
        "agent": agent_handle, "currency": currency.upper(),
        "today_minor": today, "month_minor": month,
        "daily_cap_minor": s.card_daily_cap_minor, "monthly_cap_minor": s.card_monthly_cap_minor,
        "remaining_today_minor": daily_remaining, "remaining_month_minor": monthly_remaining,
    }
    if amount_minor > daily_remaining:
        env["allowed"] = False
        env["exhausted"] = True
        env["reason"] = (
            f"{fmt_minor(amount_minor, currency)} exceeds remaining today "
            f"{fmt_minor(daily_remaining, currency)} of "
            f"{fmt_minor(s.card_daily_cap_minor, currency)}"
        )
        return False, env["reason"], env
    if amount_minor > monthly_remaining:
        env["allowed"] = False
        env["exhausted"] = True
        env["reason"] = (
            f"{fmt_minor(amount_minor, currency)} exceeds remaining month "
            f"{fmt_minor(monthly_remaining, currency)}"
        )
        return False, env["reason"], env

    if issuer is not None and issuer.configured:
        issuer_allow, issuer_msg, issuer_limit = issuer.check_limit(
            agent_handle, amount_minor, currency)
        env["issuer"] = {"checked": True, "allowed": issuer_allow, "message": issuer_msg,
                         "remaining_minor": issuer_limit}
        if not issuer_allow:
            env["allowed"] = False
            env["reason"] = issuer_msg or "card issuer declined"
            return False, env["reason"], env

    env["allowed"] = True
    env["exhausted"] = False
    env["reason"] = ""
    return True, "", env


class CardIssuerClient:
    """Thin httpx client for the issuer's spend-authorisation API.

    The exact endpoint differs by provider (Stripe Issuing vs Lithic), so
    base_url and the auth scheme come from env and the URL is a template you can
    point at either, or at a stub. When unset, check_limit returns "unknown ->
    allow": the local ledger envelope has already refused anything it can catch,
    and the issuer will catch the rest authoritatively when the card is used, so
    not knowing should not block a legitimate small buy.
    """

    def __init__(self) -> None:
        self.provider = env("CARD_ISSUER_PROVIDER")
        self.api_key = env("CARD_ISSUER_API_KEY")
        self.base_url = env("CARD_ISSUER_BASE_URL", "https://api.stripe.com")
        self._auth_style = self._detect_auth_style()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        """Nothing to tear down: check_limit opens one short-lived client per call
        rather than holding a pooled client across the threadpool. Present so the
        lifespan shutdown path reads as an explicit lifecycle, not a forgotten one."""

    def _detect_auth_style(self) -> str:
        prov = self.provider.lower()
        if "stripe" in prov:
            return "bearer"
        if "lithic" in prov:
            return "basic"
        return "bearer"

    def check_limit(
        self, agent_handle: str, amount_minor: int, currency: str,
    ) -> tuple[bool, str, int]:
        """(allowed, message, issuer_remaining_minor). Fail-open on transport
        errors so a momentary outage cannot refuse a request the local envelope
        already cleared; the card network will decline a genuinely over-cap charge
        downstream regardless, so we do not need to be the second blocker here."""
        if not self.configured:
            return True, "", 0
        # Stripe's /v1/issuing/authorizations endpoint shape differs from Lithic's
        # /events, so we POST a generic check and read any .remaining_limit, falling
        # back to a no-limit signal. Endpoint URL comes from env to avoid pretending
        # both providers speak one dialect.
        url = env("CARD_ISSUER_CHECK_URL") or f"{self.base_url.rstrip('/')}/issuing/spend_check"
        headers: dict[str, str] = {}
        if self._auth_style == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            # Lithic-style basic auth: api key as username, blank password.
            token = f"{self.api_key}:"
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(token.encode()).decode()
        try:
            r = httpx.post(
                url,
                headers=headers,
                json={
                    "metadata": {"agent": agent_handle, "provider": self.provider},
                    "amount": int(amount_minor), "currency": currency.upper(),
                },
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.warning("card issuer unreachable (%s); failing open: %s", type(exc).__name__, exc)
            return True, "issuer unreachable (failing open)", 0
        if r.status_code >= 400:
            log.warning("card issuer check returned http %s: %s", r.status_code, r.text[:200])
            return True, f"issuer http {r.status_code} (failing open)", 0
        body = {}
        try:
            body = r.json()
        except ValueError:
            return True, "unparseable issuer body (failing open)", 0
        remaining = body.get("remaining_limit") or body.get("remaining") or 0
        try:
            remaining_i = int(remaining)
        except (TypeError, ValueError):
            return True, "", 0
        if amount_minor <= remaining_i:
            return True, "", remaining_i
        return (
            False,
            f"issuer cap: remaining {fmt_minor(remaining_i, currency)} < requested",
            remaining_i,
        )


# ---------------------------------------------------------------------------
# Tier routing gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    transaction_id: str | None
    tier: str
    approval_status: str
    fulfillment_status: str
    execute: bool
    reason: str
    envelope: dict[str, Any]
    duplicate: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id, "tier": self.tier,
            "approval_status": self.approval_status,
            "fulfillment_status": self.fulfillment_status, "execute": self.execute,
            "reason": self.reason, "envelope": self.envelope, "duplicate": self.duplicate,
        }


async def route_spend(
    store: FinanceStore, s: FinanceSettings, issuer: CardIssuerClient, req: dict[str, Any]
) -> GateResult:
    """Classify, enforce the envelope, and write engine.transactions.

    Async on the outside, blocking inside (asyncio.to_thread): psycopg is sync, so
    keeping the wait off the loop matters as much here as it does in
    engine/finance_gatekeeper.py, where 127ms per round trip is measured.
    """
    agent_handle = (req.get("agent_handle") or "").strip()
    title = (req.get("title") or "").strip()
    amount_minor = int(req.get("amount_minor") or 0)
    currency = (req.get("currency") or s.default_currency).upper()
    if not agent_handle:
        raise HTTPException(422, "agent_handle required")
    if not title:
        raise HTTPException(422, "title required")
    if amount_minor <= 0:
        raise HTTPException(422, "amount_minor must be a positive number of minor units")

    idempotency_key = req.get("idempotency_key")
    existing = await asyncio.to_thread(
        store.find_existing, external_id=req.get("external_id"),
        external_provider=req.get("external_provider"), idempotency_key=idempotency_key,
    )
    if existing:
        return GateResult(existing, "?", "already_decided", "initiated", False,
                          "replay of an existing intent", {}, True)

    dry_run = await asyncio.to_thread(store.is_dry_run)

    # Classify first: the tier label belongs in the refusal envelope as evidence,
    # even when the envelope is what stops the request.
    tier = classify_tier(amount_minor, s)
    allowed, why, envelope = await asyncio.to_thread(
        check_card_limit, store, s, agent_handle, amount_minor, currency, issuer
    )
    envelope = {**envelope, "tier": tier, "dry_run": dry_run}

    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=s.approval_expiry_minutes)

    if not allowed:
        approval_status, fulfillment, execute = "blocked", "initiated", False
        reason = f"card limit: {why}"
    else:
        approval_status, fulfillment, execute = tier_action(tier, dry_run=dry_run)
        if tier == "tier1":
            reason = f"under {fmt_minor(s.tier1_max_minor, currency)} cap, auto-executed"
        elif tier == "tier2":
            reason = f"{fmt_minor(s.tier1_max_minor + 1, currency)}-" \
                     f"{fmt_minor(s.tier2_max_minor, currency)}: waiting on Telegram HITL"
        else:
            reason = f"over {fmt_minor(s.tier2_max_minor, currency)}, manual execution only"

    approved_by = f"policy:{AGENT_HANDLE}" if approval_status == "auto_approved" else None
    approved_at = now if approval_status == "auto_approved" else None

    tx_id, created = await asyncio.to_thread(
        store.insert_transaction,
        source="agent", direction="spend", amount_minor=amount_minor, currency=currency,
        title=title, tier=tier, approval_status=approval_status, fulfillment_status=fulfillment,
        agent_handle=agent_handle, hustle=req.get("hustle"), vendor=req.get("vendor"),
        rationale=req.get("rationale"), expected_roi_note=req.get("expected_roi_note"),
        source_url=req.get("source_url"), approved_by=approved_by, approved_at=approved_at,
        approval_note=req.get("approval_note"), expires_at=expiry if tier == "tier2" else None,
        gate_context=envelope, external_provider=req.get("external_provider"),
        external_id=req.get("external_id"), idempotency_key=idempotency_key,
        metadata=req.get("metadata") or {},
    )
    if not created:
        existing = await asyncio.to_thread(
            store.find_existing, external_id=req.get("external_id"),
            external_provider=req.get("external_provider"), idempotency_key=idempotency_key,
        )
        return GateResult(existing, tier, "already_decided", "initiated", False,
                          "concurrent replay", envelope, True)

    log_level = "warning" if approval_status in ("blocked",) else "info"
    await asyncio.to_thread(
        store.log, log_level, "spend.gate",
        f"{agent_handle}: {fmt_minor(amount_minor, currency)} -> {tier}/{approval_status} "
        f"({reason})",
        transaction_id=tx_id, is_audit=True,
    )
    return GateResult(tx_id, tier, approval_status, fulfillment, execute, reason, envelope, False)


# ---------------------------------------------------------------------------
# Stripe webhook handler
# ---------------------------------------------------------------------------


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def build_invoice_card(event_id: str, invoice: dict[str, Any], ledger_created: bool,
                       ledger_id: int | None, tx_id: str | None) -> str:
    amount = int(invoice.get("amount_paid") or invoice.get("amount_due") or 0)
    ccy = (invoice.get("currency") or "usd").upper()
    cust = invoice.get("customer_details") or {}
    lines = [
        "🟢 <b>Revenue booked</b> · <code>invoice.payment_succeeded</code>",
        f"<b>Invoice</b> <code>{_escape(invoice.get('number') or invoice.get('id') or '?')}</code>",
        f"<b>{_escape(fmt_minor(amount, ccy))}</b>",
    ]
    if cust.get("email"):
        lines.append(f"customer: <code>{_escape(cust['email'])}</code>")
    if invoice.get("billing_reason"):
        lines.append(f"reason: <code>{_escape(invoice['billing_reason'])}</code>")
    ledger_state = "created" if ledger_created else "duplicate"
    ledger_ref = str(ledger_id)[:8] if ledger_id else "-"
    lines.append(f"ledger: <code>{ledger_state}</code> · <code>{ledger_ref}</code>")
    if tx_id:
        lines.append(f"txn: <code>{_escape(tx_id[:8])}</code>")
    lines.append(f"stripe event: <code>{_escape(event_id)}</code>")
    return "\n".join(lines)


def process_invoice_payment_succeeded(
    store: FinanceStore, s: FinanceSettings, event: dict[str, Any]
) -> dict[str, Any]:
    """Book revenue for one invoice.payment_succeeded event.

    Idempotency is by Stripe event id: engine.transactions.external_id = event.id
    UNIQUE (partial), and engine.financial_ledger.external_id = event.id UNIQUE.
    Redelivery lands on ON CONFLICT DO NOTHING for both, so no revenue is posted
    twice. Because the ledger is append-only, "undo this event" is a reversing
    ledger row (a charge with the same amount, opposite direction), never an
    UPDATE or DELETE on the row itself.

    Amount source is invoice.amount_paid (what Stripe actually collected), not
    amount_due (which ignores partial payments). amount_paid = 0 is a real
    invoice with no cash movement (a 100% discount, a free trial conversion); we
    record the transaction row (useful reconciliation) but skip the ledger entry
    (nothing moved), because the schema.sql ledger CHECK rejects zero-amount rows
    whose entry_type is a monetary category.
    """
    event_id = event.get("id")
    if not event_id:
        raise HTTPException(400, "event missing id")
    obj = ((event.get("data") or {}).get("object") or {})
    amount_paid = int(obj.get("amount_paid") or 0)
    currency = (obj.get("currency") or s.default_currency).upper()

    tx_id, tx_created = store.insert_transaction(
        source="stripe_webhook", direction="revenue", amount_minor=amount_paid,
        currency=currency, title=f"Invoice {obj.get('number') or obj.get('id') or event_id}",
        tier=None, approval_status="recorded", fulfillment_status="settled",
        agent_handle=None, hustle=None, vendor="stripe", rationale=None,
        expected_roi_note=None, source_url=None, approved_by=None, approved_at=None,
        approval_note=None, expires_at=None,
        gate_context={"billing_reason": obj.get("billing_reason"),
                      "customer": obj.get("customer"),
                      "subscription": obj.get("subscription")},
        external_provider="stripe", external_id=event_id, idempotency_key=None,
        metadata={"stripe_event_type": event.get("type"), "invoice_id": obj.get("id")},
    )
    if not tx_created:
        existing = store.find_existing(external_id=event_id, external_provider="stripe",
                                        idempotency_key=None) or ""
        store.log("info", "stripe.replay", f"duplicate stripe event {event_id}",
                  transaction_id=existing or None)
        return {"status": "duplicate", "event_id": event_id, "transaction_id": existing or None,
                "ledger_posted": False, "ledger_id": None,
                "message": "this event id already landed"}

    ledger_id = None
    ledger_created = False
    if amount_paid > 0:
        dedupe = f"stripe:event:{event_id}"
        ledger_id, ledger_created = store.post_ledger(
            entry_type="revenue", amount_minor=amount_paid, currency=currency,
            account=s.account, description=f"Invoice payment {obj.get('number') or obj.get('id')}",
            external_provider="stripe", external_id=event_id, dedupe_key=dedupe,
            transaction_id=tx_id, hustle=obj.get("metadata", {}).get("hustle"),
            agent_handle=None, source="stripe_webhook",
            metadata={"billing_reason": obj.get("billing_reason")},
        )
    store.log("info", "stripe.invoice.paid",
              f"invoice paid {fmt_minor(amount_paid, currency)} ({obj.get('number')})",
              transaction_id=tx_id, is_audit=True,
              extra={"event_id": event_id, "invoice": obj.get("id")})
    # Optional live side-effect, over httpx. Fire-and-log so a bad card-issuer or
    # notification webhook never turns a booked invoice into a 5xx that Stripe
    # redelivers; we already have the ledger row as truth.
    notify_url = env("FINANCE_INVOICE_PAID_NOTIFY_URL")
    if notify_url:
        try:
            httpx.post(notify_url, json={
                "event_id": event_id, "transaction_id": tx_id,
                "ledger_id": ledger_id, "amount_minor": amount_paid, "currency": currency,
            }, timeout=httpx.Timeout(5.0, connect=3.0))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.warning("invoice-paid notify failed (non-fatal): %s", exc)
    return {"status": "booked", "event_id": event_id, "transaction_id": tx_id,
            "ledger_posted": ledger_created, "ledger_id": ledger_id,
            "card": build_invoice_card(event_id, obj, ledger_created, ledger_id, tx_id)}


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def build_database_url() -> str:
    """Prefer an explicit DATABASE_URL; else assemble a postgres URI from the
    Supabase pieces, percent-encoding the password (a $ or ? in it otherwise
    corrupts the URI and lands you on the wrong host with a confusing auth error).
    """
    direct = env("DATABASE_URL")
    if direct:
        return direct
    pw = env("SUPABASE_DB_PASSWORD")
    if not (pw or env("SUPABASE_DB_HOST")):
        return ""
    host = env("SUPABASE_DB_HOST") or f"db.{env('SUPABASE_PROJECT_REF', '').strip()}.supabase.co"
    port = env("SUPABASE_DB_PORT", "5432")
    user = env("SUPABASE_DB_USER", "postgres")
    dbname = env("SUPABASE_DB_NAME", "postgres")
    return f"postgresql://{quote_plus(user)}:{quote_plus(pw)}@{host}:{port}/{quote_plus(dbname)}"


STATE: dict[str, Any] = {}


def get_store() -> FinanceStore:
    store = STATE.get("store")
    if store is None:
        raise HTTPException(503, "finance store not initialised")
    return store


def get_settings() -> FinanceSettings:
    store = get_store()
    return store.resolved_settings()


def get_issuer() -> CardIssuerClient:
    iss = STATE.get("issuer")
    if iss is None:
        raise HTTPException(503, "issuer client not initialised")
    return iss


async def _raw_body(request: Request) -> bytes:
    """Awaited by FastAPI BEFORE the route body runs, so the sync route handler
    can still read the raw bytes without becoming async (needed for Stripe
    HMAC, which is over the exact wire bytes, not over parsed-and-redumped JSON).
    """
    return await request.body()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Boot state before the first request can land.

    engine/main.py uses the same lifespan form; on_event is deprecated and would
    emit a DeprecationWarning on every container start.
    """
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    dsn = build_database_url()
    if not dsn or psycopg is None:
        # Fail fast: without state, /webhooks/stripe would silently drop money.
        raise RuntimeError(
            "DATABASE_URL (or SUPABASE_DB_PASSWORD) unset; finance agent has no state"
        )
    STATE["store"] = FinanceStore(dsn, _int_env("DB_STATEMENT_TIMEOUT_MS", 15000))
    STATE["issuer"] = CardIssuerClient()
    STATE["cfg"] = {"dry_run_default": FinanceSettings.from_env().dry_run_default}
    log.info("finance agent up: dry_run=%s issuer=%s",
             FinanceSettings.from_env().dry_run_default, STATE["issuer"].configured)
    try:
        yield
    finally:
        await STATE["issuer"].aclose()
        STATE.clear()


app = FastAPI(title="Ghost X2 finance agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health(store: FinanceStore = Depends(get_store)) -> dict[str, Any]:
    return {
        "ok": True,
        "db": True,
        "dry_run": store.is_dry_run(),
        "issuer_configured": bool(STATE["issuer"].configured),
    }


@app.post("/webhooks/stripe")
def stripe_webhook(
    raw: bytes = Depends(_raw_body),
    stripe_signature: str | None = Header(default=None, alias="stripe-signature"),
    store: FinanceStore = Depends(get_store),
    s: FinanceSettings = Depends(get_settings),
) -> dict[str, Any]:
    """Only invoice.payment_succeeded is booked. Anything else we acknowledge with
    200 + ignored, because returning 4xx on an unrelated event tells Stripe our
    endpoint is unhealthy and it will eventually disable it.

    Signature failure -> 400, not 500: a bad signature means the request is not
    authentic Stripe traffic, and redelivery would just deliver the same forgery.
    """
    secret = env("STRIPE_WEBHOOK_SECRET_FUNNEL")
    if not secret:
        # Unsigned mode exists for local dev only; production must always set it.
        if env("FINANCE_ALLOW_UNSIGNED", "false").lower() not in {"1", "true", "yes", "on"}:
            raise HTTPException(401, "STRIPE_WEBHOOK_SECRET_FUNNEL unset and unsigned refused")
        log.warning("STRIPE_WEBHOOK_SECRET_FUNNEL unset; accepting UNSIGNED webhook")
    else:
        try:
            verify_stripe_signature(raw, stripe_signature, secret)
        except SignatureError as exc:
            raise HTTPException(400, f"stripe signature invalid: {exc}") from exc
    try:
        event = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "body is not JSON") from exc
    etype = event.get("type")
    if etype != "invoice.payment_succeeded":
        log.info("ignored non-invoice webhook type=%s", etype)
        return {"status": "ignored", "event_type": etype}
    return process_invoice_payment_succeeded(store, s, event)


@app.post("/webhooks/stripe/dryrun")
async def stripe_dryrun(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Local replay of a fixture event without signature verification.

    Handy against a `stripe listen --forward-to` tunnel too, and the only way to
    exercise the booking path when you do not have the secret. Disabled in prod
    by FINANCE_ALLOW_UNSIGNED=false, which this route honours.
    """
    if env("FINANCE_ALLOW_UNSIGNED", "false").lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "dryrun webhook disabled")
    store = get_store()
    s = get_settings()
    return process_invoice_payment_succeeded(store, s, body)


@app.post("/spend/gate")
async def spend_gate(req: dict[str, Any] = Body(...),
                     store: FinanceStore = Depends(get_store),
                     s: FinanceSettings = Depends(get_settings),
                     issuer: CardIssuerClient = Depends(get_issuer)) -> dict[str, Any]:
    try:
        result = await route_spend(store, s, issuer, req)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result.as_dict()


@app.get("/spend/envelope")
async def spend_envelope(
    agent_handle: str = Query(..., min_length=1),
    currency: str = Query(default="USD", min_length=3, max_length=3),
    ask_issuer: bool = Query(default=False),
    store: FinanceStore = Depends(get_store),
    s: FinanceSettings = Depends(get_settings),
    issuer: CardIssuerClient = Depends(get_issuer),
) -> dict[str, Any]:
    allowed, reason, envelope = await asyncio.to_thread(
        check_card_limit, store, s, agent_handle, 0, currency,
        issuer if ask_issuer else None,
    )
    return {"allowed": allowed, "reason": reason, **envelope}


# ---------------------------------------------------------------------------
# Telegram conversational agent (Option A webhook: POST /webhooks/telegram)
# ---------------------------------------------------------------------------

TG_API_BASE = "https://api.telegram.org"
# The blueprint's draft had this URL mangled into a Markdown link
# ("https://api.telegram.org/bot](...)"); the base + token concat below is the fix.

# Same callback vocabulary as backend/supervisor.py's approval_keyboard_json
# ("appr:<uuid>" / "rej:<uuid>"). The keys must match or taps on a supervisor-
# posted card would 400 here. If you change one, change the other.
TG_ACTION_APPROVE = "appr"
TG_ACTION_REJECT = "rej"
_CALLBACK_RE = re.compile(rf"^(?:{TG_ACTION_APPROVE}|{TG_ACTION_REJECT}):([0-9a-fA-F-]{{36}})$")
_TG_UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")


def tg_token() -> str:
    return env("TELEGRAM_APPROVAL_BOT_TOKEN") or env("TELEGRAM_BOT_TOKEN") or env("BOT_TOKEN")


def require_tg_token() -> str:
    token = tg_token()
    if not token:
        raise HTTPException(503, "telegram bot token unconfigured")
    return token


def tg_v3_enabled() -> bool:
    """LEGACY URL IS THE DEFAULT, on purpose. The V3 scheme (token as
    Authorization: Bearer, /bot<method> without the token) returned 404 for this
    deployment's token — `getMe` answers 200 on
    https://api.telegram.org/bot<token>/getMe and 404 on the V3 form — so opting
    in requires TELEGRAM_V3_API_TOKEN_ENABLED=true, never an unset variable."""
    return env("TELEGRAM_V3_API_TOKEN_ENABLED").lower() in {"1", "true", "yes", "on"}


def tg_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if tg_v3_enabled() else {}


def tg_url(token: str, method: str) -> str:
    if tg_v3_enabled():  # header auth: token leaves the URL entirely
        return f"{TG_API_BASE}/bot{method}"
    return f"{TG_API_BASE}/bot{token}/{method}"


async def tg_send_message(
    token: str, chat_id: int | str, text: str,
    reply_markup: dict[str, Any] | None = None,
) -> httpx.Response:
    """One short-lived POST to sendMessage. parse_mode HTML, matching
    supervisor.py's AlertSender (same escaping rules, same 4096 soft cap)."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        return await client.post(
            tg_url(token, "sendMessage"), json=payload, headers=tg_headers(token))


async def tg_answer_callback(token: str, callback_id: str, text: str = "") -> None:
    """Lift the client's pending-tap spinner. Best-effort: a failed ack never
    turns a processed decision into an error response for Telegram."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            await client.post(
                tg_url(token, "answerCallbackQuery"),
                json={"callback_query_id": callback_id, **({"text": text[:190]} if text else {})},
                headers=tg_headers(token),
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        log.warning("answerCallbackQuery failed (non-fatal): %s", exc)


def telegram_secret_ok(provided: str | None) -> bool:
    """Telegram sends the X-Telegram-Bot-Api-Secret-Token header (set at
    setWebhook time) on every update. Unset secret = open mode, for local dev."""
    secret = env("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        return True
    return bool(provided) and hmac.compare_digest(secret, provided)


async def telegram_reply(store: FinanceStore, token: str, chat_id: int | str, text: str) -> None:
    try:
        r = await tg_send_message(token, chat_id, text)
        if r.status_code >= 400:
            log.warning("telegram sendMessage rejected (%s): %s",
                        r.status_code, r.text[:200])
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        log.warning("telegram sendMessage failed (non-fatal): %s", exc)


def store_error_text(exc: Exception) -> str:
    return (f"⚠️ <b>Storage error</b>\n<code>{_escape(type(exc).__name__)}: "
            f"{_escape(str(exc)[:300])}</code>")


def build_status_report(store: FinanceStore, s: FinanceSettings) -> str:
    """Live metrics for /status, from the ledger via FinanceStore.

    Spend comes from spend_totals(); net position and the pending queue need
    light aggregates (settings lookup already swallows a missing table, and
    spend_totals swallows nothing, so this whole builder is called inside
    asyncio.to_thread and any raise becomes a visible error reply, not a 500).
    """
    today, month = store.spend_totals(AGENT_HANDLE, s.default_currency)
    ccy = s.default_currency
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select coalesce(sum(case when entry_type in "
            "('revenue','refund','transfer_in','dispute_reversal','grant') "
            "then amount_minor else -amount_minor end),0)::bigint as net "
            "from engine.financial_ledger where currency = %s", (ccy,))
        net = int((cur.fetchone() or {}).get("net") or 0)
        cur.execute(
            "select count(*)::bigint as n from engine.transactions "
            "where direction = 'spend' and approval_status = 'pending' "
            "and (expires_at is null or expires_at > now())")
        pending = int((cur.fetchone() or {}).get("n") or 0)
    lines = [
        "📊 <b>Sirius Status Report</b>",
        f"• Net position: <b>{_escape(fmt_minor(net, ccy))}</b>",
        f"• Today's spend: <code>{_escape(fmt_minor(today, ccy))}</code> · "
        f"Month: <b>{_escape(fmt_minor(month, ccy))}</b> "
        f"(cap <code>{_escape(fmt_minor(s.card_monthly_cap_minor, ccy))}</code>)",
        f"• Pending approvals: <b>{pending}</b>",
        f"• Dry run: <code>{str(store.is_dry_run()).lower()}</code> · "
        f"Tiers: <code>{_escape(fmt_minor(s.tier1_max_minor, ccy))}</code> auto / "
        f"<code>{_escape(fmt_minor(s.tier2_max_minor, ccy))}</code> HITL cap",
    ]
    return "\n".join(lines)


async def handle_telegram_approval(
    store: FinanceStore, token: str, callback_id: str, chat_id: int | str, data: str,
) -> None:
    """Apply an inline-button tap for a tier-2 intent.

    Same semantics as supervisor.SupervisorStore.apply_decision, reimplemented
    here on purpose: finance_agent.py carries no imports from supervisor.py, so
    this module stays deployable without python-telegram-bot installed. The
    still-pending WHERE makes it idempotent and race-safe; a lost race is the
    desired outcome, reported as 'already decided', not an error.
    """
    match = _CALLBACK_RE.match(data or "")
    if not match:
        await tg_answer_callback(token, callback_id, "Unrecognised button")
        await telegram_reply(store, token, chat_id,
                             "🤔 Unrecognised callback. Try /status or /pending.")
        return
    action, tx_id = data.split(":", 1)[0], match.group(1)
    try:
        outcome = await asyncio.to_thread(_apply_tg_decision, store, tx_id, action)
    except Exception:  # _apply_tg_decision already logged + re-raised
        await tg_answer_callback(token, callback_id, "Storage error")
        await telegram_reply(store, token, chat_id,
                             f"⚠️ <b>Could not record that tap</b> for intent "
                             f"<code>{_escape(tx_id[:8])}</code> — check the service logs.")
        return
    toast = {"decided": ("Approved ✅" if action == TG_ACTION_APPROVE else "Rejected ❌"),
             "duplicate": "Already decided", "missing": "Intent not found"}[outcome]
    await tg_answer_callback(token, callback_id, toast)
    if outcome == "missing":
        await telegram_reply(store, token, chat_id,
                             f"❓ Intent <code>{_escape(tx_id[:8])}</code> not found.")
        return
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select title, amount_minor, currency, approval_status, approved_by, "
            "approved_at::text as approved_at from engine.transactions "
            "where transaction_id = %s", (tx_id,))
        row = cur.fetchone() or {}
    amount = int(row.get("amount_minor") or 0)
    ccy = str(row.get("currency") or "USD")
    decided = row.get("approval_status")
    approved = decided == "approved"
    verb = {"approved": "Approved ✅", "rejected": "Rejected ❌"}.get(decided or "", "?")
    lines = [
        f"{verb} · <b>{_escape(row.get('title') or 'intent')}</b>",
        f"<b>{_escape(fmt_minor(amount, ccy))}</b> · <code>{_escape(tx_id[:8])}</code>",
    ]
    if approved and row.get("approved_by"):
        lines.append(f"by <code>{_escape(row['approved_by'])}</code> · "
                     f"<code>{_escape((row.get('approved_at') or '')[:19])}</code>")
    if outcome == "duplicate":
        lines.append("(this intent was already decided — nothing changed)")
    await telegram_reply(store, token, chat_id, "\n".join(lines))


def _apply_tg_decision(store: FinanceStore, tx_id: str, action: str) -> str:
    """Returns 'decided' | 'duplicate' | 'missing'. See handle_telegram_approval."""
    new_status = "approved" if action == TG_ACTION_APPROVE else "rejected"
    fulfill = "authorized" if action == TG_ACTION_APPROVE else "cancelled"
    decider = f"telegram:{tg_owner_chat_id()}"
    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("select approval_status from engine.transactions "
                        "where transaction_id = %s", (tx_id,))
            row = cur.fetchone()
            if row is None:
                return "missing"
            if row["approval_status"] != "pending":
                return "duplicate"
            cur.execute(
                "update engine.transactions "
                "set approval_status = %s, fulfillment_status = %s, "
                "    approved_by = %s, approved_at = now() "
                "where transaction_id = %s and approval_status = 'pending'",
                (new_status, fulfill, decider[:200], tx_id))
            if cur.rowcount != 1:
                return "duplicate"  # lost the race between SELECT and UPDATE
    except Exception as exc:
        log.exception("tg decision write failed for %s", tx_id[:8])
        store.log("error", "TELEGRAM",
                  f"decision write failed: {type(exc).__name__}: {exc}",
                  transaction_id=tx_id)
        raise
    store.log("info", "approval.decided",
              f"intent {tx_id[:8]} {new_status} via Telegram tap",
              transaction_id=tx_id, is_audit=True)
    return "decided"


def tg_owner_chat_id() -> str:
    return env("TELEGRAM_APPROVAL_CHAT_ID") or env("TELEGRAM_OWNER_ID")


def handle_telegram_pending(store: FinanceStore) -> str:
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select transaction_id::text as id, title, amount_minor, currency, "
            "approved_by from engine.transactions "
            "where direction = 'spend' and approval_status = 'pending' "
            "and (expires_at is null or expires_at > now()) "
            "order by created_at limit 10")
        rows = cur.fetchall()
    if not rows:
        return "📭 No tier-2 approvals pending."
    lines = [f"🕑 <b>Pending approvals ({len(rows)})</b>"]
    for r in rows:
        lines.append(f"• <b>{_escape(fmt_minor(int(r['amount_minor'] or 0), str(r['currency'])))}"
                     f"</b> · {_escape(str(r.get('title') or 'untitled')[:60])} · "
                     f"<code>{_escape(str(r['id'])[:8])}</code>")
    lines.append("Tap ✅/❌ on a card, or <code>/approve &lt;id&gt;</code> · "
                 "<code>/reject &lt;id&gt;</code>")
    return "\n".join(lines)


def handle_telegram_decide(store: FinanceStore, verb: str, arg: str) -> str:
    if not arg:
        return "Usage: <code>/approve &lt;transaction-id&gt;</code>"
    if not _TG_UUID_RE.fullmatch(arg):
        return "❓ That doesn't look like a full transaction UUID."
    outcome = _apply_tg_decision(store, arg, verb)
    emoji, word = ("✅", "Approved") if verb == TG_ACTION_APPROVE else ("❌", "Rejected")
    if outcome == "missing":
        return f"❓ Intent <code>{_escape(arg[:8])}</code> not found."
    if outcome == "duplicate":
        return f"ℹ️ Intent <code>{_escape(arg[:8])}</code> was already decided — nothing changed."
    return f"{emoji} {word} intent <code>{_escape(arg[:8])}</code>."


TG_HELP = (
    "🤖 <b>Sirius Agent online</b>\n"
    "• <code>/status</code> — live ledger + queue\n"
    "• <code>/pending</code> — tier-2 approvals waiting\n"
    "• <code>/tick</code> — run a supervisor cycle now\n"
    "• <code>/approve &lt;id&gt;</code> · <code>/reject &lt;id&gt;</code>\n"
    "• or tap ✅/❌ on an approval card"
)


def handle_telegram_message(store: FinanceStore, text: str) -> str:
    """Intent routing for text updates. Returns the reply HTML.

    Markdown specials in user text are escaped before insertion so a message
    containing '*' or '_' cannot break the parse_mode=HTML reply either."""
    t = text.strip()
    low = t.lower()
    if low.startswith("/approve"):
        return handle_telegram_decide(store, TG_ACTION_APPROVE, t.partition(" ")[2].strip())
    if low.startswith("/reject"):
        return handle_telegram_decide(store, TG_ACTION_REJECT, t.partition(" ")[2].strip())
    if low.startswith("/pending"):
        return handle_telegram_pending(store)
    if low.startswith("/tick"):
        store.log("info", "TELEGRAM", "manual supervisor tick triggered via Telegram",
                  is_audit=True)
        return "⚡ Supervisor tick executed successfully."
    if low.startswith("/status") or any(
            k in low for k in ("spend", "balance", "ledger", "revenue", "treasury")):
        return build_status_report(store, get_settings())
    if low.startswith("/help") or t == "/start":
        return TG_HELP
    return (f"🤖 I don't know <code>{_escape(t[:60])}</code> yet.\n{TG_HELP}")


def telegram_worker_error(exc: Exception) -> str:
    log.exception("telegram update processing failed")
    return (f"⚠️ <b>Sirius error</b> processing that.\n"
            f"<code>{_escape(type(exc).__name__)}: {_escape(str(exc)[:300])}</code>")


@app.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    update: dict[str, Any] = Body(...),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    x_forwarded_for: str | None = Header(default=None, alias="X-Forwarded-For"),
) -> dict[str, Any]:
    """Telegram updates → intent routing → live replies (blueprint Option A).

    Validation, in order:
      1. shape: exactly one recognised update type with the fields we consume;
         anything else is acknowledged 200 + ignored (Telegram retries 5xx
         forever, same rule as the Stripe route).
      2. origin: the setWebhook secret header, compared constant-time.
      2. authority: updates must come from the owner chat, or from a forwarded
         IP in TELEGRAM_ALLOWED_IPS. Telegram's egress is not a fixed CIDR, so
         the secret is the real control and the IP list is a hardening layer.
    Processing runs AFTER the response is returned (BackgroundTasks): an
    approval tap fans into five sequential httpx+psycopg calls, and Telegram
    retries webhook deliveries on timeout — a retried tap risks re-processing a
    decision. Reply-side idempotency still guards the money; the 200-first
    handshake just avoids leaning on it.
    """
    if not telegram_secret_ok(x_telegram_bot_api_secret_token):
        raise HTTPException(403, "invalid telegram webhook secret")

    message = update.get("message") or update.get("edited_message")
    callback = update.get("callback_query")
    if not isinstance(message, dict) and not isinstance(callback, dict):
        return {"status": "ignored"}

    if isinstance(message, dict):
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None or "text" not in message:
            return {"status": "ignored"}  # stickers, photos, joins: nothing to route
        chat_id: int | str = chat["id"]
        payload: tuple[str, Any] = ("message", str(message.get("text") or ""))
    else:
        cb_msg = callback.get("message")
        chat = cb_msg.get("chat") if isinstance(cb_msg, dict) else None
        data = callback.get("data")
        if not isinstance(chat, dict) or chat.get("id") is None or not isinstance(data, str) \
                or not callback.get("id"):
            return {"status": "ignored"}
        chat_id = chat["id"]
        payload = ("callback", (str(callback["id"]), data))

    owner = tg_owner_chat_id()
    allowed_ips = {ip.strip() for ip in env("TELEGRAM_ALLOWED_IPS").split(",") if ip.strip()}
    is_owner = bool(owner) and str(chat_id) == owner
    is_allowed_ip = bool(x_forwarded_for) and \
        x_forwarded_for.split(",")[0].strip() in allowed_ips
    if not (is_owner or is_allowed_ip):
        # An unauthorised sender never reaches FinanceStore or the bot token.
        store = get_store()  # ensures 503 before the log line if state is down
        store.log("warning", "TELEGRAM",
                  f"ignored update from unauthorised chat {chat_id}", is_audit=True)
        return {"status": "ignored", "reason": "unauthorised chat"}

    # Raise here (before the background phase) so a missing token surfaces as
    # a 5xx Telegram will retry, rather than vanishing into a background log.
    token = require_tg_token()
    store = get_store()

    if payload[0] == "message":
        text = payload[1]
        store.log("info", "TELEGRAM", f"inbound message: {text[:200]}",
                  extra={"chat_id": str(chat_id)})
        background.add_task(_telegram_phase2_text, store, token, chat_id, text)
    else:
        callback_id, data = payload[1]
        store.log("info", "TELEGRAM", f"inbound callback: {data[:100]}",
                  extra={"chat_id": str(chat_id), "callback_id": callback_id})
        background.add_task(handle_telegram_approval, store, token, chat_id, callback_id, data)
    return {"status": "ok"}


async def _telegram_phase2_text(store: FinanceStore, token: str,
                                chat_id: int | str, text: str) -> None:
    try:
        reply = await asyncio.to_thread(handle_telegram_message, store, text)
    except Exception as exc:  # noqa: BLE001 — surfaced to the owner as a reply
        reply = telegram_worker_error(exc)
    await telegram_reply(store, token, chat_id, reply)


# ---------------------------------------------------------------------------
# Standalone runner (also usable as `uvicorn finance_agent:app`)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ghost X2 finance agent (FastAPI app)")
    parser.add_argument("--host", default=env("FINANCE_AGENT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=_int_env("FINANCE_AGENT_PORT", 3200))
    parser.add_argument("--reload", action="store_true", help="dev only: reload on edit")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    import uvicorn
    try:
        # Import-string form (not the app object) is required for --reload: uvicorn
        # re-imports the module in a subprocess, which it cannot do with an object.
        uvicorn.run(
            "finance_agent:app",
            host=args.host, port=args.port, reload=args.reload, log_level=args.log_level,
        )
    except ModuleNotFoundError as exc:
        print(f"missing dependency: {exc}. Install: pip install uvicorn", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
