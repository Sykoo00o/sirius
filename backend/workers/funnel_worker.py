"""Funnel worker: landing-page trigger sequences and trial-conversion metrics.

Two jobs, on two clocks
-----------------------
1. Fire landing-page trigger sequences: a timed ladder of steps (SMS today,
   upsell email tomorrow) that a visitor opts into on the page and that must
   fire at wall-clock offsets from opt-in, regardless of when the process
   happens to wake up. Scheduled with APScheduler.
2. Track trial-conversion metrics into engine.hustle_metrics: how many visitors
   became trials and how many trials became paid. Also scheduled.

Why APScheduler rather than BaseWorker's tick loop
--------------------------------------------------
The base loop assumes work is always available and freshness is measured from
"now - tick_seconds". Neither holds for a trigger sequence. A step is due at
`opt_in_time + offset`, which can be forty minutes or two days away, and firing
it eight minutes late is fine while firing it eight hours late is a compliance
problem (the disclosure window closes). APScheduler's scheduler-backed store
gives that "wake at a deadline" property, and crucially it survives a restart:
a row-based job store means a sequence armed before a deploy still fires after
it, which a set of in-process timers does not.

So this class does NOT subclass BaseWorker. It reuses the parts of common.py
that are orthogonal to the loop (WorkerStore, SpendGateway, Lead, env reading)
and replaces the loop. The trade is stated plainly further down, under
"WHAT THIS FILE DOES NOT DO".

Metric mapping, and why it is not a free choice
-----------------------------------------------
hustle_metrics has no trial columns. It has `targets_discovered` and
`targets_executed`, whose schema comment calls them "how many sourced deals
converted" for the arbitrage hustle. The funnel reuses them, which was already
this module's convention before the rewrite:

    targets_discovered = trials started (visitors who entered the funnel)
    targets_executed   = trials converted to paid

That makes conversion rate `targets_executed / targets_discovered` on the funnel
row, and it is lossy: a visitor who saw a page but never started a trial is
invisible in both columns, so this table cannot report page-to-trial rate. Trial
start time lives in `engine.trial_events`, which this module owns, so the finer
funnel is recoverable there rather than lost.

Reads come from Postgres, writes go to the internet, money goes through
backend/finance_agent.py. Never imports anything from engine/ (see common.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

try:
    from .common import (
        Lead,
        SpendGateway,
        SpendTicket,
        WorkerStore,
        _float_env,
        _int_env,
        build_database_url,
        build_gateway,
        env,
        env_int_to_minor,
        load_dotenv,
    )
except ImportError:  # pragma: no cover - `python funnel_worker.py` from this dir
    from common import (  # type: ignore[no-redef]
        Lead,
        SpendGateway,
        SpendTicket,
        WorkerStore,
        _float_env,
        _int_env,
        build_database_url,
        build_gateway,
        env,
        env_int_to_minor,
        load_dotenv,
    )

log = logging.getLogger("workers.funnel")

#: Queue `kind` values, also the dispatch keys in handle().
EVENT_SOURCE = "funnel_events"
CONVERSION_SOURCE = "pending_conversions"
TRIGGER_SOURCE = "trigger_events"

#: Sequence step kinds. Every template step is a real action: no filler kinds.
#: (An earlier draft had a 'wait' step that consumed no vendor call and existed
#: only to carry spacing, but absolute offsets make spacing implicit, so it had
#: nothing left to do.)
STEP_SMS = "sms"
STEP_EMAIL = "email"
#: Terminal marker. A conversion row with fired_at set is counted exactly once,
#: so a retried sequence cannot inflate targets_executed.
STEP_CONVERSION = "conversion"
BILLABLE_STEPS = frozenset({STEP_SMS, STEP_EMAIL})

#: Conversion uploads older than this hurt more than help. Meta's CAPI and
#: Google both accept late events, but a late event teaches the optimiser about
#: a buyer profile that has already rotated out. Hours, because ad sets
#: re-optimise on roughly a daily cycle.
DEFAULT_CONVERSION_MAX_AGE_HOURS = 24.0

#: Ad spend attributed per uploaded conversion. Real attribution is unknowable
#: from inside the funnel: the platform will not tell us what one upload cost.
#: So this is an explicit, env-tunable allocation, logged next to the ledger row
#: so the assumption is auditable rather than buried inside a number.
DEFAULT_COST_PER_UPLOAD_MINOR = 40  # USD 0.40

#: How often the sequence sweeper wakes to arm rows that came due. Coarse on
#: purpose: a step due at 14:00 that arms at 14:04 costs nothing, and every
#: extra wake is a Postgres read.
DEFAULT_SWEEP_SECONDS = 60

#: Cadence of the funnel-metrics refresh. Not hourly: see _refresh_metrics's
#: docstring for why the counting window and this interval have to line up.
DEFAULT_METRICS_INTERVAL_MINUTES = 15

#: Steps that miss this window while the process was down fire immediately,
#: late. Inside it, lateness costs a slightly less timely message; beyond it,
#: the message has lost its premise and firing it anyway is the worse option.
#: Picked from the same 6-hour freshness premise as DEFAULT_SPEND_FRESHNESS_HOURS
#: in common.py so the two "too late to act" rules do not drift apart.
DEFAULT_LATE_STEP_GRACE_HOURS = 6.0


def _ident(name: str) -> str:
    """Validate a schema name before interpolating it into DDL.

    Not a style rule. `create schema if not exists {s}` takes an identifier, not a
    bindable value, so an unvalidated env-supplied name is an injection path into
    every statement this file builds. This mirrors the identical guard in
    common.py (`WORKER_SCHEMA`) rather than trusting it to have already run: this
    module reads the same variable independently to qualify its own tables, and a
    second validator on the string that reaches SQL text is cheap.
    """
    cleaned = name.strip().lower()
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", cleaned):
        raise ValueError(f"unsafe schema name {name!r}")
    return cleaned


class FunnelWorker:
    """Trigger-sequence executor and funnel-metrics reporter for Hustle A."""

    HANDLE = "worker:funnel"
    HUSTLE = "funnel"

    JOB_QUEUE = "funnel_queue_tick"
    JOB_SWEEP = "funnel_sequence_sweep"
    JOB_METRICS = "funnel_metrics_refresh"

    def __init__(self, database_url: str, gateway: SpendGateway, *,
                 tick_seconds: int = 60, batch_size: int = 25,
                 freshness_hours: float = 6.0, dry_run: bool = False) -> None:
        self.store = WorkerStore(database_url)
        self.database_url = database_url
        self.gateway = gateway
        self.tick_seconds = max(5, int(tick_seconds))
        self.batch_size = max(1, int(batch_size))
        self.max_age_seconds = int(freshness_hours * 3600)
        self.dry_run = bool(dry_run)

        self.schema = _ident(env("WORKER_SCHEMA", "engine"))
        self.queue_table = f"{self.schema}.worker_queue"
        self.sequence_table = f"{self.schema}.funnel_sequences"
        self.step_table = f"{self.schema}.funnel_sequence_steps"
        self.trial_table = f"{self.schema}.trial_events"

        self.conversion_max_age = timedelta(
            hours=_float_env("FUNNEL_CONVERSION_MAX_AGE_HOURS",
                             DEFAULT_CONVERSION_MAX_AGE_HOURS))
        self.late_step_grace = timedelta(
            hours=_float_env("FUNNEL_LATE_STEP_GRACE_HOURS", DEFAULT_LATE_STEP_GRACE_HOURS))
        self.cost_per_upload_minor = _int_env("FUNNEL_COST_PER_UPLOAD_MINOR",
                                              DEFAULT_COST_PER_UPLOAD_MINOR)
        self.autocharge_amount_minor = env_int_to_minor(
            _float_env("FUNNEL_AUTOCARGE_PRICE", 1.00))
        self.metrics_every_minutes = max(1, _int_env("FUNNEL_METRICS_INTERVAL_MINUTES",
                                                    DEFAULT_METRICS_INTERVAL_MINUTES))

        self.upload_url = env("FUNNEL_CAPI_URL")
        self.upload_token = env("FUNNEL_CAPI_TOKEN")
        self.upload_timeout = httpx.Timeout(15.0, connect=5.0)

        # Revenue events booked this process lifetime, so the daily rollup books
        # each event exactly once even when Stripe redelivers it. Bounded because
        # a worker runs for weeks and this set never shrinks on its own.
        self._booked: dict[str, float] = {}
        self._scheduler: BlockingScheduler | None = None
        self._errors = 0

    # ------------------------------------------------------------------ schema

    def ensure_tables(self) -> None:
        """Create what this worker owns. Idempotent, and cheap enough per tick.

        Four objects, and the reasoning differs for each:

        * worker_queue is shared with the previous design and unchanged in shape.
          `status` is text with a CHECK rather than an enum so adding a state does
          not need ALTER TYPE inside a hot worker, and the partial index on
          (available_at) where status='pending' is what keeps claims fast: the
          processed majority leaves the index entirely instead of being scanned
          and discarded.
        * funnel_sequences carries `scheduled_at` as the sequence's zero point.
          Every step offset is measured against THAT, not against opt-in time, so
          a re-arm that shifts the sequence moves all its steps with it. Deriving
          step times from opt_in_time instead would let an armed sequence and a
          re-armed one disagree about when step 2 was due.
        * funnel_sequence_steps has a unique (sequence_id, step_index). A ladder
          that gets armed twice, which is exactly what a double delivery does,
          must fail on insert rather than double-send and double-count.
        * trial_events is the per-visitor fact table. hustle_metrics cannot hold
          it (no columns, and the two it borrows are daily integers), so the
          fine-grained funnel lives here and the daily rollup reads from it.
        """
        sql = f"""
            create schema if not exists {self.schema};

            create table if not exists {self.queue_table} (
              id            bigint generated always as identity primary key,
              worker        text        not null,
              kind          text        not null,
              dedupe_key    text,
              payload       jsonb       not null default '{{}}'::jsonb,
              status        text        not null default 'pending'
                            check (status in ('pending','claimed','done','failed','expired')),
              -- Business time: when the thing happened out in the world.
              occurred_at   timestamptz not null default now(),
              -- Retry time: when we are next allowed to try. Separate because a
              -- throttled vendor makes those two differ.
              available_at  timestamptz not null default now(),
              attempts      integer     not null default 0,
              last_error    text,
              created_at    timestamptz not null default now(),
              updated_at    timestamptz not null default now()
            );
            create unique index if not exists worker_queue_dedupe_uq
              on {self.queue_table} (worker, dedupe_key) where dedupe_key is not null;
            create index if not exists worker_queue_claim_idx
              on {self.queue_table} (worker, available_at)
              where status = 'pending';

            create table if not exists {self.sequence_table} (
              id                bigint generated always as identity primary key,
              visitor_key       text        not null,
              -- 'tripwire' (pay immediately) and 'trial' (7-day, bill later) need
              -- different ladders and different conversion arithmetic.
              funnel_type       text        not null default 'trial'
                                check (funnel_type in ('tripwire','trial')),
              offer_id          text,
              landing_url       text,
              -- Terminal state machine: an abandoned or refunded sequence stops
              -- generating steps rather than firing at a visitor who left.
              status            text        not null default 'pending'
                                check (status in ('pending','armed','converted',
                                                  'abandoned','cancelled','refunded','failed')),
              -- Zero point for every step offset. See the docstring.
              scheduled_at      timestamptz,
              opt_in_at         timestamptz not null default now(),
              -- Billing period, in days, for a trial. 7 is the spec's low-cost
              -- 7-day trial; stored per row because it varies by offer.
              trial_days        integer     not null default 7
                                check (trial_days between 1 and 365),
              trial_amount_minor bigint     not null default 0
                                check (trial_amount_minor >= 0),
              rebill_amount_minor bigint    not null default 0
                                check (rebill_amount_minor >= 0),
              -- text, not char(3): a bpchar space-pads to width, so DEFAULT 'USD'
              -- stores 'USD ' and the regex check would reject its own default for
              -- any writer that omits the column. Length enforced with length()
              -- and letters with the plus-star regex, deliberately WITHOUT a braced
              -- quantifier: ensure_tables builds this DDL inside an f-string, and
              -- on Python 3.12 a single brace anywhere - even inside a SQL comment
              -- like this one - is parsed as a replacement field, so a written-out
              -- brace-three silently became '[A-Z]3' in the live CHECK and matched
              -- nothing. Escaping it as a doubled brace works but reads as a typo;
              -- length() has no braces to lose and states exactly what it enforces.
              currency          text        not null default 'USD'
                                check (length(currency) = 3 and currency ~ '^[A-Z]+$'),
              payload         jsonb       not null default '{{}}'::jsonb,
              last_error        text,
              created_at        timestamptz not null default now(),
              updated_at        timestamptz not null default now()
            );
            create index if not exists funnel_sequence_due_idx
              on {self.sequence_table} (scheduled_at)
              where status in ('pending','armed');
            create index if not exists funnel_sequence_visitor_idx
              on {self.sequence_table} (visitor_key, opt_in_at desc);
            -- One active sequence per visitor per funnel. Without this a visitor
            -- who refreshes the landing page four times gets four ladders.
            create unique index if not exists funnel_sequence_active_uq
              on {self.sequence_table} (visitor_key, funnel_type)
              where status in ('pending','armed');

            create table if not exists {self.step_table} (
              id           bigint generated always as identity primary key,
              sequence_id  bigint      not null
                           references {self.sequence_table}(id) on delete cascade,
              step_index   integer     not null,
              kind         text        not null
                           check (kind in ('sms','email','conversion')),
              -- Absolute due time: sequence.scheduled_at + the step's offset.
              -- Not null in practice (every kind has a time); left nullable only
              -- so a hand-inserted diagnostic row cannot abort a sweep.
              due_at       timestamptz,
              fired_at     timestamptz,
              status       text        not null default 'pending'
                           check (status in ('pending','fired','failed','skipped')),
              attempts     integer     not null default 0,
              cost_minor   bigint      not null default 0 check (cost_minor >= 0),
              last_error   text,
              payload      jsonb       not null default '{{}}'::jsonb,
              unique (sequence_id, step_index)
            );
            create index if not exists funnel_step_due_idx
              on {self.step_table} (due_at)
              where status = 'pending';
            -- Terminal-step accounting. Partial so a backfill cannot recount a
            -- conversion that predates the current run.
            create index if not exists funnel_step_conversion_accured_idx
              on {self.step_table} (fired_at)
              where kind = 'conversion' and status = 'fired';

            create table if not exists {self.trial_table} (
              id                  bigint generated always as identity primary key,
              visitor_key         text        not null,
              funnel_type         text        not null default 'trial',
              offer_id            text,
              -- 'started' when a visitor commits to the offer, 'paid' when the
              -- first charge lands, 'cancelled' inside the trial window.
              status              text        not null
                                  check (status in ('started','paid','cancelled')),
              occurred_at         timestamptz not null default now(),
              amount_minor        bigint      not null default 0,
              currency            text        not null default 'USD'
                                  check (length(currency) = 3 and currency ~ '^[A-Z]+$'),
              sequence_id         bigint,
              idempotency_key     text,
              created_at          timestamptz not null default now()
            );
            create unique index if not exists trial_event_dedupe_uq
              on {self.trial_table} (idempotency_key) where idempotency_key is not null;
            -- The metrics window read is (status, occurred_at). Covering index so
            -- the count never touches the heap.
            create index if not exists trial_event_window_idx
              on {self.trial_table} (status, occurred_at);
        """
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)

    def close(self) -> None:
        """No-op today: WorkerStore opens a connection per statement.

        Named and called from run()'s finally anyway, so that moving to a pooled
        connection is a one-line change here rather than a silent leak of one
        connection per restart.
        """

    # ------------------------------------------------------------ queue plumbing

    def enqueue(self, kind: str, identity: str, payload: dict[str, Any],
                *, occurred_at: datetime | None = None) -> bool:
        """Queue one event. False if that identity is already queued (a redelivery).

        ON CONFLICT DO NOTHING rather than a pre-check: two webhook deliveries
        arriving concurrently both pass a SELECT, and uploading a conversion
        twice is a real cost.
        """
        sql = (f"insert into {self.queue_table} (worker, kind, dedupe_key, payload, occurred_at)"  # noqa: S608 - table name is a validated constant
               " values (%s, %s, %s, %s::jsonb, coalesce(%s, now())) "
               "on conflict (worker, dedupe_key) where dedupe_key is not null "
               "do nothing returning id")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (self.HANDLE, kind, identity, json.dumps(payload, default=str),
                                  occurred_at.astimezone(UTC) if occurred_at else None))
                return cur.fetchone() is not None
        except Exception as exc:  # intake must not kill the tick
            log.warning("enqueue(%s) failed: %s", kind, exc)
            return False

    def pending(self, kind: str, limit: int, max_age: timedelta | None,
                now: datetime) -> list[Lead]:
        """Claim up to `limit` pending rows of one kind as leads.

        One statement, so two replicas cannot take the same row. `for update skip
        locked` deliberately carries no volatile predicate: common.py's claim()
        documents a measured PostgreSQL 16 behaviour where a `now()` comparison
        under a locking clause makes "excluded by the predicate" and "someone else
        holds the lock" return the identical empty result. Here that would hide
        work forever, so the age cut runs in Python instead. Rows past the cut are
        marked 'expired' rather than left pending, so they stop being re-discovered
        on every tick.

        Ordering by created_at, not occurred_at, is the other half of the same
        problem: ordered by event time, the un-actable old rows would occupy the
        head of every batch forever and starve the fresh rows behind them.
        """
        sql = f"""
            with picked as (
              select id from {self.queue_table}
               where worker = %s and kind = %s and status = 'pending'
                 and available_at <= now()
               order by created_at
               limit %s
               for update skip locked
            )
            update {self.queue_table} q
               set status = 'claimed', attempts = q.attempts + 1, updated_at = now()
              from picked
             where q.id = picked.id
            returning q.id, q.kind, q.dedupe_key, q.payload, q.occurred_at, q.attempts
        """  # noqa: S608 - table names are validated constants; all values are bind params
        cutoff = now - max_age if max_age else None
        leads: list[Lead] = []
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (self.HANDLE, kind, limit))
            rows = cur.fetchall()
            expired: list[int] = []
            for row in rows:
                occurred = row["occurred_at"]
                if occurred.tzinfo is None:
                    occurred = occurred.replace(tzinfo=UTC)
                if cutoff is not None and occurred < cutoff:
                    expired.append(int(row["id"]))
                    continue
                leads.append(Lead(
                    source=row["kind"], identity=str(row["dedupe_key"] or row["id"]),
                    created_at=occurred,
                    expected_value_minor=int(row["payload"].get("amount_minor") or 0),
                    currency=str(row["payload"].get("currency") or "USD").upper(),
                    payload={"queue_id": int(row["id"]), "attempts": int(row["attempts"]),
                             **row["payload"]},
                ))
            if expired:
                cur.execute(f"update {self.queue_table} set status = 'expired', updated_at = now()"  # noqa: S608
                            " where id = any(%s)", (expired,))
                log.info("%s expired %d stale %s row(s) past %s",
                         self.HANDLE, len(expired), kind, max_age)
        return leads

    def finish(self, lead: Lead, *, ok: bool, error: str = "") -> None:
        queue_id = (lead.note or {}).get("queue_id")
        if not queue_id:
            return
        # Backoff so a vendor that is rejecting us is retried on a slope instead
        # of every tick: attempt n waits 2^n minutes, capped at a day.
        sql = (f"update {self.queue_table} set status = %s, last_error = %s,"  # noqa: S608
               " updated_at = now(),"
               " available_at = now() + make_interval(mins => least(1440, power(2, attempts)::int))"
               " where id = %s")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, ("done" if ok else "failed", error[:500] or None, queue_id))
        except Exception as exc:  # the row stays claimed; the next sweep re-offers it
            log.warning("finish(queue_id=%s) failed: %s", queue_id, exc)

    # ------------------------------------------------------------------ arming

    def arm(self, *, visitor_key: str, template: str = "trial_default",
            funnel_type: str = "trial", offer_id: str | None = None,
            landing_url: str | None = None, lead_email: str | None = None,
            lead_phone: str | None = None, currency: str = "USD",
            trial_amount_minor: int = 0, rebill_amount_minor: int = 0,
            trial_days: int = 7, opt_in_at: datetime | None = None,
            started: bool = True) -> int | None:
        """Persist a visitor's sequence and its step ladder in one transaction.

        Returns the sequence id, or None if the visitor already holds an active
        sequence of this funnel type.

        Args:
            visitor_key: stable identifier for the visitor. Hash it upstream
                rather than passing a raw email: it lands unencrypted next to
                conversion state.
            template: which ladder from TEMPLATES, resolved in Python.
            started: record a 'started' trial event alongside the ladder. Set
                False for a page view that is not yet a trial commitment, which
                is how page-to-trial rate stays measurable in trial_events even
                though hustle_metrics cannot express it.

        Materialising the ladder at opt-in time, rather than computing due times
        from the template on every sweep, is deliberate: it means an operator can
        see exactly what a given visitor is scheduled to receive, and changing
        the template later does not retroactively move steps a visitor already
        received. That is a compliance property, not a convenience. Each step's
        due time is `scheduled_at + offset` in absolute terms, so `after_days: 7`
        means day seven of that visitor's trial regardless of the other steps.

        Single transaction because the sequence and its steps must land together.
        Half an armed ladder is the worst state here: a sequence row with no steps
        sweeps as "nothing due" forever and sends nothing.
        """
        # KeyError, not a silent fallback. `template or TEMPLATES["trial_default"]`
        # would send an SMS ladder to someone whose record said "minimal", which
        # is a message the visitor did not sign up for and a dispute waiting to
        # happen; a caller that names a template that does not exist is a bug, and
        # main() validates --template the same way.
        if template not in TEMPLATES:
            raise KeyError(f"unknown sequence template {template!r}; "
                           f"choose one of {', '.join(sorted(TEMPLATES))}")
        template_steps = TEMPLATES[template]
        opted = (opt_in_at or datetime.now(UTC)).astimezone(UTC)
        insert_seq = (f"insert into {self.sequence_table}"  # noqa: S608
                      " (visitor_key, funnel_type, offer_id, landing_url, status, opt_in_at,"
                      " trial_days, trial_amount_minor, rebill_amount_minor, currency, payload)"
                      " values (%s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s::jsonb)"
                      " on conflict (visitor_key, funnel_type)"
                      "    where status in ('pending','armed')"
                      " do nothing returning id")
        insert_step = (f"insert into {self.step_table}"  # noqa: S608
                       " (sequence_id, step_index, kind, due_at, payload)"
                       " values (%s, %s, %s, %s, %s::jsonb)")
        try:
            # NOT store._connect(): that is autocommit, and the lock plus the
            # two inserts have to be one atomic fact. Same reasoning as claim().
            import psycopg
            with psycopg.connect(self.database_url, autocommit=False,
                                 options=self.store._options()) as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(insert_seq, (
                            visitor_key, funnel_type, offer_id, landing_url, opted,
                            int(trial_days), int(trial_amount_minor), int(rebill_amount_minor),
                            norm_currency(currency),
                            json.dumps({"template": template, "lead_email": lead_email,
                                        "lead_phone": lead_phone}, default=str)))
                        row = cur.fetchone()
                        if row is None:
                            conn.rollback()
                            return None
                        sequence_id = int(row["id"])
                        for index, step in enumerate(template_steps):
                            cur.execute(insert_step, (
                                sequence_id, index, step["kind"],
                                self._shift(opted, step),
                                json.dumps(step, default=str)))
                        cur.execute(
                            f"update {self.sequence_table} set status = 'armed',"  # noqa: S608
                            " scheduled_at = %s, updated_at = now() where id = %s",
                            (opted, sequence_id))
                        if started:
                            cur.execute(
                                f"insert into {self.trial_table}"  # noqa: S608
                                " (visitor_key, funnel_type, offer_id, status, occurred_at,"
                                " amount_minor, currency, sequence_id, idempotency_key)"
                                " values (%s, %s, %s, 'started', %s, %s, %s, %s, %s)"
                                " on conflict (idempotency_key) where idempotency_key is not null"
                                " do nothing",
                                (visitor_key, funnel_type, offer_id, opted,
                                 int(trial_amount_minor), norm_currency(currency), sequence_id,
                                 f"started:{sequence_id}"))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return sequence_id
        except Exception as exc:
            log.warning("arm(%s) failed: %s", visitor_key[:40], exc)
            raise

    @staticmethod
    def _shift(base: datetime, step: dict[str, Any]) -> datetime:
        """Absolute due time for one step: sequence zero point + the step's offset.

        Absolute, not relative to the previous step. A template says `after_days:
        7` and means day 7 of the trial, so a step's time must be readable without
        knowing the steps around it. Cumulative offsets turn "a 7-day trial" into
        day 11, and a single step naming `after_hours: 1` reads as one hour to a
        reader and, under an additive reading, adds to whatever came before. That
        is the hour/day bug in its purest form, and here it was live.

        Offsets are additive *within* a step (minutes+hours+days of that one step)
        so a template can write `after_hours: 26` or `after_days: 1, after_hours: 2`
        without either spelling being wrong.
        """
        return base + timedelta(
            minutes=int(step.get("after_minutes") or 0),
            hours=int(step.get("after_hours") or 0),
            days=int(step.get("after_days") or 0))

    def due_steps(self, limit: int) -> list[dict[str, Any]]:
        """Claim steps whose due time has passed, oldest first, across sequences.

        A join in one statement rather than a Python loop over sequences, because
        the common case is a hundred visitors mid-ladder and only a handful of
        them have something due this minute.

        The sequence must be armed for its steps to be eligible: that is what stops
        a cancelled or refunded visitor receiving step 3 of a ladder they exited.
        """
        sql = f"""
            with picked as (
              select s.id from {self.step_table} s
                join {self.sequence_table} q on q.id = s.sequence_id
               where s.status = 'pending'
                 and s.due_at is not null
                 and s.due_at <= now()
                 and q.status in ('pending','armed')
               order by s.due_at
               limit %s
               for update of s skip locked
            )
            update {self.step_table} s
               set status = 'fired', attempts = s.attempts + 1
              from picked
             where s.id = picked.id
            returning s.id, s.sequence_id, s.step_index, s.kind, s.due_at, s.payload, s.attempts
        """  # noqa: S608 - table names are validated constants; all values are bind params
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
        for row in rows:
            due = row["due_at"]
            if due.tzinfo is None:
                due = due.replace(tzinfo=UTC)
            # 'fired' is set by the claim, so a step this process is about to act
            # on is never re-claimed by a sibling. Roll it back if the action
            # raises; see sweep().
            row["due_at"] = due
        return rows

    def mark_step(self, step_id: int, *, ok: bool, cost_minor: int = 0,
                  error: str = "") -> None:
        """Record a step's outcome, and un-claim it on failure so it retries.

        A failed step goes back to 'pending' with its attempts bumped. Leaving it
        'fired' after a vendor 500 would silently drop a message the visitor is
        still owed; leaving it claimed forever would be the same thing wearing a
        different hat.
        """
        if ok:
            sql = (f"update {self.step_table} set cost_minor = cost_minor + %s,"  # noqa: S608
                   " last_error = null where id = %s")
            args: tuple[Any, ...] = (int(cost_minor), step_id)
        else:
            sql = (f"update {self.step_table} set status = 'pending',"  # noqa: S608
                   " last_error = %s where id = %s")
            args = (error[:500] or "step failed", step_id)
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, args)
        except Exception as exc:  # a step we cannot stamp will be retried, which is safe
            log.warning("mark_step(%s) failed: %s", step_id, exc)

    # -------------------------------------------------------------- sequence job

    def sweep(self) -> dict[str, Any]:
        """Fire due sequence steps. This is APScheduler's 'sequence_tick' job."""
        self.ensure_tables()
        # Abandon sequences nobody finished: an armed ladder whose trial window
        # closed with no conversion will never fire anything again, and leaving it
        # armed keeps it in the due index forever.
        self.expire_stale_sequences()
        rows = self.due_steps(self.batch_size * 4)
        fired, skipped_late, failed = 0, 0, 0
        now = datetime.now(UTC)
        for row in rows:
            due = row["due_at"]
            if now - due > self.late_step_grace:
                # Past grace the premise is gone (see DEFAULT_LATE_STEP_GRACE_HOURS).
                # 'skipped' rather than 'failed': nothing went wrong, the window
                # closed, and a failure counter here would page someone for a
                # restart that happened eight hours ago.
                self.skip_step(int(row["id"]), f"missed by {(now - due).total_seconds():.0f}s")
                skipped_late += 1
                continue
            try:
                outcome = self.fire_step(row)
            except Exception as exc:
                self.mark_step(int(row["id"]), ok=False,
                               error=f"{type(exc).__name__}: {exc}")
                failed += 1
                log.warning("step %s failed: %s", row["id"], exc)
                continue
            self.mark_step(int(row["id"]), ok=True,
                           cost_minor=int(outcome.get("cost_minor") or 0))
            fired += 1
        summary = {"fired": fired, "skipped_late": skipped_late, "failed": failed,
                   "claimed": len(rows)}
        log.info("%s sweep %s", self.HANDLE, summary)
        return summary

    def skip_step(self, step_id: int, reason: str) -> None:
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(f"update {self.step_table} set status = 'skipped', last_error = %s"  # noqa: S608
                            " where id = %s", (reason[:500], step_id))
        except Exception as exc:
            log.warning("skip_step(%s) failed: %s", step_id, exc)

    def expire_stale_sequences(self) -> int:
        """Move sequences whose trial window ended without conversion to 'abandoned'.

        Left armed forever, such a sequence stays in the due index and is re-scanned
        on every sweep while never firing anything, so this is index hygiene as much
        as a state transition.

        `returning id` rather than `cur.rowcount`. WorkerStore's connection runs
        autocommit, which puts psycopg in pipeline mode, and a plain UPDATE that
        promises no rows goes down the simple-protocol path there and desyncs the
        pipeline (observed: the next statement in the transaction died with
        "current transaction is aborted"). Returning makes the extended protocol
        mandatory, so the driver stops guessing. As a bonus the count now comes
        from the rows actually written rather than a protocol-dependent estimate.
        """
        sql = (f"update {self.sequence_table} set status = 'abandoned', updated_at = now()"  # noqa: S608
               " where status in ('pending','armed')"
               "   and opt_in_at + make_interval(days => trial_days) < now()"
               " returning id")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql)
                return len(cur.fetchall())
        except Exception as exc:
            log.warning("expire_stale_sequences failed: %s", exc)
            return 0

    def fire_step(self, step: dict[str, Any]) -> dict[str, Any]:
        """Execute one claimed step. Returns metric deltas and step cost."""
        kind = str(step["kind"])
        payload = step.get("payload") or {}
        visitor_key = self.visitor_key_for(int(step["sequence_id"]))
        identity = f"step:{step['id']}"

        if kind == STEP_CONVERSION:
            # The billing moment. Counted here rather than on the webhook because
            # this row is the one that knows the visitor committed, and it is
            # claimed exactly once. A real Stripe charge is separately reconciled
            # by _on_revenue_event; see the docstring on _on_conversion for why
            # counting only there would understate the funnel.
            if self.dry_run:
                return {}
            self.record_trial_event(visitor_key, "paid",
                                    amount_minor=int(payload.get("amount_minor") or 0),
                                    sequence_id=int(step["sequence_id"]))
            self.mark_converted(int(step["sequence_id"]))
            self.log("info", "funnel.trial.converted",
                     f"visitor {visitor_key[:24]} converted", step_id=int(step["id"]))
            return {"targets_executed": 1}

        if kind not in BILLABLE_STEPS:
            self.log("warning", "funnel.step.unknown_kind", f"unknown step {kind!r}")
            return {}

        provider = f"{kind}_provider"
        url, token = provider_config(kind)
        if not url:
            raise RuntimeError(
                f"{provider.upper()}_URL is unset; refusing to pretend the {kind} was sent")

        body = {
            "to": payload.get("to") or self.contact_for(visitor_key, kind),
            "template": payload.get("template") or kind,
            "sequence_id": int(step["sequence_id"]),
            "identity": identity,
            "vars": dict(payload.get("vars") or {}),
        }
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        price = self.cost_per_upload_minor
        if self.dry_run:
            outcome = self.request_spend(SpendTicket(
                title=f"funnel: {kind} (dry run)", amount_minor=price,
                rationale="dry run: no vendor call made, nothing charged",
                idempotency_key=f"funnel:step:{step['id']}", vendor=kind))
            # A dry run that skips the gate entirely proves nothing about whether
            # the real call would clear tier 1, so it asks and reports.
            self.log("info", "funnel.step.dry_run",
                     f"would send {kind} for {identity} ({outcome.tier}/"
                     f"{outcome.approval_status})", step_id=int(step["id"]))
            return {}

        outcome = self.request_spend(SpendTicket(
            title=f"funnel: {kind} {payload.get('template') or ''}"[:200],
            amount_minor=price,
            rationale=str(payload.get("rationale") or f"{kind} step of a trigger sequence"),
            expected_roi_note="trial-to-paid conversion",
            source_url=payload.get("landing_url"), vendor=kind,
            idempotency_key=f"funnel:step:{step['id']}",
            metadata={"sequence_id": int(step["sequence_id"]), "kind": kind}))
        if not outcome.execute:
            # Queued for a human tap or blocked. Nothing was sent, so nothing is
            # counted, and the step is un-claimed so a later tick finds it.
            raise RuntimeError(f"spend gate held this {kind}: {outcome.reason}")

        with httpx.Client(timeout=self.upload_timeout) as client:
            response = client.post(url, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"{kind} send http {response.status_code}: {response.text[:200]}")

        self.log("info", "funnel.step.sent", f"{kind} sent for {identity}",
                 step_id=int(step["id"]), transaction_id=outcome.transaction_id,
                 cost_minor=price)
        # cost_minor accumulates into hustle_metrics separately by the caller, so
        # the step's own spend is booked by the finance agent, not here.
        return {"cost_minor": price}

    def visitor_key_for(self, sequence_id: int) -> str:
        sql = f"select visitor_key from {self.sequence_table} where id = %s"  # noqa: S608
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (sequence_id,))
            return str((cur.fetchone() or {}).get("visitor_key") or "")

    def contact_for(self, visitor_key: str, kind: str) -> str:
        """Pull the visitor's email or phone off the sequence row's payload."""
        sql = f"select payload from {self.sequence_table} where visitor_key = %s"  # noqa: S608
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (visitor_key,))
                row = cur.fetchone() or {}
                payload = row.get("payload") or {}
                key = "lead_email" if kind == STEP_EMAIL else "lead_phone"
                return str(payload.get(key) or "")
        except Exception as exc:
            log.warning("contact_for(%s) failed: %s", visitor_key[:40], exc)
            return ""

    def mark_converted(self, sequence_id: int) -> None:
        sql = (f"update {self.sequence_table} set status = 'converted', updated_at = now()"  # noqa: S608
               " where id = %s and status <> 'converted'")
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (sequence_id,))

    # ------------------------------------------------------------- trial events

    def record_trial_event(self, visitor_key: str, status: str, *, amount_minor: int = 0,
                           currency: str = "USD", sequence_id: int | None = None,
                           offer_id: str | None = None, idempotency_key: str | None = None,
                           occurred_at: datetime | None = None) -> bool:
        """Append one trial fact. False if already recorded (a redelivery).

        Append-only rather than an update-in-place: "when did this visitor convert"
        and "did they then cancel" are both facts with their own timestamps, and a
        row that gets overwritten loses the first one. The metrics read picks the
        latest status per visitor, so the ladder is expressible without a state
        machine here.
        """
        key = idempotency_key or ":".join(
            [visitor_key, status, offer_id or "", str(sequence_id or "")])
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        sql = (f"insert into {self.trial_table}"  # noqa: S608
               " (visitor_key, funnel_type, offer_id, status, occurred_at, amount_minor,"
               " currency, sequence_id, idempotency_key)"
               " values (%s, %s, %s, %s, coalesce(%s, now()), %s, %s, %s, %s)"
               " on conflict (idempotency_key) where idempotency_key is not null"
               " do nothing")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (visitor_key, "trial", offer_id, status,
                                  occurred_at.astimezone(UTC) if occurred_at else None,
                                  int(amount_minor), norm_currency(currency), sequence_id, digest))
                return (cur.rowcount or 0) > 0
        except Exception as exc:  # a trial fact we cannot write is worth the noise
            log.warning("record_trial_event(%s/%s) failed: %s", visitor_key[:40], status, exc)
            return False

    # -------------------------------------------------------- metrics (job two)

    def refresh_metrics(self) -> dict[str, Any]:
        """Write the funnel's trial-conversion counters into engine.hustle_metrics.

        Second scheduled job, on its own interval, and the reason is the write
        shape. hustle_metrics targets_* columns are per-(day, hustle) integers and
        the only accumulation this table offers is additive (add_metrics) or
        overwrite (set_gauges). Neither is idempotent for a COUNT unless the window
        and the cadence agree.

        So the window is exactly one job interval, and the job stamps which windows
        it has already booked:

        * Window = [now - interval, now). Every trial row is counted in exactly one
          window, so a per-window COUNT summed across days equals the total.
        * Bookkeeping: the last window's end time in worker_state.note. If a tick
          is late or missed, the next one advances to catch up, still booking each
          window once.

        This means FUNNEL_METRICS_INTERVAL_MINUTES and the job trigger are one
        setting, not two. Change one without the other and windows either overlap
        (double counting) or skip (undercounting), which is precisely the failure
        this whole method exists to avoid.
        """
        cursor_key = self.JOB_METRICS
        last_end = self.store.cursor_get(self.HANDLE, cursor_key)
        now = datetime.now(UTC)
        interval = timedelta(minutes=self.metrics_every_minutes)
        try:
            window_start = (datetime.fromisoformat(last_end).astimezone(UTC)
                            if last_end else now - interval)
        except ValueError:
            window_start = now - interval

        totals = {"trials_started": 0, "trials_paid": 0, "trials_cancelled": 0}
        windows = 0
        guard = 0
        # Catch up window by window. Capped so a week-long outage replays at most
        # this many windows here; the rest are recovered by the nightly digest from
        # trial_events, which still holds every row.
        while window_start < now and guard < 200:
            window_end = min(window_start + interval, now)
            counts = self.count_trials_between(window_start, window_end)
            for key, value in counts.items():
                totals[key] += value
            windows += 1
            window_start = window_end
            guard += 1

        if window_start < now:
            # Hit the catch-up cap. Record where we stopped so the next run resumes
            # rather than silently skipping the unscanned range.
            self.store.cursor_put(self.HANDLE, cursor_key,
                                  (window_start - interval).isoformat())
        else:
            self.store.cursor_put(self.HANDLE, cursor_key, now.isoformat())

        deltas: dict[str, int] = {}
        if totals["trials_started"]:
            deltas["targets_discovered"] = totals["trials_started"]
        if totals["trials_paid"]:
            deltas["targets_executed"] = totals["trials_paid"]
        if deltas:
            # add_metrics rejects anything outside its additive allow-list, which
            # is the guardrail that makes this the only legal way to write these.
            self.store.add_metrics(self.HANDLE, self.HUSTLE, deltas)

        summary = {"windows": windows, **totals, **deltas}
        log.info("%s metrics %s", self.HANDLE, summary)
        self.log("info", "funnel.metrics.refreshed",
                 f"{totals['trials_started']} trials started, {totals['trials_paid']} paid",
                 windows=windows, **totals)
        return summary

    def count_trials_between(self, start: datetime, end: datetime) -> dict[str, int]:
        """Count trial events in a half-open window, de-duplicated per visitor.

        DISTINCT ON per visitor_key picks the latest event of the window, so a
        visitor who starts and converts in the same window counts once as started
        and once as paid, not twice as paid.
        """
        sql = f"""
            select
              count(*) filter (where status = 'started')   as started,
              count(*) filter (where status = 'paid')      as paid,
              count(*) filter (where status = 'cancelled') as cancelled
            from (
              select distinct on (visitor_key) visitor_key, status
                from {self.trial_table}
               where occurred_at >= %s and occurred_at < %s
               order by visitor_key, occurred_at desc, id desc
            ) latest
        """  # noqa: S608 - table names are validated constants; all values are bind params
        with self.store._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (start, end))
            row = cur.fetchone() or {}
        return {"trials_started": int(row.get("started") or 0),
                "trials_paid": int(row.get("paid") or 0),
                "trials_cancelled": int(row.get("cancelled") or 0)}

    # ------------------------------------------------------------- queue (job 1)

    def queue_tick(self) -> dict[str, Any]:
        """Drain the intake queue: revenue events, conversion uploads, triggers."""
        self.ensure_tables()
        self.ingest_stripe_backlog()
        now = datetime.now(UTC)
        counts: dict[str, int] = {}
        for kind, max_age in (
            (EVENT_SOURCE, None),
            (CONVERSION_SOURCE, self.conversion_max_age),
            (TRIGGER_SOURCE, timedelta(seconds=self.max_age_seconds)),
        ):
            leads = self.pending(kind, self.batch_size, max_age, now)
            done = 0
            for lead in leads:
                if lead.age_seconds(now) > self.max_age_seconds and kind == TRIGGER_SOURCE:
                    # Freshness matters for things we pay for and not for things
                    # we only record, so only the third source gets filtered here.
                    self.finish(lead, ok=True)
                    continue
                try:
                    self.handle(lead)
                    done += 1
                except Exception as exc:
                    self.finish(lead, ok=False, error=f"{type(exc).__name__}: {exc}")
                    self.log("error", "worker.lead.failed",
                             f"{lead.source}:{lead.identity[:60]}: {type(exc).__name__}: {exc}")
            counts[kind] = done
        log.info("%s queue tick %s", self.HANDLE, counts)
        return counts

    def ingest_stripe_backlog(self) -> int:
        """Mirror funnel Stripe events into the queue from the ledger's own rows.

        Reads events the finance agent already landed in engine.transactions, so
        this worker does not open its own Stripe endpoint and cannot disagree with
        the ledger about what revenue exists.
        """
        sql = ("select transaction_id::text as id, amount_minor, currency, title,"
               " external_id, created_at from engine.transactions "
               "where source = 'stripe_webhook' and direction = 'revenue' "
               "and hustle = 'funnel' and created_at > now() - interval '3 days' "
               "order by created_at desc limit %s")
        try:
            with self.store._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (self.batch_size * 4,))
                rows = cur.fetchall()
        except Exception as exc:  # no mirrored rows yet is normal on a fresh database
            log.debug("stripe backlog read skipped: %s", exc)
            return 0
        inserted = 0
        for row in rows:
            if self.enqueue(EVENT_SOURCE, f"rev:{row['id']}", {
                    "event": "subscription.revenue", "transaction_id": row["id"],
                    "amount_minor": int(row["amount_minor"]), "currency": row["currency"],
                    "title": row["title"]}):
                inserted += 1
        return inserted

    def handle(self, lead: Lead) -> dict[str, Any] | None:
        """Dispatch one queued lead by kind. One lead, one outcome."""
        handler = {
            EVENT_SOURCE: self._on_revenue_event,
            CONVERSION_SOURCE: self._on_conversion,
            TRIGGER_SOURCE: self._on_trigger,
        }.get(lead.source)
        if handler is None:
            self.log("warning", "worker.lead.unknown_kind",
                     f"no handler for kind {lead.source!r}", kind=lead.source)
            self.finish(lead, ok=True)
            return None
        try:
            result = handler(lead)
        except Exception as exc:  # mark the row failed, then let the caller count it
            self.finish(lead, ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        self.finish(lead, ok=True)
        if result:
            deltas = {k: v for k, v in result.items() if k != "cost_minor"}
            if int(result.get("cost_minor") or 0):
                deltas["cost_minor"] = int(result["cost_minor"])
            if deltas:
                self.store.add_metrics(self.HANDLE, self.HUSTLE, deltas)
        return result

    def _on_revenue_event(self, lead: Lead) -> dict[str, Any]:
        """Book subscription revenue into today's rollup, once per event."""
        transaction_id = str(lead.note.get("transaction_id") or lead.identity)
        if transaction_id in self._booked:
            return {}
        self._booked[transaction_id] = time.time()
        if len(self._booked) > 5000:  # a worker runs for weeks; this set never shrinks itself
            self._booked.pop(next(iter(self._booked)))
        amount = int(lead.note.get("amount_minor") or 0)
        self.log("info", "funnel.revenue.noted",
                 f"{fmt_title(lead)}", transaction_id=transaction_id)
        metrics: dict[str, int] = {"revenue_minor": amount}
        event_kind = str(lead.note.get("event") or "")
        if "cancel" in event_kind:
            metrics["cancellations"] = 1
            self.record_trial_event(transaction_id, "cancelled", amount_minor=amount)
        else:
            # A renewal and a new signup both grow the paying base; only a
            # cancellation reduces it. new_subscriptions is incremented for
            # anything that is not a cancellation because the mirrored row does
            # not carry Stripe's distinction, and over-counting new undersells
            # churn-neutral growth rather than hiding it.
            metrics["new_subscriptions"] = 1
            self.record_trial_event(transaction_id, "paid", amount_minor=amount)
        return metrics

    def _on_conversion(self, lead: Lead) -> dict[str, Any] | None:
        """Upload a delayed conversion to the ad platform.

        The highest-value branch in this file. An offline conversion upload is
        what tells the bidding algorithm what a buyer looks like, and it decays.
        If the upload endpoint is unconfigured we do NOT silently succeed, because
        a funnel that looks healthy while uploading nothing is the specific failure
        mode where spend continues and nobody learns why ROAS fell.
        """
        if not self.upload_url:
            self.log("warning", "funnel.capi.unconfigured",
                     "FUNNEL_CAPI_URL unset: conversion NOT uploaded", identity=lead.identity)
            return None
        body = {
            "event_name": lead.note.get("event_name") or "Purchase",
            "event_time": int(lead.created_at.timestamp()),
            "action_source": "website",
            "event_id": lead.identity,
            "user_data": dict(lead.note.get("user_data") or {}),
            "custom_data": {k: lead.note[k] for k in ("value", "currency") if k in lead.note},
        }
        headers = {"Content-Type": "application/json"}
        if self.upload_token:
            headers["Authorization"] = f"Bearer {self.upload_token}"
        if self.dry_run:
            self.log("info", "funnel.conversion.dry_run",
                     f"would upload {lead.identity}", event_name=body["event_name"])
            return None
        with httpx.Client(timeout=self.upload_timeout) as client:
            response = client.post(self.upload_url, json=body, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"capi upload http {response.status_code}: {response.text[:200]}")
        cost = self.cost_per_upload_minor
        self.log("info", "funnel.conversion.uploaded",
                 f"uploaded {lead.identity} aged {lead.age_seconds()}s",
                 event_name=body["event_name"], cost_minor=cost)
        return {"targets_executed": 1, "cost_minor": cost}

    def _on_trigger(self, lead: Lead) -> dict[str, Any] | None:
        """A page trigger that costs money, or arms a sequence. Ask the gate first."""
        action = str(lead.note.get("action") or "trigger")

        if action == "arm_sequence":
            # Free to persist, so it does not need the gate. The paid steps it
            # creates are each gated individually when they come due.
            sequence_id = self.arm(
                visitor_key=str(lead.note.get("visitor_key") or lead.identity),
                template=str(lead.note.get("template") or "trial_default"),
                funnel_type=str(lead.note.get("funnel_type") or "trial"),
                offer_id=lead.note.get("offer_id"),
                landing_url=lead.note.get("landing_url") or lead.note.get("source_url"),
                lead_email=lead.note.get("email"), lead_phone=lead.note.get("phone"),
                currency=lead.currency,
                trial_amount_minor=int(lead.note.get("trial_amount_minor") or 0),
                rebill_amount_minor=int(lead.note.get("rebill_amount_minor") or 0),
                trial_days=int(lead.note.get("trial_days") or 7),
                opt_in_at=lead.created_at,
                started=bool(lead.note.get("started", True)))
            return {"targets_discovered": 1} if sequence_id else None

        price = int(lead.note.get("amount_minor") or lead.expected_value_minor
                    or self.autocharge_amount_minor)
        ticket = SpendTicket(
            title=f"funnel: {action} for {lead.identity[:40]}",
            amount_minor=price, currency=lead.currency,
            rationale=str(lead.note.get("rationale") or f"page trigger {action}"),
            expected_roi_note=str(lead.note.get("expected_roi") or "negative-option LTV"),
            source_url=lead.note.get("landing_url"),
            vendor=lead.note.get("vendor") or "funnel-stack",
            idempotency_key=f"funnel:{lead.identity_hash()}",
            metadata={"trigger": action, "lead_identity": lead.identity[:200]})
        if self.dry_run:
            self.log("info", "funnel.trigger.dry_run",
                     f"would request {price} minor for {action}", identity=lead.identity)
            return {"targets_discovered": 1}
        outcome = self.request_spend(ticket)
        if not outcome.execute:
            # Queued for a tap, or blocked by the envelope/tier. Nothing was paid,
            # so only discovery is counted; the cost column stays honest about
            # money that actually moved.
            return {"targets_discovered": 1}
        self.log("info", "funnel.trigger.executed",
                 f"{action} fired, txn {str(outcome.transaction_id)[:8]}",
                 transaction_id=outcome.transaction_id)
        return {"targets_executed": 1, "cost_minor": price}

    # ------------------------------------------------------------------ spend

    def request_spend(self, ticket: SpendTicket):
        """Ask the gate. Raises if the gate is unreachable.

        Duplicated from BaseWorker rather than inherited, because this class does
        not subclass it. Keeping the copy thin (delegate to the gateway, log the
        verdict) is what prevents the two stacks drifting into two implementations
        of the tier rule.
        """
        if not self.gateway.configured:
            raise SpendGatewayUnset(
                "FINANCE_AGENT_URL is unset; refusing to spend ungated. "
                "Set FINANCE_AGENT_URL=http://finance-gatekeeper:3200")
        outcome = self.gateway.request_spend(ticket, self.HANDLE, self.HUSTLE)
        self.log("warning" if outcome.blocked else "info", "worker.spend.gated",
                 f"{ticket.title} -> {outcome.tier}/{outcome.approval_status} ({outcome.reason})",
                 is_audit=True, transaction_id=outcome.transaction_id,
                 amount_minor=ticket.amount_minor, tier=outcome.tier,
                 approval_status=outcome.approval_status, duplicate=outcome.duplicate)
        return outcome

    def log(self, level: str, event: str, message: str, *, is_audit: bool = False,
            **extra: Any) -> None:
        self.store.log(self.HANDLE, level, event, message, hustle=self.HUSTLE,
                       is_audit=is_audit, extra=extra or None)

    # -------------------------------------------------------------- scheduling

    def build_scheduler(self) -> BlockingScheduler:
        """Wire the two jobs. The job store is Postgres, not memory.

        That choice is the whole reason APScheduler earns its dependency here. A
        MemoryJobStore would lose an armed sequence on every deploy; with the
        SQLAlchemy store backed by the same database, a job scheduled before a
        restart is still there after it. Misfire handling is set generously
        because a sequence step five minutes late is still wanted, and
        _on_listener logs the misses rather than swallowing them, so a restart that
        dropped a window is visible instead of quietly costing conversions.
        """
        store_backend = env("WORKER_JOB_STORE", "sqlalchemy").lower()
        jobstore: Any
        if store_backend in ("memory", "null"):
            # A memory store means an armed sequence dies with the process. Fine
            # for a laptop and for --once; a deploy on it will silently lose
            # ladders on every restart, so it says so out loud rather than tuning
            # it back on later and being surprised.
            jobstore = None
            log.warning("WORKER_JOB_STORE=%s: armed sequences will NOT survive a restart",
                        store_backend)
        else:
            # Imported here, not at module scope. Two reasons, both real: `--once`
            # and `--help` need no job store at all, so a missing driver should not
            # stop a cron pass or a help screen; and an ImportError raised here
            # carries a message naming the fix, whereas the one raised at import
            # time lands as a traceback on stderr that a supervisor restart loop
            # will dutifully repeat forever.
            try:
                from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
            except ImportError as exc:
                raise RuntimeError(
                    "the funnel worker's job store needs SQLAlchemy so that armed"
                    " sequences survive a restart. Install it"
                    " (pip install 'apscheduler[sqlalchemy]') or set"
                    " WORKER_JOB_STORE=memory to accept losing them."
                ) from exc
            jobstore = SQLAlchemyJobStore(url=jobstore_url_from(self.database_url))
        scheduler = BlockingScheduler(timezone="UTC", job_defaults={
            # Coalesce a backlog of missed runs into one catch-up execution: ten
            # missed sweeps should produce one sweep, not ten identical ones.
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": int(self.tick_seconds * 3),
        })
        if jobstore is not None:
            scheduler.add_jobstore(jobstore, "default")

        scheduler.add_job(self.queue_tick, IntervalTrigger(seconds=self.tick_seconds),
                          id=self.JOB_QUEUE, name="funnel queue drain",
                          replace_existing=True, next_run_time=datetime.now(UTC))
        scheduler.add_job(self.sweep, IntervalTrigger(seconds=_int_env(
                              "FUNNEL_SWEEP_SECONDS", DEFAULT_SWEEP_SECONDS)),
                          id=self.JOB_SWEEP, name="funnel sequence tick",
                          replace_existing=True, next_run_time=datetime.now(UTC))
        scheduler.add_job(self.refresh_metrics,
                          CronTrigger.from_crontab(env("FUNNEL_METRICS_CRON")
                                                   or f"*/{self.metrics_every_minutes} * * * *",
                                                   timezone="UTC"),
                          id=self.JOB_METRICS, name="funnel metrics refresh",
                          replace_existing=True)
        scheduler.add_listener(self._on_listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
        self._scheduler = scheduler
        return scheduler

    def _on_listener(self, event: Any) -> None:
        """Log scheduler-level failures into agent_logs, where operators look.

        APScheduler swallows a job's exception into this callback. If it stays in
        stderr, a job that has failed every run for a week still reports as
        healthy on the dashboard, which is the failure mode this whole file is
        organised around avoiding.
        """
        if getattr(event, "exception", None):
            log.error("job %s raised: %s", event.job_id, event.exception)
            self.log("error", "worker.job.failed",
                     f"{event.job_id}: {type(event.exception).__name__}: {event.exception}",
                     job_id=event.job_id)
        else:
            log.warning("job %s missed its run window", event.job_id)
            self.log("warning", "worker.job.missed",
                     f"{event.job_id} missed its window (was the process down?)",
                     job_id=event.job_id)

    def run(self) -> None:
        """Start the scheduler and block. SIGTERM/SIGINT shut it down cleanly."""
        self.ensure_tables()
        if not self.gateway.configured:
            # Loud at boot, not at the first purchase, which may be hours away.
            log.warning("FINANCE_AGENT_URL unset: this worker can collect but cannot spend")
        scheduler = self.build_scheduler()
        log.info("worker %s starting | hustle=%s queue=%ss sweep=%ss metrics=%smin dry_run=%s",
                 self.HANDLE, self.HUSTLE, self.tick_seconds,
                 _int_env("FUNNEL_SWEEP_SECONDS", DEFAULT_SWEEP_SECONDS),
                 self.metrics_every_minutes, self.dry_run)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.close()

    def run_once(self) -> dict[str, Any]:
        """One pass of everything, synchronously. For cron, smoke tests, CI."""
        self.ensure_tables()
        queue = self.queue_tick()
        sequence = self.sweep()
        metrics = self.refresh_metrics()
        return {"queue": queue, "sequence": sequence, "metrics": metrics}


class SpendGatewayUnset(RuntimeError):
    """No gate configured, so no spend is authorised. Refuse rather than assume."""


def norm_currency(raw: Any) -> str:
    """Normalise any caller's currency to a bare upper-case 3-letter code.

    The columns are `text` (see ensure_tables: a char(3) currency CHECK rejects its
    own DEFAULT because of pad bytes), so nothing here is forced by the column
    width. This is about the *value*, not the storage: a caller that sends
    `"usd "`, `"Usd"` or a stray `"us dollars"` should not get an inconsistent mix
    of spellings across funnel_sequences and trial_events. Upper-case + strip
    non-letters keeps those two tables agreeing, and the `if len != 3 -> "USD"`
    guard stops a malformed code from silently writing, say, "US" and splitting
    the funnel stats across two currencies that are really one.
    """
    cleaned = re.sub(r"[^A-Za-z]", "", str(raw or ""))[:3].upper()
    return cleaned if len(cleaned) == 3 else "USD"


def provider_config(kind: str) -> tuple[str, str]:
    """(url, token) for an SMS/email provider. Unknown kinds resolve to empty."""
    prefix = {"sms": "FUNNEL_SMS", "email": "FUNNEL_EMAIL"}.get(kind)
    if not prefix:
        return "", ""
    return env(f"{prefix}_URL"), env(f"{prefix}_TOKEN")


def jobstore_url_from(database_url: str) -> str:
    """Turn a psycopg DSN into an SQLAlchemy URL the APScheduler store accepts.

    `postgresql+psycopg://` is the psycopg3 dialect, and matters here: the default
    `postgresql://` resolves to psycopg2, which this image does not install, so the
    store would fail to open at boot rather than at the first restart.
    """
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://"):]
    if database_url.startswith("postgresql+psycopg://") or "://" in database_url:
        return database_url
    raise ValueError("cannot derive an SQLAlchemy URL from the configured database URL")


#: Trigger-sequence ladders. Every offset is ABSOLUTE from sequence.scheduled_at
#: (= opt-in time): `after_days: 7` is day 7 of that visitor's trial, not 7 days
#: after the previous step. Offsets are minutes/hours/days rather than a wall
#: clock so a ladder holds whatever timezone the visitor is in.
TEMPLATES: dict[str, list[dict[str, Any]]] = {
    # Spec Hustle A: a low-cost 7-day trial. The first SMS goes out while the
    # visitor is still on the page (minute 0 is the difference between an SMS they
    # connect to the offer and one they do not), then a spaced ladder, then the
    # billing moment on day 7. The pre-bill SMS on day 5 is the negative-option
    # disclosure: late enough to be actionable, early enough that a customer can
    # still cancel before the charge, which is what keeps disputes off the account.
    "trial_default": [
        {"kind": "sms", "after_minutes": 0, "template": "trial_confirm",
         "rationale": "immediate opt-in confirmation; also the receipt-style proof"},
        {"kind": "email", "after_minutes": 5, "template": "trial_welcome",
         "rationale": "delivery of the thing they signed up for"},
        {"kind": "email", "after_days": 3, "template": "trial_value",
         "rationale": "mid-trial value nudge before the bill"},
        {"kind": "sms", "after_days": 5, "template": "trial_upcoming_bill",
         "rationale": "pre-bill notice; the disclosure that keeps disputes off the account"},
        {"kind": "email", "after_days": 6, "template": "trial_last_chance",
         "rationale": "final cancellation prompt before charge"},
        {"kind": "conversion", "after_days": 7, "rationale": "trial converts to paid"},
    ],
    # Paid at the front door, so the ladder is about the rebill, not the trial.
    "tripwire_default": [
        {"kind": "email", "after_minutes": 0, "template": "order_receipt"},
        {"kind": "email", "after_days": 2, "template": "upsell"},
        {"kind": "conversion", "after_days": 3, "rationale": "tripwire upsell accepted"},
    ],
    # Nothing but the welcome and the disclosure: for offers where the whole point
    # is minimum contact.
    "minimal": [
        {"kind": "email", "after_minutes": 0, "template": "welcome"},
    ],
}


def fmt_title(lead: Lead) -> str:
    """Human label for a revenue lead, money included.

    Kept local rather than imported from common: this line is read by a human at
    02:00 comparing the funnel against the dashboard, and a bare "1900" next to
    what is actually USD 19.00 invites the wrong decision.
    """
    try:
        from .common import fmt_minor
    except ImportError:  # pragma: no cover - flat-module fallback
        from common import fmt_minor  # type: ignore[no-redef]
    note = lead.note or {}
    amount = int(note.get("amount_minor") or 0)
    label = str(note.get("title") or lead.identity)[:80]
    return f"{label} {fmt_minor(amount, str(note.get('currency') or 'USD'))}"


def main(argv: list[str] | None = None) -> int:
    """Bootstrap. Note the argument order: common.main_entry is
    (worker_class, description, argv) and assumes a BaseWorker subclass with
    run()/tick_once(); this class has run_once() and a scheduler, so the wiring is
    done here and main_entry's argparse helpers are reused instead.
    """
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="workers.funnel_worker",
        description="Funnel worker: landing-page trigger sequences and trial metrics.",
        epilog="All configuration comes from the environment (.env is read for you).")
    parser.add_argument("--once", action="store_true",
                        help="run one pass of every job and exit (cron / smoke test)")
    parser.add_argument("--tick-seconds", type=int, default=None,
                        help="override FUNNEL/WORKER_QUEUE_TICK_SECONDS")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="leads per queue drain")
    parser.add_argument("--dry-run", action="store_true",
                        help="take no vendor action; still ask the gate so tier routing is proven")
    parser.add_argument("--arm", metavar="VISITOR_KEY", default=None,
                        help="persist one sequence for VISITOR_KEY and exit (smoke test)")
    parser.add_argument("--template", default="trial_default",
                        help=f"template for --arm: one of {', '.join(sorted(TEMPLATES))}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    dsn = build_database_url()
    if not dsn:
        print("DATABASE_URL (or SUPABASE_DB_PASSWORD) is unset; nowhere to write. "
              "Refusing to start worker:funnel.", file=sys.stderr)
        return 2

    worker = FunnelWorker(
        dsn, build_gateway(),
        tick_seconds=args.tick_seconds
        or _int_env("FUNNEL_QUEUE_TICK_SECONDS", 0)
        or _int_env("WORKER_TICK_SECONDS", 60),
        batch_size=args.batch_size or _int_env("WORKER_BATCH_SIZE", 25),
        freshness_hours=_float_env("WORKER_FRESHNESS_HOURS", 6.0),
        dry_run=args.dry_run)

    if args.arm:
        if args.template not in TEMPLATES:
            print(f"unknown template {args.template!r}; "
                  f"choose one of {', '.join(sorted(TEMPLATES))}", file=sys.stderr)
            return 2
        sequence_id = worker.arm(visitor_key=args.arm, template=args.template)
        print(json.dumps({"armed": sequence_id, "template": args.template}))
        return 0 if sequence_id else 1

    if args.once:
        try:
            print(json.dumps(worker.run_once(), default=str))
        except Exception as exc:
            print(f"pass failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0

    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
