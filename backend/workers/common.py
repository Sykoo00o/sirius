"""Shared plumbing for the schema.sql workers (funnel, arbitrage, agency).

Why this exists
---------------
All three workers need the same four things: a DB connection, a structured log
line, a daily-metric upsert, and a spend request that obeys the tier gate. The
spend request in particular must not be re-implemented per worker. A worker that
invents its own `insert into engine.transactions` can write
`approval_status='auto_approved'` on a $400 purchase and nobody would notice
until the reconciliation. So the only way a worker spends is by calling the
finance agent, which owns the tier policy, the card envelope, and the gate
context that a human reads before tapping Approve.

Two boundaries this module keeps
--------------------------------
1. Schema. These workers target supabase/schema.sql (agent_logs,
   financial_ledger, transactions, hustle_metrics) and talk to
   backend/finance_agent.py. They deliberately do NOT import engine/*, which is
   a parallel stack built on migrations/0001_engine_core.sql with its own
   `agent_logs`, `approval_requests` and `ledger_entries` tables and its own RPCs
   (`report_heartbeat`, `request_approval`). Importing across would mean one
   process writing two schemas at once. See engine/workers/ for that stack.
2. Blocking. psycopg3 and the outbound httpx call are synchronous, and the tick
   loop is an asyncio loop so SIGTERM lands mid-sleep. Every blocking helper here
   is therefore paired with an `a`-prefixed wrapper that runs it in a thread.
   engine/worker_base.py measures one Postgres round trip at ~127 ms to this
   region; on the loop that is 127 ms where the signal handler cannot run.

handle vs hustle
----------------
`handle` identifies the process ('worker:funnel') and is what agent_logs and
transactions.actor columns record. `hustle` is the enum tag ('funnel') on ledger
and metrics rows. They are separate fields because the same hustle could be
worked by more than one process (a scraper and a bidder, say) and their spend
must stay attributable to whoever caused it. The card envelope is per-handle for
the same reason: one worker blowing its daily budget must not silence its sibling.
"""

from __future__ import annotations

import abc
import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

try:  # optional at import time so `--help` and unit tests work without the driver
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised only in a stripped image
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

log = logging.getLogger("workers")

#: Consecutive failing ticks before the process reports 'error' instead of
#: 'running'. A single failure is a bad page, not an outage; three is a pattern
#: and the difference decides whether a human gets paged.
ERRORS_BEFORE_UNWELL = 3

#: A spend ticket that outlives this has lost its premise. A ticket price, an ad
#: slot and a lead are all perishable: approving a 9-hour-old "buy this for $80"
#: may spend money against a deal that no longer exists.
DEFAULT_SPEND_FRESHNESS_HOURS = 6

#: Longest single tick allowed before it is declared hung. Generous on purpose:
#: a browser page load or a scraped vendor price page can be slow without being
#: broken, and a false hang alarm trains people to ignore alarms.
TICK_TIMEOUT_SECONDS = 300



# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def load_dotenv() -> None:
    """Populate os.environ from the repo-root .env without clobbering real env.

    Search order is cwd then this file's parent, because a worker runs as
    `python -m workers.funnel_worker` from backend/ in the image, and from the
    repo root on a laptop. Container deployments inject real environment
    variables, and `setdefault` means those always win over a stale .env.
    """
    here = Path(__file__).resolve()
    candidates = [Path.cwd() / ".env", *(parent / ".env" for parent in here.parents)]
    candidates += [parent / ".env" for parent in here.parents[2:]]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                if key:
                    os.environ.setdefault(key, value)
        except OSError:
            continue
        return


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int_env(name: str, default: int) -> int:
    raw = env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    raw = env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def build_database_url() -> str:
    """Prefer DATABASE_URL; fall back to the split SUPABASE_DB_* parts.

    Connection-string-first keeps secrets out of the image. The split form
    exists because Supabase's pooled connection string is not always handed out
    as one string, and password characters must be percent-encoded or `#` in a
    password truncates the host.
    """
    url = env("DATABASE_URL") or env("SUPABASE_DB_URL")
    if url:
        return url
    password = env("SUPABASE_DB_PASSWORD")
    if not password:
        return ""
    host = env("SUPABASE_DB_HOST", "db")
    port = env("SUPABASE_DB_PORT", "5432")
    dbname = env("SUPABASE_DB_NAME", "postgres")
    user = env("SUPABASE_DB_USER", "postgres")
    return (f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{dbname}?sslmode=require")


#: Schema the workers read and write. `engine` matches schema.sql. Overridable
#: for a staging database that installs under another name.
#:
#: This one env var feeds BOTH the connection search_path and the qualified names
#: below, which is why it is validated instead of trusted. A schema name reaching
#: a search_path parameter is session configuration; the same string interpolated
#: into `insert into {TABLE}` is SQL text, so anything outside a plain identifier
#: would be an injection path. Rejecting loudly at import is better than silently
#: quoting something a DBA cannot recognise in a GRANT.
_SCHEMA_RAW = env("WORKER_SCHEMA", "engine")
# `or "engine"` first: an env reader returns its default for "unset" AND for "set
# but blank", and a blank would build the invalid search_path `,public`, which
# PostgreSQL refuses at connect time.
_SCHEMA_OK = re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", _SCHEMA_RAW or "engine")
SCHEMA = _SCHEMA_RAW if _SCHEMA_OK else "engine"
if SCHEMA != _SCHEMA_RAW:
    # Log once at import; the fallback must be visible or a typo'd schema name
    # looks like "my tables vanished" rather than "your env var was rejected".
    os.environ["WORKER_SCHEMA_REJECTED"] = _SCHEMA_RAW

#: Connection search_path: our schema first so the schema's own bare `::enum`
#: casts resolve, then public, so a deployment that installed something else
#: there still sees it.
SEARCH_PATH = f"{SCHEMA},public"
STATE_TABLE = f"{SCHEMA}.worker_state"
AGENT_LOGS_TABLE = f"{SCHEMA}.agent_logs"


def fmt_minor(amount: int, currency: str = "USD") -> str:
    """Render minor units as money. Mirrors finance_agent.fmt_minor exactly."""
    sign = "-" if amount < 0 else ""
    units, cents = divmod(abs(int(amount)), 100)
    return f"{sign}{currency} {units:,}.{cents:02d}"


def env_int_to_minor(raw: float) -> int:
    """Turn an env-supplied whole-currency amount into minor units.

    Rounding here instead of at each call site keeps `WORKER_TIER1_MAX=30`
    meaning exactly 3000 minor units in all three workers. Truncation would make
    a float-parse artifact like 29.999999 read as 29.

    `round()` returns an int for one argument and the input type for two, which
    is why the outer cast is not redundant: without it the declared `-> int`
    would be a lie for a Decimal-valued env read.
    """
    return int(round(raw * 100))  # noqa: RUF046 - see docstring


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lead:
    """One unit of inbound work: a visitor trigger, a scraped target, a client.

    Deliberately a flat record with a `payload` bag rather than one class per
    source. Source-specific fields live in `payload` so adding a vendor does not
    require a new table, while the four columns that drive decisions (source,
    identity, freshness, expected value) stay typed and impossible to typo.
    """

    source: str
    identity: str
    created_at: datetime
    expected_value_minor: int = 0
    currency: str = "USD"
    payload: dict[str, Any] | None = None

    def age_seconds(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        return max(0, int((moment - self.created_at).total_seconds()))

    @property
    def note(self) -> dict[str, Any]:
        return self.payload or {}

    def identity_hash(self) -> str:
        """Stable short hash of the identity, for idempotency keys.

        Identities are URLs, email addresses and order refs. All of them are
        PII-shaped or awkwardly long, and `idempotency_key` is a unique index:
        hashing keeps the index narrow and keeps a customer email out of a
        column built for machine keys.
        """
        import hashlib

        return hashlib.sha256(f"{self.source}:{self.identity}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SpendTicket:
    """A spend request, before the gate sees it."""

    title: str
    amount_minor: int
    currency: str = "USD"
    rationale: str = ""
    expected_roi_note: str | None = None
    source_url: str | None = None
    vendor: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] | None = None

    def to_gate_request(self, handle: str, hustle: str) -> dict[str, Any]:
        """Body for POST /spend/gate (see finance_agent.route_spend)."""
        return {
            "agent_handle": handle, "title": self.title,
            "amount_minor": int(self.amount_minor), "currency": self.currency.upper(),
            "hustle": hustle, "vendor": self.vendor, "rationale": self.rationale,
            "expected_roi_note": self.expected_roi_note, "source_url": self.source_url,
            "idempotency_key": self.idempotency_key, "metadata": self.metadata or {},
        }


@dataclass(frozen=True)
class SpendOutcome:
    """What the gate decided. `execute` is the only field a worker must branch on."""

    transaction_id: str | None
    tier: str
    approval_status: str
    execute: bool
    reason: str
    duplicate: bool

    @classmethod
    def from_gate_response(cls, body: dict[str, Any]) -> SpendOutcome:
        return cls(
            transaction_id=body.get("transaction_id"),
            tier=str(body.get("tier") or "?"),
            approval_status=str(body.get("approval_status") or "unknown"),
            execute=bool(body.get("execute")),
            reason=str(body.get("reason") or ""),
            duplicate=bool(body.get("duplicate")),
        )

    @property
    def blocked(self) -> bool:
        return self.approval_status == "blocked"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class WorkerStore:
    """Direct psycopg access for the three things a worker writes itself.

    Deliberately narrow. Reads of pending approvals belong to the supervisor and
    money-posting belongs to the finance agent, so neither appears here: a worker
    that can write the ledger is a worker that will double-book revenue.
    """

    def __init__(self, database_url: str, statement_timeout_ms: int = 15000) -> None:
        if psycopg is None:  # pragma: no cover
            raise RuntimeError("psycopg is not installed; workers cannot reach the database")
        self.database_url = database_url
        self.statement_timeout_ms = statement_timeout_ms

    def _options(self) -> str:
        """Connection-time session settings, shared by the autocommit and the
        transactional connection paths. `options` is the psycopg3 spelling for
        both statement_timeout and search_path: neither can be set after connect.

        `search_path=engine,public` is load-bearing, not tidiness. schema.sql
        installs its types into `engine` and leaves bare `::approval_tier` casts
        in SQL that relies on `engine` being on the path. Supabase's own roles get
        `engine` onto the path by grant, so under the postgres/superuser role the
        default is just `"$user", public` and the very first cast fails outright:
        "type \\"approval_tier\\" does not exist" (reproduced on PostgreSQL 16
        against schema.sql). Setting it per connection covers every statement,
        whichever role or pooler opened it.
        """
        return (f"-c statement_timeout={self.statement_timeout_ms} "
                f"-c search_path={SEARCH_PATH}")

    def _connect(self) -> Any:
        # autocommit: every statement on this path is one complete fact (a log
        # line, an upsert). A transaction spanning Python work would hold locks
        # across a scrape. The one place that needs a real transaction is claim(),
        # which opens its own connection because the lock must outlive the check.
        return psycopg.connect(
            self.database_url, row_factory=dict_row, autocommit=True,
            options=self._options(),
        )

    # ------------------------------------------------------------------ logs

    def log(self, handle: str, level: str, event: str, message: str, *,
            hustle: str | None = None, is_audit: bool = False,
            extra: dict[str, Any] | None = None) -> None:
        """Write one agent_logs row. Never raises.

        agent_logs has no agent_handle foreign key on purpose, so a log line
        lands even when everything else is broken. But a log write must also
        never abort the money path: a worker that dies because its own logging
        hit a statement timeout has destroyed a real, committed effect.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "insert into agent_logs "  # unqualified on purpose: see _connect
                    "(agent_handle, hustle, level, event, message, context, is_audit) "
                    "values (%s, %s, %s::log_level, %s, %s, %s::jsonb, %s)",
                    (handle, hustle, level, event, message[:2000],
                     json.dumps(extra or {}, default=str), is_audit),
                )
        except Exception as exc:  # a log failure must not abort the money path
            log.warning("agent_logs write failed (%s): %s", event, exc)

    # --------------------------------------------------------------- metrics

    def add_metrics(self, handle: str, hustle: str, deltas: dict[str, int],
                    *, day: date | None = None) -> None:
        """Add a set of non-money counters to today's (day, hustle) rollup row.

        Two rules, both learned from the table definition:

        * Only columns whose semantics are "number of things" are added. The
          *_ratio_pct and *_margin_pct columns are averages-with-a-denominator
          and cannot be summed, and mrr_minor / active_subscriptions are
          end-of-day STOCKS while new_subscriptions / cancellations are FLOWS:
          adding a stock to itself once per tick yields 24x the MRR by midnight.
          Those are written by set_gauges, and a later flow tick preserves them.
        * net_profit_minor is `generated always as (...) stored`, so it is
          absent from the INSERT and from the DO UPDATE list. Naming it raises.
        """
        additive = {
            "revenue_minor", "cost_minor", "gross_volume_minor", "mrr_minor",
            "active_subscriptions", "new_subscriptions", "cancellations",
            "dispute_count", "chargeback_count", "targets_discovered",
            "targets_executed", "clients_onboarded", "support_tickets",
        }
        bad = set(deltas) - additive
        if bad:
            raise ValueError(
                f"not summable, use set_gauges or an explicit ratio write: {sorted(bad)}")
        cols = ["day", "hustle", "computed_at"]
        vals = ["%s", "%s", "now()"]
        updates = ["computed_at = now()"]
        params: list[Any] = [day or datetime.now(UTC).date(), hustle]
        for key, value in deltas.items():
            cols.append(key)
            vals.append("%s")
            # Qualified on BOTH sides. `key = key + excluded.key` fails with
            # AmbiguousColumn, not with a wrong number: the bare name on the right
            # resolves against the target row's namespace as well, and this table
            # has a `day` column, so the right-hand side genuinely matches two
            # relations. First tick of a new day takes the INSERT branch, so this
            # only breaks from the second tick onward, which is a nasty time to
            # discover it.
            updates.append(f"{key} = engine.hustle_metrics.{key} + excluded.{key}")
            params.append(int(value))
        sql = (
            f"insert into engine.hustle_metrics ({', '.join(cols)}) "  # noqa: S608
            f"values ({', '.join(vals)}) "
            f"on conflict (day, hustle) do update set {', '.join(updates)}"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (*params,))
        log.debug("%s metrics %s -> %s %s", handle, deltas, hustle, day)

    def set_gauges(self, handle: str, hustle: str, gauges: dict[str, int | float],
                   *, day: date | None = None) -> None:
        """Overwrite end-of-day stock columns with the current true reading.

        Called once per tick after a vendor read, so repeated ticks converge
        instead of accumulating. Flows are untouched, which is what lets a flow
        tick and a gauge tick run in either order and land on the same row.
        """
        if not gauges:
            return
        allowed = {
            "revenue_minor", "cost_minor", "gross_volume_minor", "mrr_minor",
            "active_subscriptions", "new_subscriptions", "cancellations",
            "dispute_count", "chargeback_count", "targets_discovered",
            "targets_executed", "clients_onboarded", "support_tickets",
            "dispute_ratio_pct", "realized_margin_pct", "expected_margin_pct",
            "llm_cost_usd", "roi_pct", "currency",
        }
        bad = set(gauges) - allowed
        if bad:
            raise ValueError(f"unknown or generated hustle_metrics column: {sorted(bad)}")
        cols = ["day", "hustle", "computed_at", *list(gauges)]
        vals = ["%s", "%s", "now()"] + ["%s"] * len(gauges)
        updates = [f"{key} = excluded.{key}" for key in gauges] + ["computed_at = now()"]
        sql = (
            f"insert into engine.hustle_metrics ({', '.join(cols)}) "  # noqa: S608
            f"values ({', '.join(vals)}) "
            f"on conflict (day, hustle) do update set {', '.join(updates)}"
        )
        params = [day or datetime.now(UTC).date(), hustle, *gauges.values()]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
        log.debug("%s gauges %s -> %s", handle, gauges, hustle)

    # ------------------------------------------------------- worker bookkeeping

    def _ensure_state(self, conn: Any) -> None:
        """Create the tiny worker_state table on first use. Idempotent.

        Why a table at all, rather than an advisory lock: `pg_try_advisory_lock`
        dies with the connection, so a replica sitting between connections holds
        nothing, and it cannot express "this job's window is claimed until 03:00"
        across a reconnect, which is what a once-a-night fetch needs. So: one row
        per (handle, job), a token, and an expiry.

        Creation happens here, in the worker, because schema.sql is a migration
        file the user owns and this table is operational scaffolding, not a
        business record. `engine` is schema-qualified explicitly because CREATE
        TABLE resolves through search_path, and the point of letting the path be
        overridden is moot if the table lands in whichever schema is first.
        """
        with conn.cursor() as cur:
            cur.execute(f"""
                create table if not exists {STATE_TABLE} (
                  handle        text        not null,
                  job           text        not null,
                  lease_token   uuid        not null default gen_random_uuid(),
                  leases_taken  integer     not null default 0,
                  acquired_at   timestamptz not null default now(),
                  expires_at    timestamptz not null default now(),
                  note          jsonb       not null default '{{}}'::jsonb,
                  primary key (handle, job)
                )""")

    def claim(self, handle: str, job: str, lease_seconds: int) -> bool:
        """Take an exclusive, expiring lease for one kind of work. True if ours.

        The mutual-exclusion primitive for a worker scaled past one replica: a
        second replica that arrives while the lease is held takes nothing and
        reports a zero tick instead of scraping the same 400 pages twice.

        Two statements inside ONE transaction, because the lock has to be held
        across the check. An earlier version tried to do it in a single statement,
        which looks obviously correct and is not: a CTE cannot see a row that a
        sibling CTE of the same statement inserted, so on a first-ever claim the
        UPDATE branch finds no row, the INSERT branch reports success, and the
        caller is told it holds a lease it never stamped. Measured, not theorised.

        A first version got this wrong the other way, probing in one autocommit
        transaction and grabbing in a second: two processes both saw "no live
        lease" and both took it, so the check was decorative. That is the failure
        mode a lease exists to prevent, so the transaction boundary is the point.

        The volatile `now() >= w.expires_at` above is safe here and worth stating
        why, because the same shape is NOT safe where supervisor.py's
        read_pending() avoided it. Measured on PostgreSQL 16 against a
        two-row table (one live, one expired) under
        `FOR UPDATE SKIP LOCKED`:
          * with `where expires_at > now()`  -> only the LIVE row returned
          * with no such predicate           -> BOTH rows returned
        That is not a locking bug: a row the predicate excludes is excluded from
        any query, locking or not. The trap is specifically that `skip locked`
        makes "excluded" and "someone else holds it" return the same empty result,
        so a predicate meant to identify work you want ends up hiding it forever.
        Here the predicate asks for EXACTLY the rows this statement consumes
        (expired, hence grabbable), so excluding the live lease is the correct
        answer rather than a hidden one. read_pending() had it inverted: it wanted
        due rows while filtering on not-due, which is why the clock moved there
        and stayed here. Neither is a `now()` problem, so neither needs a floor()
        workaround; an earlier draft of this comment claimed otherwise and was
        wrong.
        """
        grab = f"""
            update {STATE_TABLE} w
               set lease_token = gen_random_uuid(),
                   leases_taken = w.leases_taken + 1,
                   acquired_at = now(),
                   expires_at = now() + make_interval(secs => %s),
                   note = %s::jsonb
              where w.handle = %s and w.job = %s
                and now() >= w.expires_at
              -- Skip, never queue: a replica that finds the row locked is not
              -- late, it is redundant, and making it wait buys nothing.
              for update skip locked
            returning w.lease_token
        """  # noqa: S608 - table name is a module constant, all values are params
        note = json.dumps({"pid": os.getpid()})
        try:
            # NOT self._connect(), which is autocommit: see the docstring.
            with psycopg.connect(self.database_url, autocommit=False,
                                 options=self._options()) as conn:
                try:
                    self._ensure_state(conn)
                    with conn.cursor() as cur:
                        # First-ever claim: whoever wins the INSERT owns it outright,
                        # so there is nothing to contest and no UPDATE needed.
                        cur.execute(
                            f"insert into {STATE_TABLE} (handle, job, expires_at, note) "  # noqa: S608
                            "values (%s, %s, now() + make_interval(secs => %s), %s::jsonb) "
                            "on conflict (handle, job) do nothing returning lease_token",
                            (handle, job, lease_seconds, note))
                        if cur.fetchone() is not None:
                            return True
                        cur.execute(grab, (lease_seconds, note, handle, job))
                        won = cur.fetchone() is not None
                        if not won:
                            # Record the miss, so "why did replica B do nothing?"
                            # has an answer in the same stream as the work.
                            cur.execute(
                                f"insert into {AGENT_LOGS_TABLE} "  # noqa: S608
                                "(agent_handle, level, event, message, context) "
                                "values (%s, 'debug', 'worker.lease.contended', %s, %s::jsonb)",
                                (handle, f"lease busy for {job}", json.dumps({"job": job})))
                        return won
                except Exception:
                    conn.rollback()
                    raise
                conn.commit()
        except Exception as exc:  # a lease we cannot read is no lease: do not act
            log.warning("claim(%s) failed, treating as not-ours: %s", job, exc)
            return False

    def cursor_get(self, handle: str, job: str, default: str | None = None) -> str | None:
        """Read this worker's persisted position for a job (page token, last id)."""
        sql = ("select note->>'cursor' as cursor from engine.worker_state "
               "where handle = %s and job = %s")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (handle, job))
                row = cur.fetchone()
        except Exception as exc:  # no readable position: fall back to the beginning
            log.warning("cursor_get(%s) failed: %s", job, exc)
            return default
        return (row or {}).get("cursor") or default

    def cursor_put(self, handle: str, job: str, cursor: str | None) -> None:
        """Persist position. Survives restart so a 400-page scrape resumes."""
        sql = ("update engine.worker_state set note = "
               "jsonb_set(note, '{cursor}', to_jsonb(%s::text)) "
               "where handle = %s and job = %s")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (cursor, handle, job))
        except Exception as exc:  # position is resumable-at-worst, not authoritative
            log.warning("cursor_put(%s) failed: %s", job, exc)

    def seen(self, handle: str, identity_hash: str) -> bool:
        """Has this identity already been processed? Identity-grained, global.

        Checked before acting, never after: on a source that re-publishes the
        same page forever (every funnel visit, every ticket listing) the
        alternative is paying for the same thing twice.
        """
        sql = ("select 1 as hit from engine.agent_logs "
               "where agent_handle = %s and event = 'worker.item.seen' "
               "and context->>'item' = %s limit 1")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (handle, identity_hash))
                return cur.fetchone() is not None
        except Exception as exc:
            # Fail closed on a read we cannot answer: re-processing costs money,
            # skipping one item costs one item.
            # Fail closed: re-processing costs money, skipping costs one item.
            log.warning("seen() failed, treating as seen: %s", exc)
            return True

    def mark_seen(self, handle: str, hustle: str, identity_hash: str,
                  identity: str) -> None:
        sql = ("insert into engine.agent_logs "
               "(agent_handle, hustle, level, event, message, context) "
               "values (%s, %s, 'debug', 'worker.item.seen', %s, %s::jsonb)")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (handle, hustle, f"processed {identity[:120]}",
                                  json.dumps({"item": identity_hash,
                                              "source_identity": identity[:200]})))
        except Exception as exc:  # worst case this item is retried next tick
            log.warning("mark_seen() failed for %s: %s", identity[:80], exc)


# ---------------------------------------------------------------------------
# Spend, via the finance agent
# ---------------------------------------------------------------------------


class SpendGateway:
    """Asks backend/finance_agent.py to authorise a purchase.

    HTTP rather than an in-process import of route_spend, so that what a worker
    does and what the gate does cannot drift into two implementations of the tier
    rule. The agent is the only writer of tier decisions; if a worker could call
    classify_tier itself, it could also pass its own verdict.
    """

    def __init__(self, base_url: str, token: str = "", timeout_seconds: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = httpx.Timeout(timeout_seconds, connect=5.0)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request_spend(self, ticket: SpendTicket, handle: str, hustle: str) -> SpendOutcome:
        url = f"{self.base_url}/spend/gate"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=ticket.to_gate_request(handle, hustle),
                                   headers=self._headers())
        # 5xx and 429 are worth one retry: the gate is stateless per request and
        # the idempotency key makes a double submit harmless. 4xx is a rejected
        # shape, and repeating it just wastes the tick.
        if response.status_code in (429, 502, 503, 504):
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=ticket.to_gate_request(handle, hustle),
                                       headers=self._headers())
        if response.status_code >= 400:
            raise SpendGatewayError(
                f"{url} -> http {response.status_code}: {response.text[:200]}")
        return SpendOutcome.from_gate_response(response.json())

    def envelope(self, handle: str) -> dict[str, Any]:
        url = f"{self.base_url}/spend/envelope"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params={"agent_handle": handle},
                                  headers=self._headers())
        if response.status_code >= 400:
            raise SpendGatewayError(f"{url} -> http {response.status_code}")
        return response.json()


class SpendGatewayError(RuntimeError):
    """The gate could not be reached, or refused the request outright."""


# ---------------------------------------------------------------------------
# Base worker
# ---------------------------------------------------------------------------


class BaseWorker(abc.ABC):
    """Tick loop for one hustle worker.

    Contract for subclasses: set HANDLE/HUSTLE, implement collect(). Everything
    else (freshness, dedupe, gating, metrics, logging, signals) is inherited, so
    a new hustle is one file and cannot forget to ask before spending.
    """

    #: Must match how the agent is named in dashboards; keep the 'worker:' prefix.
    HANDLE: str = "worker"
    #: engine.hustle_kind: funnel | arbitrage | whitelabel
    HUSTLE: str = "funnel"

    def __init__(self, database_url: str, gateway: SpendGateway,
                 *, tick_seconds: int, batch_size: int,
                 freshness_hours: float = DEFAULT_SPEND_FRESHNESS_HOURS) -> None:
        self.store = WorkerStore(database_url)
        self.gateway = gateway
        self.tick_seconds = tick_seconds
        self.batch_size = batch_size
        self.max_age_seconds = int(freshness_hours * 3600)
        self._errors = 0
        self._seq = 0
        self._stopping = asyncio.Event()

    # -------------------------------------------------------------- subclasses

    @abc.abstractmethod
    async def collect(self) -> list[Lead]:
        """Pull this tick's inbound work. Raise on failure; return [] when idle."""

    # B027 is deliberate on both hooks: an empty non-abstract async method is the
    # point. Most workers have nothing to warm up, and marking these abstract
    # would force all three to write `return None` twice to say "nothing".
    async def on_start(self) -> None:
        """Optional warm-up: a browser context, a vendor token exchange."""

    async def on_stop(self) -> None:
        """Optional teardown. Runs even after a crash."""

    def handle_action(self, lead: Lead) -> dict[str, Any] | None:
        """Act on one lead. Default: no side effects, log only.

        Override to place a bid, deploy a page or provision a client. Returning
        None means "nothing bought"; returning a dict of metric deltas (plus an
        optional '_spend' SpendTicket) is how a worker reports what happened.
        """
        self.log("info", "worker.lead.noted",
                 f"{lead.source}:{lead.identity[:80]} aged {lead.age_seconds()}s")
        return None

    # ------------------------------------------------------------------ helpers

    def log(self, level: str, event: str, message: str, *, is_audit: bool = False,
            **extra: Any) -> None:
        self.store.log(self.HANDLE, level, event, message, hustle=self.HUSTLE,
                       is_audit=is_audit, extra=extra or None)

    def is_fresh(self, lead: Lead, now: datetime | None = None) -> bool:
        return lead.age_seconds(now) <= self.max_age_seconds

    def is_new(self, lead: Lead) -> bool:
        """Not yet processed by any replica. Marks it seen as it answers True."""
        if self.store.seen(self.HANDLE, lead.identity_hash()):
            return False
        self.store.mark_seen(self.HANDLE, self.HUSTLE, lead.identity_hash(), lead.identity)
        return True

    def request_spend(self, ticket: SpendTicket) -> SpendOutcome:
        """Ask the gate. Raises SpendGatewayError if the gate is unreachable.

        Raising is correct here. If we cannot ask, we do not know whether a $200
        buy needs a human tap, and "assume yes" is how an ungated card gets built.
        The tick fails, the error is counted, and the supervisor sees three in a
        row.
        """
        if not self.gateway.configured:
            raise SpendGatewayError(
                "FINANCE_AGENT_URL is unset; refusing to spend ungated. "
                "Set FINANCE_AGENT_URL=http://finance-gatekeeper:3200")
        outcome = self.gateway.request_spend(ticket, self.HANDLE, self.HUSTLE)
        level = "warning" if outcome.blocked else "info"
        self.log(level, "worker.spend.gated",
                 f"{ticket.title} {fmt_minor(ticket.amount_minor, ticket.currency)} "
                 f"-> {outcome.tier}/{outcome.approval_status} ({outcome.reason})",
                 is_audit=True, transaction_id=outcome.transaction_id,
                 amount_minor=ticket.amount_minor, tier=outcome.tier,
                 approval_status=outcome.approval_status, duplicate=outcome.duplicate)
        return outcome

    # ------------------------------------------------------------------- tick

    async def tick_once(self) -> dict[str, Any]:
        """One pass. Returns metrics; raises on unexpected failure."""
        started = time.monotonic()
        metrics: dict[str, int] = {}
        counts = {"collected": 0, "skipped_stale": 0, "skipped_seen": 0,
                  "acted": 0, "blocked": 0, "queued": 0}
        leads = await asyncio.wait_for(self.collect(), timeout=TICK_TIMEOUT_SECONDS)
        counts["collected"] = len(leads)
        now = datetime.now(UTC)

        for lead in leads[: self.batch_size]:
            if not self.is_fresh(lead, now):
                counts["skipped_stale"] += 1
                self.log("debug", "worker.lead.stale",
                         f"{lead.source}:{lead.identity[:60]} older than "
                         f"{self.max_age_seconds}s, not acted on")
                continue
            if not self.is_new(lead):
                counts["skipped_seen"] += 1
                continue
            try:
                result = await asyncio.to_thread(self.handle_action, lead)
            except Exception as exc:
                self.log("error", "worker.lead.failed",
                         f"{lead.source}:{lead.identity[:60]}: {type(exc).__name__}: {exc}")
                continue
            counts["acted"] += 1
            if result:
                ticket = result.pop("_spend", None)
                if ticket is not None:
                    outcome = self.request_spend(ticket)
                    if outcome.blocked:
                        counts["blocked"] += 1
                    elif not outcome.execute:
                        counts["queued"] += 1
                for key, value in result.items():
                    metrics[key] = metrics.get(key, 0) + int(value)

        if metrics:
            await asyncio.to_thread(self.store.add_metrics, self.HANDLE, self.HUSTLE, metrics)

        metrics["duration_ms"] = int((time.monotonic() - started) * 1000)
        for key, value in counts.items():
            log.info("%s tick %s=%s", self.HANDLE, key, value)
        self.log("info", "worker.tick",
                 f"collected={counts['collected']} acted={counts['acted']} "
                 f"queued={counts['queued']} blocked={counts['blocked']}",
                 **counts)
        if counts["skipped_stale"]:
            # Stale input is a fact about the world, not an error: a scraper that
            # outruns the gate should keep scraping and say so.
            self.log("warning", "worker.lead.backlog",
                     f"{counts['skipped_stale']} lead(s) too old to act on; "
                     f"freshness window {self.max_age_seconds}s",
                     skipped_stale=counts["skipped_stale"])
        return metrics

    # -------------------------------------------------------------------- run

    async def run_forever(self) -> None:
        log.info("worker %s starting | hustle=%s tick=%ss batch=%s freshness=%ss gate=%s",
                 self.HANDLE, self.HUSTLE, self.tick_seconds, self.batch_size,
                 self.max_age_seconds, self.gateway.base_url or "UNSET")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Not contextlib.suppress: the exception names WHY it is ignorable,
            # which here is a real portability fact rather than "we do not care".
            # Windows has no event-loop signal handling; Ctrl-C still arrives as
            # KeyboardInterrupt in run(), so only the graceful SIGTERM path is
            # missing there, not the shutdown itself.
            with contextlib.suppress(NotImplementedError):  # pragma: no cover - Windows
                loop.add_signal_handler(sig, self._stopping.set)

        if not self.gateway.configured:
            # Loud at boot, not at the first purchase, which may be hours away.
            log.warning("FINANCE_AGENT_URL unset: this worker can collect but cannot spend")
            self.log("warning", "worker.gate.unconfigured",
                     "FINANCE_AGENT_URL unset; spend will be refused until it is set")

        await self.on_start()
        try:
            while not self._stopping.is_set():
                self._seq += 1
                try:
                    await self.tick_once()
                    self._errors = 0
                except Exception as exc:
                    self._errors += 1
                    level = "error" if self._errors >= ERRORS_BEFORE_UNWELL else "warning"
                    log.log(logging.WARNING if level == "warning" else logging.ERROR,
                            "tick failed (%d in a row): %s", self._errors, exc)
                    # A failing tick still reports. A worker that goes silent is
                    # indistinguishable from one that is merely idle, and only one
                    # of those two needs someone awake.
                    self.log(level, "worker.tick.failed",
                             f"{type(exc).__name__}: {exc}",
                             consecutive_errors=self._errors, seq=self._seq)
                try:
                    await asyncio.wait_for(self._stopping.wait(),
                                           timeout=max(5, self.tick_seconds))
                except TimeoutError:
                    continue
        finally:
            try:
                await self.on_stop()
            except Exception as exc:
                log.warning("on_stop failed: %s", exc)

    def run(self) -> None:
        # Ctrl-C is a supported way to stop a worker, not an error: run_forever's
        # finally block has already run on_stop() and closed the connection.
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(self.run_forever())


# ---------------------------------------------------------------------------
# Entry-point factory
# ---------------------------------------------------------------------------


def worker_settings(module_doc: str) -> tuple[type[BaseWorker], dict[str, Any]]:
    """Build the worker class and its env-derived config from a shared shape.

    Env names are per-worker on purpose: the funnel tick rate and the scrape tick
    rate want different values, and a single WORKER_TICK_SECONDS would force a
    deploy-wide compromise. Names follow WORKER_<HANDLE>_KEY, which is why this is
    generated rather than written out three times.
    """
    raise NotImplementedError(module_doc)


def make_parser(handle: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"workers.{handle}",
        description=description,
        epilog="All configuration comes from the environment (.env is read for you).",
    )
    parser.add_argument("--once", action="store_true",
                        help="run a single tick and exit (cron / smoke test)")
    parser.add_argument("--tick-seconds", type=int, default=None,
                        help="override WORKER_TICK_SECONDS")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="override WORKER_BATCH_SIZE (leads per tick)")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect and report, but take no action and spend nothing")
    return parser


def tick_seconds_for(worker_class: type[BaseWorker], args: argparse.Namespace) -> int:
    prefix = worker_class.HANDLE.upper().replace(":", "_").replace("-", "_")
    default = _int_env(f"WORKER_{prefix}_TICK_SECONDS", 0) or _int_env("WORKER_TICK_SECONDS", 60)
    return args.tick_seconds or default


def batch_size_for(worker_class: type[BaseWorker], args: argparse.Namespace) -> int:
    prefix = worker_class.HANDLE.upper().replace(":", "_").replace("-", "_")
    default = (_int_env(f"WORKER_{prefix}_BATCH_SIZE", 0)
               or _int_env("WORKER_BATCH_SIZE", 25))
    return args.batch_size or default


def freshness_hours_for(worker_class: type[BaseWorker]) -> float:
    prefix = worker_class.HANDLE.upper().replace(":", "_").replace("-", "_")
    return (_float_env(f"WORKER_{prefix}_FRESHNESS_HOURS", 0.0)
            or _float_env("WORKER_FRESHNESS_HOURS", DEFAULT_SPEND_FRESHNESS_HOURS))


def build_gateway() -> SpendGateway:
    return SpendGateway(env("FINANCE_AGENT_URL"), env("FINANCE_AGENT_TOKEN"))


def bootstrap(worker_class: type[BaseWorker], argv: list[str] | None,
              description: str) -> tuple[BaseWorker, argparse.Namespace]:
    """Standard main(): env, logging, config, one construct path for all three."""
    load_dotenv()
    args = make_parser(worker_class.HANDLE, description).parse_args(argv)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    dsn = build_database_url()
    if not dsn:
        print("DATABASE_URL (or SUPABASE_DB_PASSWORD) is unset; nowhere to write. "
              f"Refusing to start {worker_class.HANDLE}.", file=sys.stderr)
        raise SystemExit(2)
    if psycopg is None:
        print("psycopg is not installed. pip install 'psycopg[binary]'", file=sys.stderr)
        raise SystemExit(2)
    worker = worker_class(
        dsn, build_gateway(),
        tick_seconds=tick_seconds_for(worker_class, args),
        batch_size=batch_size_for(worker_class, args),
        freshness_hours=freshness_hours_for(worker_class),
    )
    worker.dry_run = args.dry_run  # type: ignore[attr-defined]
    return worker, args


def main_entry(worker_class: type[BaseWorker], description: str,
               argv: list[str] | None = None) -> int:
    worker, args = bootstrap(worker_class, argv, description)
    if args.once:
        try:
            metrics = asyncio.run(worker.tick_once())
        except Exception as exc:
            print(f"tick failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"worker": worker.HANDLE, "metrics": metrics}, default=str))
        return 0
    worker.run()
    return 0


__all__ = [
    "DEFAULT_SPEND_FRESHNESS_HOURS", "ERRORS_BEFORE_UNWELL", "BaseWorker", "Lead",
    "SpendGateway", "SpendGatewayError", "SpendOutcome", "SpendTicket", "WorkerStore",
    "build_database_url", "env", "env_int_to_minor", "fmt_minor", "load_dotenv",
    "main_entry", "worker_settings",
]
