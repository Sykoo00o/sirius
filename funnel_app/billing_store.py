"""SQLite tables Phase 2 needs: subscriptions, renewal notifications, save-offer outcomes.

Three tables, none of which existed in Phase 1, because Phase 1 never moved money:

  * ``funnel_subscriptions`` is the funnel's *own* record of a subscription, keyed by
    the processor's id. It is deliberately a state table rather than an append-only one:
    ``/dashboard`` and ``/account/cancel`` both need "what is this subscription's state
    right now" without replaying a log, and a cancel that has not reached the DB and the
    processor in the same breath is a compliance bug, not a styling one.
  * ``funnel_notifications`` remembers which lifecycle emails went out. The renewal
    notice is due at day −2 (spec §2.1) and the sweep that sends it runs on a timer, so
    without this table a restart re-sends a "your card is about to be charged" email to
    somebody who already read it.
  * ``funnel_save_offer_events`` is the instrumentation spec §2.3(6) asks for. Every
    cancel-event outcome lands here as a stable dotted event name so save-offer
    economics can be computed from rows rather than remembered from vibes.

Why SQLite rather than Supabase again: same argument as ``storage.py``. A lane-C
experiment must not own a production migration before it has traffic, and the column
names below line up with what ``engine.trial_events`` already expects so the lift later
is a migration rather than a redesign.

State lives here for display. The processor is the authority on money: ``current_period_
end`` is copied from the Stripe subscription, never guessed locally, because the renewal
date is a disclosure and a disclosed date has to be the real one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

#: The states a funnel subscription can be in. Names mirror Stripe's own vocabulary
#: where one exists, because a local invention has to be translated at every webhook
#: boundary and translations are where drift hides.
STATUSES = (
    "trialing",              # paying the entry price, has not rebilled yet
    "active",                # paying the recurring price
    "cancelled_at_period_end",  # the state a one-click cancel sets
    "paused",                # save-offer A taken
    "past_due",              # a rebill declined; grace retry (§2.1) still applies
    "canceled",              # truly over, no more charges coming
)

#: A subscription in any of these states still owes money, so it still gets the
#: pre-billing notice. ``canceled`` and a period-end cancel inside its final period do
#: not: telling someone about a charge that will not happen is its own kind of alarm.
BILLING_STATUSES = ("trialing", "active", "past_due")

_OFFER_OUTCOMES = (
    "pause_selected", "downgrade_selected", "discount_selected",
    "cancel_confirmed", "survey_reason",
)


def utc_now() -> str:
    """Second-resolution UTC ISO-8601, matching ``storage.utc_now``. Day-level display,
    second-resolution storage: a renewal date shown as a day must not straddle a
    midnight because the stored anchor was coarse."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_SCHEMA = """
create table if not exists funnel_subscriptions (
  visitor_key          text    primary key,
  subscription_id      text    not null default '',
  customer_id          text    not null default '',
  email                text    not null default '',
  status               text    not null,
  plan                 text    not null default 'full',
  trial_amount_minor   integer not null default 0,
  rebill_amount_minor  integer not null default 0,
  currency             text    not null default 'usd',
  trial_end            text    not null default '',
  current_period_end   text    not null default '',
  card_brand           text    not null default '',
  card_last4           text    not null default '',
  plan_signature       text    not null default '',
  answers_json         text    not null default '{}',
  coach_note           text    not null default '',
  paused_until         text    not null default '',
  discount_months      integer not null default 0,
  subscription_item_id text    not null default '',
  cancelled_at         text    not null default '',
  save_offer_outcome   text    not null default '',
  created_at           text    not null,
  updated_at           text    not null
);

create table if not exists funnel_notifications (
  id              integer primary key autoincrement,
  visitor_key     text    not null,
  kind            text    not null,
  sent_at         text    not null,
  detail          text    not null default '',
  unique (visitor_key, kind)
);

create index if not exists funnel_notifications_kind_idx
  on funnel_notifications (kind, sent_at);

create table if not exists funnel_save_offer_events (
  id           integer primary key autoincrement,
  visitor_key  text    not null,
  event        text    not null,
  occurred_at  text    not null,
  detail_json  text    not null default '{}'
);

create index if not exists funnel_save_offer_events_event_idx
  on funnel_save_offer_events (event, occurred_at);
"""


class BillingStore:
    """Thread-safe wrapper over one connection per call.

    Same reasoning as ``ResponseStore``: FastAPI runs the sync handlers on a threadpool,
    and a shared connection is the first thing to break under two tabs.
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

    # ---- subscriptions -----------------------------------------------------

    def upsert_subscription(self, *, visitor_key: str, **fields: Any) -> dict[str, Any]:
        """Insert or update the visitor's subscription row, returning the stored row.

        Partial update on purpose: the webhook knows the status, the checkout handler
        knows the email, and neither should have to supply the other's columns or wipe
        them. Unknown column names raise rather than being ignored, because a typo in a
        column name here silently drops a billing field, and a silently dropped billing
        field is how a renewal date goes missing on the account page.
        """
        known = {
            "subscription_id", "customer_id", "email", "status", "plan",
            "trial_amount_minor", "rebill_amount_minor", "currency", "trial_end",
            "current_period_end", "card_brand", "card_last4", "plan_signature",
            "answers_json", "coach_note", "paused_until", "discount_months",
            "subscription_item_id", "cancelled_at", "save_offer_outcome",
        }
        unexpected = set(fields) - known
        if unexpected:
            raise ValueError(f"unknown subscription field(s): {sorted(unexpected)}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']!r}, expected one of {STATUSES}")
        now = utc_now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "select * from funnel_subscriptions where visitor_key = ?",
                (visitor_key,),
            ).fetchone()
            if existing is None:
                row = {"visitor_key": visitor_key, "created_at": now, "updated_at": now}
                row.update(fields)
                if "status" not in row:
                    raise ValueError("status is required when creating a subscription row")
                columns = ", ".join(row)
                conn.execute(
                    f"insert into funnel_subscriptions ({columns}) "
                    f"values ({', '.join('?' for _ in row)})",
                    tuple(row.values()),
                )
            else:
                if fields:
                    assignments = ", ".join(f"{key} = ?" for key in fields)
                    conn.execute(
                        f"update funnel_subscriptions set {assignments}, updated_at = ? "
                        "where visitor_key = ?",
                        (*fields.values(), now, visitor_key),
                    )
        row_obj = self.get_subscription(visitor_key)
        assert row_obj is not None  # the write above guarantees it
        return row_obj

    def get_subscription(self, visitor_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "select * from funnel_subscriptions where visitor_key = ?",
                (visitor_key,),
            ).fetchone()
        return dict(row) if row else None

    def find_by_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        """Reverse lookup, for the webhook when metadata has no visitor key: a subscription
        created in the dashboard, or a re-delivery of an event whose metadata we never
        controlled. An empty id matches nothing rather than matching the first row."""
        if not subscription_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "select * from funnel_subscriptions where subscription_id = ? "
                "order by updated_at desc limit 1",
                (subscription_id,),
            ).fetchone()
        return dict(row) if row else None

    def clear_notification(self, visitor_key: str, kind: str) -> bool:
        """Forget that ``kind`` was sent to this visitor. True when a marker was removed.

        Exists for one situation: the same subscription paying for a *new* trial period. A
        dedupe marker keyed on (visitor, kind) cannot tell "this customer's second trial" from
        "the same trial, twice", and suppressing the second receipt to avoid a duplicate would
        lose the only disclosure record that second charge ever gets.
        """
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "delete from funnel_notifications where visitor_key = ? and kind = ?",
                (visitor_key, kind),
            )
            return cursor.rowcount > 0

    def due_notifications(self, kind: str, *, now_iso: str) -> list[dict[str, Any]]:
        """Subscriptions whose ``kind`` notice is due and not yet sent.

        ``kind`` names which timestamp to read and which status set qualifies, so the one
        caller that schedules this (the renewal sweep) does not pass a half-matching pair
        of arguments. A blank timestamp means "this subscription has no known date", and
        is skipped rather than treated as due: an email saying "you will be charged soon"
        with no date behind it is noise, not notice.
        """
        date_column, statuses = {
            "renewal_reminder": ("current_period_end", BILLING_STATUSES),
            "trial_expiry": ("trial_end", ("trialing",)),
        }[kind]
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""select s.* from funnel_subscriptions s
                    left join funnel_notifications n
                      on n.visitor_key = s.visitor_key and n.kind = ?
                    where s.status in ({placeholders})
                      and n.id is null
                      and s.{date_column} != ''
                      and s.{date_column} <= ?""",
                (kind, *statuses, now_iso),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- notifications -----------------------------------------------------

    def mark_notification_sent(self, *, visitor_key: str, kind: str,
                               detail: str = "") -> bool:
        """Record a send. True when this call recorded it, False when it was already
        recorded, which is the caller's signal that the email was already sent and
        must not be sent twice after a restart."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """insert or ignore into funnel_notifications (visitor_key, kind, sent_at, detail)
                   values (?, ?, ?, ?)""",
                (visitor_key, kind, utc_now(), detail[:300]),
            )
            return cursor.rowcount > 0

    def notifications_sent(self, visitor_key: str) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "select kind from funnel_notifications where visitor_key = ?",
                (visitor_key,),
            ).fetchall()
        return [row["kind"] for row in rows]

    # ---- save-offer events -------------------------------------------------

    def log_event(self, *, visitor_key: str, event: str, **detail: Any) -> None:
        """Log one save-offer outcome. Raises on an unknown event name rather than
        storing a typo: the whole value of this table is that the event names are
        stable enough to group by years later."""
        if event not in _OFFER_OUTCOMES:
            raise ValueError(f"unknown save-offer event {event!r}, expected one of {_OFFER_OUTCOMES}")
        with self._lock, self._connect() as conn:
            conn.execute(
                "insert into funnel_save_offer_events (visitor_key, event, occurred_at, detail_json) "
                "values (?, ?, ?, ?)",
                (visitor_key, event, utc_now(),
                 json.dumps(detail, ensure_ascii=False, default=str)),
            )

    def event_counts(self) -> dict[str, int]:
        """Outcome counts for the cancel screen and `/health`."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "select event, count(*) as n from funnel_save_offer_events group by event"
            ).fetchall()
        return {row["event"]: int(row["n"]) for row in rows}

    def subscription_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "select status, count(*) as n from funnel_subscriptions group by status"
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}


def iso_from_epoch(seconds: int | float | None) -> str:
    """Stripe returns unix timestamps; this repo stores ISO. Kept here so the mapping
    happens in exactly one place, and a missing timestamp stores as '' (unknown) rather
    than 1970-01-01, which would read as an overdue charge from fifty years ago."""
    if not seconds:
        return ""
    return datetime.fromtimestamp(int(seconds), timezone.utc).replace(
        microsecond=0).isoformat()


def human_date(iso: str | date) -> str:
    """Day-level, spelled out, for disclosure copy: 'Monday 08 December'.

    Takes the ISO strings this module stores *or* a `date`, and that is not flexibility for
    its own sake: `funnel_copy.human_date` takes a date, this one took a string, both were
    imported bare into modules that also import each other, and a caller that picked up the
    wrong one by name got a `TypeError` from inside `accept_offer`'s rescue clause, which then
    told a customer their successful pause had failed. One tolerant entry point removes the
    class of mistake rather than the instance of it.

    A blank or unparseable input returns an empty string, and the templates are written so an
    empty date falls back to the *estimated* date rather than rendering a sentence with a hole
    in it. A renewal date is a disclosure; a missing one must degrade to the honest
    approximation, never to a blank.
    """
    if isinstance(iso, datetime):
        parsed = iso
    elif isinstance(iso, date):
        # A bare date has no zone, and reading `strftime` off it directly is what avoids
        # shifting a month-end across the UTC boundary into the neighbouring day.
        return iso.strftime("%A %d %B %Y")
    else:
        if not iso:
            return ""
        try:
            parsed = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return ""
    return parsed.strftime("%A %d %B %Y")


def plan_label(plan: str, *, discounted: bool = False) -> str:
    """Words for a subscription's plan, shared by the dashboard, the cancel screen and the
    cancellation email.

    One function because a paid member reading three names for the thing they bought is how
    someone ends up emailing "which plan am I actually on?", and because the lite tier has to
    be described identically on the screen that sold it and the screen that bills for it.
    """
    base = "lite plan" if plan == "lite" else "full plan"
    return f"{base}, half price" if discounted else base
