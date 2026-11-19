"""SQLite persistence for quiz responses. Deliberately not Postgres, in Phase 1.

Why a local file rather than the shared `engine` schema: Phase 0 says one rollup per
lane, and `engine.hustle_metrics` is that rollup. Writing a per-visitor table into the
engine schema before the funnel has traffic means a throwaway experiment owns a
production migration. So Phase 1 keeps responses in SQLite, and the row shape below
matches what the engine's per-visitor table already expects:

  * `visitor_key` answers to `engine.trial_events.visitor_key`, so the funnel's cohort
    history can be back-filled into `trial_events` in Phase 2 without a rename.
  * `offer_id` is the variant label slot (`trial_events.offer_id` is where test
    variables live), fixed at `rank4-routine-v1` for the baseline. One offer, one
    variant, one price (`LANE_C_PHASE0_STEP1-2.md` §2).
  * `status` uses the same vocabulary as `trial_events`: started, paid, cancelled.

`append_event` inserts, it never updates. A funnel that overwrites state in place loses
the timing data every ratio in the tracker is computed from.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Baseline variant label. Changing the funnel copy or price is a new label, and the
#: label has to change *before* the traffic, not after, or the cohort cannot be split.
OFFER_ID = "rank4-routine-v1"

_STATUSES = ("started", "previewed", "checkout_viewed", "paid", "cancelled")

_SCHEMA = """
create table if not exists funnel_responses (
  id             integer primary key autoincrement,
  visitor_key    text    not null,
  occurred_at    text    not null,
  status         text    not null,
  offer_id       text    not null,
  step           integer not null default 0,
  answered       integer not null default 0,
  answers_json   text    not null default '{}',
  notes_json     text    not null default '{}',
  plan_signature text    not null default '',
  plan_source    text    not null default '',
  coach_source   text    not null default '',
  user_agent     text    not null default '',
  referrer       text    not null default ''
);

create index if not exists funnel_responses_visitor_idx
  on funnel_responses (visitor_key, occurred_at);
create index if not exists funnel_responses_status_idx
  on funnel_responses (status, occurred_at);
"""


def utc_now() -> str:
    """Second-resolution UTC ISO-8601. Day-level display is what the funnel needs, and
    second resolution keeps a UTC-stored anchor from straddling a midnight boundary in
    renewal-date copy."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ResponseStore:
    """Tiny thread-safe SQLite wrapper. One connection per call: FastAPI runs sync
    handlers on a threadpool, and a shared connection would be the first thing to
    break under load."""

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

    def append_event(
        self,
        *,
        visitor_key: str,
        status: str,
        answers: dict[str, Any] | None = None,
        notes: dict[str, Any] | None = None,
        step: int = 0,
        plan_signature: str = "",
        plan_source: str = "",
        coach_source: str = "",
        offer_id: str = OFFER_ID,
        user_agent: str = "",
        referrer: str = "",
    ) -> int:
        """Insert one event row. Raises ValueError on an unknown status rather than
        inventing a fifth value that the tracker's formulas do not know about."""
        if status not in _STATUSES:
            raise ValueError(f"unknown status {status!r}, expected one of {_STATUSES}")
        if not offer_id:
            # An event with no variant label cannot be attributed to a test arm later,
            # which is the one thing the cohort layer exists for.
            raise ValueError("offer_id is required: every row must name its variant")
        answers = answers or {}
        answered = sum(1 for value in answers.values() if value)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """insert into funnel_responses (
                       visitor_key, occurred_at, status, offer_id, step, answered,
                       answers_json, notes_json, plan_signature, plan_source,
                       coach_source, user_agent, referrer
                   ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    visitor_key, utc_now(), status, offer_id, step, answered,
                    json.dumps(answers, ensure_ascii=False),
                    json.dumps(notes or {}, ensure_ascii=False),
                    plan_signature, plan_source, coach_source,
                    user_agent[:300], referrer[:300],
                ),
            )
            return int(cursor.lastrowid or 0)

    def latest_answers(self, visitor_key: str) -> dict[str, Any] | None:
        """Most recent stored answers for a visitor, so a refresh on `/preview` does
        not lose a finished quiz. Notes travel under their own `_note` keys."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """select answers_json, notes_json from funnel_responses
                   where visitor_key = ? and status in ('started', 'previewed')
                   order by id desc limit 1""",
                (visitor_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            answers = json.loads(row["answers_json"] or "{}")
            notes = json.loads(row["notes_json"] or "{}")
        except json.JSONDecodeError:
            return None
        if not answers:
            return None
        for key, value in notes.items():
            if value:
                answers[f"{key}_note"] = [value]
        return answers

    def counts(self) -> dict[str, int]:
        """Funnel counts for the local tracker. Ratios are deliberately *not* computed
        here: the tracker's rule is to store counts and compute rates, since averaging
        stored rates over a zero-denominator day invents a number."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "select status, count(*) as n, count(distinct visitor_key) as visitors"
                " from funnel_responses group by status"
            ).fetchall()
        return {row["status"]: int(row["visitors"]) for row in rows}

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """select visitor_key, occurred_at, status, step, answered,
                          plan_signature, plan_source, coach_source
                   from funnel_responses order by id desc limit ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
