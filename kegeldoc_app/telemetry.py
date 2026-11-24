"""Per-visitor funnel events and the agent-visible log lines that mirror them.

Two tables, one module, and both exist for the same reason: the rollup `hustle_metrics`
has no per-visitor columns, so it cannot answer a single cohort question
(`LANE_C_PHASE0_STEP1-2.md` §3.3). The engine's `engine.trial_events` is the production
answer to that, and this module is its local equivalent: same column names, same status
vocabulary, same idempotency net, so the lift later is a migration rather than a redesign.

  * ``trial_events`` is the cohort fact table. One row per funnel moment, append-only,
    carrying `visitor_key`, `offer_id`, `test_variable`, the card funding type and the
    three amounts every ratio in the Day-1 sheet is computed from.
  * ``agent_logs`` mirrors the rows that a supervisor or a support agent would need to
    *see* rather than merely count: a fraud flag, a dispute opened, a refund issued, an
    expired checkout with no payment. One line each, with the event's own identity in
    `context`, which is what keeps one Stripe incident one query instead of a time-window
    scan (`supabase/schema.sql:214`).

The idempotency rule, and it is the reason this table is safe to write from a webhook:
Stripe re-delivers events, a customer refreshes a page, and the metrics export runs
twice on a morning when someone reruns it. A duplicate row inflates a denominator, which
does not look broken; it just quietly reports the ratios we would prefer. So `record()`
takes an `idempotency_key` and a partial unique index
(`unique (...) where idempotency_key is not null`, the same shape as
`funnel_worker.py`'s `trial_event_dedupe_uq`) enforces one row per key. Events that
*genuinely* repeat per visitor, a quiz answer sheet submitted twice on purpose, are left
without a key and stay unlimited.

Column parity with `engine.trial_events`: `visitor_key`, `funnel_type`, `offer_id`,
`occurred_at`, `amount_minor`, `currency`, `idempotency_key`. Deliberate additions, both
named in §3.3's list of metrics that have no home today: `test_variable` (variant
identity, which `funnel_sequences` has nowhere for) and `card_funding`/`card_country`
(prepaid-BIN share, the leading indicator the spec asks us to instrument from charge one).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Baseline variant label, read from the same place `storage.py` reads it. One offer, one
#: variant, one price until a test is pre-registered; when it is, the label changes *before*
#: the traffic arrives or the cohort cannot be split afterwards.
DEFAULT_TEST_VARIABLE = "baseline"

#: Engine's funnel_type, restricted to what this lane actually is. Kept as a vocabulary
#: rather than a bare literal so a second funnel in this app is a new value here and a real
#: cohort split, not an unlabeled mixture in one column.
FUNNEL_TYPE = "trial"

#: The funnel moments Phase 3 instruments. The names are the contract: the metrics export
#: and every ratio in §3.4 group by them, so a typo here is a silently missing metric
#: rather than an error, which is why `record` refuses an unknown name.
EVENTS = (
    "lp_view",              # landing page rendered
    "quiz_started",         # first quiz question seen
    "quiz_completed",       # full answer set captured
    "trial_started",        # a trial row exists (charged in live mode, recorded in rehearsal)
    "trial_paid",           # first money captured
    "cancel_clicked",       # the one-click cancel was taken (§2.3)
    "save_offer_selected",  # an A/B/C retention offer taken instead of a confirmed cancel
    "survey_submitted",     # post-cancel survey answered
    "refund_issued",        # charge.refunded
    "dispute_created",      # charge.dispute.created, the 1% ceiling numerator
    "radar_early_warning",  # radar.early_warning.created, the ≤ 0.5% ceiling numerator
    # Four more, because the §3.4 sheet asks for five numbers that cannot come from Stripe's
    # dashboard at all, and two of them are denominators whose absence silently flatters a
    # ratio. The spec's `rebill_declines_silent` name means *unrecoverable-after-one-retry*
    # declines (its own formula divides by `rebill_attempts`, which the sheet never defines,
    # so the metric had no denominator); recorded here as the honest pair instead.
    "preview_shown",              # the sunk-effort artefact rendered (diagnostic, LP→trial)
    "checkout_viewed",            # the card screen rendered, price disclosed on it
    "checkout_expired",           # abandoned Checkout session: a trial start that never paid
    "rebill_succeeded",           # a captured invoice, the conversion event for a rebill
    "rebill_declined",            # a failed invoice, recoverable by the day +3 retry
    "rebill_decline_terminal",    # the +3 retry failed too (§2.1 stop, dunning exhausted)
)

#: The subset whose arrival should also produce an `agent_logs` line: money moved or a
#: processor signal arrived, which is what an agent or a human triaging an account needs to
#: see as a named event rather than infer from a delta in a counter.
AGENT_NOTABLE_EVENTS = (
    "refund_issued",
    "dispute_created",
    "radar_early_warning",
)

#: Card funding types, spelled as Stripe spells `payment_method.card.funding`. `""` means
#: "not observed" (rehearsal, or the payment method was never expanded on the event object)
#: and is stored as the empty string rather than guessed at: an unknown BIN type counted as
#: consumer would understate the prepaid share, and the prepaid share is read as a warning
#: indicator, so the safe default is "unknown", never "clean".
CARD_FUNDING_TYPES = ("credit", "debit", "prepaid", "unknown", "")


#: Ad attribution, captured on `lp_view` only and stored in its `detail_json`, so no column
#: changes and the engine-parity columns stay untouched. The five standard `utm_*` keys and
#: nothing else: a click id such as `fbclid` or `gclid` identifies one person's click rather
#: than an ad, and the sheet never needs it. The join is `visitor_key`: an `lp_view` carrying
#: `utm_content=<variant_code>` ties that visitor's later `trial_started` / `trial_paid` rows to
#: the ad variant that brought them. First touch per visitor per UTC day, because `lp_view` is
#: deduped on that key: a visitor who arrives organically and clicks an ad later the same day
#: keeps the organic (empty) attribution.
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")

#: Per-value cap. A query string is attacker-controlled and lands in a row the export reads,
#: so it is bounded rather than trusted; 100 characters is several times any real campaign name.
UTM_MAX_LENGTH = 100


def attribution_from_query(params: Any) -> dict[str, str]:
    """The whitelisted `utm_*` values in a query mapping, cleaned, or `{}` when there are none.

    Takes any mapping with `.get` (starlette's `QueryParams`, a plain dict). Values are stripped
    of non-printable characters and surrounding whitespace and cut to `UTM_MAX_LENGTH`; an empty
    value after cleaning is dropped rather than stored as `""`, so "no UTM" has one spelling.
    Case is kept: a variant code is an identifier, and lowercasing it would break the join.
    """
    out: dict[str, str] = {}
    for key in UTM_KEYS:
        raw = params.get(key) if params is not None else None
        if raw is None:
            continue
        value = "".join(ch for ch in str(raw) if ch.isprintable()).strip()[:UTM_MAX_LENGTH]
        if value:
            out[key] = value
    return out

#: The three amounts every ratio is built from. Named separately from `amount_minor`
#: because they are three different money flows on one row and a reader of
#: `amount_minor` alone cannot tell captured from refunded.
_COLUMNS = (
    "occurred_at", "visitor_key", "funnel_type", "offer_id", "test_variable",
    "event", "amount_minor", "gross_volume_minor", "refunded_minor",
    "card_funding", "card_country", "currency", "reference_id", "detail_json",
    "idempotency_key",
)

_SCHEMA = """
create table if not exists trial_events (
  id                 integer primary key autoincrement,
  occurred_at        text    not null,
  visitor_key        text    not null default '',
  funnel_type        text    not null default 'trial',
  offer_id           text    not null default '',
  test_variable      text    not null default 'baseline',
  event              text    not null,
  -- `amount_minor` is *captured* money (the trial charge, the refund value, the disputed
  -- amount), which is what the gate's refund and dispute percentages divide. Separate from
  -- `gross_volume_minor` because a dispute is counted against authorised volume, not
  -- against what we kept, and §3.4's formula is explicit about that denominator.
  amount_minor       integer not null default 0,
  gross_volume_minor integer not null default 0,
  refunded_minor     integer not null default 0,
  card_funding       text    not null default '',
  card_country       text    not null default '',
  currency           text    not null default 'usd',
  -- Processor identity for the object this row describes (a charge id, a dispute id, a
  -- session id). Stored because it is the string to paste into the Stripe dashboard when a
  -- ratio trips, and because the idempotency key below is usually built from it.
  reference_id       text    not null default '',
  detail_json        text    not null default '{}',
  idempotency_key    text
);

-- The dedupe net, partial exactly like the engine's so a NULL key (an event that
-- legitimately repeats per visitor) never collides.
create unique index if not exists trial_event_dedupe_uq
  on trial_events (idempotency_key) where idempotency_key is not null;
-- The metrics export's read is (occurred_at, event): a day's rows grouped by event.
create index if not exists trial_events_window_idx
  on trial_events (occurred_at, event);
create index if not exists trial_events_visitor_idx
  on trial_events (visitor_key, occurred_at);

create table if not exists agent_logs (
  id            integer primary key autoincrement,
  created_at    text    not null,
  agent_handle  text    not null default 'worker:funnel',
  level         text    not null default 'info',
  event         text    not null,
  message       text    not null default '',
  context_json  text    not null default '{}',
  is_audit      integer not null default 0,
  -- Second log line for the same event means the handler ran twice, which is a fact worth
  -- being able to see; a unique key would have thrown it away.
  idempotency_key text
);
create index if not exists agent_logs_event_idx
  on agent_logs (event, created_at);

-- The same net as `trial_event_dedupe_uq`, for the money events: a refund that arrives twice
-- should read as one, because the line is a fact about one transaction rather than an audit
-- trail of how many times the handler woke up. Repeats *without* a key still append, and that
-- is what makes an unconditional log call safe to leave unconditional: the key is how a caller
-- asks for the dedupe, and leaving it out is a visible choice rather than a missing feature.
create unique index if not exists agent_logs_dedupe_uq
  on agent_logs (idempotency_key) where idempotency_key is not null;
"""


def utc_now() -> str:
    """Second-resolution UTC ISO-8601, matching `storage.utc_now` and
    `billing_store.utc_now`. Day-level grouping, second-resolution storage: a row that
    straddles a UTC midnight in coarse resolution lands in the wrong day's bucket, and the
    daily sheet is read one row per day."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def day_of(occurred_at: str) -> str:
    """The UTC calendar day a stored timestamp belongs to, as `YYYY-MM-DD`.

    Sliced off the front rather than parsed, because every writer in this repo stores the
    same ISO string and a parse that fails on one legacy row would drop a whole day from
    the export. Returns the input unchanged when it is too short to hold a date, which
    keeps a malformed value visible in the output instead of turning it into an empty day
    that silently merges with the other bad ones.
    """
    return occurred_at[:10] if len(occurred_at) >= 10 else occurred_at


class TelemetryStore:
    """Append-only funnel telemetry, one SQLite connection per call.

    Same shape as `BillingStore` and `ResponseStore`, for the same reason: FastAPI runs the
    sync handlers on a threadpool and a shared connection is the first thing to fail under
    two tabs. It lives in the *billing* database file rather than the responses file,
    because a metrics export then reads subscriptions and events from one connection and
    cannot compare a count from one file against a count from another written a second
    apart at a day boundary.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma synchronous = normal")
        return conn

    # ---- writes ------------------------------------------------------------

    def record(self, *, event: str, visitor_key: str = "",
               offer_id: str = "", test_variable: str = DEFAULT_TEST_VARIABLE,
               amount_minor: int = 0, gross_volume_minor: int = 0,
               refunded_minor: int = 0, card_funding: str = "",
               card_country: str = "", currency: str = "usd",
               reference_id: str = "", idempotency_key: str = "",
               occurred_at: str = "", detail: dict[str, Any] | None = None,
               funnels: str = FUNNEL_TYPE) -> bool:
        """Append one funnel event. True when this call wrote the row, False when an
        identical key was already present.

        The boolean is the point, not a nicety: a caller that knows it was a duplicate can
        log that fact, and the metrics tests assert on it. A False return is *not* an error
        and never raises, because every call site is either a webhook redelivery or a page
        view, and neither should 500 because we already counted this visitor.

        Raises on an unknown `event` name. That is a programming error, not a runtime
        condition, and storing it would mean the export silently reports 0 for a metric
        whose rows landed under a name nothing groups by.
        """
        if event not in EVENTS:
            raise ValueError(f"unknown funnel event {event!r}, expected one of {EVENTS}")
        if card_funding not in CARD_FUNDING_TYPES:
            raise ValueError(f"unknown card funding {card_funding!r}, "
                             f"expected one of {CARD_FUNDING_TYPES}")
        stamp = occurred_at or utc_now()
        row = {
            "occurred_at": stamp,
            "visitor_key": visitor_key or "",
            "funnel_type": funnels,
            # `offer_id` carries the variant label upstream too, so an empty one falls back
            # to the test variable rather than to a blank: a row with no variant identity
            # cannot be attributed to a cohort and would pollute the baseline.
            "offer_id": offer_id or test_variable or DEFAULT_TEST_VARIABLE,
            "test_variable": test_variable or DEFAULT_TEST_VARIABLE,
            "event": event,
            "amount_minor": int(amount_minor or 0),
            "gross_volume_minor": int(gross_volume_minor or 0),
            "refunded_minor": int(refunded_minor or 0),
            "card_funding": card_funding or "",
            "card_country": card_country or "",
            "currency": (currency or "usd").lower(),
            "reference_id": reference_id or "",
            "detail_json": json.dumps(detail or {}, ensure_ascii=False, default=str),
            "idempotency_key": idempotency_key or None,
        }
        columns = ", ".join(_COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        statement = (f"insert or ignore into trial_events ({columns}) "
                     f"values ({placeholders})")
        params = tuple(row[column] for column in _COLUMNS)
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(statement, params)
            except sqlite3.IntegrityError as exc:
                # `or ignore` covers the dedupe index, which is the one conflict we expect.
                # Anything else here (a NOT NULL we violated, a CHECK the engine's table has
                # and we mirror) would otherwise pass silently and report the event as a
                # duplicate, so a genuine bug in a call site would look like a redelivery.
                raise ValueError(f"could not record funnel event {event!r}: {exc}") from exc
            return cursor.rowcount > 0

    def log(self, *, event: str, message: str = "", level: str = "info",
            context: dict[str, Any] | None = None, is_audit: bool = False,
            agent_handle: str = "worker:funnel",
            idempotency_key: str = "") -> bool:
        """Append one agent-visible log line mirroring a funnel event.

        Idempotent the same way `record` is when given a key. `agent_logs` is append-only
        upstream too, so nothing here updates a row later: a resolution arrives as a new
        line, which is the shape that keeps the timeline honest about what was known when.
        """
        if level not in ("debug", "info", "warning", "error", "critical"):
            raise ValueError(f"unknown log level {level!r}")
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """insert or ignore into agent_logs
                   (created_at, agent_handle, level, event, message, context_json,
                    is_audit, idempotency_key)
                   values (?, ?, ?, ?, ?, ?, ?, ?)""",
                (utc_now(), agent_handle, level, event, message[:500],
                 json.dumps(context or {}, ensure_ascii=False, default=str),
                 1 if is_audit else 0, idempotency_key or None),
            )
            return cursor.rowcount > 0

    # ---- reads -------------------------------------------------------------

    def events(self, day: str = "", *, event: str = "") -> list[dict[str, Any]]:
        """Stored rows for one UTC day (or all days when `day` is empty), oldest first.

        Ordered rather than returned in heap order because the export appends one row per
        day and a reader sanity-checks the first rows by eye; an unordered first page is a
        sheet where the numbers cannot be spot-checked at all.
        """
        sql = "select * from trial_events"
        clauses: list[str] = []
        args: list[Any] = []
        if day:
            # Prefix comparison, not `date(occurred_at) = ?`, so the (occurred_at, event)
            # index does the work and a malformed timestamp stays visible rather than
            # quietly becoming NULL.
            clauses.append("occurred_at >= ? and occurred_at < ?")
            args += [day, f"{day}~"]  # '~' sorts above every ISO timestamp character
        if event:
            clauses.append("event = ?")
            args.append(event)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by occurred_at, id"
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def event_counts(self, day: str = "") -> dict[str, int]:
        """Count per event name for one UTC day. Missing events are absent, not 0:
        the export distinguishes "nobody did this" from "this counter exists and is
        zero" by writing 0 for both, and the distinction lives in the caller."""
        sql = "select event, count(*) as n from trial_events"
        args: tuple[Any, ...] = ()
        if day:
            sql += " where occurred_at >= ? and occurred_at < ?"
            args = (day, f"{day}~")
        sql += " group by event"
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return {row["event"]: int(row["n"]) for row in rows}

    def has_event(self, idempotency_key: str) -> bool:
        """Whether a key already landed. Used by callers that must not double-send an
        email or double-count on a redelivered webhook, and by the tests."""
        if not idempotency_key:
            return False
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "select 1 from trial_events where idempotency_key = ? limit 1",
                (idempotency_key,),
            ).fetchone()
        return row is not None

    def log_rows(self, event: str = "") -> list[dict[str, Any]]:
        """Agent-visible lines, newest last. `event` filters; empty returns all."""
        sql = "select * from agent_logs"
        args: tuple[Any, ...] = ()
        if event:
            sql += " where event = ?"
            args = (event,)
        sql += " order by id"
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
