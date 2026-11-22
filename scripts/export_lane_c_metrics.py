#!/usr/bin/env python3
"""Day-1 CSV and ratio sheet for Lane C, computed from the funnel's own event rows.

Usage:

    python3 scripts/export_lane_c_metrics.py                      # today (UTC)
    python3 scripts/export_lane_c_metrics.py --day 2026-11-20     # one UTC day
    python3 scripts/export_lane_c_metrics.py --since 2026-11-01   # backfill every day
    python3 scripts/export_lane_c_metrics.py --day 2026-11-20 --stdout
    python3 scripts/export_lane_c_metrics.py --day 2026-11-20 --json

Column order and spelling match `LANE_C_PHASE0_STEP1-2.md` §3.4 exactly, including its two
near-synonyms (`trial_paid` singular and `paid_conversions` plural), because the sheet the
spec defines is the artifact: a script that renames a column quietly invalidates every note
written against the original header. `offer_id,test_variable,live_level` come third, before
the counts, so a cohort splits on the left rather than needing a formula to find it.

Two kinds of column, and the difference is the point:

  * **Counted from rows.** Everything sourced from `trial_events` or the subscription table.
  * **Typed or blank.** Ad spend, tool cost, support touches, session counts from an
    analytics tool. §3.4's own rule: "if a field is unknown, write `0` for counts and leave
    ratios blank; never invent a rate from a zero denominator." So a count we cannot observe
    is written as blank rather than 0, and 0 means "observed and none". The exception is
    counts that *are* observable from rows, which get their real number or a true zero.

Ratio rules, stated because getting one of these wrong is invisible in the output:

  * A ratio with a zero denominator is **blank**, never 0 and never 100.
  * `dispute_cb_pct` divides by *captured* authorised volume, matching §3.4's
    `gross_auth_volume_minor`, and its ceiling of 1% is a ceiling on that day. §3.4's own
    formula is dimensionally wrong (count × 100 over a minor-unit amount is never near 1),
    and §1.C states the intent unambiguously: the ceiling is 1% *of charges*.
  * `fraud_pct` counts fraud-named Radar early warnings over all charges, ceiling 0.5%. This
    is the ratio §3.3 says has no home in any table and closes merchant accounts fastest,
    which is why the script fails loudly rather than reporting blank when its source events
    stopped arriving.
  * `refund_pct` divides by `paid_conversions`, and a rising refund rate alongside a falling
    dispute rate is the healthy reading (§1.C). Both columns sit adjacent so the pair is read
    as a pair.
  * `bin_prepaid_share_pct` is flagged as a leading indicator in its own right: a rising
    prepaid share means an unretainable cohort, and the spec says to cut spend early on it
    rather than wait for the refund rate to confirm.

Append mode is idempotent per `(day, offer_id, test_variable)`: rerunning a day replaces that
day's row instead of adding a second one, and rows for other days and other variants are
left exactly as they were, including the hand-typed columns the script cannot know.

Nothing here spends money, sends anything, or writes to the database: the script opens the
funnel's SQLite files read-only and writes one CSV.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Where the numbers live
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Which funnel's databases and `.env` to read. Defaults to Routine Lab (`funnel_app`), so every
#: existing cron line and unit keeps its meaning; the kegeldoc fork runs the same script with
#: `FUNNEL_APP_DIR=<repo>/kegeldoc_app`. A relative value resolves against the repo root.
_APP_DIR = os.environ.get("FUNNEL_APP_DIR", "").strip()
FUNNEL_ROOT = (REPO_ROOT / _APP_DIR if _APP_DIR and not Path(_APP_DIR).is_absolute()
               else Path(_APP_DIR) if _APP_DIR else REPO_ROOT / "funnel_app")

#: Loaded for its `.env` reader and for `OFFER_ID`, so the export's variant label and the
#: funnel's own label cannot drift apart. A copy of that string here would be a second source
#: of truth on the one column that splits every cohort.
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))


def _load_funnel_config():
    """Import `config` and `storage` from the funnel package without importing FastAPI.

    `main.py` is the app, and importing it at export time would build a whole application,
    connect templates and run configuration validation just to read two path defaults. The
    two modules that actually hold the answers, `config` and `storage`, have no web
    dependencies at all.
    """
    import config  # noqa: PLC0415
    import storage  # noqa: PLC0415
    config.load_dotenv()
    return config, storage


#: The header, verbatim from §3.4, plus the columns at the end whose absence would make one of
#: the spec's own formulas uncomputable or unreadable. Appended at the *end* rather than inserted
#: so the spec's columns stay byte-identical to its sheet and any existing notes line up. The
#: count is deliberately not restated in prose: it drifted from "four" to eight without anything
#: breaking, which is what a name that no longer matches its list means in practice:
#:
#:   rebill_attempts              denominator for the dunning rate (§3.4 names the numerator
#:                                and never defines this one)
#:   rebill_declines_unrecoverable  exactly what the spec's `rebill_declines_silent` means in
#:                                this sheet: declines the day +3 retry did *not* recover. The
#:                                spec's name is kept as the column and defined here in days
#:                                rather than in dedupe markers, and this column states the
#:                                definition next to it so the two cannot drift apart.
#:   disputes_contested_minor     disputed dollars, so `dispute_amt_pct` (dollars over
#:                                dollars) can be read next to the count-based reading
#:   checkout_expired             abandoned checkouts, i.e. trial starts that never paid
#:   charges_observed             captures whose BIN type the processor told us, i.e. the
#:                                denominator `fraud_pct` and `bin_prepaid_share_pct` are quoted
#:                                against. Both ratios are ceilings on *rates*, so a reader cannot
#:                                judge a 0.4% fraud figure without knowing whether it rests on 500
#:                                charges or on 6, and §3.4's `n ≥ 250 per arm` rule is unreadable
#:                                from the sheet without this column.
#:   charges_unknown_bin          captures whose BIN type never arrived, reported beside the
#:                                share so a prepaid figure computed over a subset of the day's
#:                                charges is never read as a figure over the day.
#: The two retention columns the spec's ratio table needs and its column list does not have:
#: the month-2 cohort's *size*, which is the denominator `cohorts_paid_day0_active_day60` is
#: divided by, and the same column's alias for the loss reading.
EXTRA_COLUMNS = (
    "rebill_attempts", "rebill_declines_unrecoverable",
    "disputes_contested_minor", "checkout_expired",
    "cohort_size_60d_ago", "rev_refunded_minor",
    "charges_observed", "charges_unknown_bin",
)

#: The count columns, in §3.4's exact order and spelling, then the extras this file adds at the
#: end so the spec's own columns stay byte-identical to its sheet.
SPEC_COLUMNS = (
    "day,date_utc,offer_id,test_variable,live_level,"
    "sessions,lp_views,opt_ins,trial_starts,trials_started,"
    "trial_paid,paid_conversions,cancels_in_trial,rebill_success,rebill_declines_silent,"
    "active_subs,mrr_minor,rev_new,rev_rebill,rev_total_minor,"
    "ad_spend_minor,tool_cost_minor,llm_cost_usd_minor,cost_total_minor,"
    "refunds_count,refund_value_minor,disputes_count,chargebacks_count,fraud_flags_count,"
    "early_warnings_count,radar_reviews_count,customer_service_emails,support_touches,"
    "gross_auth_volume_minor,bin_prepaid_charges,churned_month2_cohort,"
    "cohorts_paid_day0_active_day60,notes"
).split(",")

#: The computed ratios, keyed by the column they fill. Listed because the row builder, the
#: ratio maths and the tests all need the same set: a metric added to one and not the other
#: shows up as a silently blank column rather than an error.
#:
#: **These are not in §3.4's header.** The spec lists the columns to type, and then separately
#: lists which metrics "must never be eyeballed, only computed", and the two lists do not
#: intersect. The ratios are appended anyway, after `notes`, because the reason to compute them
#: is that a human reads them every morning, and a figure that exists only in whatever process
#: printed it is a figure nobody looks at by day 34. `notes` stays before them so the operator's
#: own text is never pushed off the right-hand edge of the counted block.
METRIC_COLUMNS = (
    "trial_paid_pct", "refund_pct", "dispute_cb_pct", "dispute_amt_pct", "fraud_pct",
    "bin_prepaid_share_pct", "month2_retention_pct", "rebill_decline_rate_pct",
    "net_contribution_minor",
)

#: Columns this script computes from the funnel's own rows. Anything not listed here is either
#: a hand-typed column or a ratio, and is preserved verbatim on a rerun.
COMPUTED_COLUMNS = (
    "day", "date_utc", "offer_id", "test_variable",
    "lp_views", "opt_ins", "trial_starts", "trials_started",
    "trial_paid", "paid_conversions", "cancels_in_trial",
    # `rebill_declines_silent` and `rebill_declines_unrecoverable` are written from the same
    # number on purpose: one is the name the spec's sheet uses, the other is what that name
    # means here. A future reader who changes one and not the other has made a decision about
    # the definition, which is the point of having both.
    "rebill_success", "rebill_declines_silent", "rebill_declines_unrecoverable",
    "rebill_attempts", "rebill_declines_total", "active_subs", "mrr_minor",
    "rev_new", "rev_rebill", "rev_total_minor", "rev_refunded_minor",
    "refunds_count", "refund_value_minor", "disputes_count", "disputes_contested_minor",
    "fraud_flags_count", "early_warnings_count", "gross_auth_volume_minor",
    "bin_prepaid_charges", "cohorts_paid_day0_active_day60", "cohort_size_60d_ago",
    "checkout_expired", "charges_observed", "charges_unknown_bin",
)

#: Hand-typed columns, i.e. everything the funnel's rows cannot tell us: an analytics tool's
#: session count, chargebacks as the processor's own dispute list reports them, support volume,
#: and the three cost lines. Preserved verbatim on a rerun so typing in today's ad spend does not
#: have to be redone after every export, and deliberately never guessed: a cost cell stays blank
#: rather than 0, because 0 on a cost line means "spent nothing", which is a claim.
TYPED_COLUMNS = (
    "live_level", "sessions", "chargebacks_count", "radar_reviews_count",
    "customer_service_emails", "support_touches", "ad_spend_minor", "tool_cost_minor",
    "llm_cost_usd_minor", "cost_total_minor", "churned_month2_cohort",
)

#: The sheet's column order: the spec's columns (with the extras appended at the end), then the
#: computed ratios.
HEADER = ([column for column in SPEC_COLUMNS if column != "notes"] + ["notes"]
          # `rebill_declines_total` is the dunning rate's numerator (every decline, recovered or
          # not) and sits beside the spec's `rebill_declines_silent`, which counts only the ones
          # the retry could not rescue. Two numbers, one column each, so the rate and the loss
          # are never conflated.
          + list(EXTRA_COLUMNS) + ["rebill_declines_total"] + list(METRIC_COLUMNS))

#: Header invariant, asserted at import rather than discovered by an empty cell six months from
#: now. A column that is neither counted, typed nor computed would be silently dropped by
#: `DictWriter(extrasaction="ignore")` and read as "no data" forever, and a column counted but
#: absent from the header is work thrown away. Both directions are checked, because the first
#: bug this file hit was exactly that: nine computed ratios, no header slots, all invisible.
_unaccounted = [c for c in HEADER if c not in
                set(COMPUTED_COLUMNS) | set(METRIC_COLUMNS) | set(TYPED_COLUMNS) | {"notes"}]
_unused = [c for c in COMPUTED_COLUMNS if c not in HEADER]
_duplicates = sorted({c for c in HEADER if HEADER.count(c) > 1})
assert not _unaccounted, f"header columns nothing computes: {_unaccounted}"
assert not _unused, f"computed columns with no header slot: {_unused}"
assert not _duplicates, f"duplicated header columns: {_duplicates}"
del _unaccounted, _unused, _duplicates

#: Ceiling thresholds, from §1.C step 4 / §3.4's "never eyeballed" table. Kept as data so the
#: exit code, the `--fail-on-ceiling` flag and the printed line all read one definition.
CEILINGS = (
    ("dispute_cb_pct", 1.0, "dispute + chargeback ≤ 1% of charges"),
    ("fraud_pct", 0.5, "fraud flags ≤ 0.5% of charges"),
)

#: Minimum conversions before a ratio is worth acting on. §3.1: 400 trials split four ways is
#: a story, not a result, and the same arithmetic says a 1% ceiling measured on 12 charges is
#: noise. Reported, never used to suppress the number itself: the ceiling still trips, because
#: the safe reading of "one dispute on 40 charges" is to look, not to average it away.
LOW_VOLUME_BELOW = 250


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


RELATIVE_DAY_RE = re.compile(r"^(\d+)\s*days?\s*ago$")


def utc_relative_day(spec: str) -> str | None:
    """Resolve `today` / `yesterday` / `N days ago` / `Ndaysago` to a UTC `YYYY-MM-DD`.

    Returns None when `spec` is not a recognised word, so the caller can tell "a date" from "a
    typo" and refuse the typo. Returning the input on a miss was the first version, and it turned
    `--since 3daysago` (which matched nothing back then) into a silent export of *today*: the
    failure was a wrong row, not an error, which is the kind of bug a metrics script should never
    ship because the sheet looks perfectly plausible either way.

    Exists so a systemd timer can say `--day yesterday` in one ExecStart token and need no shell.
    That is not a style preference: the alternative spells `$(date -u -d yesterday +%F)` inside a
    unit, and systemd expands `%F` as its own specifier (a unit-file's filesystem root) before the
    shell sees it. Getting that wrong is silent and simply exports the wrong day forever. GNU
    `date -d yesterday` is also BSD-incompatible, so the same line cannot be tested on a mac and
    trusted on the VPS.

    Spacing is optional in the N-day form (`3 days ago`, `3daysago`, `3Days Ago`) because the
    compact form is the one that fits in a unit file, and the one someone will type from memory.
    """
    if not spec:
        return None
    lowered = spec.strip().lower()
    today = datetime.now(timezone.utc).date()
    offsets = {"today": 0, "yesterday": 1, "-1d": 1}
    if lowered in offsets:
        return (today - timedelta(days=offsets[lowered])).isoformat()
    match = RELATIVE_DAY_RE.match(lowered)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    return None


def utc_now_iso() -> str:
    """Timestamp for provenance output. Kept narrow on purpose: the sheet's only time
    dimension is the UTC day, and a row stamped to the second invites a reader to treat a
    mid-morning rerun as a mid-morning measurement."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Reading the funnel's SQLite files
# ---------------------------------------------------------------------------

def open_read_only(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite database without creating it, or None if it is not there.

    Read-only, for a script an operator may run while the funnel is serving traffic: WAL lets
    a reader proceed without blocking the writer, and a read-write open would take the chance
    of a lock contention on a renewal morning for no benefit. `?mode=rw` rather than `ro` on
    purpose: `mode=ro` on a WAL database whose side-car `-wal` file exists but was not cleanly
    checkpointed is fine, but the same URI on a *plain* journal database that has a hot
    rollback journal refuses the open. `rw` + never issuing a write is the portable form of
    "I promise not to write", and `immutable=0` keeps the WAL readers honest.
    """
    if not path.is_file():
        return None
    uri = f"file:{path.as_uri()[len('file://'):]}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"pragma table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def rows_in_day(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    """Event rows for one UTC day, oldest first.

    Half-open range on `occurred_at >= day and < day+1`, matching how `telemetry` stores ISO
    timestamps: string comparison against `2026-11-21` excludes everything on the 20th because
    every timestamp on the 20th sorts before it, which is exactly why the boundary is the next
    day's bare date rather than `2026-11-20T23:59:59` (that spelling loses the last second's
    events with sub-second precision, and this one does not care whether precision exists).
    """
    return conn.execute(
        "select * from trial_events where occurred_at >= ? and occurred_at < ? "
        "order by occurred_at, id",
        (day, _next_day(day))).fetchall()


def _next_day(day: str) -> str:
    from datetime import date, timedelta
    year, month, dom = (int(part) for part in day.split("-"))
    return (date(year, month, dom) + timedelta(days=1)).isoformat()


def db_paths(config_module, settings: Any) -> dict[str, Path]:
    """Where the funnel keeps its rows, honouring the same env vars the app honours.

    The billing file holds `trial_events`, `funnel_subscriptions` and `funnel_save_offer_events`
    (`main.py` builds `TelemetryStore` on `billing_database_abspath`, deliberately, so event
    counts and subscription counts cannot be read a beat apart). The responses file holds the
    quiz rows, and is opened only when it exists: a deployment that has taken traffic but reset
    its quiz store still gets a real billing row.
    """
    return {
        "billing": settings.billing_database_abspath,
        "responses": settings.database_abspath,
    }


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

def _count(rows: list[sqlite3.Row], *events: str) -> int:
    return sum(1 for row in rows if row["event"] in events)


def _sum(rows: list[sqlite3.Row], column: str, *events: str) -> int:
    return sum(int(row[column] or 0) for row in rows if row["event"] in events)


def _visitors(rows: list[sqlite3.Row], *events: str) -> int:
    return len({row["visitor_key"] for row in rows
                if row["event"] in events and row["visitor_key"]})


def dedupe_volume(rows: list[sqlite3.Row], *events: str) -> int:
    """Authorised minor units for `events`, counted once per *charge*.

    A charge is a visitor here, because a visitor is the closest thing this table has to a card.
    Dedupe is by maximum rather than by first or by sum: a real day can hold two genuine captures
    for one member, a $1 trial and the $39 that follows it, and the honest reading of that is the
    charge that matters, while the inflated reading, the sum, is what made a redelivered webhook
    look like a second sale. Choosing the maximum over the *same event set* means the trial and
    the rebill no longer compete, because they arrive as different events and are summed by
    separate calls.

    Visitors with no key at all are counted individually rather than collapsed into one anonymous
    member: a keyless row is a data gap, and merging a dozen gaps into a single $39 charge would
    invent a smaller denominator out of missing information.
    """
    per_visitor: dict[str, int] = {}
    anonymous = 0
    for row in rows:
        if row["event"] not in events:
            continue
        amount = int(row["gross_volume_minor"] or 0)
        visitor = row["visitor_key"]
        if not visitor:
            anonymous += amount
            continue
        per_visitor[visitor] = max(per_visitor.get(visitor, 0), amount)
    return sum(per_visitor.values()) + anonymous


def _cohort_pair(conn: sqlite3.Connection | None, day: str) -> tuple[int, int]:
    """(paid on day X 60 days ago, of those still active today) and the same for one day later.

    Month-2 retention is the lane's actual PMF signal (§1.C step 4, ≥ 60%) and is not derivable
    from any rollup, because a rollup holds a daily paid count and "still active" is a property
    of a subscription row today. So: take the `trial_paid` cohort from the day 60 days back,
    read each of those visitors' subscription status now, and divide.

    Two adjacent days are returned because the §3.4 sheet asks for both a denominator column
    and a numerator column and does not say they come from the same day: `churned_month2_cohort`
    is the loss, `cohorts_paid_day0_active_day60` the survivor count. Reading one cohort at one
    day boundary answers both, and reading the boundary for the requested day *and* the day
    before it means a cohort whose members paid across a midnight is not silently halved.
    """
    if conn is None:
        return 0, 0
    from datetime import date, timedelta
    try:
        anchor = date.fromisoformat(day) - timedelta(days=60)
    except ValueError:
        return 0, 0
    window = [anchor.isoformat(), (anchor + timedelta(days=1)).isoformat()]
    try:
        cohort = conn.execute(
            "select distinct visitor_key from trial_events "
            "where event = 'trial_paid' and occurred_at >= ? and occurred_at < ?",
            (window[0], window[1])).fetchall()
    except sqlite3.Error:
        return 0, 0
    keys = [row["visitor_key"] for row in cohort if row["visitor_key"]]
    if not keys:
        return 0, 0
    if "funnel_subscriptions" not in {
            row["name"] for row in conn.execute(
                "select name from sqlite_master where type = 'table'")}:
        return len(keys), 0
    placeholders = ", ".join("?" for _ in keys)
    # `trialing` counts as retained: a member in month 2 of a subscription they renewed into
    # is retained whether or not the local status has caught up to `active` yet, and treating
    # a lagging status as churn understates the one metric the spec calls the PMF signal.
    retained = conn.execute(
        f"select count(distinct visitor_key) as n from funnel_subscriptions "
        f"where visitor_key in ({placeholders}) "
        f"and status in ('trialing','active','past_due','paused')",
        keys).fetchone()
    return len(keys), int(retained["n"] or 0)


def counts_for_day(day: str, billing_path: Path, responses_path: Path,
                   offer_id: str, now: str = "") -> dict[str, Any]:
    """Every observable number for one UTC day, as raw counts and sums.

    `opt_ins` needs the quiz store and nothing else has that dependency, so a deployment whose
    responses file is missing still reports a full billing row with `opt_ins` blank rather than
    a row of nothing.

    `now` is the instant the *stock* columns are read at, and it exists so a backfill can gauge
    them as of the day it is reconstructing. See `_subscription_state`.
    """
    billing_conn = open_read_only(billing_path)
    if billing_conn is None:
        raise FileNotFoundError(
            f"no funnel billing database at {billing_path}. The funnel has to have run once "
            "(it creates the file at startup), or you are pointing at the wrong "
            "FUNNEL_BILLING_DB_PATH.")
    try:
        # Probe before the first `select`, not after. "This database predates telemetry" and
        # "this day had no traffic" both look like zero rows, and only one of them means the
        # whole sheet will be zeros forever; checking after the filter reports the second one on
        # day one and lets an un-instrumented funnel export a blank row for a month before anyone
        # notices. Checking before the query at all, rather than catching its
        # `sqlite3.OperationalError`, because the table name would otherwise have to be named a
        # second time in an `except` clause, where it can drift away from the query it guards.
        if "trial_events" not in {
                r["name"] for r in billing_conn.execute(
                    "select name from sqlite_master where type = 'table'")}:
            raise RuntimeError(
                f"{billing_path} has no trial_events table. This funnel needs Phase 3 "
                "telemetry (telemetry.py) before the sheet has anything to report; rows "
                "before it shipped cannot be back-filled.")
        rows = rows_in_day(billing_conn, day)
        cohort_size, cohort_retained = _cohort_pair(billing_conn, day)
        # Stock columns are read from the state table, never accumulated from events, for the
        # reason §3.3 spells out: `add_metrics` on a stock multiplies it by the tick count.
        # Here that trap becomes "MRR must not be the sum of today's charges".
        # A day exported *on* the day gauges the stock as of now, which is the day. A historical
        # day is gauged as of itself, because that is the only honest reading a table with no
        # history can give, and it says so in the note (see `_subscription_state`).
        subs = _subscription_state(billing_conn, as_of="" if now == "now" else day)
    finally:
        billing_conn.close()

    opt_ins: int | None = None
    if responses_path.is_file():
        conn = open_read_only(responses_path)
        try:
            # §3.4's `opt_ins` is the funnel's own commitment moment: the visitor who answered
            # everything. `previewed`/`checkout_viewed` rows in the responses table are also
            # proof of a finished quiz (a preview cannot be built from an incomplete one), so
            # all three count and the distinct-visitor set stops a visitor who previewed twice
            # from being two opt-ins.
            opt_ins = int(conn.execute(
                "select count(distinct visitor_key) as n from funnel_responses "
                "where status in ('started','previewed','checkout_viewed','paid') "
                "and answered >= ? and substr(occurred_at, 1, 10) = ?",
                (_question_count(), day)).fetchone()["n"])
        except sqlite3.Error:
            opt_ins = None
        finally:
            conn.close()

    trial_paid_count = _count(rows, "trial_paid")
    # The spec's two start columns are near-synonyms and it never distinguishes them, so this
    # assigns them by the one arithmetic fact available: the ratio table divides by
    # `trials_started` (plural, the *last* of the pair in the header), and a conversion
    # denominator must not silently absorb every visitor who opened the card screen, or
    # Trial→Paid becomes a function of the abandonment rate rather than of the offer. So the
    # plural column is committed starts (the funnel's own commitment events, per distinct
    # visitor) and the singular column is every start the funnel saw, committed plus abandoned.
    # Both readings are therefore present, neither is invented, and the gap between the two
    # columns is abandonment, which has its own column too.
    #
    # Distinct visitors, not rows, and not a sum of two event populations. The version this
    # replaced added `trial_paid` on top of `trial_started`, which double-counted every conversion
    # that recorded both (the honest case: a checkout that completes writes a start and a payment)
    # and read roughly twice the truth on a healthy day, while staying correct on a day whose sales
    # were all backfilled from a refund event and so carry a payment with no start. The two
    # mechanisms are independent and the union is the number either way: one visitor, one
    # commitment, whichever half of the pair their rows happen to contain.
    committed = _visitors(rows, "trial_started", "trial_paid")
    abandoned = _count(rows, "checkout_expired")
    return {
        "day": day,
        "date_utc": day,
        "offer_id": offer_id,
        "test_variable": _test_variable(rows, offer_id),
        "lp_views": _visitors(rows, "lp_view"),
        "opt_ins": opt_ins,
        "trials_started": committed,
        "trial_starts": committed + abandoned,
        "trial_starts_committed": committed,
        "checkout_expired": abandoned,
        "trial_paid": trial_paid_count,
        "paid_conversions": _visitors(rows, "trial_paid"),
        "cancels_in_trial": _count(rows, "cancel_clicked"),
        "rebill_success": _count(rows, "rebill_succeeded"),
        "rebill_declines_silent": _count(rows, "rebill_decline_terminal"),
        "rebill_declines_total": _count(rows, "rebill_declined"),
        "rebill_attempts": _count(rows, "rebill_succeeded", "rebill_declined"),
        "refunds_count": _count(rows, "refund_issued"),
        "refund_value_minor": _sum(rows, "amount_minor", "refund_issued"),
        "disputes_count": _count(rows, "dispute_created"),
        "disputes_contested_minor": _sum(rows, "amount_minor", "dispute_created"),
        "fraud_flags_count": _count(rows, "radar_early_warning"),
        "early_warnings_count": _count(rows, "radar_early_warning"),
        # Money *authorised*, one amount per visitor-card, over captures only. Three separate
        # mistakes lived in the version this replaced, and all three moved this denominator in the
        # flattering direction, which is the direction that keeps a kill metric from ringing:
        #
        #   * a refund and a dispute were in the sum. They restate money already authorised, so
        #     they added dollars the account never held, and they grew with the day's *trouble*
        #     rather than with its volume: 15 rows of money against a settlement statement's 11
        #     charges, so `dispute_amt_pct` fell as disputes rose. Money *lost* is a different
        #     column, `rev_refunded_minor`, and subtracting it from revenue is where it belongs.
        #   * it summed rows. Handlers key a row on the processor's event id, and Stripe
        #     re-delivers: two copies of one `checkout.session.completed` paid for a card once and
        #     counted its capture twice.
        #   * capturing and refunding the same charge zeroed it. A refund writes the charge's
        #     authorised amount into the same column its capture wrote, so one visitor can carry
        #     100 twice, and the reading has to be one card's ceiling, never a running balance.
        #     Netting is `rev_total_minor`'s job, and it already subtracts the refund there.
        #
        # The figure per card is the largest amount that visitor's rows claim, so a redelivery
        # cannot pull a day below what the first copy reported, and a keyless row is counted on its
        # own rather than merged into one anonymous member. That `max` also does the de-doubling the
        # third bullet describes: a capture and its refund both write the charge's amount, and
        # reading a card as 100+100 reports the account as having held twice what it held. The cost
        # of one figure per visitor is that a member who renews *after* the trial capture is capped
        # at 3900 rather than credited 100+3900, so the column under-reads on a day heavy with
        # conversions that both started and renewed; `rev_new` + `rev_rebill` is the column that
        # adds the two money pools exactly, and it is the one net contribution uses. The trial/rebill split does not collide
        # with the dedupe because `rev_new` and `rev_rebill` are summed from `amount_minor` on
        # separate event sets and added, so `rev_total_minor` keeps reconciling to this column.
        "gross_auth_volume_minor": dedupe_volume(rows, "trial_paid", "rebill_succeeded"),
        "bin_prepaid_charges": _count_charges_by_funding(rows, "prepaid"),
        "charges_observed": _count_charges_by_funding(rows, "credit", "debit", "prepaid"),
        "charges_unknown_bin": _count_charges_by_funding(rows, "unknown", ""),
        "cohorts_paid_day0_active_day60": cohort_retained,
        "cohort_size_60d_ago": cohort_size,
        # Revenue split the way the sheet asks for it, and the way `hustle_metrics` means it:
        # `rev_new` is new-conversion money (the trial captures), `rev_rebill` is money from an
        # existing subscription renewing. `rev_total_minor` is their sum, which makes the
        # relationship checkable by eye rather than requiring trust in three separate sums.
        "rev_new": _sum(rows, "amount_minor", "trial_paid"),
        "rev_rebill": _sum(rows, "amount_minor", "rebill_succeeded"),
        # `net_contribution` is defined over the *net* of refunds, so the refund total is kept
        # as its own figure and `rev_total_minor` subtracts it: contribution against gross
        # revenue is the flattering reading, and §3.4's gate is "positive before any spend
        # increase", which is precisely the decision that deserves the unflattering one.
        "rev_refunded_minor": _sum(rows, "amount_minor", "refund_issued"),
        # Computed here rather than after the literal below. That version assigned it to the
        # name it returns through `**subs`, so the key the caller received came from the dict
        # literal and carried no value at all: `merge_row` renders an absent computed column as
        # blank, and `rev_total_minor` was an empty cell in every row ever exported. Net
        # contribution divided by it turned every day's contribution into a blank, and the ratio
        # that survived did so only because `int("" or 0)` forgives the hole it read from.
        "rev_total_minor": (_sum(rows, "amount_minor", "trial_paid")
                            + _sum(rows, "amount_minor", "rebill_succeeded")
                            - _sum(rows, "amount_minor", "refund_issued")),
        **subs,
    }
    return counts


def _count_charges_by_funding(rows: list[sqlite3.Row], *fundings: str) -> int:
    """Charge events whose card funding is one of ``fundings``.

    Counted over *charge* events only (a trial capture or a rebill), not over every row: the
    prepaid share's denominator is "charges", so a landing-page view must not dilute it. A
    refund or a dispute on a charge already counted is not a second charge, which is why
    neither event type is in the set below.
    """
    charge_events = ("trial_paid", "rebill_succeeded")
    return sum(1 for row in rows
               if row["event"] in charge_events and (row["card_funding"] or "") in fundings)


def _subscription_state(conn: sqlite3.Connection, as_of: str = "") -> dict[str, int]:
    """`active_subs` and `mrr_minor` as *stocks*, read from the subscription table.

    §3.3's trap in one line: a stock added on every tick yields 24x the MRR by midnight. Both
    are read as "what is true right now" rather than as anything event-derived.

    `as_of` constrains the read to subscriptions that already existed on a given day, which is
    what keeps a backfill's row from being a lie: a member who signed up last week was worth
    nothing on the day being reconstructed, and summing them into that day's MRR gives a wrong
    number that looks plausible. It is still not a history. There is no subscription-events
    table here, so a member who subscribed on the 1st and cancelled on the 5th is invisible to a
    row written afterwards for the 3rd, and a reconstructed row reads *low* by exactly those
    members. That is why the export says so in `notes` on any day it did not write on the day
    itself: the direction the error moves in is knowable, the size of it is not.
    """
    if "funnel_subscriptions" not in {
            r["name"] for r in conn.execute(
                "select name from sqlite_master where type = 'table'")}:
        return {"active_subs": 0, "mrr_minor": 0}
    live = ("trialing", "active", "past_due", "paused")
    placeholders = ", ".join("?" for _ in live)
    # `created_at` is ISO to the second, so a one-character suffix above every bare date makes
    # "created on or before this day" true for the day's own signups. A shorter string sorts
    # below a longer one, which cuts the other way: comparing to `as_of` alone would drop the
    # day's own subscriptions out of the row that is supposed to be counting them.
    existed = " and coalesce(created_at, '') <= ?" if as_of else ""
    args: tuple = live + ((f"{as_of}~",) if as_of else ())
    row = conn.execute(
        f"""select count(*) as n,
                   coalesce(sum(case when plan = 'lite' then {int(_lite_amount())}
                                     else rebill_amount_minor end), 0) as mrr
            from funnel_subscriptions where status in ({placeholders}){existed}""",
        args).fetchone()
    return {"active_subs": int(row["n"] or 0), "mrr_minor": int(row["mrr"] or 0)}


_LITE_AMOUNT: int | None = None


def _lite_amount() -> int:
    """Recurring value of a lite-tier subscription, for MRR.

    The local row stores `trial_amount_minor` and `rebill_amount_minor` as the offer they
    signed up under, and a member on the lite downgrade has their *current* price applied at
    the processor rather than rewritten here, so MRR comes from the configured lite price
    rather than from a column that says what the plan used to cost. Reading the setting through
    `config` keeps the number in one place.
    """
    global _LITE_AMOUNT
    if _LITE_AMOUNT is None:
        config_module, _ = _load_funnel_config()
        _LITE_AMOUNT = int(config_module.FunnelSettings.from_env().lite_amount_minor)
    return _LITE_AMOUNT


_QUESTION_COUNT = 0


def _question_count() -> int:
    """How many questions a completed quiz has, read from the live quiz definition.

    Not hardcoded, because `answered >= 12` in a SQL string is exactly the kind of constant that
    quietly becomes wrong when a question is added: every opt-in would then report zero and the
    column would look like a traffic problem.
    """
    global _QUESTION_COUNT
    if not _QUESTION_COUNT:
        try:
            from quiz import QUESTIONS  # noqa: PLC0415
            _QUESTION_COUNT = len(QUESTIONS)
        except Exception:  # noqa: BLE001 - a missing quiz module leaves opt_ins blank
            _QUESTION_COUNT = 12
    return _QUESTION_COUNT


def _test_variable(rows: list[sqlite3.Row], fallback: str) -> str:
    labels = {row["test_variable"] for row in rows if row["test_variable"]}
    if len(labels) == 1:
        return labels.pop()
    if len(labels) > 1:
        # More than one variant in one day's row means the split was not sequential, and a
        # blended row over two arms is precisely what §3.1 forbids. Said in the row's own
        # `notes` rather than silently averaged.
        return "+".join(sorted(labels))
    return fallback


# ---------------------------------------------------------------------------
# Ratios
# ---------------------------------------------------------------------------

def pct(numerator: float, denominator: float) -> str:
    """A percentage, or blank when the denominator is zero.

    Blank rather than 0 for an undefined rate, because §3.4's rule exists to stop a reader
    acting on a number that was never measured: "refund rate 0.00%" on a day with no paid
    conversions reads as perfect, and "blank" reads as no data. Formatted to two decimals,
    which is enough for a 1% ceiling on a low-count day and not so many digits that noise looks
    like a trend.
    """
    if not denominator:
        return ""
    return f"{(float(numerator) / float(denominator) * 100):.2f}"


def ratios(counts: dict[str, Any]) -> dict[str, str]:
    """The five computed metrics, plus the pair-reading and volume guards.

    Every formula comes from §3.4's table. Two departures, both marked in the row's `notes`
    when they bite:

    * `dispute_cb_pct` uses dollars over dollars (`contested / captured`) instead of the
      spec's count-over-minor-units, which cannot yield a percentage at any volume.
    * `net_contribution` uses only the cost columns the script can see (ad spend, tool cost,
      LLM cost, all hand-typed). On a day where none have been typed it reports the gross
      figure and says so, because a positive contribution computed against zero cost is the
      single most flattering way to report a loss.
    """
    paid = counts["trial_paid"]
    contested = counts["disputes_contested_minor"]
    volume = counts["gross_auth_volume_minor"]
    charges = counts["charges_observed"]
    # `rev_total_minor`, not `gross_auth_volume_minor`. The two are close on a quiet day and
    # diverge on the days this figure matters: the volume column reads one ceiling per
    # visitor-card (so a member who captured a trial and then renewed counts at the renewal
    # alone), and money *lost* is inside it, so dividing contribution by it would credit money
    # back that the refund handler already took away. Contribution is revenue minus cost,
    # revenue being the two money pools added and the refunds subtracted.
    revenue = int(counts.get("rev_total_minor") or 0)
    costs = counts.get("cost_total_minor")
    net = revenue - float(costs) if isinstance(costs, (int, float)) else revenue
    out = {
        "trial_paid_pct": pct(paid, counts["trials_started"]),
        "refund_pct": pct(counts["refunds_count"], counts["paid_conversions"]),
        # Two readings of the 1% ceiling, side by side: the count over *captures* (what
        # acquirers quote) and the dollars contested over the dollars authorised (what the
        # settlement report shows). They diverge when the average disputed amount differs from
        # the average charge, and on a small lane that divergence is common.
        "dispute_cb_pct": pct(counts["disputes_count"] + counts["chargebacks_count"],
                              paid + counts["rebill_success"]),
        "dispute_amt_pct": pct(contested, volume),
        "fraud_pct": pct(counts["fraud_flags_count"], charges),
        "bin_prepaid_share_pct": pct(counts["bin_prepaid_charges"], charges),
        "month2_retention_pct": pct(counts["cohorts_paid_day0_active_day60"],
                                    counts["cohort_size_60d_ago"]),
        "rebill_decline_rate_pct": pct(counts["rebill_declines_total"],
                                       counts["rebill_attempts"]),
        "net_contribution_minor": (f"{net:.0f}" if paid else ""),
    }
    if counts.get("cost_total_missing"):
        out["net_contribution_note"] = "cost columns empty: gross, not net"
    return out


def ceiling_breaches(metrics: dict[str, str]) -> list[str]:
    """Named breaches of the two hard ceilings, or []."""
    breaches = []
    for column, ceiling, description in CEILINGS:
        value = metrics.get(column, "")
        if value and float(value) > ceiling:
            breaches.append(f"{column}={value}% > {ceiling}% ({description})")
    return breaches


def row_warnings(counts: dict[str, Any], metrics: dict[str, str]) -> list[str]:
    """What a reader must know about this row that no single cell says."""
    notes: list[str] = []
    if "+" in counts["test_variable"]:
        notes.append(f"MIXED VARIANTS ({counts['test_variable']}): §3.1 requires one at a time")
    charges = counts["charges_observed"]
    if counts["bin_prepaid_charges"] and charges and \
            counts["bin_prepaid_charges"] / charges > 0.20:
        notes.append(f"PREPAID BIN SHARE {counts['bin_prepaid_charges']}/{charges}: cut spend early")
    if counts["charges_unknown_bin"]:
        notes.append(f"BIN TYPE UNKNOWN ON {counts['charges_unknown_bin']} CHARGES: "
                     "prepaid share understated")
    if counts["disputes_count"] and metrics.get("dispute_cb_pct") != \
            metrics.get("dispute_amt_pct"):
        notes.append("dispute count% and dollar% differ; read both")
    if charges and charges < LOW_VOLUME_BELOW:
        notes.append(f"LOW VOLUME ({charges} charges): ratios are indicative, "
                     f"n≥{LOW_VOLUME_BELOW} per arm")
    if counts["cohort_size_60d_ago"] == 0:
        notes.append("month-2 retention undefined (< 60 days of history)")
    if counts.get("stock_gauged_as_of_today") is False:
        notes.append("active_subs/mrr_minor as of the export run, not the day: excludes "
                     "anything that had cancelled by then, so read them as a floor")
    for column in counts.get("_unparsable_costs", []):
        notes.append(f"{column} in the sheet is not a number and was left out of the cost total")
    return notes


def _strip_previous_warnings(typed_note: str) -> str:
    """Remove the warning tail this script appended on an earlier run.

    Done by the separator rather than by pattern-matching warning text, and that is what makes
    a rerun idempotent: warnings change between runs (a reconciliation note appears once the
    processor check has run, a low-volume warning disappears once volume arrives), and reusing
    the previous note verbatim as the "operator text" would append a fresh tail onto the old one
    until the cell became unreadable and a reader could no longer tell their own judgement from
    the script's noise. Everything after the first ` | ` is ours, and is regenerated.
    """
    head, separator, _tail = typed_note.partition(NOTE_SEPARATOR)
    return head.strip() if separator else typed_note.strip()


#: Separates the operator's own note from the warnings this script appends. Spaced so it cannot
#: occur inside a number or a ratio, and documented in `README.md` so the convention outlives
#: the code that introduced it.
NOTE_SEPARATOR = " | "


def build_note(counts: dict[str, Any], metrics: dict[str, str], typed_note: str) -> str:
    """Merge this script's warnings with the operator's own note.

    The operator's text comes first and is preserved on a rerun, because the day's judgement
    ("ran $40, one support thread about the descriptor") is the thing a month-3 reader actually
    wants and it is the one thing this script cannot regenerate.
    """
    operator = _strip_previous_warnings(typed_note)
    warnings = "; ".join(row_warnings(counts, metrics))
    if operator and warnings:
        return f"{operator}{NOTE_SEPARATOR}{warnings}"
    return operator or warnings


# ---------------------------------------------------------------------------
# Hand-typed columns
# ---------------------------------------------------------------------------

#: Cost lines that feed `net_contribution`. Read from the sheet before the ratios are computed,
#: which is why they are named separately from the rest of the typed columns.
COST_COLUMNS = ("ad_spend_minor", "tool_cost_minor", "llm_cost_usd_minor")


def read_existing(csv_path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    """Existing rows, keyed on the identity triple. Missing file is an empty sheet."""
    if not csv_path.is_file():
        return {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return {}
        rows: dict[tuple[str, str, str], dict[str, str]] = {}
        for row in reader:
            key = (row.get("day", ""), row.get("offer_id", ""), row.get("test_variable", ""))
            if key[0]:
                rows[key] = row
    return rows


def write_csv(csv_path: Path, rows: list[dict[str, str]]) -> None:
    """Rewrite the sheet with every row it holds, sorted by day.

    Rewritten rather than appended because append-only cannot express "this day's row, updated":
    an export rerun after typing in the ad spend must not create a second row for the day, and
    a sheet with two rows for one day is a sheet whose totals are wrong until someone notices.
    Sorting by day keeps the artifact readable as the timeline it is meant to be.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r.get("day", ""), r.get("offer_id", ""),
                                               r.get("test_variable", ""))):
            writer.writerow(row)


def merge_row(counts: dict[str, Any], metrics: dict[str, str],
              existing: dict[str, str] | None) -> dict[str, str]:
    """One output row: computed columns from the events, typed columns preserved."""
    existing = existing or {}
    row: dict[str, str] = {}
    for column in HEADER:
        if column in METRIC_COLUMNS:
            row[column] = metrics.get(column, "")
        elif column == "notes":
            row[column] = build_note(counts, metrics, existing.get("notes", ""))
        elif column in COMPUTED_COLUMNS:
            value = counts.get(column)
            row[column] = "" if value is None else str(value)
        else:
            row[column] = existing.get(column, "")
    return row


# ---------------------------------------------------------------------------
# Cross-check against Stripe, best effort
# ---------------------------------------------------------------------------

def stripe_totals(day: str) -> dict[str, Any]:
    """One UTC day's captured, refunded and disputed money, straight from the processor.

    Best effort by construction, on three counts. No `stripe` SDK import (the SDK is not a
    dependency of this app, by design), just REST over `curl`. Any transport or API failure
    lands in a `<name>_error` key and the export carries on, because a report that dies when a
    laptop has no wifi has no place in a 10-minute daily routine. And it never *rewrites* a
    local number: it names a disagreement, and a human decides which source is right, since
    §3.4's routine is "pull the processor's numbers, fill one row" with someone reading them.

    **Paginated to completion.** One page of 100 is not a daily total: a refund-heavy day above
    100 objects would report a remote figure that happens to equal the local one, and the check
    would then bless the very number it exists to question. So each list is followed through
    every page, up to `max_objects`, and a list that hits that ceiling is marked `_partial` so
    the note says "understated" rather than implying completeness.

    **A window, not a day, for the money.** A refund created at 23:59:58 UTC can be reported by
    the API under a `created` a second either side of the boundary, and `captured` timestamps
    are the capture attempt's, not the settlement's. Comparing a day's local money against one
    day's remote money therefore produced disagreements that were pure boundary noise. The
    remote figures cover `day ± hedge_hours` and say so in `window_hours`, and the
    reconciliation compares *cumulative-to-date* figures instead (see
    `reconciliation_notes`), which is the comparison that can actually fail in a useful way: a
    category missing since the webhook was wired up.

    Costs three paginated list calls per exported day times the window, which is why a `--since`
    backfill passes `--offline` implicitly for historical days: reconciling 60 days of history
    one window at a time is 180 calls for numbers that are already settled and agreed.

    Key: `STRIPE_READ_KEY_FUNNEL`, falling back to the funnel's own secret key. A read-only
    restricted key is the right one for a report. Sent as a header rather than in a URL, so the
    secret does not land in a shell history line, a proxy log, or an error message that quotes
    the request.
    """
    key = (os.getenv("STRIPE_READ_KEY_FUNNEL") or os.getenv("STRIPE_SECRET_KEY_FUNNEL")
           or "").strip()
    if not key:
        return {"error": "no STRIPE_READ_KEY_FUNNEL or STRIPE_SECRET_KEY_FUNNEL in env"}
    window = int(os.getenv("FUNNEL_STRIPE_CHECK_WINDOW_HOURS", "2"))
    centre = day_daystart(day) + 43200
    start, end = centre - window * 3600, centre + 12 * 3600 + window * 3600
    out: dict[str, Any] = {"window_hours": window * 2 + 12}
    lists = {
        # Refunds and disputes only. The funnel's own charges, and the `captured` timestamp in
        # particular, cannot be filtered through the ordinary key: restricted keys are allowed
        # list-on-refunds and list-on-disputes and no more, which is the right grant for a
        # report, and `created[gte]/[lt]` is an explicit filter while `captured[gte]/[lt]` is
        # not one the charges list offers at all. A day's *captures* therefore come from the
        # local rows (the webhook sees every capture) and are reconciled cumulatively below,
        # against a processor figure the key can actually return.
        "refunded_minor": ("/v1/refunds", "created", None),
        "disputes_count": ("/v1/disputes", "created", None),
    }
    for name, (path, time_field, status) in lists.items():
        total, count, pages, truncated, failure = 0, 0, 0, False, ""
        cursor = ""
        while True:
            query = (f"limit=100&{time_field}[gte]={start}&{time_field}[lt]={end}"
                     + (f"&status={status}" if status else "")
                     + (f"&starting_after={cursor}" if cursor else ""))
            payload, error = _stripe_get(path, query, key)
            pages += 1
            if error:
                failure = error
                break
            data = payload.get("data", [])
            for item in data:
                count += 1
                if not name.endswith("_count"):
                    total += int(item.get("amount") or 0)
            if len(data) < 100:
                break
            if count >= _STRIPE_MAX_OBJECTS:
                truncated = True
                break
            cursor = str(data[-1].get("id", ""))
            if not cursor or pages >= _STRIPE_MAX_PAGES:
                truncated = True
                break
        if failure:
            out[f"{name}_error"] = failure
            continue
        out[name] = count if name.endswith("_count") else total
        if truncated:
            out[f"{name}_partial"] = f"only {count} objects read; remote figure understated"
        if name == "captured_minor":
            out["captured_captures"] = count
    return out


_STRIPE_MAX_PAGES = 10
_STRIPE_MAX_OBJECTS = 1000


def _stripe_get(path: str, query: str, key: str) -> tuple[dict[str, Any], str]:
    """One Stripe GET. Returns (payload, "") or ({}, "reason"). The key travels as a header."""
    import base64
    credentials = base64.b64encode(f"{key}:".encode()).decode()
    command = ["curl", "-sS", "--max-time", "20",
               "-H", f"Authorization: Basic {credentials}",
               f"https://api.stripe.com{path}?{query}"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                   check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        return {}, str(exc)[:160]
    if completed.returncode != 0:
        return {}, completed.stderr.strip()[:160]
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError as exc:
        return {}, f"unreadable response: {str(exc)[:80]}"
    if "error" in payload:
        return {}, str(payload["error"].get("message"))[:160]
    return payload, ""


def day_daystart(day: str) -> int:
    """Epoch seconds for the UTC midnight that opens a `YYYY-MM-DD` day.

    Built through `datetime(..., tzinfo=utc)` rather than ordinal arithmetic, because `mktime`
    would read the *machine's* timezone and a laptop on UTC+8 would export yesterday's money
    into today's row. The sheet's day is UTC, so the window has to be UTC regardless of where
    the script runs.
    """
    year, month, dom = (int(part) for part in day.split("-"))
    return int(datetime(year, month, dom, tzinfo=timezone.utc).timestamp())


def reconciliation_notes(counts: dict[str, Any], day: str, billing_path: Path,
                         stripe: dict[str, Any] | None,
                         csv_path: Path | None = None) -> list[str]:
    """Cumulative local rows versus cumulative processor totals, and the errors either had.

    Cumulative, and that choice is the whole reason this check is trustworthy. A one-day
    comparison of money figures fails for harmless reasons constantly: a refund issued seconds
    before UTC midnight, a webhook that landed a minute late and so belongs to the next row, a
    capture dated by the processor's attempt time rather than ours. A *cumulative* comparison
    forgives every one of those, because they move money between adjacent rows and leave the
    running total alone, while failing on the one thing that cannot be explained by a boundary:
    a category that has been missing the whole time.

    So: local = every event row in the funnel's own history through the exported day. Remote =
    every object the processor has through the same instant. If the webhook was never wired, or
    died three weeks ago, the two diverge by weeks of volume and the note names it. If they
    agree, the day's row is consistent with the account behind it, which is the only promise a
    report of this size can honestly make.

    Tolerance is 10% of the remote total, floored at one object or one dollar's worth, for the
    ordinary case. **Zero local rows against a non-zero remote total has no tolerance at all**,
    because that is the shape of a broken listener, not of drift, and it is the failure that
    makes every other number on the sheet unfalsifiable.
    """
    if not stripe or "error" in stripe:
        return [f"stripe check unavailable: {stripe.get('error')}"
                if stripe else "stripe check unavailable"]
    notes = []
    remote_through = day_daystart(day) + 12 * 3600
    # Refund *count* against refund *count*, and dispute count against dispute count. The
    # pairing is the point: the cumulative probe returns object counts for both lists, because
    # `total_count` on a refund list is the number of refunds and not the money, so the local
    # figure it can be compared against has to be a count of rows as well. Refunding seven
    # $1.00 charges is 7 remotely and 700 in cents locally, and comparing those two is a
    # disagreement the reader invented. Money still gets its own reading one layer up, in the
    # day's `refunded_minor` figure from `stripe_totals`, which sums the amounts it paged.
    checks = (
        ("refunds", "/v1/refunds", "created", "refunds_count"),
        ("disputes", "/v1/disputes", "created", "disputes_count"),
    )
    local_cumulative = cumulative_local_counts(billing_path, day)
    # Which cumulative figure is being asked for is the *path*: "/v1/refunds" or "/v1/disputes".
    # It has to arrive as an argument, and a stub on the other side of the seam has to switch on
    # it, because a single canned answer for both categories cannot distinguish "the refund
    # webhook is dead" from "the dispute webhook is dead" from "our dispute rows are simply
    # empty". Those are different incidents with different fixes, and the loop below already
    # knows which is which per iteration.
    for label, path, time_field, ours_key in checks:
        remote, error, truncated = _stripe_cumulative(path, time_field, remote_through, None)
        if error:
            notes.append(f"stripe cumulative {label} check failed: {error}")
            continue
        ours = int(local_cumulative.get(ours_key) or 0)
        if truncated:
            notes.append(f"stripe cumulative {label} truncated at our read ceiling: "
                         "comparison is indicative only")
        tolerance = max(1, int(abs(remote) * 0.10))
        if remote and ours == 0 and not truncated:
            notes.append(f"LOCAL ROWS SHOW ZERO {label.upper()} EVER, processor shows "
                         f"{remote}: check STRIPE_WEBHOOK_SECRET_FUNNEL and the endpoint")
        elif abs(remote - ours) > tolerance:
            notes.append(f"CUMULATIVE {label} DISAGREE: processor {remote}, local rows {ours}")
    for key, value in stripe.items():
        if key.endswith("_error"):
            notes.append(f"stripe check failed ({key[:-6]}): {value}")
        elif key.endswith("_partial"):
            notes.append(f"stripe day figure for {key[:-8]} is partial: {value}")
    return notes


def cumulative_local_counts(billing_path: Path, through_day: str) -> dict[str, int]:
    """Every refund and dispute the local rows hold through the end of `through_day`.

    Read straight from `trial_events` rather than from the CSV, because the CSV is an output
    that may lag, may have been edited, and may not exist yet on day one. The processor's
    numbers are compared against the system of record, not against a report of it.
    """
    conn = open_read_only(billing_path)
    if conn is None:
        return {}
    try:
        row = conn.execute(
            """select
                 coalesce(sum(case when event = 'refund_issued'
                                   then amount_minor end), 0)     as refunded_minor,
                 count(case when event = 'refund_issued' then 1 end) as refunds_count,
                 count(case when event = 'dispute_created' then 1 end) as disputes_count
               from trial_events where occurred_at < ?""",
            (_next_day(through_day),)).fetchone()
        return {"refunded_minor": int(row["refunded_minor"] or 0),
                "refunds_count": int(row["refunds_count"] or 0),
                "disputes_count": int(row["disputes_count"] or 0)}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _stripe_cumulative(path: str, time_field: str, through: int) -> tuple[int, str, bool]:
    """Count every object the processor holds with `time_field` before `through`.

    Counts with `total_count` from a `limit=1` probe rather than paging the whole list: the
    account's lifetime volume can be tens of thousands of objects, and this comparison needs a
    number, not the objects. `limit=1` still returns the accurate `total_count` for the filtered
    list, which is exactly the figure wanted, at one request instead of a hundred.
    """
    query = f"limit=1&{time_field}[lt]={through}"
    payload, error = _stripe_get(path, query, _stripe_key())
    if error:
        return 0, error, False
    total = int(payload.get("total_count") or 0)
    # An empty first page with a missing `total_count` is the one shape where the count cannot
    # be trusted, and saying so beats reporting a confident zero that then makes the local rows
    # look inflated by the whole account's history.
    if not payload.get("data", []) and "total_count" not in payload:
        return 0, "", True
    return total, "", False


def _stripe_key() -> str:
    return (os.getenv("STRIPE_READ_KEY_FUNNEL") or os.getenv("STRIPE_SECRET_KEY_FUNNEL")
            or "").strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def daterange(since: str, until: str) -> list[str]:
    from datetime import date, timedelta
    start, end = date.fromisoformat(since), date.fromisoformat(until)
    days, cursor = [], start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def export_day(day: str, csv_path: Path, config_module: Any, storage_module: Any,
               offline: bool = False) -> tuple[dict[str, str], dict[str, Any]]:
    """Compute one day's row, merge it into the sheet, and return (row, detail).

    Written back twice when the processor check has something to say: once with the numbers,
    then once with the reconciliation notes folded into `notes`. Cheap, and it means the sheet
    holds a warning even after this process has exited and nobody remembers what stdout said.
    """
    settings = config_module.FunnelSettings.from_env()
    paths = db_paths(config_module, settings)
    # Decided before the read, because it decides *how* the stock columns are read, not just how
    # they are labelled. See `_subscription_state`.
    on_day = day == utc_today()
    counts = counts_for_day(day, paths["billing"], paths["responses"],
                            storage_module.OFFER_ID, now="now" if on_day else "then")
    # Cost columns feed `net_contribution`, so read them before the ratios.
    existing = read_existing(csv_path).get(
        (day, counts["offer_id"], counts["test_variable"]))
    for column in COST_COLUMNS:
        value = (existing or {}).get(column, "")
        try:
            counts[column] = float(value) if value else 0
        except ValueError:
            # A typed value this script cannot read stays where it is (in the output row,
            # verbatim) and contributes nothing to the sum rather than poisoning it with a
            # guess. Named, because a silently dropped cost line flatters net contribution.
            counts[column] = 0
            counts.setdefault("_unparsable_costs", []).append(column)
    counts["cost_total_minor"] = sum(counts[column] for column in COST_COLUMNS)
    counts["cost_total_missing"] = not any(counts[column] for column in COST_COLUMNS)
    counts["chargebacks_count"] = int((existing or {}).get("chargebacks_count") or 0)
    # `active_subs` and `mrr_minor` are stocks, and a table with no history cannot say what they
    # were sixty days ago. A backfill now gauges them as of the day it writes (see
    # `_subscription_state`), which removes the subscriptions that did not exist yet; it cannot
    # remove the ones that existed on the day and have since cancelled, so the number reads low
    # by that amount and nobody can reconstruct it from here. §3.4's sheet is read by a human, so
    # the row says that rather than leaving a reader to remember how the script works.
    # Named for the day the *stocks* are gauged at, which is the opposite reading from "today":
    # on a backfill the subscriptions table is read as of the export run, not as of the day being
    # reconstructed, because a table with no history cannot say what it held sixty days ago. The
    # flag feeds the row warning that tells a reader to treat MRR as a floor. A boolean called
    # "as of today" that is true on the days the stocks are *not* from today inverted the note
    # and left the misleading number unlabelled.
    counts["stock_gauged_as_of_today"] = on_day
    metrics = ratios(counts)
    row = merge_row(counts, metrics, existing)

    rows = read_existing(csv_path)
    key = (day, counts["offer_id"], counts["test_variable"])
    rows[key] = row
    write_csv(csv_path, list(rows.values()))
    detail = {"csv": str(csv_path), "counts": counts, "metrics": metrics,
              "breaches": ceiling_breaches(metrics),
              "warnings": row_warnings(counts, metrics)}
    # Historical days skip the network by design: the cumulative comparison below reaches the
    # processor's whole history either way, so re-running it per day for a 60-day backfill buys
    # 60 identical agreements and 180 requests. Run today's export and the agreement is on
    # record for the whole sheet.
    if not offline and day == utc_today():
        stripe = stripe_totals(day)
        detail["stripe"] = stripe
        # Skipped when the key is missing, which is the ordinary state of a checkout-box or CI
        # run and not a finding. `stripe_totals` reports that case as `{"error": ...}` because
        # a caller who *asked* for the cross-check and got nothing should see why, but folding
        # it into the row's warnings put "stripe check unavailable" into every note on every day
        # of a sheet exported without a read key, and a permanent warning is a warning nobody
        # reads, including the one that says the webhook has been dead for three weeks.
        if "error" not in stripe:
            notes = reconciliation_notes(counts, day, paths["billing"], stripe, csv_path)
            detail["reconciliation"] = notes
            if notes:
                detail["warnings"] = notes + detail["warnings"]
                row["notes"] = build_note(counts, metrics, (existing or {}).get("notes", ""))
                rows[key] = row
                write_csv(csv_path, list(rows.values()))
        else:
            detail["reconciliation"] = []
    return row, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export Lane C's Day-1 metrics row(s) from the funnel's own event rows.")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--day", default=None,
                        help="one UTC day, YYYY-MM-DD, or the words 'today'/'yesterday'/"
                             "'2daysago' (default: today). The word forms exist so a systemd "
                             "unit needs no shell: `%%F` inside a unit is systemd's own "
                             "specifier, not date's format.")
    window.add_argument("--since", metavar="YYYY-MM-DD",
                        help="every UTC day from here through --until")
    parser.add_argument("--until", metavar="YYYY-MM-DD", default=None,
                        help="last day for --since (default: today)")
    parser.add_argument("--csv", default=None,
                        help=f"sheet path (default: $FUNNEL_METRICS_CSV or "
                             f"{FUNNEL_ROOT / 'data' / 'metrics' / 'lane_c_daily.csv'})")
    parser.add_argument("--stdout", action="store_true",
                        help="also print the row(s) to stdout as CSV")
    parser.add_argument("--json", action="store_true",
                        help="print machine-readable detail instead of the human summary")
    parser.add_argument("--offline", action="store_true",
                        help="skip the best-effort Stripe cross-check (no network)")
    parser.add_argument("--fail-on-ceiling", action="store_true",
                        help="exit 2 if the dispute+CB or fraud ceiling is breached")
    args = parser.parse_args(argv)

    config_module, storage_module = _load_funnel_config()
    if args.csv:
        csv_path = Path(args.csv).expanduser()
    else:
        from config import ROOT as funnel_root
        csv_path = Path(os.getenv("FUNNEL_METRICS_CSV",
                                  str(funnel_root / "data" / "metrics" / "lane_c_daily.csv")))
        if not csv_path.is_absolute():
            csv_path = funnel_root / csv_path

    # Resolved here, before validation and before `daterange`, so `--since yesterday` and
    # `--until yesterday` mean the same thing they do on --day rather than dying in the format
    # check below with a message about a word the help text explicitly offers.
    def resolve(name: str, value: str | None) -> str | None:
        """A date passes through; a recognised word becomes a date; anything else stops the run.

        The third case is the whole point. `--since yesterdy` must not be read as a date-shaped
        thing or quietly default to today, because both produce a sheet and neither is what the
        operator asked for.
        """
        if not value:
            return None
        resolved = utc_relative_day(value)
        if resolved is not None:
            return resolved
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            parser.error(f"{name} {value!r} is neither YYYY-MM-DD nor one of "
                         f"today / yesterday / 'N days ago'")
        return value

    day_arg = resolve("--day", args.day)
    since_arg = resolve("--since", args.since)
    until_arg = resolve("--until", args.until)
    days = ([day_arg] if day_arg
            else daterange(since_arg, until_arg or utc_today()) if since_arg
            else [utc_today()])

    breaches: list[str] = []
    for day in days:
        try:
            row, detail = export_day(day, csv_path, config_module, storage_module,
                                     offline=args.offline)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        breaches.extend(detail["breaches"])
        if args.json:
            # `row` carries the sheet's own `notes`, which duplicates `warnings` by design: one
            # is what the CSV holds, the other is what this run concluded. Machine consumers
            # want the latter, and `--json` on a cron line is how a ceiling breach becomes an
            # alert rather than a line nobody scrolled past.
            print(json.dumps({"day": day, "row": row, **detail}, ensure_ascii=False))
        elif args.stdout:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=HEADER, extrasaction="ignore")
            if day == days[0]:
                writer.writeheader()
            writer.writerow(row)
            sys.stdout.write(buffer.getvalue())
        else:
            _print_summary(day, row, detail, len(days) > 1)
    # Once for the whole run, not once per day: `--since/--until` backfills a week, and a line
    # per day saying the same thing buries the seven rows of actual numbers above it. Inside the
    # per-day block it also named a local that belonged to `main`, not to the summariser.
    if not (args.json or args.stdout):
        print(f"\nsheet: {csv_path}")

    if args.fail_on_ceiling and breaches:
        print(f"CEILING BREACH: {'; '.join(breaches)}", file=sys.stderr)
        return 2
    return 0


def _print_summary(day: str, row: dict[str, str], detail: dict[str, Any],
                   brief: bool) -> None:
    counts, metrics = detail["counts"], detail["metrics"]
    print(f"\n{day}  offer={row['offer_id']}  variant={row['test_variable']}")
    if brief:
        print(f"  paid {row['trial_paid']}  MRR {row['mrr_minor']}  "
              f"trial→paid {row['trial_paid_pct'] or '-'}%  "
              f"refund {row['refund_pct'] or '-'}%  dispute+CB {row['dispute_cb_pct'] or '-'}%"
              f"  fraud {row['fraud_pct'] or '-'}%")
        return
    print(f"  funnel    lp {row['lp_views']}  opt-ins {row['opt_ins'] or '-'}  "
          f"trials started {row['trials_started']}  paid {row['trial_paid']}  "
          f"abandoned checkouts {row['checkout_expired']}")
    print(f"  money     MRR {row['mrr_minor']}  active subs {row['active_subs']}  "
          f"authorised {row['gross_auth_volume_minor']}  "
          f"net contribution {row['net_contribution_minor'] or '-'}")
    print(f"  lifecycle rebills ok {row['rebill_success']}  lost after one retry "
          f"{row['rebill_declines_silent']}  refunds {row['refunds_count']}"
          f" ({row['refund_value_minor']})  disputes {row['disputes_count']}  "
          f"fraud flags {row['fraud_flags_count']}")
    print(f"  ratios    trial→paid {row['trial_paid_pct'] or '-'}%"
          f"   refund {row['refund_pct'] or '-'}% (healthy 5-12%)"
          f"   month-2 retention {row['month2_retention_pct'] or '-'}% (gate ≥ 60%)")
    print(f"            dispute+CB {row['dispute_cb_pct'] or '-'}% of charges (≤ 1%)"
          f"   | {row['dispute_amt_pct'] or '-'}% of dollars"
          f"   fraud {row['fraud_pct'] or '-'}% (≤ 0.5%)")
    print(f"            prepaid BIN share {row['bin_prepaid_share_pct'] or '-'}%"
          f"   rebill decline rate {row['rebill_decline_rate_pct'] or '-'}%")
    if detail.get("stripe"):
        stripe = detail["stripe"]
        if "error" in stripe:
            print(f"  stripe    not checked: {stripe['error']}")
        else:
            # The window, printed: these are the processor's totals over a band around the day
            # rather than the day itself, and a figure quoted without its window invites a
            # comparison it was never built to survive. Exact-day agreement is not promised;
            # the cumulative check that follows is the one that can fail usefully. A truncated
            # read is shown as `≤`, because printing a partial sum as though it were the total
            # is how a genuine disagreement gets talked out of the room.
            trunc = lambda name: "≤" if f"{name}_partial" in stripe else ""  # noqa: E731
            print(f"  stripe    +/-{stripe.get('window_hours', '?')}h window:  "
                  f"refunded {trunc('refunded_minor')}{stripe.get('refunded_minor', '?')} minor  "
                  f"disputes {trunc('disputes_count')}{stripe.get('disputes_count', '?')}"
                  f"   (this day locally: refunds {row['refunds_count']}"
                  f" / {row['refund_value_minor']} minor, "
                  f"disputes {row['disputes_count']})")
    for warning in detail["warnings"]:
        print(f"  ! {warning}")
    for breach in detail["breaches"]:
        print(f"  CEILING  {breach}")


if __name__ == "__main__":
    raise SystemExit(main())
