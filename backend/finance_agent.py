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
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote_plus

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request

# Sibling module, imported by name: uvicorn and `python finance_agent.py` both put
# this directory on sys.path, but a WSGI/container launcher might not. The import
# is therefore guarded, and a failure to load the router degrades the chat to
# keyword routing rather than taking the money endpoints down with it.
try:
    import llm_router as llm_routing
except ImportError:  # pragma: no cover - depends on launcher, not on this file
    llm_routing = None  # type: ignore[assignment]

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
    # Amount above which an approval card carries a ⚠️ marker: the
    # "high-value" line from the spec ($50 default). The card itself is sent
    # for EVERY pending tier-2 intent (floor = tier1_max, $30) — suppressing a
    # $30 ask entirely was the one interpretation that silently loses approvals.
    approval_notify_floor_minor: int
    supervisor_enabled: bool
    supervisor_interval_seconds: int
    supervisor_micro_cap_minor: int
    supervisor_anomaly_factor: int

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
            approval_notify_floor_minor=_int_env("TELEGRAM_APPROVAL_NOTIFY_FLOOR_MINOR", 5000),
            # Default OFF: the VPS may also run standalone supervisor.py
            # (systemd or cron), and two auto-approver loops double-decide
            # independently. Flip SUPERVISOR_ENABLED=true when the in-API loop
            # is the sole owner of ticks.
            supervisor_enabled=env("SUPERVISOR_ENABLED").lower() in {"1", "true", "yes", "on"},
            # Floor is the 5s in _supervisor_interval, NOT a second 30s here:
            # two clamps on one knob meant a test (or an operator) setting 5 or
            # 1 silently got 30 — a lie about config is worse than a rejected
            # one. The default (45) sits in the documented 30-60s band; below 5
            # is clamped at read time, above 3600 makes the tick meaningless.
            supervisor_interval_seconds=min(3600, max(5, _int_env("SUPERVISOR_INTERVAL_SECONDS", 45))),
            supervisor_micro_cap_minor=_int_env("SUPERVISOR_MICRO_CAP_MINOR", 500),
            supervisor_anomaly_factor=max(2, _int_env("SUPERVISOR_ANOMALY_FACTOR", 5)),
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
            approval_notify_floor_minor=self.setting(
                "telegram.approval_floor_minor", s.approval_notify_floor_minor),
            # Loop cadence/flags come from ENV, deliberately NOT from the DB
            # settings table: these are read on every tick, and "change a knob
            # without a redeploy" wants a restart-cheap surface, not a hot
            # table poll that half-applies mid-loop.
            supervisor_enabled=s.supervisor_enabled,
            supervisor_interval_seconds=s.supervisor_interval_seconds,
            supervisor_micro_cap_minor=s.supervisor_micro_cap_minor,
            supervisor_anomaly_factor=s.supervisor_anomaly_factor,
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
        # Placeholders stay PLAIN %s, consumed in list order: the (col, expr,
        # value) triples keep binding and cast aligned by construction. The
        # short-indexed form (%s1) this used to emit was a psycopg2-ism —
        # psycopg3 only honours named placeholders like %(name)s, and under
        # client-side binding "%s1::x" degrades to "$11::x", which PostgreSQL
        # cannot type: IndeterminateDatatype on the FIRST gated spend ever to
        # hit /spend/gate in production (proven on the live PG16: bare %s +
        # cast OK, %s1 + cast fails with any parameter count).
        cols = ", ".join(col for col, _, _ in fields)
        vals = ", ".join(expr for _, expr, _ in fields)
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
    # Pending tier-2 spend becomes an interactive Telegram card (inline
    # Approve/Deny). Best-effort by design: the queue row above is the durable
    # record, so a delivery failure logs and returns — it never 5xx's the gate.
    if tx_id and approval_status == "pending":
        try:
            await notify_telegram_pending(store, tx_id, s)
        except Exception as exc:
            log.warning("approval card dispatch failed for %s: %s", tx_id[:8], exc)
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
    cfg0 = FinanceSettings.from_env()
    STATE["cfg"] = {"dry_run_default": cfg0.dry_run_default}
    log.info("finance agent up: dry_run=%s issuer=%s supervisor=%s",
             cfg0.dry_run_default, STATE["issuer"].configured, cfg0.supervisor_enabled)
    global _SUPERVISOR_TASK
    if cfg0.supervisor_enabled:
        # Fire one immediate tick, then settle into the cadence: first-boot
        # state (expired queues, pending backlog) lands before the dashboard
        # polls, and a dead DB at boot costs one backoff, not the whole app —
        # run_supervisor_loop swallows everything except cancellation.
        _SUPERVISOR_TASK = asyncio.create_task(run_supervisor_loop(),
                                               name="sirius-supervisor")
    try:
        yield
    finally:
        if _SUPERVISOR_TASK is not None:
            _SUPERVISOR_TASK.cancel()
            with suppress(asyncio.CancelledError):
                await _SUPERVISOR_TASK
            _SUPERVISOR_TASK = None
        await STATE["issuer"].aclose()
        if llm_routing is not None:
            await llm_routing.aclose_router()
        STATE.clear()


app = FastAPI(title="Ghost X2 finance agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health(store: FinanceStore = Depends(get_store)) -> dict[str, Any]:
    return {
        "ok": True,
        "db": True,
        "dry_run": store.is_dry_run(),
        "issuer_configured": bool(STATE["issuer"].configured),
        # Which models the chat path would actually use, no secrets: a wrong slug
        # or a missing key is otherwise invisible until an owner's message fails.
        "llm": _llm_health(),
    }


@app.get("/llm/routing")
def llm_routing_view() -> dict[str, Any]:
    """Routing table in full (providers, tier chains, attempt counters)."""
    if llm_routing is None:
        return {"ok": False, "error": "llm_router module not importable"}
    return {"ok": True, **llm_routing.get_router().describe()}


def _llm_health() -> dict[str, Any]:
    if llm_routing is None:
        return {"configured": False, "error": "llm_router module not importable"}
    try:
        router = llm_routing.get_router()
        return {
            "configured": bool(router.providers),
            "enabled": tg_llm_enabled(),
            "providers": [p["name"] for p in router.describe()["providers"]],
            "fast_model": router.candidates(llm_routing.TIER_FAST)[0].label,
            "reasoning_model": router.candidates(llm_routing.TIER_REASONING)[0].label,
        }
    except Exception as exc:  # health must answer 200 with a reason, not 500
        return {"configured": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


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


@app.post("/approvals/{tx_id}/notify")
async def approvals_notify(tx_id: str,
                           store: FinanceStore = Depends(get_store)) -> dict[str, Any]:
    """(Re)send the interactive approval card for a queued intent.

    Covers cards lost to a Telegram outage, and gives the Command Deck a button
    per pending row. 404 covers 'no such row' and 'row is not pending' — a card
    for a decided intent would offer dead buttons."""
    if not _TG_UUID_RE.fullmatch(tx_id):
        raise HTTPException(422, "tx_id must be a full uuid")
    row = await asyncio.to_thread(_fetch_gate_row, store, tx_id)
    if not row:
        raise HTTPException(404, "transaction not found")
    if row.get("approval_status") != "pending":
        raise HTTPException(404, f"intent is {row.get('approval_status')}, not pending")
    sent = await notify_telegram_pending(store, tx_id)
    if not sent:
        raise HTTPException(502, "telegram delivery failed (token/chat unset?)")
    return {"status": "sent", "transaction_id": tx_id}


# ---------------------------------------------------------------------------
# Autonomous supervisor loop (runs inside this app's lifespan)
# ---------------------------------------------------------------------------


def _table_exists(cur: Any, table: str) -> bool:
    cur.execute("select to_regclass(%s) is not null as ok", (f"engine.{table}",))
    return bool((cur.fetchone() or {}).get("ok"))


def scan_error_digest(store: FinanceStore, since_minutes: int = 10,
                      handle_exclude: str | None = None) -> str | None:
    """Newest ERROR/CRITICAL agent_logs line in the window, or None.

    engine.agent_logs keeps raw Postgres errors ('column "x" does not exist')
    alongside our own; every candidate is escaped at the call site, and the
    digest is capped, so no stored line can inject HTML into the alert card.
    Raw Postgres text is kept on purpose: that is exactly what an owner needs
    to see at 3am.
    """
    try:
        with store._connect() as conn, conn.cursor() as cur:
            if not _table_exists(cur, "agent_logs"):
                return None
            sql = ("select message from engine.agent_logs "
                   "where level in ('error','critical') "
                   "and created_at > now() - (%s || ' minutes')::interval")
            params: list[Any] = [str(since_minutes)]
            if handle_exclude:
                sql += " and agent_handle <> %s"
                params.append(handle_exclude)
            cur.execute(sql + " order by created_at desc limit 1", params)
            row = cur.fetchone()
            return str(row["message"])[:200] if row else None
    except Exception:  # a diagnostics query must never break a tick
        return None


def supervisor_tick(store: FinanceStore) -> dict[str, Any]:
    """One synchronous pass (runs under asyncio.to_thread). Returns a counts
    dict for structured logging; raises only on Postgres failure, which the
    caller maps into the backoff path.

    Single-writer note: two loops racing the auto-approve can double-APPROVE
    (both UPDATEs succeed on the same still-pending row). The schema has no
    advisory-lock key column we own, so coordination is operational, not
    speculative SQL: SUPERVISOR_ENABLED (env) defaults OFF, and standalone
    supervisor.py must not run its tick while this loop is enabled.

    Risk rules:
      * micro (< micro_cap) + agent healthy + no recent agent errors
        + amount inside its card envelope -> auto_approved/authorized
        (dry_run approves the intent without executing it);
      * anything else -> left pending and re-carded at most once per hour
        (fire_and_forget card dispatch; the DB queue remains the truth);
      * untouched pending > 12h -> escalated; > expiry -> expired. Dedupe
        model (agent_logs is append-only, so a marker cannot be UPDATE-d):
        anomaly re-alerts dedupe on 'marker within the hour' and RE-ALERT
        when the hour lapses; escalation and expiry markers are lifetime
        one-shots — hence a 12h+ row re-escalates every tick until it
        crosses the expiry cutoff and leaves the queue (documented, and
        bounded: expiry is guaranteed after approval_expiry_minutes).
    """
    s = store.resolved_settings()
    counts = {"micro_approved": 0, "anomalies": 0, "escalated": 0,
              "expired": 0, "recarded": 0, "errors_flagged": 0}
    with store._connect() as conn, conn.cursor() as cur:
        if not _table_exists(cur, "transactions"):
            return counts
        cur.execute(
            "select transaction_id::text as id, title, amount_minor, currency, tier, "
            "agent_handle, vendor, rationale, expires_at::text as expires_at, "
            "created_at, approved_at from engine.transactions "
            "where direction = 'spend' and approval_status = 'pending' "
            "and (expires_at is null or expires_at > now()) "
            "order by created_at limit 20")
        rows = cur.fetchall()
        # Snapshot before ANY write this pass: every marker/dedupe probe below
        # queries agent_logs, and with autocommit a marker inserted for row A
        # would satisfy row B's "already alerted?" probe the moment row B shares
        # its transaction_id literal — and would flood the notify fan-out.
        # Fresh reads for the decision-making are what this snapshot preserves.
        existing_logs = _load_alert_history(store, [str(r["id"]) for r in rows])

        agent_errs: dict[str, str] = {}
        table_logs = _table_exists(cur, "agent_logs")
        if table_logs:
            cur.execute(
                "select agent_handle, event, message, created_at from engine.agent_logs "
                "where level in ('error','critical') "
                "and created_at > now() - interval '30 minutes' "
                "order by created_at desc limit 100")
            for r in cur.fetchall():
                agent_errs.setdefault(str(r["agent_handle"]), str(r["message"]))

        for r in rows:
            tx_id = str(r["id"])
            amount = int(r["amount_minor"] or 0)
            ccy = str(r["currency"] or s.default_currency)
            agent = str(r["agent_handle"] or AGENT_HANDLE)
            # Postgres created_at is timestamptz; a column regression to naive
            # timestamps would raise TypeError on aware-minus-naive EVERY tick,
            # silently converting the whole loop into a backoff spiral.
            created = r["created_at"]
            if isinstance(created, datetime) and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_min = (datetime.now(UTC) - created).total_seconds() / 60 if created else 0.0

            reasons: list[str] = []
            if amount > s.supervisor_micro_cap_minor:
                reasons.append(f"above micro cap {fmt_minor(s.supervisor_micro_cap_minor, ccy)}")
            # Agent-health signal: the request's OWN agent, or the shared
            # finance-agent handle (this module's writes and the loop's own
            # errors land there). It does NOT stand in for every OTHER agent:
            # arb-bot's approval cannot depend on funnel-worker's bad hour.
            err = agent_errs.get(agent) or agent_errs.get(AGENT_HANDLE)
            if err:
                reasons.append(f"agent errors recently: {err[:120]}")
            day = 24 * 60
            if age_min > day:
                reasons.append("aged over 24h")

            # Envelope check inline: reusing check_card_limit would re-query
            # spend_totals per candidate; one row at a time keeps the tick
            # boring, which is the point of a 45-second loop. Runs for every
            # candidate (even ones already anomalous) so reasons stay whole.
            cur.execute(
                "select coalesce(sum(amount_minor) filter (where occurred_at >= "
                "date_trunc('day', now())),0)::bigint as today from engine.financial_ledger "
                "where agent_handle = %s and currency = %s and entry_type in "
                "('charge','fee','expense','dispute','chargeback','transfer_out')",
                (agent, ccy.upper()))
            today = int((cur.fetchone() or {}).get("today") or 0)
            if amount > max(0, s.card_daily_cap_minor - today):
                reasons.append(
                    f"would exhaust daily card cap (room "
                    f"{fmt_minor(max(0, s.card_daily_cap_minor - today), ccy)})")

            if reasons:
                counts["anomalies"] += 1
                # Hourly re-card (marker row = dedupe ledger; transactions has
                # no alert columns in this schema).
                if ("approval.anomaly_flag", tx_id, True) not in existing_logs:
                    counts["recarded"] += 1
                    cur.execute(
                        "insert into engine.agent_logs "
                        "(agent_handle, level, event, message, context, is_audit) "
                        "values (%s, 'warning', 'approval.anomaly_flag', %s, %s::jsonb, true)",
                        (AGENT_HANDLE,
                         f"{agent}: {fmt_minor(amount, ccy)} held for human review: "
                         f"{'; '.join(reasons)}"[:2000],
                         json.dumps({"transaction_id": tx_id})))
                # One-shot escalation past 12h: the hourly card has proven
                # insufficient, so the notify layer sends it again as ⏳.
                if age_min > 12 * 60 and ("approval.escalated", tx_id) not in existing_logs:
                    counts["escalated"] += 1
                    cur.execute(
                        "insert into engine.agent_logs "
                        "(agent_handle, level, event, message, context, is_audit) "
                        "values (%s, 'warning', 'approval.escalated', %s, %s::jsonb, true)",
                        (AGENT_HANDLE,
                         f"intent {tx_id[:8]} awaiting human {int(age_min // 60)}h: "
                         f"{'; '.join(reasons)}"[:2000],
                         json.dumps({"transaction_id": tx_id})))
                # Expiry is time-bound, independent of anomaly: a row can cross
                # it while still inside the freshness window we scanned under.
                if age_min > s.approval_expiry_minutes \
                        and ("approval.expired", tx_id) not in existing_logs:
                    cur.execute(
                        "update engine.transactions set approval_status = 'expired' "
                        "where transaction_id = %s and approval_status = 'pending'",
                        (tx_id,))
                    cur.execute(
                        "insert into engine.agent_logs "
                        "(agent_handle, level, event, message, context, is_audit) "
                        "values (%s, 'warning', 'approval.expired', %s, %s::jsonb, true)",
                        (AGENT_HANDLE,
                         f"intent {tx_id[:8]} expired unanswered "
                         f"({int(age_min // 60)}h)",
                         json.dumps({"transaction_id": tx_id})))
                    counts["expired"] += 1
                continue

            # micro + healthy + inside envelope: approve (execute honored only
            # outside dry_run, mirroring tier_action's contract)
            dry = store.is_dry_run()
            cur.execute(
                "update engine.transactions set approval_status = 'auto_approved', "
                "fulfillment_status = %s, approved_by = %s, approved_at = now() "
                "where transaction_id = %s and approval_status = 'pending' returning id",
                ("authorized" if not dry else "initiated",
                 f"policy:supervisor:{AGENT_HANDLE}"[:200], tx_id))
            if cur.fetchone():
                counts["micro_approved"] += 1
                # 3 bind params (handle, message, context) against 6 columns:
                # level/event/is_audit are SQL literals. The harness-mismatch
                # that once hid here is exactly why the column list names each
                # literal position explicitly.
                cur.execute(
                    "insert into engine.agent_logs "
                    "(agent_handle, level, event, message, context, is_audit) "
                    "values (%s, 'info', 'approval.auto_approved', %s, %s::jsonb, true)",
                    (AGENT_HANDLE,
                     f"micro {fmt_minor(amount, ccy)} auto-approved: {str(r['title'])[:120]}",
                     json.dumps({"transaction_id": tx_id, "dry_run": dry})))

        # Direct scan, scoped to the instant the pass BEGAN: catches errors
        # raised during this very tick (a bug that throws on every tick alerts
        # on the first one, one digest per 15 min thanks to the notify-layer
        # throttle) and never re-alerts history, which the window excludes.
        digest = scan_error_digest(store, since_minutes=2,
                                   handle_exclude=AGENT_HANDLE)
        if digest:
            counts["errors_flagged"] = 1
        cur.execute(
            "insert into engine.agent_logs "
            "(agent_handle, level, event, message, context, is_audit) "
            "values (%s, 'info', 'supervisor.tick', %s, %s::jsonb, false)",
            (AGENT_HANDLE,
             "tick: " + ", ".join(f"{k}={v}" for k, v in counts.items()),
             json.dumps({"counts": counts, "error_digest": digest})))
    return counts


def _load_alert_history(store: FinanceStore, tx_ids: list[str]) -> set[tuple]:
    """(event, transaction_id, fresh?) for this pass's candidate ids. 'fresh'
    = raised inside the last hour; escalation/expiry markers are lifetime
    one-shots, anomaly re-alerts are hourly. One query, no per-row probes."""
    if not tx_ids:
        return set()
    try:
        with store._connect() as conn, conn.cursor() as cur:
            if not _table_exists(cur, "agent_logs"):
                return set()
            cur.execute(
                "select event, context->>'transaction_id' as tx, "
                "created_at > now() - interval '1 hour' as fresh "
                "from engine.agent_logs "
                "where event in ('approval.escalated','approval.expired',"
                "'approval.anomaly_flag') and context->>'transaction_id' = any(%s)",
                (list(tx_ids),))
            return {(r["event"], r["tx"], bool(r["fresh"])) for r in cur.fetchall()}
    except Exception:  # diagnostics only; worst case a duplicate alert
        return set()


# task is joined before the loop can observe a torn STATE, which would surface
# as a backoff line, not a traceback.
_SUPERVISOR_TASK: asyncio.Task[None] | None = None


def _supervisor_interval(store: FinanceStore) -> int:
    """Clamped again even though from_env clamps: resolved_settings is the DB-
    aware path and a future edit there could hand back 0 or a float — either
    would spin (sleep(0)) or mis-size the tick timeout. Floor 5s (below the
    operational 30s in from_env) exists so tests can drive real loop cycles."""
    try:
        iv = int(store.resolved_settings().supervisor_interval_seconds)
    except Exception:
        return 60
    return min(3600, max(1, iv))


async def run_supervisor_loop() -> None:
    """Poll the ledger forever; NEVER raise — the API dies only from its own
    faults, never from a supervisor iteration. Backoff doubles from the tick
    interval to 10 min on consecutive DB failures; any clean tick resets it.
    CancelledError propagates by design (lifespan shutdown joins us)."""
    store = get_store()
    interval = _supervisor_interval(store)
    backoff = interval
    log.info("supervisor loop starting (interval=%ss)", interval)
    while True:
        try:
            counts = await asyncio.wait_for(
                asyncio.to_thread(supervisor_tick, store), timeout=float(interval))
            # Interval re-read PER tick (cheap: env/env), so an operator edit
            # lands on the next sleep, not the next boot. A failure in that
            # read must never escape the catch below as a silent death.
            interval = _supervisor_interval(store)
            backoff = interval
            log.info("supervisor tick %s", counts)
            await _supervisor_notify(store, counts)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("supervisor tick timed out after %ss", interval)
        except Exception as exc:  # psycopg, network, or our own bug — none fatal
            log.warning("supervisor tick failed (backoff %ss): %s", backoff, exc)
        try:
            await asyncio.sleep(float(backoff))
        except asyncio.CancelledError:
            raise
        # Double, clamped, and floored at the configured interval: a transient
        # blip that happens to fail twice must NOT jump to 90s when the whole
        # loop was configured for 45 — cadence never overshoots what the
        # operator asked for. (Tests may set SUPERVISOR_INTERVAL_SECONDS=5.)
        base = _supervisor_interval(store)
        backoff = min(max(backoff * 2, base), 600)


async def _supervisor_notify(store: FinanceStore, counts: dict[str, Any]) -> None:
    """Telegram fan-out for one tick, deduped against agent_logs markers so a
    restart cannot re-alert the same event. Fire-and-log throughout."""
    token, chat = tg_token(), tg_owner_chat_id()
    if not (token and chat):
        return
    if counts.get("recarded"):
        ids = await asyncio.to_thread(_marker_ids_since, store,
                                      "approval.anomaly_flag", "5 minutes")
        for tx_id in ids[:3]:
            try:
                await notify_telegram_pending(store, tx_id)
            except Exception as exc:
                log.warning("anomaly card failed for %s: %s", tx_id[:8], exc)
    if counts.get("escalated"):
        esc = await asyncio.to_thread(_marker_ids_since, store,
                                      "approval.escalated", "10 minutes")
        for tx_id in esc[:3]:
            row = await asyncio.to_thread(_fetch_gate_row, store, tx_id)
            if row:
                await telegram_reply(store, token, chat,
                                     "⏳ " + build_approval_card(row))
    # Error digest (written by the tick that just ran, which scans a window
    # scoped to its own start): alert at most one digest per 15 min and only
    # for a NEW digest, so a persistent raw error alerts once, not every tick,
    # while a DIFFERENT new error still breaks through.
    digest = None
    try:
        digest = await asyncio.to_thread(_latest_error_digest, store)
    except Exception:
        pass
    if digest:
        global _LAST_ERROR_ALERT
        from time import monotonic
        now_mono = monotonic()
        if digest != _LAST_ERROR_ALERT[0] or now_mono - _LAST_ERROR_ALERT[1] > 900:
            _LAST_ERROR_ALERT = (digest, now_mono)
            await telegram_reply(store, token, chat,
                                 f"🚨 <b>Agent error</b>\n<code>{_escape(digest)}</code>")


_LAST_ERROR_ALERT: tuple[str | None, float] = (None, 0.0)


def _latest_error_digest(store: FinanceStore) -> str | None:
    """The digest stamped by the most recent tick's audit row. Reading it from
    the row rather than rescanning keeps notify a pure reader: exactly what
    the just-finished pass observed, no second opinion."""
    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select context->>'error_digest' as d from engine.agent_logs "
                "where event = 'supervisor.tick' order by created_at desc limit 1")
            row = cur.fetchone()
            return str(row["d"])[:200] if row and row.get("d") else None
    except Exception:
        return None


def _marker_ids_since(store: FinanceStore, event: str, window: str) -> list[str]:
    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select context->>'transaction_id' as id, max(created_at) as newest "
                "from engine.agent_logs "
                "where event = %s and created_at > now() - interval %s "
                "group by 1 order by newest desc limit 3", (event, window))
            return [str(r["id"]) for r in cur.fetchall() if r.get("id")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Telegram conversational agent (Option A webhook: POST /webhooks/telegram)
# ---------------------------------------------------------------------------

TG_API_BASE = "https://api.telegram.org"
# The blueprint's draft had this URL mangled into a Markdown link
# ("https://api.telegram.org/bot](...)"); the base + token concat below is the fix.

# Two callback vocabularies are ACCEPTED, one is emitted:
#   * "approve_<uuid>" / "deny_<uuid>"  — the interactive-card format introduced
#     with the inline-approval feature (spec), what THIS module now sends;
#   * "appr:<uuid>" / "rej:<uuid>"      — backend/supervisor.py's
#     approval_keyboard_json, which is what already-live Telegram cards carry.
# Parsing must accept both (a tap on an old card must not break mid-deploy);
# emission stays single-format so the two senders don't drift further apart.
TG_ACTION_APPROVE = "appr"
TG_ACTION_REJECT = "rej"
TG_CB_PREFIXES = {
    "approve": TG_ACTION_APPROVE, "appr": TG_ACTION_APPROVE,
    "deny": TG_ACTION_REJECT, "rej": TG_ACTION_REJECT, "reject": TG_ACTION_REJECT,
}
_CB_ALT = "|".join(sorted(TG_CB_PREFIXES, key=len, reverse=True))
_CALLBACK_RE = re.compile(rf"^({_CB_ALT})[:_]([0-9a-fA-F-]{{36}})$")
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
    parse_mode: str = "HTML",
) -> httpx.Response:
    """One short-lived POST to sendMessage. parse_mode HTML by default, matching
    supervisor.py's AlertSender (same escaping rules, same 4096 soft cap).

    `parse_mode=""` is for text nobody escaped — the plain-English chat replies,
    whose prose must survive verbatim (see `_sanitise_reply`). Sending unescaped
    text as HTML is not a cosmetic miss: Telegram parses `<b>` as a tag and rejects
    a stray `<` as a malformed entity, so a model that writes one angle bracket
    either loses the character or kills the message entirely.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        return await client.post(
            tg_url(token, "sendMessage"), json=payload, headers=tg_headers(token))


def approval_keyboard_json(tx_id: str, action_prefix: str = "approve",
                           reject_prefix: str = "deny") -> dict[str, Any]:
    """Inline [Approve ✅]/[Deny ❌] for raw httpx reply_markup.

    Deliberate mirror of supervisor.approval_keyboard_json() with the new
    callback vocabulary (underscore + deny). The receiver accepts both spellings
    (see TG_CB_PREFIXES), so old cards and new cards are tappable side by side.
    Callback data carries the FULL 36-char uuid: Telegram's 64-byte
    callback_data cap leaves ample room, and a short id would make a prefix
    collision (missing vs duplicate) indistinguishable at tap time.
    """
    return {
        "inline_keyboard": [[
            {"text": "Approve ✅",
             "callback_data": f"{action_prefix}_{tx_id}"[:64]},
            {"text": "Deny ❌",
             "callback_data": f"{reject_prefix}_{tx_id}"[:64]},
        ]]
    }


def build_approval_card(tx: dict[str, Any]) -> str:
    """The outbound ask: what is being bought, for how much, by whom, until when."""
    amount = int(tx.get("amount_minor") or 0)
    ccy = str(tx.get("currency") or "USD")
    tier = str(tx.get("tier") or "tier2")
    emoji = {"tier1": "🟢", "tier2": "🟡", "tier3": "🔴"}.get(tier, "🟡")
    lines = [f"{emoji} <b>Approval requested</b> · {_escape(tier)}"]
    if tx.get("vendor"):
        lines.append(f"<b>{_escape(tx['vendor'])}</b> · {_escape(str(tx.get('title') or ''))}")
    else:
        lines.append(f"<b>{_escape(str(tx.get('title') or 'untitled intent'))}</b>")
    lines.append(f"<b>{_escape(fmt_minor(amount, ccy))}</b>")
    facts = []
    if tx.get("agent_handle"):
        facts.append(f"agent: <code>{_escape(tx['agent_handle'])}</code>")
    facts.append(f"id: <code>{_escape(str(tx.get('transaction_id') or '')[:8])}</code>")
    if tx.get("expires_at"):
        facts.append(f"expires <code>{_escape(str(tx['expires_at'])[:16])}</code>")
    lines.append(" · ".join(facts))
    return "\n".join(lines)


def build_decision_edit(tx: dict[str, Any], decided_at: str) -> str:
    """Replacement body for editMessageText: the FINAL recorded state plus the
    decision timestamp, so the card's last word is the ledger's, not the ask's."""
    status = str(tx.get("approval_status") or "")
    verb = {"approved": "Approved ✅", "rejected": "Denied ❌"}.get(status,
                                                                  f"Closed · {_escape(status)}")
    amount = int(tx.get("amount_minor") or 0)
    ccy = str(tx.get("currency") or "USD")
    lines = [
        f"{verb} · <b>{_escape(str(tx.get('title') or 'intent'))}</b>",
        f"<b>{_escape(fmt_minor(amount, ccy))}</b> · "
        f"<code>{_escape(str(tx.get('transaction_id') or '')[:8])}</code>",
    ]
    if tx.get("approved_by"):
        lines.append(f"by <code>{_escape(tx['approved_by'])}</code>")
    lines.append(f"decided <code>{_escape(decided_at[:19])} UTC</code>")
    return "\n".join(lines)


async def tg_edit_message(
    token: str, chat_id: int | str, message_id: int, text: str,
    remove_keyboard: bool = True,
) -> httpx.Response:
    """Swap a card's body to its decided state; by default the buttons vanish
    with it (reply_markup omitted = cleared), which IS the UI's 'spent' signal.
    A 400 'message is not modified' (double tap on identical text) is normal."""
    payload: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id, "text": text[:4000],
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if not remove_keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": []})
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        return await client.post(
            tg_url(token, "editMessageText"), json=payload, headers=tg_headers(token))


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
    setWebhook time) on every update. Unset secret = open mode, FOR LOCAL DEV
    ONLY: an empty value is logged loudly at WARNING on every update (rate-
    limited), because a setWebhook made WITH a secret sends the header, but a
    webhook registered WITHOUT one sends nothing — and a deployment where both
    sides drifted empty accepts forged updates from anyone who finds the URL."""
    secret = env("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        global _SECRET_WARNED
        if not _SECRET_WARNED:
            log.warning("TELEGRAM_WEBHOOK_SECRET is empty: accepting UNSIGNED "
                        "telegram updates (production must set it, and register "
                        "the webhook with the same secret)")
            _SECRET_WARNED = True
        return True
    return bool(provided) and hmac.compare_digest(secret, provided)


_SECRET_WARNED = False


async def telegram_reply(store: FinanceStore, token: str, chat_id: int | str, text: str,
                         parse_mode: str = "HTML") -> None:
    try:
        r = await tg_send_message(token, chat_id, text, parse_mode=parse_mode)
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
    message_id: int | None = None,
) -> None:
    """Apply an inline-button tap for a tier-2 intent.

    Same semantics as supervisor.SupervisorStore.apply_decision, reimplemented
    here on purpose: finance_agent.py carries no imports from supervisor.py, so
    this module stays deployable without python-telegram-bot installed. The
    still-pending WHERE makes it idempotent and race-safe; a lost race is the
    desired outcome, reported as 'already decided', not an error.

    Accepted callback_data: (approve|deny|appr|rej|reject) [:_] <full uuid>.
    On a recorded decision the ORIGINAL card is edited (editMessageText) to the
    final ledger state + timestamp, which doubles as the idempotency tell: a
    spent card has no buttons left to tap. Missing message_id (older update
    shapes, or a tap we could not correlate) falls back to a fresh reply.
    """
    match = _CALLBACK_RE.match(data or "")
    if not match:
        await tg_answer_callback(token, callback_id, "Unrecognised button")
        await telegram_reply(store, token, chat_id,
                             "🤔 Unrecognised callback. Try /status or /pending.")
        return
    action = TG_CB_PREFIXES[match.group(1)]
    tx_id = match.group(2)
    log.debug("approval tap: cb=%s chat=%s data=%r mid=%s", callback_id, chat_id, data, message_id)
    try:
        outcome = await asyncio.to_thread(_apply_tg_decision, store, tx_id, action)
    except Exception:  # _apply_tg_decision already logged + re-raised
        await tg_answer_callback(token, callback_id, "Storage error")
        await telegram_reply(store, token, chat_id,
                             f"⚠️ <b>Could not record that tap</b> for intent "
                             f"<code>{_escape(tx_id[:8])}</code> — check the service logs.")
        return
    store.log("info", "TELEGRAM",
              f"approval tap: {action} {tx_id[:8]} -> {outcome}",
              transaction_id=tx_id, extra={"callback_id": callback_id})
    toast = {"decided": ("Approved ✅" if action == TG_ACTION_APPROVE else "Denied ❌"),
             "duplicate": "Already decided", "missing": "Intent not found"}[outcome]
    await tg_answer_callback(token, callback_id, toast)
    if outcome == "missing":
        await telegram_reply(store, token, chat_id,
                             f"❓ Intent <code>{_escape(tx_id[:8])}</code> not found.")
        return
    row = await asyncio.to_thread(_fetch_decision_row, store, tx_id)
    body = build_decision_edit(row, str(row.get("approved_at") or ""))
    if outcome == "duplicate":
        body += "\n(already decided earlier — nothing changed)"
    if message_id is None:
        await telegram_reply(store, token, chat_id, body)
        return
    try:
        r = await tg_edit_message(token, chat_id, message_id, body)
        if r.status_code >= 400 and "not modified" not in r.text:
            log.warning("editMessageText failed (%s): %s", r.status_code, r.text[:200])
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        log.warning("editMessageText unreachable (non-fatal, card stays live): %s", exc)


def _fetch_decision_row(store: FinanceStore, tx_id: str) -> dict[str, Any]:
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select transaction_id::text as transaction_id, title, amount_minor, "
            "currency, approval_status, approved_by, approved_at::text as approved_at "
            "from engine.transactions where transaction_id = %s", (tx_id,))
        return dict(cur.fetchone() or {})


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
    """Keyword routing for text updates. Returns the reply HTML, or "" when the
    message matched no command and should reach the model.

    Command matching is prefix-based and case-insensitive, as it was before, and it
    stays the only synchronous path: `/approve <uuid>` must mean approve whether or
    not a model provider is reachable. Unrecognised text returns "" and
    `route_telegram_text` decides what happens next.

    Anything the model or a user injects into a reply is escaped on the way out
    (`_sanitise_reply`), so a stray '<' cannot break parse_mode=HTML.
    """
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
    return ""


# ---------------------------------------------------------------------------
# Plain-English agent: LLM router + function calling
# ---------------------------------------------------------------------------
#
# Keyword routing above is the deterministic path, and it stays first on purpose:
# `/approve` must mean approve whether or not the model provider is reachable, and
# a slash command answered by an LLM is a command that can silently change shape.
# Anything the keyword table does not recognise falls through to here, which hands
# the sentence to the router with a small set of tools and executes whatever the
# model asks for.
#
# The tools are the SAME store writers the buttons and slash commands use
# (`_apply_tg_decision`), so there is one code path that touches approval state and
# the idempotent still-pending UPDATE guards all three.

# One prompt template, one capability sentence.
#
# The prompt is generated rather than fixed because the tool set varies by caller,
# and a model told "you can approve transactions" while holding only a read tool
# does the predictable thing: it offers to approve, then explains that approval
# "isn't wired in yet", which reads to the owner as a broken feature rather than as
# a deliberate boundary. Describing the real tool set keeps the offer and the offer's
# limit in one breath.
_SYSTEM_TEMPLATE = """You are Sirius, the autonomous financial supervisor of this operation.

You hold the ledger: every cent in and out, every pending approval, the daily and \
monthly card caps, and whether live money is armed (dry_run). You answer the owner in \
Telegram.

What you can actually do, because these are the only tools you have: {capability}.
Never offer an action that is not in that list, and never say you have done \
something no tool returned for.

How to work:
- Asked what is pending, waiting, or owed: CALL get_pending_transactions. Never \
invent a transaction id, an amount, or a count. The ledger is the only source.
- "How much did we spend", "what is the balance", "are we armed": answer from the \
ledger context injected below. If the figure is not in that context, call a tool; if \
no tool carries it, say you do not have it rather than estimating.
- When an action IS available to you and the owner asks for it, call the tool. Money \
only moves because a tool ran.
- If an instruction names no transaction, or names one you cannot match to the \
pending list, ASK which one. Do not pick the "obvious" one: guessing spends money.
- If the request needs something outside your tools (a refund issued, a vendor \
contacted, a policy changed), say plainly that you cannot do that yet, and name the \
command or button that can: /pending, /status, /approve plus the full id, /reject \
plus the full id, or the two buttons on an approval card. Write a placeholder as \
"the full id", never as angle brackets around a word: your reply is shown as plain \
text, so anything you write in angle brackets arrives at the owner's eyes as markup \
noise. Quote ids exactly as the tool returned them so the owner can copy one.

Style: plain English, no markdown, no tables, no preamble, no sign-off. Under 900 \
characters. Telegram italics only as _single underscores_. Currency exactly as the \
ledger reports it. Report to the owner like a person who runs the books: the number \
first, then what you did or what you need.
"""

CAPABILITY_READ_ONLY = (
    "reading the ledger and the pending approval queue. You cannot approve or reject "
    "anything yourself: the owner does that with the two buttons on a card, or "
    "/approve and /reject. When asked to approve, tell them that plainly and offer "
    "the exact command with the real id, ready to send"
)
CAPABILITY_FULL = (
    "read the ledger, and approve or reject any pending transaction by id when the "
    "owner asks"
)


def build_system_prompt(writer_tools_available: bool) -> str:
    return _SYSTEM_TEMPLATE.format(
        capability=CAPABILITY_FULL if writer_tools_available else CAPABILITY_READ_ONLY)

# Number of prior owner messages replayed as conversation context. Bounded and
# counted in the budget below, because every replayed character is paid for again
# on each message.
TG_LLM_HISTORY_MESSAGES = 6
# Character budget over replayed history + injected ledger context.
TG_LLM_CONTEXT_CHARS = 2600
# Ceiling on the whole message list for one chat turn. The step count below bounds
# model round-trips; this bounds what those round-trips cost, since every tool
# result is replayed to the model on the next step.
MAX_CHAT_CONTEXT_CHARS = 14_000
# A tool result may never eat more than this slice of the turn's context budget.
# Adapted down to what remains at the moment the result is appended (see
# run_chat_turn), because a cap measured against the whole budget lets one wide
# payload push the message list past the ceiling it was supposed to respect.
TOOL_RESULT_BUDGET_FRACTION = 0.35
# Never squeeze a result below this: a tool result trimmed to a stub answers
# nothing, so it is better to overshoot slightly and stop the loop.
TOOL_RESULT_MIN_CHARS = 700
# Steps, not tool calls: one step can fan out to several parallel calls, so this
# counts paid model requests, not actions taken.
MAX_CHAT_TOOL_STEPS = 3
# Reserve, in characters, below which the next request is forced to be an answer.
# Deliberately generous: the trailing instruction plus the model's reply tokens are
# not in `messages`, so a tight reserve is what makes the last call overshoot.
CHAT_CONTEXT_RESERVE_CHARS = 3600

TOOL_GET_PENDING = "get_pending_transactions"
TOOL_APPROVE = "approve_transaction"
TOOL_REJECT = "reject_transaction"

TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": TOOL_GET_PENDING,
            "description": "List spend intents currently awaiting human approval, "
                           "newest first, with their transaction ids and amounts. "
                           "Call this before approving or rejecting anything the "
                           "owner refers to by description rather than by id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows to return, 1-25. Default 10.",
                        "minimum": 1, "maximum": 25,
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_APPROVE,
            "description": "Approve a pending spend intent by transaction id, marking "
                           "it authorized. Idempotent: an id that was already decided "
                           "returns that it was already decided and changes nothing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "string",
                        "description": "Full transaction UUID, exactly as returned by "
                                       "get_pending_transactions.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional short reason, recorded in the audit log.",
                    },
                },
                "required": ["transaction_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_REJECT,
            "description": "Reject a pending spend intent by transaction id, cancelling "
                           "it. Idempotent, same as approve_transaction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "transaction_id": {
                        "type": "string",
                        "description": "Full transaction UUID.",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional short reason, recorded in the audit log.",
                    },
                },
                "required": ["transaction_id"],
            },
        },
    },
)

# Transaction ids the model produced but which are not in the pending set it was
# shown: a hallucinated id must never reach the ledger as a decision.
_LOOSE_ID_KEYS = ("transaction_id", "tx_id", "id", "transaction")


def tg_llm_enabled() -> bool:
    """Opt-in, default ON, and switched off by itself when it cannot possibly work.

    A deployment with no LLM key keeps the keyword behaviour exactly; the flag exists
    so a provider outage mid-conversation does not have to be a redeploy to silence
    the chat path.

    `llm_available()` asks the shared router rather than rebuilding providers from
    env, for one reason beyond the avoided work: if the router has already been built,
    this reports the same thing the chat branch is about to use. Two reads of the
    environment could disagree when one had a key and the other had not.
    """
    return llm_available()


def llm_available() -> bool:
    """Whether the plain-English branch can run right now.

    Env is the authority, read fresh, not the shared router's cached provider list.
    The router builds its providers once, so after start it reports what the
    environment held when it was built: an operator who removes a key and restarts
    nothing would keep getting routed to a provider that can no longer authenticate.
    The router then fails those calls one at a time, per message, rather than the
    switch reflecting the operator's current intent.
    """
    if env("TG_LLM_ENABLED").lower() in {"0", "false", "no", "off"}:
        return False
    if llm_routing is None:
        return False
    try:
        return bool(llm_routing.providers_from_env())
    except Exception as exc:  # a broken LLM config disables chat, it never 500s
        log.warning("llm router unusable, plain-english chat disabled: %s", exc)
        return False


def get_llm_completion(prompt: str, *, system: str = "", tier: str = "fast",
                       messages: Sequence[dict[str, Any]] | None = None,
                       tools: Sequence[dict[str, Any]] | None = None,
                       tool_choice: Any = None, **kw: Any) -> Any:
    """Synchronous wrapper over the async router, for sync callers and tests.

    Raises RuntimeError inside a running loop rather than blocking it: the request
    path must call the coroutine directly, and a blocked loop thread here would
    stall every other webhook in the process.
    """
    if llm_routing is None:
        raise RuntimeError("llm_router module unavailable")
    coro = llm_routing.get_llm_completion(
        prompt, system=system, tier=tier, messages=messages,
        tools=tools, tool_choice=tool_choice, **kw)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("inside a running loop, await llm_router.get_llm_completion directly")


def _truncate_utf8(text: str, limit: int) -> bytes:
    """Cut on a character boundary. A model reply split mid-emoji is not worth a
    UnicodeDecodeError on the reply path."""
    return text.encode()[:limit]


def _recent_owner_messages(store: FinanceStore, limit: int = TG_LLM_HISTORY_MESSAGES,
                           exclude: str = "") -> list[dict[str, Any]]:
    """Last few inbound Telegram messages, oldest first, from agent_logs.

    The log is the only place inbound text lands, so history costs no new table.
    Tool turns are not recoverable from it (the tool's reply was an outbound
    message, indistinguishable from any other reply), so the model sees
    user/assistant text pairs and nothing else: enough to resolve "approve that
    one", not enough to make it think it already acted.
    """
    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute(
                # event/message column names come from the store's own log() seam:
                # event lands in `event`, and inbound text is prefixed there.
                "select message from engine.agent_logs "
                "where agent_handle = %s and event = 'TELEGRAM' "
                "and message like 'inbound message:%%' "
                "order by created_at desc limit %s",
                (AGENT_HANDLE, limit + 1))
            rows = cur.fetchall()
    except Exception as exc:
        log.debug("telegram history lookup unavailable: %s", exc)
        return []
    texts: list[str] = []
    for r in reversed(rows):
        text = str(r.get("message") or "").replace("inbound message:", "", 1).strip()
        if text and text != exclude:
            texts.append(text[:400])
    # Alternate as user/assistant pairs; the newest inbound text is added by the
    # caller, so replayed turns get an empty assistant placeholder.
    messages: list[dict[str, Any]] = []
    for text in texts:
        messages.append({"role": "user", "content": text})
        messages.append({"role": "assistant", "content": "ok"})
    return messages[-limit:]


def _ledger_snapshot(store: FinanceStore) -> str:
    """Small authoritative context block, so read-only questions need no tool call.

    Compact on purpose: this rides on every chat message, and the fast tier gets a
    new copy each time rather than a growing conversation.
    """
    s = get_settings()
    today = month = net = 0
    try:
        today, month = store.spend_totals(AGENT_HANDLE, s.default_currency)
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select coalesce(sum(case when entry_type in "
                "('revenue','refund','transfer_in','dispute_reversal','grant') "
                "then amount_minor else -amount_minor end),0)::bigint as net "
                "from engine.financial_ledger where currency = %s", (s.default_currency,))
            net = int((cur.fetchone() or {}).get("net") or 0)
            cur.execute(
                "select count(*)::bigint as n, coalesce(sum(amount_minor),0)::bigint as total "
                "from engine.transactions where direction = 'spend' "
                "and approval_status = 'pending' "
                "and (expires_at is null or expires_at > now())")
            pend = cur.fetchone() or {}
        pending_n = int(pend.get("n") or 0)
        pending_total = int(pend.get("total") or 0)
    except Exception as exc:
        log.debug("ledger snapshot failed, continuing without it: %s", exc)
        return "Ledger context: unavailable right now (database error). Say so if asked."
    return (
        "Ledger context (authoritative, computed just now):\n"
        f"- net position: {fmt_minor(net, s.default_currency)}\n"
        f"- spend today: {fmt_minor(today, s.default_currency)} · "
        f"month: {fmt_minor(month, s.default_currency)} · "
        f"monthly cap {fmt_minor(s.card_monthly_cap_minor, s.default_currency)}"
        "\n"
        f"- pending approvals: {pending_n} totalling "
        f"{fmt_minor(pending_total, s.default_currency)}\n"
        f"- auto-approve ceiling: {fmt_minor(s.tier1_max_minor, s.default_currency)} · "
        f"human-review ceiling: {fmt_minor(s.tier2_max_minor, s.default_currency)}\n"
        f"- dry_run (live money armed?): {str(store.is_dry_run()).lower()}\n"
        f"- supervisor auto-approver: {'on' if s.supervisor_enabled else 'off'}, "
        f"micro cap {fmt_minor(s.supervisor_micro_cap_minor, s.default_currency)}"
    )


def _pending_rows(store: FinanceStore, limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 10), 25))
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select transaction_id::text as transaction_id, title, vendor, amount_minor, "
            "currency, tier, created_at::text as created_at, expires_at::text as expires_at "
            "from engine.transactions "
            "where direction = 'spend' and approval_status = 'pending' "
            "and (expires_at is null or expires_at > now()) "
            "order by created_at desc limit %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


def _tool_get_pending(store: FinanceStore, args: dict[str, Any]) -> dict[str, Any]:
    """The only source of ids the model may quote back. Amounts are pre-formatted
    so the model reproduces money instead of dividing integers."""
    rows = _pending_rows(store, int(args.get("limit") or 10))
    return {
        "pending_count": len(rows),
        "transactions": [
            {
                # id and amount first: truncation drops the tail of this list, and
                # an id the model can still quote back is worth more than a vendor.
                "transaction_id": r["transaction_id"],
                "amount": fmt_minor(int(r.get("amount_minor") or 0),
                                    str(r.get("currency") or "USD")),
                "title": str(r.get("title") or "")[:80],
                "vendor": str(r.get("vendor") or "")[:40],
                "amount_minor": int(r.get("amount_minor") or 0),
                "tier": r.get("tier"),
                "requested_at": r.get("created_at"),
            }
            for r in rows
        ],
        "note": "Quote transaction_id values exactly as listed. If the owner means "
                "something not listed, the intent is already decided or does not exist.",
    }


def _is_uuid(value: str) -> bool:
    """Canonical 8-4-4-4-12 hex only.

    The stricter check exists because `::text` casts, psycopg and the schema's own
    lookups all require canonical form: a compact 32-hex id, or a canonical id
    padded with whitespace, makes PostgreSQL raise `invalid input syntax for type
    uuid`, which reads like a database outage and buries the actual answer.
    """
    return bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value))


def _decided_row_state(store: FinanceStore, tx_id: str) -> str | None:
    """Approval status of a row by full UUID, or None when no such row exists.

    Read outside the pending filter on purpose: it exists so a stale id reports
    'already approved' rather than 'not found', which is the difference between the
    model telling the owner their own decision back and telling them the
    transaction never existed. Any read failure returns None, which is the honest
    answer when state is unknown.
    """
    try:
        with store._connect() as conn, conn.cursor() as cur:
            cur.execute("select approval_status from engine.transactions "
                        "where transaction_id = %s", (tx_id,))
            row = cur.fetchone()
    except Exception as exc:
        log.debug("decided-state lookup failed for %s: %s", tx_id[:8], exc)
        return None
    return str(row["approval_status"]) if row else None


def _resolve_tx_id(store: FinanceStore, raw: str) -> tuple[str | None, str]:
    """Map whatever the model produced to exactly one pending transaction UUID.

    A full UUID is checked against the pending set only, so a stale or invented id
    cannot be applied to a row the model never saw. That filter is the real safety
    property, and it runs before any write, so an ambiguous or shortened prefix that
    happens to be unique is as safe to honour as a full id. This matters because a
    cheap model given twenty pending rows and no guidance will paraphrase the id it
    was shown, and refusing on formatting alone would turn a correct guess about
    which row was meant into a dead end.

    Prefix matching is hex-only, so it cannot widen a decimal-looking token into
    something that matches two vendors' rows by luck of characters.

    Second return value is the reason, and when an id is a real row that merely is
    not pending any more, the reason names its actual status: models repeat the
    tool's wording to the owner, so 'already approved' beats a shrug.
    """
    cand = (raw or "").strip().strip("<>`").strip()
    if not cand:
        return None, "no transaction id supplied"
    pending = _pending_rows(store, 25)
    by_id = {r["transaction_id"].lower(): r["transaction_id"] for r in pending}
    if cand.lower() in by_id:
        return by_id[cand.lower()], "exact"
    # Hex-only, 4 characters minimum (the shortest id any card or command displays,
    # so an owner reading "approve a3f2" is honoured). Dashes are stripped, so both
    # canonical and compact spellings reach the same comparison.
    flat = cand.lower().replace("-", "")
    if len(flat) < 4 or not re.fullmatch(r"[0-9a-f]+", flat):
        return None, f"'{cand[:40]}' is not a transaction id"
    hits = {tx for tx_id, tx in by_id.items() if tx_id.replace("-", "").startswith(flat)}
    if len(hits) == 1:
        return hits.pop(), "unique prefix"
    if len(hits) > 1:
        return None, f"'{cand[:12]}' matches several pending transactions"
    if len(hits) == 0 and _is_uuid(cand):
        # Canonical form is what lets us query the row at all (see _is_uuid).
        # A non-canonical id that matched nothing above is simply unknown.
        state = _decided_row_state(store, cand)
        if state and state != "pending":
            return None, f"that transaction was already {state}, so nothing changed"
        if state == "pending":
            # Pending in the table but absent from the queue we just read: expired.
            return None, "that transaction is pending but past its expiry window"
    return None, "no pending transaction matches that id (already decided, or not found)"


def _tidy_note(note: Any) -> str:
    return str(note or "").strip()[:200]


def _tool_decide(store: FinanceStore, args: dict[str, Any], action: str,
                 tool_name: str) -> dict[str, Any]:
    raw_id = next((str(args[k]) for k in _LOOSE_ID_KEYS if args.get(k)), "")
    tx_id, why = _resolve_tx_id(store, raw_id)
    if tx_id is None:
        # Refusal, not failure: the model relays it and asks the owner to be specific.
        store.log("warning", "llm.tool_refused",
                  f"{tool_name} refused: {why}", extra={"raw_id": raw_id[:40]},
                  is_audit=True)
        return {"ok": False, "error": why,
                "hint": "Call get_pending_transactions and use a listed id."}

    outcome = _apply_tg_decision(store, tx_id, action)
    row = _fetch_decision_row(store, tx_id)
    note = _tidy_note(args.get("note"))
    if outcome == "missing":
        return {"ok": False, "error": "that transaction disappeared before the decision "
                                      "was written; nothing changed"}
    if outcome == "duplicate":
        return {
            "ok": True, "duplicate": True,
            "transaction_id": tx_id,
            "approval_status": row.get("approval_status"),
            "amount": fmt_minor(int(row.get("amount_minor") or 0),
                                str(row.get("currency") or "USD")),
            "note": "Already decided earlier, so nothing changed.",
        }
    store.log("info", "llm.tool_executed",
              f"{tool_name} applied to {tx_id[:8]}"
              + (f": {note}" if note else ""),
              transaction_id=tx_id, is_audit=True)
    return {
        "ok": True, "duplicate": False,
        "transaction_id": tx_id,
        "approval_status": row.get("approval_status") or
                           ("approved" if action == TG_ACTION_APPROVE else "rejected"),
        "title": str(row.get("title") or "")[:120],
        "amount": fmt_minor(int(row.get("amount_minor") or 0),
                            str(row.get("currency") or "USD")),
        "decided_at": row.get("approved_at"),
        "dry_run": store.is_dry_run(),
    }


def _tool_arguments_failed(tool_name: str, err: str) -> dict[str, Any]:
    return {"ok": False, "error": f"the {tool_name} call arrived malformed: {err}",
            "hint": "Try again with a complete transaction_id."}


def _tool_unknown(name: str) -> dict[str, Any]:
    return {"ok": False, "error": f"there is no tool named {name!r}"}


TOOLS: dict[str, Callable[[FinanceStore, dict[str, Any]], dict[str, Any]]] = {
    TOOL_GET_PENDING: _tool_get_pending,
    TOOL_APPROVE: lambda s, a: _tool_decide(s, a, TG_ACTION_APPROVE, TOOL_APPROVE),
    TOOL_REJECT: lambda s, a: _tool_decide(s, a, TG_ACTION_REJECT, TOOL_REJECT),
}


def run_tool(store: FinanceStore, name: str, arguments: dict[str, Any],
             *, parse_error: str = "") -> dict[str, Any]:
    """Single execution seam for model tool calls (unit-testable in isolation)."""
    if parse_error:
        return _tool_arguments_failed(name, parse_error)
    handler = TOOLS.get(name)
    if handler is None:
        return _tool_unknown(name)
    try:
        return handler(store, arguments or {})
    except Exception as exc:  # a DB write failing must read as a tool result, not a 500
        log.exception("tool %s execution failed", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _longest_list_path(
    value: Any, path: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], list] | None:
    """Path to the biggest list anywhere in a nested payload.

    The row list is what makes a tool result large wherever it sits, and dropping
    whole rows off its end is the only truncation that keeps the JSON parseable.
    """
    best: tuple[tuple[str, ...], list] | None = None
    if isinstance(value, list) and (best is None or len(value) > len(best[1])):
        best = (path, value)
    if isinstance(value, dict):
        for k, v in value.items():
            found = _longest_list_path(v, (*path, str(k)))
            if found and (best is None or len(found[1]) > len(best[1])):
                best = found
    return best


def _set_path(container: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    """Copy-on-write set at a nested path, so the caller's dict is not mutated."""
    if not path:
        return container
    head, *rest = path
    inner = container.get(head)
    updated = dict(container)
    updated[head] = _set_path(inner, rest, value) if (rest and isinstance(inner, dict)) else value
    return updated


def _bounded_tool_result(name: str, result: dict[str, Any], budget: int) -> str:
    """Serialise a tool result under a character budget.

    Truncation drops whole list entries from the tail rather than cutting the JSON
    string, so the model always receives parseable text: a sliced tail reads as a
    corrupt message and invites the model to invent the missing rows.

    A trimmed result carries `truncated_rows` and a note. That is not decoration:
    shown thirteen of forty pending items and told nothing, a model reports "the
    three pending items" to the owner with full confidence.
    """
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= budget:
        return text
    found = _longest_list_path(result)
    if found:
        path, items = found
        slim = result
        for cut in range(len(items) - 1, -1, -1):
            # Shrink from the tail: the newest rows lead the list, so a cut loses the
            # least useful entries. `slim` accumulates, so each trial is smaller than
            # the last and the first one that fits is the largest one that fits.
            slim = _set_path(slim, path, items[:cut])
            candidate = json.dumps(
                {**slim,
                 "truncated_rows": len(items) - cut,
                 "note": f"showing the first {cut} of {len(items)} rows, the rest "
                         "were trimmed to fit; the count above is the true total"},
                ensure_ascii=False, default=str)
            if len(candidate) <= budget:
                return candidate
    # Nothing left to trim: either no list exists, or every field is individually
    # larger than the budget (a giant error string, say). Re-emit a minimal result
    # whose OWN fields are shrunk to fit, rather than slicing the serialised JSON,
    # because slicing produces `{"error": "eee…` and a model reads that as a broken
    # message and starts inventing what came after the cut.
    note = "tool result too large to fit; nothing was inferred from it"
    for text_cap in (max(0, budget - 120), 80, 20, 0):
        candidate = json.dumps({"ok": bool(result.get("ok")),
                                "error": str(result.get("error")
                                             or "result too large to fit")[:text_cap],
                                "note": note}, ensure_ascii=False)
        if len(candidate) <= budget:
            return candidate
    return "{}"


def _trim_history(messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Drop oldest turns until the replay fits the budget."""
    out = list(messages)
    while out and sum(len(str(m.get("content") or "")) for m in out) > budget:
        out = out[2:] if len(out) > 1 else []
    return out


def _sanitise_reply(text: str) -> str:
    """Model text → text safe to send with parse_mode=HTML, WITHOUT escaping it.

    Not escaping is a deliberate choice, and it is about copyability. The owner's
    main use of this reply is to lift a command out of it and send it, and the
    commands carry angle-bracket placeholders: escape the prose and "/approve <id>"
    reaches Telegram as the literal string "/approve &lt;id&gt;", which Telegram then
    shows as-is, so a copy-paste of the model's own suggestion fails. The chat path
    therefore sends the model's text as ordinary text and parses the reply as no
    entity at all.

    What that gives up is styling, not safety: nothing model-authored is treated as
    markup anywhere in the chat path, so a model emitting "<b>" or a stray "<" shows
    it literally and cannot break the message or inject a tag. Escaping belongs to
    the builders that DO emit markup (cards, status reports, the command router's
    help text), and those still call `_escape` on every interpolated value.

    Markdown the model writes out of habit (`**bold**`) is flattened to plain text
    because Telegram renders it nowhere and it reads as noise.
    """
    cleaned = re.sub(r"\r\n?", "\n", (text or "").strip())
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "_")
    return _truncate_utf8(cleaned, 3500).decode("utf-8", "ignore")


def _plain_quoted(text: str, limit: int = 60) -> str:
    """Quote inbound owner text for a plain-mode reply, on one line.

    The text is echoed into a reply about itself, so it needs delimiting, and it
    arrives from a chat client that can put anything in it: newlines would push the
    apology apart from the quote, and control characters would break the layout.
    Deliberately not escaped — these replies are sent with parse_mode off.
    """
    return repr(str(text or "")[:limit])[1:-1].replace("\t", "\\t")


def _degraded_reply(err: Exception) -> str:
    kind = type(err).__name__
    if llm_routing is not None and isinstance(err, llm_routing.LLMRequestRejected):
        detail = "the model rejected the request (check the configured model names)"
    elif llm_routing is not None and isinstance(err, llm_routing.LLMNotConfigured):
        detail = "no model provider is configured (set NVIDIA_API_KEY or OPENROUTER_API_KEY)"
    else:
        detail = "every configured model is unreachable right now"
    log.warning("llm chat path degraded: %s: %s", kind, str(err)[:200])
    # Plain text, no markup: this is sent with parse_mode off like every other
    # chat-path reply, so entities here would show as literals and commands would
    # stop being copy-pasteable.
    return (
        "🤖 I could not think about that one — " + detail + ".\n"
        "The books are untouched and every command still works:\n"
        "/pending · /status · /approve <id> · /reject <id>\n"
        "replied by the keyword router, not the model"
    )


async def run_chat_turn(store: FinanceStore, prompt: str, *,
                        history: Sequence[dict[str, Any]] | None = None,
                        context: str = "",
                        router: Any = None,
                        tools: Iterable[dict[str, Any]] | None = None,
                        specs: Sequence[str] = (TOOL_GET_PENDING,),
                        max_steps: int = 4) -> str:
    """Drive one plain-English exchange, executing tool calls until the model answers.

    `specs` bounds what the model may ask for. The conversational path gets the read
    only: with approve/reject in hand and a garbled prefix, a cheap model guessing
    the wrong one of two lookalike pending ids would spend money with nobody
    reading it, and the buttons plus /approve exist for the decisive act. The
    writer tools stay registered and reachable for a caller that opts them in.

    The loop is bounded twice over, and the bounds are recomputed from the message
    list before every request rather than tracked in a counter:

    * `max_steps` bounds model round-trips, which stops a tool ping-pong.
    * `MAX_CHAT_CONTEXT_CHARS` bounds what those round-trips cost, since each tool
      result is replayed on the following step. A single tool result may take at
      most `TOOL_RESULT_BUDGET_FRACTION` of what is left, which guarantees progress
      toward the ceiling: without that per-call slice, one wide payload grows the
      list past the ceiling it exists to enforce.

    When the ceiling is reached, tools are withdrawn with an instruction to answer
    now. Withdrawing matters as much as the byte count: if the model could still
    call tools, that final request would be the one that busts the budget.

    `router` is a seam for tests and for a caller holding its own router, and must
    expose `get_completion` plus the router's exception classes (what `LLMRouter`
    provides). Passing the module instead, whose entry point is the differently named
    `get_llm_completion`, is answered with a stated failure rather than an
    AttributeError reaching the owner as "I understood that and then failed".
    """
    if router is not None:
        routing = router
    elif llm_routing is not None:
        routing = llm_routing.get_router()
    else:
        return _degraded_reply(RuntimeError("llm_router module unavailable"))
    if not hasattr(routing, "get_completion"):
        return _degraded_reply(RuntimeError(
            f"router seam got {type(routing).__name__}, which has no get_completion; "
            "pass an LLMRouter, not the module"))
    # The loop distinguishes "the provider failed" (degrade, honestly) from "our code
    # failed" (raise, loudly), and that split needs the router's exception types. A
    # double that omits them falls back to the module's, which are the same classes.
    router_error = getattr(routing, "LLMError", None)
    if router_error is None and llm_routing is not None:
        router_error = llm_routing.LLMError
    llm_errors: tuple[type[BaseException], ...] = (router_error or RuntimeError,)
    allowed = set(specs)
    tool_specs = [t for t in (tools or TOOL_SPECS)
                  if t.get("function", {}).get("name") in allowed]
    snapshot = context or _ledger_snapshot(store)
    # History shares one budget with the snapshot: context is the bigger, more
    # useful half, so it is taken first and history gets what is left.
    history_budget = max(200, TG_LLM_CONTEXT_CHARS - len(snapshot))
    # Writers are advertised only when they are actually in `tool_specs`, so the
    # model's description of itself matches the tools it was handed.
    can_write = any(t.get("function", {}).get("name") in (TOOL_APPROVE, TOOL_REJECT)
                    for t in tool_specs)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(can_write)}]
    messages.extend(_trim_history(list(history or []), history_budget))
    messages.append({"role": "user", "content": f"{snapshot}\n\nOwner: {prompt[:1500]}"})

    results: list[dict[str, Any]] = []
    stopped_for_budget = False
    for step in range(max(1, max_steps)):
        kwargs: dict[str, Any] = {"tier": "fast", "max_tokens": 600, "temperature": 0.3}
        if tool_specs:
            kwargs["tools"] = tool_specs
            kwargs["tool_choice"] = "auto"
        # Measured, not accumulated: the assistant/tool_call scaffolding has a size
        # too, and a counter that forgets it under-reads the very thing we cap.
        used_chars = sum(len(str(m.get("content") or "")) for m in messages)
        if MAX_CHAT_CONTEXT_CHARS - used_chars < CHAT_CONTEXT_RESERVE_CHARS:
            stopped_for_budget = True
            messages.append({"role": "user", "content":
                             "Context is full. Answer now with what you have, in one "
                             "short sentence, and do not call any more tools."})
            kwargs.pop("tools", None)
            kwargs.pop("tool_choice", None)
        try:
            completion = await routing.get_completion("", messages=messages, **kwargs)
        except (*llm_errors, httpx.HTTPError) as exc:
            if results:
                return _summarise_executed(results)
            return _degraded_reply(exc)

        if not completion.tool_calls:
            text = _sanitise_reply(completion.text or "")
            return text or _no_action_reply()

        messages.append({"role": "assistant", "content": completion.text or "",
                         "tool_calls": [
                             {"id": tc.id or f"call_{step}_{i}", "type": "function",
                              "function": {"name": tc.name,
                                           "arguments": tc.raw_arguments
                                           or json.dumps(tc.arguments)}}
                             for i, tc in enumerate(completion.tool_calls)]})
        for i, tc in enumerate(completion.tool_calls):
            # `specs` is enforced here, not only advertised: a model can name a tool it was
            # never handed, and `run_tool` would execute any registered one, writers included.
            if tc.name not in allowed:
                result = {"ok": False, "error": f"tool {tc.name!r} is not available in this "
                                                "conversation; nothing was changed"}
                log.warning("chat turn refused unoffered tool %s", tc.name)
            else:
                result = run_tool(store, tc.name, tc.arguments, parse_error=tc.parse_error)
            results.append({"name": tc.name, **result})
            slice_left = int((MAX_CHAT_CONTEXT_CHARS - used_chars)
                             * TOOL_RESULT_BUDGET_FRACTION)
            budget = max(TOOL_RESULT_MIN_CHARS, slice_left - len(results) * 40)
            payload = _bounded_tool_result(tc.name, result, budget)
            messages.append({"role": "tool",
                             "tool_call_id": tc.id or f"call_{step}_{i}",
                             "name": tc.name, "content": payload})

    # Spent the step budget (or the context ceiling) with no prose from the model.
    # Say what actually happened, and never imply the conversation finished neatly.
    if results:
        return _summarise_executed(results) + (
            "\n_context limit reached, so I stopped asking; nothing else changed._"
            if stopped_for_budget else "")
    return _no_action_reply()


def _summarise_executed(results: list[dict[str, Any]]) -> str:
    """Fallback reply for a turn the model never finished narrating.

    Used when the provider died mid-turn and when the loop ran out of room, and in
    both cases it must state only what this process knows for certain: a decision
    that landed, or the precise reason nothing did. Note the `ok and not error`
    test below. A read-only tool that simply returned rows is not a fact worth
    quoting back, since its payload is data rather than a failure, and dumping the
    first row of JSON as though it were an error is how a reply lies about money.
    """
    decided = [r for r in results if r.get("ok") and r.get("approval_status")]
    if decided:
        lines = [f"✅ {str(r.get('transaction_id', ''))[:8]} → "
                 f"{r.get('approval_status')!s}"
                 + (f" · {r['amount']}" if r.get("amount") else "")
                 + (" (already decided)" if r.get("duplicate") else "")
                 for r in decided]
        return "Recorded:\n" + "\n".join(lines)
    failures = [str(r.get("error"))[:120] for r in results
                if not r.get("ok") and r.get("error")]
    return ("🤖 I did not get a finished answer from that, and nothing else changed."
            + (f" What failed: {'; '.join(failures[:3])}" if failures else
               " No transaction was altered."))


def _no_action_reply() -> str:
    return ("🤖 I could not turn that into an action, and nothing was changed.\n"
            "Try /pending to see ids, or /status for the books.")


async def handle_telegram_plain_english(store: FinanceStore, text: str) -> str:
    """The LLM branch: history + ledger context in, reply HTML out."""
    history = await asyncio.to_thread(_recent_owner_messages, store,
                                      TG_LLM_HISTORY_MESSAGES, text)
    context = await asyncio.to_thread(_ledger_snapshot, store)
    return await run_chat_turn(store, text[:1500], history=history, context=context)


# ---------------------------------------------------------------------------
# Read-only chat over HTTP (for the AG-UI gateway)
# ---------------------------------------------------------------------------

#: The only tool the HTTP chat path may run. Approve/reject stay on Telegram buttons and
#: /approve, where a human taps the exact id.
CHAT_HTTP_SPECS: tuple[str, ...] = (TOOL_GET_PENDING,)
CHAT_HTTP_MAX_HISTORY = TG_LLM_HISTORY_MESSAGES
CHAT_HTTP_MAX_CHARS = 1500


def _chat_http_authorised(authorization: str | None) -> None:
    """Bearer `FINANCE_CHAT_TOKEN`, constant-time. Unset token -> 503: the route is closed, not
    open, until someone configures it. This app listens on 0.0.0.0 and has unauthenticated
    routes already; this one must not join them."""
    expected = os.getenv("FINANCE_CHAT_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "FINANCE_CHAT_TOKEN is not configured; chat over HTTP is off")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(),
                                                             expected.encode()):
        raise HTTPException(401, "missing or wrong bearer token",
                            headers={"WWW-Authenticate": "Bearer"})


def _chat_http_history(raw: Any) -> list[dict[str, str]]:
    """Client history, reduced to plain user/assistant text. Anything else is dropped: the
    caller is a browser, and a `system` or `tool` turn from it must not reach the model."""
    if not isinstance(raw, list):
        return []
    out = [{"role": item["role"], "content": item["content"][:CHAT_HTTP_MAX_CHARS]}
           for item in raw
           if isinstance(item, dict) and item.get("role") in ("user", "assistant")
           and isinstance(item.get("content"), str) and item["content"].strip()]
    return out[-CHAT_HTTP_MAX_HISTORY:]


@app.post("/chat/ask")
async def chat_ask(body: dict[str, Any] = Body(...),
                   authorization: str | None = Header(default=None),
                   store: FinanceStore = Depends(get_store)) -> dict[str, Any]:
    """One read-only plain-English turn: the model sees the ledger snapshot and may list pending
    transactions, nothing else. Never parses commands (no /approve here), never writes a row,
    never sends a Telegram message."""
    _chat_http_authorised(authorization)
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(422, "message must be a non-empty string")
    if not tg_llm_enabled():
        raise HTTPException(503, "no chat model is configured")
    history = _chat_http_history(body.get("history"))
    context = await asyncio.to_thread(_ledger_snapshot, store)
    reply = await run_chat_turn(store, message.strip()[:CHAT_HTTP_MAX_CHARS], history=history,
                                context=context, specs=CHAT_HTTP_SPECS)
    return {"ok": True, "reply": reply, "read_only": True}


class ChatReply(NamedTuple):
    """A routed reply plus the parse mode it must be sent with.

    One type instead of a bare string because the two reply families in this
    function are built under opposite rules: the command router interpolates into
    HTML markup and escapes every value, while model prose passes through
    unescaped so a suggested `/approve <id>` stays copy-pasteable. A single
    `parse_mode` has to be chosen at the send, after the fact, and inferring it
    from the text (does it contain a tag?) is how one family silently gets the
    other's treatment. The mode travels with the reply that decided it.
    """

    text: str
    parse_mode: str = "HTML"


async def route_telegram_text(store: FinanceStore, text: str) -> ChatReply:
    """The join between the two routers: commands, then the model, then a canned reply.

    Lives in the async phase (a sync function cannot await), which is why
    `handle_telegram_message` signals "unmatched" with an empty string instead of
    answering with help text. When the LLM path is off or unconfigured, unmatched
    text gets exactly the reply it got before this feature existed.
    """
    reply = await asyncio.to_thread(handle_telegram_message, store, text)
    if reply:
        return ChatReply(reply)
    if not await asyncio.to_thread(llm_available):
        return ChatReply(f"🤖 I don't know <code>{_escape(text[:60])}</code> yet.\n{TG_HELP}")
    try:
        # Plain parse mode for everything the model touched, including this module's
        # own fallback strings below: they are built for the same send.
        return ChatReply(await handle_telegram_plain_english(store, text), parse_mode="")
    except Exception as exc:
        log.exception("plain-english turn failed")
        return ChatReply(
            f"⚠️ I understood \"{_plain_quoted(text)}\" as free text and then failed: "
            f"{type(exc).__name__}: {str(exc)[:200]}\n"
            "Nothing was changed. /pending · /approve <id> · /reject <id>",
            parse_mode="")


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
        # message_id enables the edit-the-card decision path; its absence is
        # tolerated (reply fallback), unlike the fields above.
        msg_id = cb_msg.get("message_id") if isinstance(cb_msg, dict) else None
        message_id = msg_id if isinstance(msg_id, int) else None
        payload = ("callback", (str(callback["id"]), data, message_id))

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
        callback_id, data, message_id = payload[1]
        store.log("info", "TELEGRAM", f"inbound callback: {data[:100]}",
                  extra={"chat_id": str(chat_id), "callback_id": callback_id})
        background.add_task(handle_telegram_approval, store, token, chat_id, callback_id,
                            data, message_id)
    return {"status": "ok"}


async def notify_telegram_pending(
    store: FinanceStore, tx_id: str, s: FinanceSettings | None = None
) -> bool:
    """Outbound interactive approval card for a pending tier-2 intent.

    Fire-and-log, like the invoice-paid notify: a Telegram failure must not
    5xx /spend/gate (the DB queue is the durable record — supervisor's loop and
    /pending still cover any card we fail to deliver). message_id lands in
    agent_logs so a later decision edit could correlate the card.
    """
    token = tg_token()
    chat = tg_owner_chat_id()
    if not token or not chat:
        return False
    row = await asyncio.to_thread(_fetch_gate_row, store, tx_id)
    if not row:
        return False
    s = s or get_settings()
    text = build_approval_card(row)
    floor = s.approval_notify_floor_minor
    if int(row.get("amount_minor") or 0) > floor:
        text += (f"\n⚠️ over the {_escape(fmt_minor(floor, s.default_currency))} "
                 "interactive floor")
    try:
        r = await tg_send_message(token, chat, text,
                                  reply_markup=approval_keyboard_json(tx_id))
        if r.status_code >= 400:
            log.warning("approval card send failed (%s): %s", r.status_code, r.text[:200])
            return False
        msg_id = (r.json() or {}).get("result", {}).get("message_id")
        store.log("info", "approval.alert_sent", f"interactive card sent for {tx_id[:8]}",
                  transaction_id=tx_id, extra={"telegram_message_id": msg_id, "chat_id": chat})
        return True
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        log.warning("approval card unreachable (non-fatal): %s", exc)
        return False


def _fetch_gate_row(store: FinanceStore, tx_id: str) -> dict[str, Any]:
    # thin wrapper (no extra logic) so tests can monkeypatch either seam.
    return _fetch_gate_row_raw(store, tx_id)


def _fetch_gate_row_raw(store: FinanceStore, tx_id: str) -> dict[str, Any]:
    with store._connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select transaction_id::text as transaction_id, title, amount_minor, "
            "currency, tier, vendor, agent_handle, expires_at::text as expires_at "
            "from engine.transactions where transaction_id = %s", (tx_id,))
        row = cur.fetchone()
        return dict(row) if row else {}


async def _telegram_phase2_text(store: FinanceStore, token: str,
                                chat_id: int | str, text: str) -> None:
    try:
        reply = await route_telegram_text(store, text)
    except Exception as exc:
        reply = ChatReply(telegram_worker_error(exc))
    await telegram_reply(store, token, chat_id, reply.text, parse_mode=reply.parse_mode)


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
