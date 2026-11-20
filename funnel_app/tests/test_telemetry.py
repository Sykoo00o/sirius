"""Phase 3 tests: funnel telemetry, processor risk signals, and the Day-1 sheet.

Three surfaces, tested for the one property each can actually lose:

  * **Idempotency.** Stripe re-delivers events and a customer refreshes a page. A duplicate row
    inflates a denominator, which does not look broken; it just reports the ratios we would
    prefer. So every test here that fires the same event twice asserts on the *result string* or
    the return value as well as the row count, because "we noticed it was a duplicate" is the
    behaviour, not a detail of it.
  * **Ratio denominators.** A ratio over the wrong population is confidently wrong and
    invisible: refund % divided by starts rather than by conversions still prints a number
    between 0 and 100. The fixtures are deliberately asymmetric (30 sessions, 10 conversions, 1
    refund, 1 dispute) so a wrong denominator yields a visibly different percentage rather than
    the same figure to two decimal places.
  * **Instrumentation that cannot break a purchase.** Telemetry sits after money has already
    moved. Every path is asserted against a broken or absent store, and the customer-facing
    outcome must not change.

Nothing here reaches the network. `FakeStripe` (through the `live` fixture) keeps the app off
api.stripe.com, the processor cross-check in the metrics script is monkeypatched wherever it is
exercised at all, and the export reads throwaway SQLite files in `tmp_path`.

`live` and `subscribe` are imported from `test_billing` rather than reimplemented. A second copy
of the armed-app fixture here would be a second copy of about ninety lines of module-rotation
bookkeeping, and two copies drifting is how a suite ends up arming the app differently depending
on which file a test happens to sit in.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import importlib
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import get, jar_of, post, walk
from test_billing import live, subscribe  # noqa: F401  (fixtures and helper, by reference)

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNNEL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"

#: The same value the `live` fixture puts in `STRIPE_WEBHOOK_SECRET_FUNNEL`. Named here because
#: `hook` has to sign with it, and because the pairing is the assertion: a test that signed with
#: a different secret would exercise "signature rejected" while believing it tested the handler.
WEBHOOK_SECRET = "whsec_test_abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_export_script():
    """Import `scripts/export_lane_c_metrics.py` by path.

    By path rather than by name because it lives outside the package the app imports from, and
    `conftest` deliberately puts `funnel_app/` on the import path, not `scripts/`. Adding
    `scripts/` globally would make `config` ambiguous between the repo-root engine config and the
    funnel's own `config.py` for every suite in the run, which is a class of cross-contamination
    this repo has already paid for once.

    Registered in `sys.modules` before execution because `dataclasses` resolves
    `types.GenericAlias` through `sys.modules` while building slotted classes, and a module that
    is mid-execution but unregistered is where that lookup goes wrong.
    """
    spec = importlib.util.spec_from_file_location(
        "lane_c_metrics", SCRIPTS_ROOT / "export_lane_c_metrics.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["lane_c_metrics"] = module
    spec.loader.exec_module(module)
    return module


def with_store(client):
    """The client plus the telemetry store *it* built.

    Reached through `client.main.events_store` rather than constructed here, because the point of
    most of these tests is that the app's own writes land: a store this file built could receive
    rows from a service the app is not using, and the assertions would pass anyway.
    """
    client.events = client.main.events_store
    return client


@pytest.fixture()
def tele_client(client):
    return with_store(client)


@pytest.fixture()
def live_client(live):
    return with_store(live)


def hook(client, event: dict):
    """POST one Stripe event to `/webhooks/stripe`, correctly signed.

    The `live` fixture configures `STRIPE_WEBHOOK_SECRET_FUNNEL`, and the real
    `verify_signature` is *not* faked (it is a pure function, and faking it would let a
    signature regression pass every webhook test here), so an unsigned post would be refused
    before dispatch. Signing with the fixture's secret and a current timestamp is what lets
    these tests reach the handler at all. The refusal itself, and the fact that Phase 3's new
    dispute/Radar/refund branch sits behind it rather than in front of it, is the separate test
    further down.
    """
    body = json.dumps(event)
    stamp = int(time.time())
    digest = hmac.new(WEBHOOK_SECRET.encode(), f"{stamp}.{body}".encode(),
                      hashlib.sha256).hexdigest()
    return client.request(
        "POST", "/webhooks/stripe",
        content=body,
        headers={"content-type": "application/json",
                 "stripe-signature": f"t={stamp},v1={digest}"})


def stripe_event(type_name: str, obj: dict, *, event_id: str = "",
                 live_mode: bool | None = None) -> dict:
    """A Stripe event envelope.

    `type_name` may be a bare suffix (`paid`, `failed`), read as a subscription event the way the
    billing suite writes them, or fully dotted (`charge.dispute.created`), which the Phase 3
    events always are.

    `livemode` is left absent unless asked for, because absent reads as "not live" and that is the
    only reason these tests can post money events at an app holding test keys. Passing False and
    passing nothing are the same to the parser and different to a reader, hence the third state.

    Written as `livemode`, one word, on the event: that is the key Stripe's envelope actually uses,
    and the parser's habit of reading `live_mode` meant no fixture here had ever exercised the
    mode-mismatch guard on a payload shaped like production's. `stripe_payload_is_read_the_way_
    stripe_sends_it` pins the spelling against the vendor's, so the next reader of this line finds
    the reason instead of "correcting" it back.
    """
    kind = type_name if "." in type_name else f"customer.subscription.{type_name}"
    event = {"id": event_id or f"evt_{abs(hash(kind + str(sorted(obj.keys())))) % 10**8}",
             "object": "event", "type": kind, "data": {"object": obj}}
    if live_mode is not None:
        event["livemode"] = live_mode
    return event


def visitor_of(client) -> str:
    """The visitor key this conversation holds, unsigned, as the billing tables store it.

    Re-derived rather than imported from `test_billing`: that helper reads the cookie name, and
    this suite asserts on `visitor_key` columns, so a renamed cookie would leave the imported
    version passing while every row here keyed on "".
    """
    return jar_of(client)["rl_vid"].rsplit(".", 1)[0]


def rows_of(client, event: str) -> list[dict]:
    return [r for r in client.events.events() if r["event"] == event]


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def invoice_body(period_end: int, *, status: str = "paid", amount: int = 3900,
                 subscription_id: str = "sub_x", invoice_id: str = "in_1", **extra) -> dict:
    """An invoice object, in the shape `event_from_webhook` reads.

    `period_end` is a parameter rather than a default because the three invoice tests differ in
    nothing else: a period in the future is a rebill, a period the row already has is the trial
    invoice, and a period nine days past is a dunning failure Stripe has already retried.
    """
    return {"id": invoice_id, "object": "invoice", "subscription": subscription_id,
            "amount": amount, "amount_total": amount,
            "amount_paid": amount if status == "paid" else 0,
            "current_period_end": period_end, "status": status, "currency": "usd", **extra}


# ===========================================================================
# 1. The telemetry store
# ===========================================================================

def test_record_refuses_an_unknown_event_name(tmp_path):
    """A stored typo is invisible: the export groups by event name and reports 0 forever.

    Raising is the only failure mode that surfaces at the call site rather than in a month-old
    spreadsheet.
    """
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    with pytest.raises(ValueError, match="unknown funnel event"):
        store.record(event="trail_paid")


def test_record_is_idempotent_per_key_and_unlimited_without_one(tmp_path):
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    assert store.record(event="lp_view", visitor_key="v1", idempotency_key="k1") is True
    # The return value is the assertion, not the count: a caller can only suppress a
    # double-send if it learns that it was second.
    assert store.record(event="lp_view", visitor_key="v1", idempotency_key="k1") is False
    assert store.event_counts() == {"lp_view": 1}
    # No key means no cap, on purpose: a two-part survey is two legitimate submissions.
    store.record(event="survey_submitted", visitor_key="v1")
    store.record(event="survey_submitted", visitor_key="v1")
    assert store.event_counts()["survey_submitted"] == 2


def test_two_different_keys_never_collide(tmp_path):
    """Dedupe keyed on the caller's string, so a per-event key cannot swallow a second
    visitor's identical action."""
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    assert store.record(event="trial_paid", visitor_key="a", idempotency_key="tp:a")
    assert store.record(event="trial_paid", visitor_key="b", idempotency_key="tp:b")
    assert store.event_counts()["trial_paid"] == 2


def test_every_instrumented_event_name_is_accepted(tmp_path):
    """The `EVENTS` vocabulary is the contract with the export, so a name in it that the store
    rejects is a metric that exists in one file and not the other."""
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    for name in telemetry.EVENTS:
        assert store.record(event=name, visitor_key="v", idempotency_key=f"k:{name}") is True
    assert len(store.event_counts()) == len(telemetry.EVENTS)


def test_unknown_card_funding_is_rejected(tmp_path):
    """`prepaid` vs `credit` is the leading indicator, so a misspelled funding type would
    silently leave the prepaid bucket empty rather than fail."""
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    with pytest.raises(ValueError, match="card funding"):
        store.record(event="trial_paid", card_funding="charge")


def test_missing_variant_identity_defaults_to_the_baseline(tmp_path):
    """A row with no variant cannot be attributed to a cohort, and a blank in that column would
    mix with the baseline rows in every `group by` rather than standing apart as broken."""
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    store.record(event="trial_paid", visitor_key="v", test_variable="", idempotency_key="k")
    row = store.events()[0]
    assert row["test_variable"] == "baseline", row
    assert row["offer_id"] == "baseline", row


def test_rows_bucket_by_utc_day_and_event(tmp_path):
    """Half-open day boundaries: a day that loses its last second or borrows the next day's
    first minute is a sheet whose days do not sum to the month."""
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    store.record(event="lp_view", visitor_key="late",
                 occurred_at="2026-11-20T23:59:59+00:00", idempotency_key="late")
    store.record(event="lp_view", visitor_key="early",
                 occurred_at="2026-11-21T00:00:00+00:00", idempotency_key="early")
    assert store.event_counts("2026-11-20") == {"lp_view": 1}
    assert len(store.events("2026-11-20")) == 1
    assert store.events("2026-11-20")[0]["visitor_key"] == "late"


def test_agent_log_appends_repeats_on_purpose(tmp_path):
    """Money moved or a processor signal arrived, so an agent triaging the account sees a named
    line rather than inferring it from a counter delta, with the event's own identity in
    `context` so one incident stays one query.

    The second identical line is not a bug: a repeated log line is evidence the handler ran
    twice, which is a fact worth being able to see. Callers that want the opposite pass a key.
    """
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    store.log(event="dispute_created", level="warning", context={"charge_id": "ch_1"})
    store.log(event="dispute_created", context={"charge_id": "ch_1"})
    rows = store.log_rows("dispute_created")
    assert len(rows) == 2, rows
    assert rows[0]["level"] == "warning"
    assert json.loads(rows[0]["context_json"])["charge_id"] == "ch_1"


def test_agent_log_can_dedupe_when_asked(tmp_path):
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    assert store.log(event="refund_issued", idempotency_key="log:evt_1") is True
    assert store.log(event="refund_issued", idempotency_key="log:evt_1") is False
    assert len(store.log_rows("refund_issued")) == 1


def test_agent_log_rejects_an_unknown_level(tmp_path):
    """Levels decide what a supervisor alerts on, and a typo'd level reads as a line nobody
    watches."""
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(tmp_path / "t.sqlite3")
    with pytest.raises(ValueError, match="log level"):
        store.log(event="refund_issued", level="warn")


# ===========================================================================
# 2. Funnel-surface events
# ===========================================================================

def test_landing_view_is_counted_once_per_visitor_per_day(tele_client):
    """Sessions, not page hits: a visitor who refreshes four times is one session, and the
    LP→trial rate is quoted against that number."""
    tele_client.get("/")
    tele_client.get("/")
    assert tele_client.events.event_counts(today()) == {"lp_view": 1}


def test_quiz_steps_and_completion_are_counted(tele_client):
    """The step count comes from the pages that render a question.

    `/onboarding` is deliberately not one of them and the assertion says so: that exact path
    renders nothing, it is the entry redirect to the first unanswered question, and the visit
    that follows is the one that reaches a screen. Counting the hop would charge the step to a
    redirect that showed nobody anything, and the hop fires on paths where nobody started a quiz
    at all.
    """
    get(tele_client, "/")
    get(tele_client, "/onboarding")
    assert tele_client.events.event_counts(today()) == {"lp_view": 1}, \
        "the quiz's entry redirect is not a quiz start"
    get(tele_client, "/onboarding/0")
    assert tele_client.events.event_counts(today()).get("quiz_started") == 1, \
        "the page that renders the first question is what starts the quiz"
    walk(tele_client)
    get(tele_client, "/summary")
    counts = tele_client.events.event_counts(today())
    assert counts.get("quiz_started") == 1, counts
    assert counts.get("quiz_completed") == 1, counts
    # `/summary` is reachable twice on the way to checkout; a finished quiz is finished.
    get(tele_client, "/summary")
    assert tele_client.events.event_counts(today())["quiz_completed"] == 1


def test_a_session_that_arrives_through_a_redirect_is_counted_once(tele_client):
    """The visit a redirected arrival produces, not the hop it bounced through.

    The arrival that most needs counting is an ad click on a campaign URL: something serves the
    landing page *and* bounces. The two halves of that have to be separated, and this test is the
    separation, because a `lp_view` written on the landing page is a session and a `quiz_started`
    written on a redirect that showed nobody a question is a lie about intent. Both live in one
    visit, so a rule that got either wrong moved one of the sheet's two denominators, and no
    other column on the sheet would have revealed it.

    `/onboarding` answers 302 and is not counted. Followed to the page it points at, the same
    visit *is* counted, once, on the page that rendered.
    """
    get(tele_client, "/onboarding", follow=True)
    counts = tele_client.events.event_counts(today())
    assert counts.get("quiz_started") == 1, \
        "the question page the redirect resolved to is the quiz start"
    assert "lp_view" not in counts, \
        f"a quiz entry is not a landing page view: {counts}"
    # The landing page, reached by a visitor who came in through the quiz and went back.
    get(tele_client, "/")
    assert tele_client.events.event_counts(today()) == {"quiz_started": 1, "lp_view": 1}


def test_the_landing_view_survives_a_bouncing_response(tele_client, monkeypatch):
    """A landing response that serves *and* bounces is still a session, which is the one case
    where a 3xx must count, and the reason `REDIRECT_COUNTS_AT` exists as a named set.

    No route in this build answers `/` with a 3xx, so the rule is called directly rather than the
    app bent to fit the test: adding a redirect to the landing page just to satisfy a telemetry
    rule would change the customer-facing page to prove a point about a counter.

    `count_page_view` is the seam the middleware calls, and it is a named function for exactly
    this: a rule buried in an async middleware is a rule no test can poke at without driving the
    whole stack. An earlier version of this test rebound `route.endpoint` on the router and got a
    silent 200 back, which is the trap worth naming: FastAPI builds the view function at
    decoration time, so patching the route attribute changes nothing that serves, and the only
    reason it was caught is that the assertion here reads the flag the middleware passed rather
    than trusting the response.
    """
    seen: list[tuple[str, bool]] = []
    real = tele_client.main.count_page_view

    def spy(visitor_key: str, path: str, *, redirecting: bool = False) -> None:
        seen.append((path, redirecting))
        real(visitor_key, path, redirecting=redirecting)

    monkeypatch.setattr(tele_client.main, "count_page_view", spy)
    # Called directly rather than served, because no route answers `/` with a 3xx and the point is
    # the rule, not the page. The middleware's half, that a 3xx is reported as one, is pinned by
    # `test_the_middleware_reports_a_bounce` below.
    tele_client.main.count_page_view("visitor-bounce", "/", redirecting=True)
    assert seen and seen[-1] == ("/", True), seen
    assert tele_client.events.event_counts(today()) == {"lp_view": 1}, \
        tele_client.events.events()
    # And the same call for a lower step is silent, which is the other half of the rule.
    tele_client.main.count_page_view("visitor-bounce", "/checkout", redirecting=True)
    assert tele_client.events.event_counts(today()) == {"lp_view": 1}, \
        "a bounced checkout screen is not a checkout view"
    from fastapi.responses import RedirectResponse

    def bouncing(request):
        return RedirectResponse(url="/?utm_resurfaced=1", status_code=307)

    monkeypatch.setattr(tele_client.main, "landing", bouncing)
    get(tele_client, "/")


def test_the_middleware_reports_a_bounce_as_a_bounce(tele_client, monkeypatch):
    """The middleware's half of the redirect rule: it must *tell* the counter when a response 3xx'd.

    Asserted through a real redirecting route, `/onboarding`, rather than by reading the
    expression. The reason is what this line looked like when it was wrong: it read
    `response.is_redirect`, and the object coming out of `call_next` is starlette's
    `_StreamingResponse`, which has no such attribute. Every page view in the suite then vanished
    into the `except`, which is there so instrumentation can never break a page, and the only
    trace was a warning line nobody read. A test that asserts the flag arrived catches that; a
    test that asserts the counter's logic does not.
    """
    seen: list[tuple[str, bool]] = []
    real = tele_client.main.count_page_view

    def spy(visitor_key: str, path: str, *, redirecting: bool = False) -> None:
        seen.append((path, redirecting))
        real(visitor_key, path, redirecting=redirecting)

    monkeypatch.setattr(tele_client.main, "count_page_view", spy)
    get(tele_client, "/onboarding")                       # 302
    get(tele_client, "/")                                 # 200
    get(tele_client, "/onboarding/0")                     # 200
    assert ("/onboarding", True) in seen, seen
    assert ("/", False) in seen, seen
    assert not any(path == "/" and flag for path, flag in seen), \
        "a rendered landing page must not be reported as a bounce"


def test_page_view_tracking_skips_member_and_ops_surfaces(tele_client):
    """A returning member reading `/dashboard` must not land in the LP denominator, `/health`
    polled by a timer is not funnel traffic, and `/checkout/complete` is *after* the card
    screen, so counting it would inflate the step the checkout→trial rate divides by."""
    get(tele_client, "/")
    get(tele_client, "/health")
    get(tele_client, "/dashboard")
    get(tele_client, "/policy/terms")
    # The redirect branch specifically, not just the exact-path exclusion: with follow=True this
    # request is a 302 on a path that would otherwise prefix-match `/checkout`.
    get(tele_client, "/checkout/complete", follow=True)
    get(tele_client, "/nonexistent-page")
    counts = tele_client.events.event_counts(today())
    # Nothing page-shaped arrived from the member and ops surfaces. `trial_started` *is* in there
    # and belongs: `/checkout/complete` is a commitment, and leaving it out of the page-view
    # counters is not the same claim as pretending the visit never converted.
    assert counts == {"lp_view": 1, "trial_started": 1}, counts
    for surface in ("checkout_viewed", "preview_shown", "quiz_started", "quiz_completed"):
        assert surface not in counts, f"{surface} leaked from a non-funnel surface"


def test_preview_and_checkout_views_are_counted_per_visit(tele_client):
    """Diagnostic steps, deliberately un-deduped: the checkout→trial step rate is a *visit*
    rate, and a visitor who bails to the summary and returns to the card screen genuinely viewed
    it twice."""
    tele_client.get("/")
    walk(tele_client)
    tele_client.get("/preview")
    tele_client.get("/preview")
    tele_client.get("/checkout")
    counts = tele_client.events.event_counts(today())
    assert counts.get("preview_shown") == 2, counts
    assert counts.get("checkout_viewed") == 1, counts


def test_telemetry_failure_does_not_break_a_page(tele_client):
    """Instrumentation sits beside the page, not in front of it. A broken store must cost a
    metrics row and nothing else."""
    class Broken:
        def record(self, **_kwargs):
            raise RuntimeError("disk gone")

        def log(self, **_kwargs):
            raise RuntimeError("disk gone")

    tele_client.main.billing.telemetry = Broken()
    response = tele_client.get("/")
    assert response.status_code == 200, response.text[:300]
    assert post(tele_client, "/onboarding/0", [("answer", "energy")]).status_code in (200, 302)


def test_a_service_without_telemetry_behaves_as_phase_two_did(tmp_path):
    """The parameter is optional, so a caller that predates Phase 3 keeps working. Had it been
    required, every Phase 2 call site would raise at the first tracked action."""
    billing_module = importlib.import_module("billing")
    billing_store = importlib.import_module("billing_store")
    mailer = importlib.import_module("mailer")
    config = importlib.import_module("config")
    settings = config.FunnelSettings.from_env()
    store = billing_store.BillingStore(tmp_path / "b.sqlite3")
    service = billing_module.BillingService(settings, store, mailer.Mailer(settings), None)
    assert service.telemetry is None
    # A tracked action on an uninstrumented service is a no-op rather than an AttributeError, so
    # the checkout on that path still returns its row.
    assert service.track("lp_view", visitor_key="v") is False


def test_track_swallows_a_raising_store(live_client):
    """And a raising store costs the row, not the cancellation."""
    class Exploding:
        def record(self, **_kwargs):
            raise RuntimeError("locked")

        def log(self, **_kwargs):
            raise RuntimeError("locked")

    live_client.main.billing.telemetry = Exploding()
    assert live_client.main.billing.track("trial_paid", visitor_key="v") is False


# ===========================================================================
# 3. Conversion events, live mode
# ===========================================================================

def test_completed_checkout_records_start_and_paid(live_client):
    """Two events from one purchase, because the sheet computes two ratios from them: Trial→Paid
    needs a start in the denominator and a payment in the numerator."""
    subscribe(live_client)
    counts = live_client.events.event_counts(today())
    assert counts.get("trial_started") == 1, counts
    assert counts.get("trial_paid") == 1, counts


def test_rehearsal_completion_counts_a_start_but_not_a_payment(client):
    """Rehearsal records intent. Calling that a paid conversion would put a phantom capture in
    both the numerator and the money columns, and every ratio downstream would read as better
    than the funnel is. This is the plain `client` fixture: charges off, no key."""
    instrumented = with_store(client)
    instrumented.get("/")
    walk(instrumented)
    response = post(instrumented, "/checkout", [("email", "payer@example.com")])
    assert response.status_code == 200, response.text[:300]
    counts = instrumented.events.event_counts(today())
    assert counts.get("trial_started") == 1, counts
    assert "trial_paid" not in counts, counts


def test_conversion_row_carries_the_money_and_the_processor_id(live_client):
    """The row a refund ratio is built from: the captured amount, the currency, and the
    subscription id to paste into the dashboard when a ratio trips. Funding type is asserted to
    be in the vocabulary rather than to be a specific value, because the fake's payment method is
    a rehearsal object and pinning a BIN here would test the double instead of the plumbing."""
    recorder = live_client.fake()
    session_id = subscribe(live_client)
    recorder.sessions[session_id]["subscription"]
    rows = rows_of(live_client, "trial_paid")
    assert rows, live_client.events.events()
    row = rows[0]
    assert row["amount_minor"] == 100, row
    assert row["currency"] == "usd", row
    # The *session* id, not the subscription id. The conversion row is written from the checkout
    # return, the session id is what the redirect carried and what a customer quotes in an email,
    # and the subscription id reaches the later rows (invoice, refund, dispute) instead.
    assert row["reference_id"] == session_id, row
    assert row["visitor_key"] == visitor_of(live_client), row
    assert row["card_funding"] in ("credit", "debit", "prepaid", "unknown", ""), row


def test_a_second_trial_by_the_same_visitor_is_a_second_start(live_client):
    """A churned member who comes back for a trial on a *later day* is a second cohort member,
    and the sheet's `trials_started` counts them. Keying on the visitor alone would report them
    once forever and shrink the denominator permanently.

    The day is part of the key rather than the session id, and the pair of tests here is why:
    the same person completing twice inside one UTC day (this file's next test) is the *same*
    commitment arriving over two transports, and a session-scoped key counted it twice, which is
    a factor of two on the denominator of Trial→Paid.
    """
    subscribe(live_client)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    # Backdate the *idempotency key*, not just `occurred_at`. The key carries the day this event
    # was credited to, which is the thing the unique index compares, and rewriting the timestamp
    # alone leaves a today-shaped key sitting on a yesterday-shaped row: a second real start today
    # collides with a row that no longer claims today. The event store is written through
    # `telemetry.record`, so there is a private copy open per test rather than one to share, and
    # connecting by path is the same shortcut `rows_of` already takes.
    with sqlite3.connect(billing_path_of()) as conn:
        conn.execute("update trial_events set occurred_at = replace(occurred_at, ?, ?), "
                     "idempotency_key = replace(idempotency_key, ?, ?) "
                     "where event = 'trial_started'",
                     (today(), yesterday, today(), yesterday))
    second_row = post(live_client, "/checkout/start", [("email", "payer@example.com")])
    assert second_row.status_code == 303, second_row.text[:400]
    second = second_row.headers["location"].rsplit("/", 1)[-1]
    live_client.get(f"/checkout/complete?session_id={second}")
    assert len(rows_of(live_client, "trial_started")) == 2, live_client.events.events()


def test_the_same_commitment_arriving_twice_is_one_start(live_client):
    """The bug the day-scoped key exists for. `/checkout/complete` records the commitment from
    the browser redirect, and `checkout.session.completed` records it again from the webhook a
    moment later, holding a different identifier for the same sale. Two rows for one visitor on
    one day halved Trial→Paid, and nothing else on the sheet looked wrong."""
    subscribe(live_client)
    recorder = live_client.fake()
    session_id = recorder.sessions[list(recorder.sessions)[-1]]["id"] if recorder.sessions else ""
    hook(live_client, stripe_event("checkout.session.completed", {
        "id": session_id or "cs_replay", "object": "checkout.session",
        "client_reference_id": visitor_of(live_client),
        "subscription": live_client.fake().sessions[session_id]["subscription"]
        if session_id in live_client.fake().sessions else "sub_replay",
        "amount_total": 100, "currency": "usd",
        "metadata": {"visitor_key": visitor_of(live_client)}}, event_id="evt_replay"))
    assert len(rows_of(live_client, "trial_started")) == 1, live_client.events.events()


# ===========================================================================
# 4. Cancel, save offer, survey
# ===========================================================================

def test_one_click_cancel_is_counted_once_per_cancel_event(live_client):
    """A double click is the case the key exists for, and `cancel_now` returns "already
    cancelled" on the second visit, so this asserts both halves: one row, and no crash."""
    subscribe(live_client)
    assert post(live_client, "/account/cancel", []).status_code == 200
    assert post(live_client, "/account/cancel", []).status_code == 200
    assert len(rows_of(live_client, "cancel_clicked")) == 1, live_client.events.events()


def test_save_offer_selection_is_counted_and_names_its_option(live_client):
    """One funnel-level name because the sheet counts "saves taken" as one number and §3.2 test
    6 compares saves against confirmed cancels; the per-option name stays in `detail` so the
    drill-down does not need a second table."""
    subscribe(live_client)
    post(live_client, "/account/cancel", [])
    response = post(live_client, "/account/cancel/offer", [("offer", "pause")])
    assert response.status_code == 200, response.text[:400]
    rows = rows_of(live_client, "save_offer_selected")
    assert len(rows) == 1, rows
    assert json.loads(rows[0]["detail_json"])["offer_event"] == "pause_selected", rows[0]


def test_survey_submission_is_counted(live_client):
    subscribe(live_client)
    post(live_client, "/account/cancel", [])
    response = post(live_client, "/account/survey",
                    [("reason", "too_expensive"), ("note", "third party pays for two")])
    assert response.status_code == 200, response.text[:400]
    rows = rows_of(live_client, "survey_submitted")
    assert len(rows) == 1, rows
    assert json.loads(rows[0]["detail_json"])["reason"] == "too_expensive"


def test_a_failed_offer_applied_nothing_and_counted_nothing(live_client):
    """A save offer the processor refused is not a save taken. Counting it would report a
    retention win that never happened, in the same table that decides the save-offer test."""
    recorder = live_client.fake()
    subscribe(live_client)
    post(live_client, "/account/cancel", [])
    recorder.reset()
    recorder.fail_mutations_of("/subscriptions", 402, "card declined")
    response = post(live_client, "/account/cancel/offer", [("offer", "pause")])
    assert response.status_code == 200, response.text[:400]
    assert "could not be applied" in response.text.lower(), response.text[:400]
    assert not rows_of(live_client, "save_offer_selected"), live_client.events.events()


# ===========================================================================
# 5. Risk signals: refunds, disputes, Radar warnings
# ===========================================================================

def test_refund_is_counted_once_and_written_to_the_agent_log(live_client):
    """Money moved, so it is both a counted row and a visible line: the refund rate gates
    nothing on its own, but a refund is what a support agent needs to see named."""
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    event = stripe_event("charge.refunded", {
        "id": "ch_1", "object": "charge", "amount": 100, "amount_captured": 100, "paid": True,
        "status": "succeeded", "currency": "usd", "subscription": subscription_id,
        "payment_method_details": {"card": {"funding": "prepaid", "country": "US"}},
        "metadata": {"visitor_key": visitor_of(live_client)}}, event_id="evt_refund_1")
    first = hook(live_client, event)
    assert first.status_code == 200, first.text[:300]
    assert "refund_issued:counted" in first.json()["result"], first.json()
    second = hook(live_client, event)
    assert "duplicate" in second.json()["result"], second.json()
    rows = rows_of(live_client, "refund_issued")
    assert len(rows) == 1, rows
    assert rows[0]["amount_minor"] == 100
    assert rows[0]["refunded_minor"] == 100
    assert rows[0]["card_funding"] == "prepaid", "the refund must carry the BIN type it came with"
    assert len(live_client.events.log_rows("refund_issued")) == 1


def test_dispute_is_counted_once_against_authorised_volume(live_client):
    """The 1% ceiling's numerator. Keyed on the Stripe event id because that is exactly what a
    redelivery repeats, and a double-counted dispute on a thin day moves a kill metric."""
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    event = stripe_event("charge.dispute.created", {
        "id": "dp_1", "object": "dispute", "status": "needs_response",
        "subscription": subscription_id,
        "charge": {"id": "ch_9", "amount": 3900, "amount_captured": 3900, "paid": True,
                   "payment_method_details": {"card": {"funding": "prepaid", "country": "US"}},
                   "metadata": {"visitor_key": visitor_of(live_client)}}}, event_id="evt_disp_1")
    assert "dispute_created:counted" in hook(live_client, event).json()["result"]
    assert "duplicate" in hook(live_client, event).json()["result"]
    rows = rows_of(live_client, "dispute_created")
    assert len(rows) == 1, rows
    # The ratio's denominator is *authorised* volume, per §3.4's formula, and its numerator is
    # what the cardholder contests. Those are different numbers on purpose.
    assert rows[0]["amount_minor"] == 3900, rows[0]
    assert rows[0]["gross_volume_minor"] == 3900, rows[0]
    assert rows[0]["card_funding"] == "prepaid", rows[0]
    assert rows[0]["reference_id"] == "ch_9", rows[0]
    warnings = live_client.events.log_rows("dispute_created")
    assert len(warnings) == 1 and warnings[0]["level"] == "warning", warnings


def test_a_dispute_we_cannot_match_is_still_counted(live_client):
    """The §3.3 case: an event for a subscription we never saw is precisely the one a lane
    cannot afford to drop, because the ceiling it feeds is the one that closes accounts."""
    subscribe(live_client)
    event = stripe_event("charge.dispute.created", {
        "id": "dp_unmatched", "status": "needs_response",
        "charge": {"id": "ch_orphan", "amount": 3900, "amount_captured": 3900, "paid": True,
                   "metadata": {"visitor_key": "someone-else"}}}, event_id="evt_orphan")
    response = hook(live_client, event)
    assert response.status_code == 200, response.text[:300]
    assert "dispute_created:counted" in response.json()["result"], response.json()
    rows = rows_of(live_client, "dispute_created")
    assert len(rows) == 1 and rows[0]["visitor_key"] == "someone-else", rows


def test_dispute_updates_do_not_double_count(live_client):
    """`.updated`/`.closed` move who holds the money, not whether a dispute happened. Counting
    the same cardholder complaint three times would trip a 1% ceiling on one complaint."""
    subscribe(live_client)
    for suffix, status in (("updated", "won"), ("closed", "won")):
        response = hook(live_client, stripe_event(f"charge.dispute.{suffix}", {
            "id": "dp_2", "status": status,
            "charge": {"id": "ch_2", "amount": 3900, "amount_captured": 3900, "paid": True}},
            event_id=f"evt_{suffix}"))
        assert response.status_code == 200, response.text[:200]
        assert "counted-at-creation" in response.json()["result"], response.json()
    assert not rows_of(live_client, "dispute_created"), live_client.events.events()


def test_radar_early_warning_named_fraud_is_counted(live_client):
    """The ≤ 0.5% gate, and §3.3's finding that fraud-not-chargeback has no home in any table.
    This test is the reason Phase 3 exists."""
    subscribe(live_client)
    event = stripe_event("radar.early_warning.created", {
        "id": "rs_1", "object": "radar_early_warning", "warning_reason": "fraudulent",
        "livemode": True,
        "payment_intent": {"id": "pi_1", "amount": 100, "currency": "usd",
                           "card_details": {"brand": "visa", "funding": "prepaid",
                                            "country": "US"},
                           "metadata": {"visitor_key": visitor_of(live_client)}}},
        event_id="evt_rs_1")
    assert "radar_early_warning:counted" in hook(live_client, event).json()["result"]
    assert "duplicate" in hook(live_client, event).json()["result"]
    rows = rows_of(live_client, "radar_early_warning")
    assert len(rows) == 1, rows
    assert rows[0]["card_funding"] == "prepaid", rows[0]
    assert json.loads(rows[0]["detail_json"])["event_type"] == "radar.early_warning.created"
    assert len(live_client.events.log_rows("radar_early_warning")) == 1


def test_a_flat_network_radar_warning_is_still_parsed(live_client):
    """The live early-warning shape carries `network.card_funding` rather than the documented
    `card_details.funding`. A warning we cannot classify is the worst one to discard, so both
    spellings are read."""
    event = stripe_event("radar.early_warning.created", {
        "id": "rs_flat", "warning_reason": "fraudulent", "livemode": True,
        "network": {"card_funding": "prepaid", "card_brand": "visa", "card_country": "US"}},
        event_id="evt_rs_flat")
    assert "radar_early_warning:counted" in hook(live_client, event).json()["result"]
    rows = rows_of(live_client, "radar_early_warning")
    assert rows and rows[-1]["card_funding"] == "prepaid", rows


def test_a_radar_warning_inherits_the_charge_it_warns_about(live_client):
    """A Radar warning names the *intent*, and the warning object itself carries no customer at
    all while the intent's metadata carries the visitor key. Without that hop the fraud numerator
    would land with an empty `visitor_key` on every row, which is the one column a cohort
    question needs."""
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    event = stripe_event("radar.early_warning.created", {
        "id": "rs_hop", "warning_reason": "fraudulent", "livemode": True,
        "payment_intent": {"id": "pi_hop", "amount": 100, "subscription": subscription_id}},
        event_id="evt_rs_hop")
    assert "radar_early_warning:counted" in hook(live_client, event).json()["result"]
    rows = rows_of(live_client, "radar_early_warning")
    assert rows[-1]["visitor_key"] == visitor_of(live_client), rows[-1]


def test_a_non_fraud_radar_advice_is_logged_not_counted(live_client):
    """`generic_complaint` is Radar's suggestion, not the fraud numerator. Counting advice would
    stop paid traffic on the wrong signal, which is the failure the spec calls out as costing
    revenue for no protection."""
    event = stripe_event("radar.early_warning.created", {
        "id": "rs_2", "warning_reason": "generic_complaint", "livemode": True,
        "payment_intent": {"id": "pi_2", "amount": 100,
                           "card_details": {"funding": "credit"}}}, event_id="evt_rs_2")
    response = hook(live_client, event)
    assert response.status_code == 200
    assert "not-fraud-flagged" in response.json()["result"], response.json()
    assert not rows_of(live_client, "radar_early_warning"), live_client.events.events()


def test_an_unknown_event_type_is_still_ignored_with_a_200(live_client):
    """Stripe retries a 5xx forever, so an event this lane does not use must look like success.
    Phase 3 widened the handled set; this guards the door that stayed shut."""
    response = hook(live_client, stripe_event("billing.alert.triggered", {"id": "al_1"}))
    assert response.status_code == 200, response.text[:200]
    assert response.json()["result"] == "ignored:billing.alert.triggered"


def test_a_bad_signature_is_still_a_400_after_the_new_branches(live_client):
    """The signature check has to stay in front of dispatch, and the dispute/Radar/refund branch
    added in Phase 3 is exactly where a rewrite would park it *after* the routing instead."""
    body = json.dumps(stripe_event("charge.dispute.created", {"id": "dp_sig"}))
    response = live_client.request(
        "POST", "/webhooks/stripe", content=body,
        headers={"content-type": "application/json",
                 "stripe-signature": f"t={int(time.time())},v1={'de' * 32}"})
    assert response.status_code == 400, response.text[:200]
    assert not rows_of(live_client, "dispute_created"), live_client.events.events()


def test_the_live_flag_is_read_from_the_key_stripe_actually_sends(live_client):
    """`livemode` on the event, not `live_mode`, because that is the envelope Stripe sends.

    A key that appears in no payload parses as False forever, and the failure is invisible in the
    one direction that matters: the mode-mismatch guard is what keeps another merchant account's
    disputes and refunds out of our 1% and 0.5% ceilings, so a misconfigured webhook delivering
    real-money events to a test-key app reported a *clean* ceiling while the misconfiguration
    streamed past it. Every fixture that wanted a live event had to write the wrong spelling, which
    is how both sides agreed and nothing failed.

    This asserts against the vendor's spelling rather than our parser's, so it fails when the
    parser drifts and not when someone renames a helper.
    """
    event = {"id": "evt_vendor_shape", "object": "event", "livemode": True,
             "type": "charge.dispute.created",
             "data": {"object": {"id": "dp_vendor", "status": "needs_response",
                                 "charge": {"id": "ch_vendor", "amount": 3900,
                                            "amount_captured": 3900, "paid": True}}}}
    summary = live_client.stripe.event_from_webhook(event)
    assert summary["live_mode"] is True, \
        "`livemode` on the envelope is the vendor's field; a parser that reads only `live_mode` " \
        "silently disables every cross-account guard"
    response = hook(live_client, event)
    # `ignored:test-mode-account-mismatch`, and the reason string names the origin: the log has to
    # tell "another account's event" apart from "an event we do not use", because the first is an
    # incident and the second is Tuesday.
    assert "test-mode-account-mismatch" in response.json()["result"], response.json()
    assert not rows_of(live_client, "dispute_created"), live_client.events.events()


def test_an_app_with_no_client_still_recognises_a_foreign_event(tmp_path):
    """No client is the strongest "not our money" answer there is, not the weakest.

    A lane with no client cannot take money from anyone, so a live-mode event delivered to it is by
    definition another merchant account's. The guards used to ask `client is not None and
    key_type == "test"`, which answered False for the missing client, and that answer reads to a
    reviewer as "checked, found live keys" when nothing was checked at all. Every rehearsal app
    behaved this way, which is precisely the fleet-wide configuration error the guard exists to
    catch: a webhook wired at the wrong account, quietly counting that account's disputes toward our
    1% ceiling while the ceiling reported clean.

    Built without an app, because the claim is about the service's own posture and constructing one
    to prove it would let the app's arming decide the answer instead of the guard.
    """
    billing = importlib.import_module("billing")
    billing_store = importlib.import_module("billing_store")
    mailer = importlib.import_module("mailer")
    config = importlib.import_module("config")
    settings = config.FunnelSettings.from_env()
    service = billing.BillingService(settings, billing_store.BillingStore(
        tmp_path / "no-client.sqlite3"), mailer.Mailer(settings), None)
    assert service.holds_test_keys() is True, \
        "a lane that cannot charge must not classify a live event as its own"
    assert service.live is False, "and it is still rehearsal: the guard is not an arming switch"


def test_a_live_event_on_test_keys_never_enters_our_ratios(live_client, monkeypatch):
    """A live-mode event while we hold test keys means another account is taking this lane's
    money. Applying it would import that account's disputes into our ceiling; discarding it
    silently would hide the misconfiguration. So: one named log line, zero counted rows.

    The no-funnel-client case is built by removing the key and disarming, rather than by posting a
    live event at a live app, which cannot happen with `FakeStripe` in the way. `client.pin.disarm`
    is what makes that possible, and it is deliberate that the descriptor needs to be *told* rather
    than simply tolerating `None`: `BillingService.client` is pinned at class level for the whole
    `live` app, the app re-arms itself on several routes, and a blanket pass for `None` would let a
    re-arm or an unrelated route silently take the recorder away and leave this test asserting
    against a client it never configured. Here the state is asked for, the re-arm is pointed at
    `None`, and the assertion below proves the arrangement held rather than assuming it.

    Note which side of the guard does the work: `mode_mismatch` is decided from the event's own
    `livemode` flag against the key type this app holds, so `client is None` must *not* be what
    produces the answer. The assert after the response is what pins that.
    """
    monkeypatch.delenv("STRIPE_SECRET_KEY_FUNNEL", raising=False)
    monkeypatch.delenv("STRIPE_PUBLISHABLE_KEY_FUNNEL", raising=False)
    # The disarmed state, *derived* the way production derives it: `main.billing_client` re-reads
    # the environment, the key is gone, so the app is disarmed and stays disarmed across the
    # webhook route's own re-arm. A hand-written `billing.client = None` would not have survived the
    # first request, and the guard would have been measured on an app holding a client.
    assert live_client.refresh_client() is None
    assert live_client.main.billing.client is None
    event = stripe_event("charge.dispute.created", {
        "id": "dp_other", "status": "needs_response", "livemode": True,
        "charge": {"id": "ch_other", "amount": 3900, "amount_captured": 3900, "paid": True}},
        event_id="evt_other_acct", live_mode=True)
    response = hook(live_client, event)
    assert response.status_code == 200, response.text[:300]
    assert "test-mode-account-mismatch" in response.json()["result"], response.json()
    assert not rows_of(live_client, "dispute_created"), live_client.events.events()
    # Note what the webhook route did on the way in: `billing.client = billing_client()`, which is
    # the re-arm, and the reason a disarmed app has to be armed through the factory rather than by
    # writing the attribute. Writing `None` and letting the route re-arm would have had the guard
    # decide "we hold test keys" from a client that is present, which is a different app than the
    # one this test says it built, and it passes either way.
    assert live_client.main.billing.client is None, \
        "the webhook route re-arms on the way in, and that re-arm must not invent a client the " \
        "environment does not support"
    monkeypatch.setenv("STRIPE_SECRET_KEY_FUNNEL", "sk_test_rearmed_after_incident")
    assert live_client.refresh_client() is live_client.recorder, \
        "put the key back and the same route re-arms, which is the behaviour that makes this a " \
        "state rather than a fixture trick"
    # Named, so the misconfiguration is visible in the same place the ratios are read from.
    assert any("mode" in r["event"] or "mismatch" in r["event"]
               for r in live_client.events.log_rows()), live_client.events.log_rows()


# ===========================================================================
# 6. Rebill and decline telemetry
# ===========================================================================

def test_a_successful_rebill_is_counted_once(live_client):
    """A renewal is a distinct conversion event from the trial capture, and it is the revenue
    the lane is actually about."""
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    future = int(time.time()) + 29 * 86400
    event = stripe_event("invoice.paid", invoice_body(
        future, subscription_id=subscription_id, invoice_id="in_real",
        metadata={"visitor_key": visitor_of(live_client)}), event_id="evt_invoice_1")
    response = hook(live_client, event)
    assert response.status_code == 200, response.text[:300]
    rebills = rows_of(live_client, "rebill_succeeded")
    assert len(rebills) == 1, rebills
    assert rebills[0]["amount_minor"] == 3900, rebills[0]
    assert rebills[0]["gross_volume_minor"] == 3900, rebills[0]


def test_the_trial_invoice_is_not_also_a_rebill(live_client):
    """Counting the $1 trial capture twice would inflate both the conversion count and the
    authorised volume it divides by, and the trial row already landed via checkout."""
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    row = live_client.billing.store.get_subscription(visitor_of(live_client))
    same_period = int(datetime.fromisoformat(row["current_period_end"]).timestamp())
    hook(live_client, stripe_event("invoice.paid", invoice_body(
        same_period, subscription_id=subscription_id, invoice_id="in_trial", amount=100,
        metadata={"visitor_key": visitor_of(live_client)}), event_id="evt_trial_invoice"))
    assert not rows_of(live_client, "rebill_succeeded"), live_client.events.events()


def test_a_first_decline_records_the_retry_date(live_client):
    """§2.1: one retry at day +3, then stop. The date, in the row, so the export can tell
    "waiting for the retry" apart from "lost" without a marker table."""
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    event = stripe_event("invoice.payment_failed", invoice_body(
        int(time.time()) + 86400, status="past_due", subscription_id=subscription_id,
        invoice_id="in_fail", attempted=False, attempt_count=1,
        customer_details={"email": "payer@example.com"},
        metadata={"visitor_key": visitor_of(live_client)}), event_id="evt_decline_1")
    response = hook(live_client, event)
    assert response.status_code == 200, response.text[:300]
    declines = rows_of(live_client, "rebill_declined")
    assert len(declines) == 1, declines
    detail = json.loads(declines[0]["detail_json"])
    assert detail.get("retry_on", "").endswith("+3d"), detail
    assert detail.get("terminal") is False, detail
    assert not rows_of(live_client, "rebill_decline_terminal"), \
        "a first decline is a scheduled retry, not a lost member"


def test_a_decline_after_the_retry_window_is_a_named_loss(live_client):
    """The unrecoverable-decline reading the sheet's `rebill_declines_silent` column means.
    `attempted: True` on a failure this far past the period is Stripe saying it already spent the
    retry, and that is a fact about the invoice rather than something to infer from a marker we
    may never have set."""
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    old = int(time.time()) - 9 * 86400
    event = stripe_event("invoice.payment_failed", invoice_body(
        old, status="past_due", subscription_id=subscription_id, invoice_id="in_late",
        attempted=True, attempt_count=2, customer_details={"email": "payer@example.com"},
        metadata={"visitor_key": visitor_of(live_client)}), event_id="evt_decline_late")
    assert hook(live_client, event).status_code == 200
    assert len(rows_of(live_client, "rebill_decline_terminal")) == 1, live_client.events.events()
    assert len(live_client.events.log_rows("rebill_decline_terminal")) == 1


def test_a_retry_still_owed_inside_the_window_is_not_a_loss(live_client):
    """A second failure inside the retry window is the retry itself, and it is not a lost member.

    The `attempt_count >= 2` floor earns its place here rather than in the aged case: the export is
    run daily, the deadline reads as crossed at the first hour past midnight, and without the floor
    a decline whose retry is due *this morning* books a loss this morning while a late-delivered
    duplicate of the same event books a loss three days ago. Same invoice, two loss dates.
    """
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    # Two days past the period: the +3 is not yet spent, whatever the processor's own ladder says.
    near = int(time.time()) - 2 * 86400
    hook(live_client, stripe_event("invoice.payment_failed", invoice_body(
        near, status="past_due", subscription_id=subscription_id, invoice_id="in_due_today",
        attempted=True, attempt_count=2, customer_details={"email": "payer@example.com"},
        metadata={"visitor_key": visitor_of(live_client)}), event_id="evt_due_today"))
    assert len(rows_of(live_client, "rebill_declined")) == 1, live_client.events.events()
    assert not rows_of(live_client, "rebill_decline_terminal"), live_client.events.events()


def test_replayed_declines_never_invent_a_loss(live_client):
    """Stripe re-sends `invoice.payment_failed` per attempt. Every attempt is an attempt (the
    sheet's decline *rate* needs all of them) but a replay must not manufacture a churn it did not
    witness, or the loss column stops meaning churn. The mailer already behaves this way through a
    per-period marker, so telemetry agreeing with it is the property under test.

    The version this replaced read `attempted` plus "we hold an email" as the processor confirming
    it had spent our retry, and declared a churn on attempt two of a period that had not aged at
    all. The processor's ladder runs in hours; ours runs in the calendar days of §2.1, and only our
    own clock can say the +3 is spent.
    """
    session_id = subscribe(live_client)
    subscription_id = live_client.fake().sessions[session_id]["subscription"]
    period = int(time.time()) + 86400
    for attempt in range(3):
        response = hook(live_client, stripe_event("invoice.payment_failed", invoice_body(
            period, status="past_due", subscription_id=subscription_id, invoice_id="in_1",
            attempted=attempt > 0, attempt_count=attempt + 1,
            customer_details={"email": "payer@example.com"},
            metadata={"visitor_key": visitor_of(live_client)}),
            event_id=f"evt_fail{attempt}"))
        assert response.status_code == 200, response.text[:300]
    assert len(rows_of(live_client, "rebill_declined")) == 3, live_client.events.events()
    assert not rows_of(live_client, "rebill_decline_terminal"), live_client.events.events()


# ===========================================================================
# 7. Abandoned checkouts
# ===========================================================================

def test_an_expired_checkout_is_counted_so_the_denominator_is_honest(live_client):
    """Without this row, Trial→Paid divides only by conversions and always reads optimistic.
    Keyed on the session id, so a redelivered expiry is one row."""
    recorder = live_client.fake()
    session_id = subscribe(live_client)
    recorder.reset()
    event = stripe_event("checkout.session.expired", {
        "id": session_id, "object": "checkout.session", "amount_total": 100,
        "client_reference_id": visitor_of(live_client),
        "payment_method_details": {"card": {"funding": "prepaid", "country": "US"}},
        "metadata": {"visitor_key": visitor_of(live_client)}}, event_id="evt_expiry_1")
    assert hook(live_client, event).status_code == 200
    assert "expired" in hook(live_client, event).json()["result"]
    rows = rows_of(live_client, "checkout_expired")
    assert len(rows) == 1, rows
    assert rows[0]["card_funding"] == "prepaid", rows[0]


# ===========================================================================
# 8. The Day-1 export
# ===========================================================================

@pytest.fixture()
def sheet(tmp_path, monkeypatch):
    """An isolated metrics world: throwaway databases, no funnel key, no network.

    The billing database is *created* here, empty, because that is the world the export runs in:
    the funnel opens it at startup, so a day with no traffic is a database with no rows, not a
    missing file. Making the file exist is what lets the empty-day test assert the thing it
    actually cares about (blank ratios) rather than tripping over the export refusing to guess
    whether the funnel has ever run. The missing-file case stays covered, separately, by
    `test_export_reports_a_missing_or_uninstrumented_database`.
    """
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(tmp_path / "billing.sqlite3"))
    monkeypatch.setenv("FUNNEL_DB_PATH", str(tmp_path / "responses.sqlite3"))
    monkeypatch.delenv("STRIPE_READ_KEY_FUNNEL", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY_FUNNEL", raising=False)
    importlib.import_module("telemetry").TelemetryStore(billing_path_of())
    export = load_export_script()
    return {"export": export, "tmp": tmp_path,
            "csv": tmp_path / "metrics" / "lane_c_daily.csv"}


def billing_path_of() -> Path:
    return Path(importlib.import_module("config").FunnelSettings.from_env()
                .billing_database_abspath)


def seed_day(sheet, day: str, *, visitors: int, paid: int, prepaid: int = 0,
             refunds: int = 0, disputes: int = 0, fraud: int = 0, rebills: int = 0,
             declined: int = 0, terminal: int = 0, abandoned: int = 0,
             variant: str = "baseline", cancel: int = 0, survey: int = 0) -> None:
    """Write one day's events straight into the store the export reads.

    Visitor keys are deterministic and shared across the loops on purpose: `p0` commits, pays,
    and (for `cancel`/`survey`) cancels, so one member produces a coherent row rather than a set
    of unrelated counts. That matters because several columns count *distinct visitors*
    (`lp_views`, `paid_conversions`, `trials_started`) and several count rows, and a fixture that
    used fresh keys everywhere would make the two readings unable to disagree with each other.
    """
    telemetry = importlib.import_module("telemetry")
    store = telemetry.TelemetryStore(billing_path_of())
    for index in range(visitors):
        visitor = f"{day}-v{index}"
        store.record(event="lp_view", visitor_key=visitor, test_variable=variant,
                     occurred_at=f"{day}T09:00:00+00:00", idempotency_key=f"lp:{visitor}")
    for index in range(paid):
        visitor = f"{day}-p{index}"
        funding = "prepaid" if index < prepaid else "credit"
        store.record(event="trial_started", visitor_key=visitor, test_variable=variant,
                     occurred_at=f"{day}T10:00:00+00:00", idempotency_key=f"ts:{visitor}")
        store.record(event="trial_paid", visitor_key=visitor, test_variable=variant,
                     amount_minor=100, gross_volume_minor=100, card_funding=funding,
                     occurred_at=f"{day}T10:01:00+00:00", idempotency_key=f"tp:{visitor}")
    for index in range(abandoned):
        store.record(event="checkout_expired", visitor_key=f"{day}-a{index}",
                     test_variable=variant, amount_minor=100,
                     occurred_at=f"{day}T10:02:00+00:00", idempotency_key=f"ce:{index}")
    for index in range(cancel):
        store.record(event="cancel_clicked", visitor_key=f"{day}-p{index}",
                     test_variable=variant, occurred_at=f"{day}T11:00:00+00:00",
                     idempotency_key=f"cc:{index}")
    for index in range(survey):
        store.record(event="survey_submitted", visitor_key=f"{day}-p{index}",
                     test_variable=variant, occurred_at=f"{day}T11:01:00+00:00")
    for index in range(refunds):
        store.record(event="refund_issued", visitor_key=f"{day}-p{index}", test_variable=variant,
                     amount_minor=100, gross_volume_minor=100, card_funding="credit",
                     occurred_at=f"{day}T12:00:00+00:00", idempotency_key=f"ri:{index}")
    for index in range(disputes):
        store.record(event="dispute_created", visitor_key=f"{day}-p{index}",
                     test_variable=variant, amount_minor=3900, gross_volume_minor=100,
                     card_funding="prepaid", occurred_at=f"{day}T12:30:00+00:00",
                     idempotency_key=f"dc:{index}")
    for index in range(fraud):
        store.record(event="radar_early_warning", visitor_key=f"{day}-p{index}",
                     test_variable=variant, amount_minor=100, card_funding="prepaid",
                     occurred_at=f"{day}T13:00:00+00:00", idempotency_key=f"rw:{index}")
    for index in range(rebills):
        store.record(event="rebill_succeeded", visitor_key=f"{day}-p{index}",
                     test_variable=variant, amount_minor=3900, gross_volume_minor=3900,
                     card_funding="credit", occurred_at=f"{day}T14:00:00+00:00",
                     idempotency_key=f"bs:{index}")
    for index in range(declined):
        store.record(event="rebill_declined", visitor_key=f"{day}-p{index}",
                     test_variable=variant, amount_minor=3900,
                     occurred_at=f"{day}T15:00:00+00:00", idempotency_key=f"bd:{index}")
    for index in range(terminal):
        store.record(event="rebill_decline_terminal", visitor_key=f"{day}-p{index}",
                     test_variable=variant, amount_minor=3900,
                     occurred_at=f"{day}T15:30:00+00:00", idempotency_key=f"bt:{index}")


def read_rows(sheet):
    with sheet["csv"].open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def rewrite_csv(sheet, rows) -> None:
    with sheet["csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=sheet["export"].HEADER)
        writer.writeheader()
        writer.writerows(rows)


def export(sheet, day: str = ""):
    export_module = sheet["export"]
    config_module = importlib.import_module("config")
    storage_module = importlib.import_module("storage")
    return export_module.export_day(day or "2026-11-20", sheet["csv"], config_module,
                                    storage_module, offline=True)


def add_subscription_rows(rows: list[tuple]) -> None:
    """Create the subscription table with hand-written rows, or merge into one that exists.

    In production a subscription row is the deliverable of a checkout, and an `UPDATE` would need
    a row to exist. This is the *stock* the export gauges, and the only thing the two tests below
    need from it.
    """
    path = billing_path_of()
    with sqlite3.connect(path) as conn:
        conn.execute("""create table if not exists funnel_subscriptions (
                          visitor_key text primary key, status text, plan text,
                          trial_amount_minor integer, rebill_amount_minor integer,
                          created_at text)""")
        conn.executemany("insert or replace into funnel_subscriptions values (?,?,?,?,?,?)",
                         rows)


def test_header_matches_the_spec_and_carries_the_ratios(sheet):
    """§3.4's column list, verbatim, so a note written against the original sheet still lines
    up; then the extras; then the computed ratios, which the spec lists separately from its
    columns and which a header without them would silently drop. An `extrasaction="ignore"`
    DictWriter loses a column with no message at all, which is exactly the bug this asserts
    against: nine ratios, computed every run, none of them ever written."""
    export_module = sheet["export"]
    spec = (
        "day,date_utc,offer_id,test_variable,live_level,"
        "sessions,lp_views,opt_ins,trial_starts,trials_started,"
        "trial_paid,paid_conversions,cancels_in_trial,rebill_success,rebill_declines_silent,"
        "active_subs,mrr_minor,rev_new,rev_rebill,rev_total_minor,"
        "ad_spend_minor,tool_cost_minor,llm_cost_usd_minor,cost_total_minor,"
        "refunds_count,refund_value_minor,disputes_count,chargebacks_count,fraud_flags_count,"
        "early_warnings_count,radar_reviews_count,customer_service_emails,support_touches,"
        "gross_auth_volume_minor,bin_prepaid_charges,churned_month2_cohort,"
        "cohorts_paid_day0_active_day60,notes").split(",")
    header = export_module.HEADER
    assert [c for c in header if c in spec] == spec, "spec columns reordered or renamed"
    for ratio in ("trial_paid_pct", "refund_pct", "dispute_cb_pct", "fraud_pct"):
        assert ratio in header, f"{ratio} computed but not in the sheet"
    assert len(header) == len(set(header)), "duplicate column"


def test_ratio_denominators_are_the_right_populations(sheet):
    """The arithmetic, on an asymmetric fixture so a wrong denominator is a different number
    rather than the same one to two decimals: 30 sessions, 10 conversions, 1 refund, 1 dispute.

    Refund % is *per conversion* (the spec's own formula), so 1/10 = 10.00% and not 1/30 = 3.33%.
    Trial→Paid is per committed start, and a committed start is a *visitor*, so the ten who paid
    contribute ten and not twenty even though each wrote both a start row and a payment row.

    Fraud % and the prepaid share are per **charge**, and a charge is a capture: ten trials plus one
    renewal is eleven, not the fifteen rows that mention money. The refund and the dispute are in
    that fifteen because §3.4's dollar ratios need their amounts in the authorised-volume column,
    which is a money denominator and not a count of charges. Reading one as the other is exactly
    the mistake worth a test: it moved 9.09% for 10.00% at n=11, and at a real n it moves by a
    factor of the average number of post-capture events per member, so it *looks* like noise.
    """
    seed_day(sheet, "2026-11-20", visitors=30, paid=10, prepaid=2, refunds=1, disputes=1,
             fraud=1, rebills=1, declined=1, terminal=1, abandoned=2, cancel=2, survey=1)
    row, detail = export(sheet)
    assert row["lp_views"] == "30", row
    assert row["trial_paid"] == "10"
    assert row["trials_started"] == "10", "committed starts are the conversion denominator"
    assert row["trial_starts"] == "12", "every start the funnel saw includes the abandonments"
    assert row["checkout_expired"] == "2"
    assert row["cancels_in_trial"] == "2"
    assert row["refunds_count"] == "1" and row["refund_value_minor"] == "100"
    assert row["rebill_success"] == "1"
    assert row["rebill_declines_silent"] == "1"
    assert row["rebill_attempts"] == "2", "one success and one decline are two attempts"
    assert row["trial_paid_pct"] == "100.00", "10 paid of 10 committed starts"
    assert row["refund_pct"] == "10.00", "1 refund per 10 conversions, not per 30 starts"
    # 11 captures: the 10 trial payments and the 1 renewal. Not 15, which is every row carrying an
    # amount, and not 12, which would count the abandonment as a charge that never happened.
    assert row["charges_observed"] == "11", row
    assert row["fraud_pct"] == "9.09", "1 fraud flag per 11 charges, not per 30 sessions"
    assert row["bin_prepaid_share_pct"] == "18.18", row
    assert row["rebill_decline_rate_pct"] == "50.00"
    assert row["fraud_flags_count"] == "1" and row["early_warnings_count"] == "1"
    # Money: authorised volume is what the dollar-denominated ratios divide by. Nine trial captures
    # at 100 plus one renewal at 3900 is 4800, and the refund and the dispute add nothing: they
    # restate money already in that figure, and a denominator that grows with trouble is a
    # denominator that hides it. Nine, not ten, because `p0` is the one member with a refund and a
    # dispute on its single card, and a card's authorised ceiling is the largest amount its rows
    # claim, not their sum: taking 100 + 100 there reports the account as having held 200.
    assert row["gross_auth_volume_minor"] == "4800", row
    assert int(row["rev_new"]) == 1000 and int(row["rev_rebill"]) == 3900
    assert int(row["rev_total_minor"]) == 4900 - 100, "net of the refund"


def test_ratios_on_capture_events_not_on_money_bearing_rows(sheet):
    """A refund and a dispute are not two more charges, and saying so has to be visible.

    Both events carry an amount, because §3.4's dollar columns need them to, and both are written
    by handlers that fill in the card facts of the charge they refer to. So a charge counter that
    selects on "row has an amount" or "row has a funding type" quietly grows the denominator with
    every after-the-fact event and reports a *lower* fraud rate precisely on the days with the most
    trouble. This pins it on a day whose signals are all pointed at one member.
    """
    seed_day(sheet, "2026-11-20", visitors=5, paid=1, refunds=1, disputes=1, fraud=1)
    row, _detail = export(sheet)
    assert row["trial_paid"] == "1"
    assert row["charges_observed"] == "1", \
        "one capture, however many events later describe it"
    assert row["fraud_pct"] == "100.00", \
        f"one fraud flag against the one charge that exists: {row['fraud_pct']}"
    assert row["bin_prepaid_share_pct"] == "0.00", \
        "the trial capture was a credit card; the dispute on it was not a second, prepaid charge"


def test_ceiling_breaches_are_named_and_the_exit_code_follows(sheet):
    """Both kill metrics, on numbers that trip them by a lot so a swapped numerator or a
    percent-not-multiplied bug cannot pass by coincidence."""
    seed_day(sheet, "2026-11-20", visitors=10, paid=10, prepaid=4, disputes=1, fraud=1)
    _row, detail = export(sheet)
    breaches = detail["breaches"]
    assert any(b.startswith("dispute_cb_pct") for b in breaches), breaches
    assert any(b.startswith("fraud_pct") for b in breaches), breaches
    # A clean day, 400 sessions and no signals at all, so nothing may breach.
    seed_day(sheet, "2026-11-21", visitors=400, paid=300)
    _row, clean = export(sheet, "2026-11-21")
    assert clean["breaches"] == [], clean["breaches"]


def test_ratios_are_blank_rather_than_zero_on_an_empty_day(sheet):
    """§3.4's own rule. "0.00%" reads as a perfect refund rate; blank reads as no data, and a
    zero-denominator day that prints a number is how a lane gets told it is healthy."""
    row, detail = export(sheet)
    for ratio in ("trial_paid_pct", "refund_pct", "dispute_cb_pct", "fraud_pct",
                  "bin_prepaid_share_pct", "month2_retention_pct",
                  "rebill_decline_rate_pct"):
        assert row[ratio] == "", f"{ratio} invented a rate: {row[ratio]!r}"
    assert row["lp_views"] == "0", "a count we can observe and found none of is a real zero"
    # Contribution is net of cost, and net against no cost is the most flattering possible
    # reading of a day nobody spent on, so a day with no charge reports nothing rather than 0.
    assert row["net_contribution_minor"] == "", row
    assert any("retention" in w.lower() for w in detail["warnings"]), detail["warnings"]


def test_prepaid_share_and_unknown_bins_are_both_reported(sheet):
    """The prepaid share is a leading indicator only if the unknowns are named alongside it: a
    share computed over the charges whose BIN we could read understates the warning in exactly
    the direction that keeps traffic running."""
    seed_day(sheet, "2026-11-20", visitors=40, paid=5, prepaid=2)
    row, detail = export(sheet)
    assert row["bin_prepaid_share_pct"] == "40.00", row
    assert any("prepaid bin share" in w.lower() for w in detail["warnings"]), detail["warnings"]
    with sqlite3.connect(billing_path_of()) as conn:
        conn.execute("update trial_events set card_funding = 'unknown' "
                     "where event = 'trial_paid' and id in "
                     "(select id from trial_events where event='trial_paid' limit 2)")
    _row, detail = export(sheet)
    assert any("unknown" in w.lower() for w in detail["warnings"]), detail["warnings"]


def test_export_preserves_hand_typed_columns_and_is_idempotent(sheet):
    """Rerunning a day must neither add a second row nor wipe the ad spend someone typed in.
    Append-only cannot express "this day, updated", and a sheet with two rows for one day has
    totals that are wrong until somebody notices."""
    seed_day(sheet, "2026-11-20", visitors=20, paid=10)
    export(sheet)
    rows = read_rows(sheet)
    rows[0]["ad_spend_minor"] = "4000"
    rows[0]["sessions"] = "19"
    rows[0]["notes"] = "ran $40 on meta, one support thread about the descriptor"
    rewrite_csv(sheet, rows)
    row, _detail = export(sheet)
    assert len(read_rows(sheet)) == 1
    assert row["ad_spend_minor"] == "4000"
    assert row["sessions"] == "19", "a hand-typed column must not be recomputed back"
    assert row["notes"].startswith("ran $40 on meta"), row["notes"]
    assert int(row["net_contribution_minor"]) == int(row["rev_total_minor"]) - 4000, row
    # A third run must not double the warning tail: the operator's text and the script's
    # warnings have to stay separable across reruns or the cell becomes unreadable.
    row_again, _ = export(sheet)
    assert row_again["notes"].count(sheet["export"].NOTE_SEPARATOR) == 1, row_again["notes"]


def test_an_unparsable_cost_cell_is_named_not_guessed(sheet):
    """A cost line a human typed as "≈40" is kept verbatim and left out of the sum, loudly. A
    silent zero on a cost column is the most flattering way to report a loss, which is the exact
    decision §3.4's net-contribution gate governs."""
    seed_day(sheet, "2026-11-20", visitors=10, paid=10)
    export(sheet)
    rows = read_rows(sheet)
    rows[0]["ad_spend_minor"] = "≈40"
    rewrite_csv(sheet, rows)
    row, detail = export(sheet)
    assert any("not a number" in w.lower() for w in detail["warnings"]), detail["warnings"]
    assert row["ad_spend_minor"] == "≈40", "the cell keeps what the operator typed"


def test_a_mixed_variant_day_is_flagged(sheet):
    """§3.1: one variable live at a time. A blended row over two arms is the thing the test
    queue forbids, and it can only arrive by accident, so the sheet has to say it."""
    # Distinct visitor keys, which is what makes this two arms rather than one member who
    # happened to be tagged twice: a row-level blend of two members is the accidental case, a
    # single member with two labels is the same string in the column and a different problem.
    seed_day(sheet, "2026-11-20", visitors=10, paid=5, variant="baseline")
    for index in range(4):
        importlib.import_module("telemetry").TelemetryStore(billing_path_of()).record(
            event="lp_view", visitor_key=f"other-v{index}", test_variable="headline-v2",
            occurred_at="2026-11-20T09:30:00+00:00", idempotency_key=f"olv{index}")
        importlib.import_module("telemetry").TelemetryStore(billing_path_of()).record(
            event="trial_paid", visitor_key=f"other-p{index}", test_variable="headline-v2",
            amount_minor=100, gross_volume_minor=100, card_funding="credit",
            occurred_at="2026-11-20T10:30:00+00:00", idempotency_key=f"otp{index}")
    row, detail = export(sheet)
    assert "+" in row["test_variable"], row["test_variable"]
    assert any("mixed variants" in w.lower() for w in detail["warnings"]), detail["warnings"]


def test_opt_ins_need_the_quiz_store_and_survive_its_absence(sheet, monkeypatch):
    """`opt_ins` is the one column whose source is a different file, so its absence costs that
    one column and nothing else. A deployment that reset its quiz store still gets a full
    billing row."""
    storage_module = importlib.import_module("storage")
    quiz_module = importlib.import_module("quiz")
    store = storage_module.ResponseStore(
        Path(importlib.import_module("config").FunnelSettings.from_env().database_abspath))
    answers = {f"q{n}": ["x"] for n in range(len(quiz_module.QUESTIONS))}
    store.append_event(visitor_key="q1", status="started", answers=answers,
                       step=len(quiz_module.QUESTIONS))
    seed_day(sheet, "2026-11-20", visitors=5, paid=2)
    row, _detail = export(sheet)
    assert row["opt_ins"] == "1", row
    monkeypatch.setenv("FUNNEL_DB_PATH", str(sheet["tmp"] / "gone.sqlite3"))
    row, _detail = export(sheet)
    assert row["opt_ins"] == "", "a missing quiz store leaves the column blank, never 0"
    assert row["trial_paid"] == "2", "the billing half still reports"


def test_subscription_stocks_are_read_as_stocks(sheet):
    """§3.3's trap: a stock summed per tick yields 24x the MRR by midnight. `mrr_minor` must
    equal what the live subscriptions are worth *now*, not a day's charges, and must price the
    lite tier at its own rate rather than at the offer the member first signed up under."""
    seed_day(sheet, "2026-11-20", visitors=10, paid=3)
    add_subscription_rows([
        ("s1", "active", "full", 100, 3900, "2026-11-01T00:00:00+00:00"),
        ("s2", "active", "lite", 100, 3900, "2026-11-01T00:00:00+00:00"),
        # A cancelled member is not MRR, however much they paid today.
        ("s3", "canceled", "full", 100, 3900, "2026-11-01T00:00:00+00:00"),
    ])
    row, _detail = export(sheet)
    lite = int(importlib.import_module("config").FunnelSettings.from_env().lite_amount_minor)
    assert row["active_subs"] == "2", row
    assert int(row["mrr_minor"]) == 3900 + lite, row
    assert int(row["mrr_minor"]) != int(row["rev_total_minor"]), "MRR is not today's revenue"


def test_backfilled_rows_say_their_stocks_are_todays(sheet):
    """A historical row whose MRR is today's MRR is a wrong number that looks plausible, and the
    only fix worse than noting it is pretending a local state table remembers what it was sixty
    days ago."""
    seed_day(sheet, "2026-10-01", visitors=10, paid=5)
    row, detail = export(sheet, "2026-10-01")
    assert any("as of the export run" in w for w in detail["warnings"]), detail["warnings"]
    assert "as of the export run" in row["notes"], row["notes"]


def test_month_two_retention_reads_the_original_cohort(sheet):
    """The lane's real PMF signal, and not derivable from any rollup: a rollup holds a daily
    paid count, while "still active" is a property of a subscription row today. So the export
    must reach back 60 days for the cohort and read their status now."""
    day = (datetime.now(timezone.utc).date() - timedelta(days=60)).isoformat()
    seed_day(sheet, day, visitors=10, paid=4)
    add_subscription_rows([
        # 3 of the 4 retained, one of them `trialing`, which counts: a lagging processor status
        # is not churn. Plus a member who never paid, who is not in the denominator.
        (f"{day}-p0", "active", "full", 100, 3900, f"{day}T10:01:00+00:00"),
        (f"{day}-p1", "active", "full", 100, 3900, f"{day}T10:01:00+00:00"),
        (f"{day}-p2", "trialing", "full", 100, 3900, f"{day}T10:01:00+00:00"),
        (f"{day}-p3", "canceled", "full", 100, 3900, f"{day}T10:01:00+00:00"),
        ("outsider", "active", "full", 100, 3900, f"{day}T10:01:00+00:00"),
    ])
    row, _detail = export(sheet, today())
    assert row["cohort_size_60d_ago"] == "4", row
    assert row["cohorts_paid_day0_active_day60"] == "3", row
    assert row["month2_retention_pct"] == "75.00", row


def test_cumulative_check_names_a_silent_listener(sheet, monkeypatch):
    """Zero local refund rows against a processor that has some is the shape of a broken
    webhook, not of drift, and it gets no tolerance. Everything else about this check forgives a
    boundary, because the cumulative comparison moves money between adjacent rows rather than out
    of the total."""
    export_module = sheet["export"]
    seed_day(sheet, "2026-11-20", visitors=10, paid=5)
    path = billing_path_of()
    # The processor's count arrives per *path*, so the two categories can be answered
    # separately, and answering them separately is what makes the rest of this test say anything.
    # With one canned number for both lists, the "local rows present and remote in the band" step
    # below would still fire the dispute alarm after the refunds had been made to agree, and the
    # only way to hush it would be to seed disputes too: a fixture whose own bookkeeping decided
    # which alarm fired, instead of the code under test. This is the shape the real call has,
    # argument for argument.
    def remote(path, _time_field, _through, *rest, refunds=7, disputes=7):
        return (refunds if path == "/v1/refunds" else disputes, "", False)

    monkeypatch.setattr(export_module, "_stripe_cumulative", remote)
    notes = export_module.reconciliation_notes({}, "2026-11-20", path, {"ok": True})
    assert any("ZERO REFUNDS EVER" in n for n in notes), notes
    assert any("ZERO DISPUTES EVER" in n for n in notes), notes
    # Local rows present, remote in the band: nothing is claimed either way. Seeding seven local
    # refund rows against the processor's seven settles the refund half. For disputes the funnel
    # has none and never had one, so the processor has to have none either, and that is the
    # reading a lane with no disputes actually has. It goes quiet through the ordinary comparison
    # (zero against zero, inside any tolerance) rather than through the dead-listener alarm, which
    # is the distinction the whole check is built on: the alarm fires on *zero local, some
    # remote*, never on a category that is simply empty on both sides. The keyword rides along
    # `*args`/`**kwargs` rather than naming `path`: the first positional argument is an endpoint
    # path, and a parameter of the same name in a lambda would shadow the closure's.
    store = importlib.import_module("telemetry").TelemetryStore(path)
    for index in range(7):
        store.record(event="refund_issued", visitor_key=f"r{index}", amount_minor=100,
                     gross_volume_minor=100, occurred_at="2026-11-20T12:00:00+00:00",
                     idempotency_key=f"cum:{index}")
    monkeypatch.setattr(export_module, "_stripe_cumulative",
                        lambda *args, **kwargs: remote(*args, disputes=0, **kwargs))
    assert export_module.reconciliation_notes({}, "2026-11-20", path, {"ok": True}) == []
    # A remote figure that disagrees by more than the band is named.
    monkeypatch.setattr(export_module, "_stripe_cumulative",
                        lambda *args, **kwargs: (40, "", False))
    notes = export_module.reconciliation_notes({}, "2026-11-20", path, {"ok": True})
    assert any("disagree" in n.lower() for n in notes), notes
    # An unavailable check says so rather than reporting agreement.
    assert any("unavailable" in n.lower() for n in export_module.reconciliation_notes(
        {}, "2026-11-20", path, {"error": "no key"}))


def test_export_reports_a_missing_or_uninstrumented_database(sheet):
    """Two different problems, both common: the funnel has never run, and the funnel predates
    Phase 3 so there are no events to count. A blank sheet from either is indistinguishable from
    a day with no traffic, which is the wrong thing to conclude."""
    export_module = sheet["export"]
    missing = Path(sheet["tmp"] / "never-created.sqlite3")
    with pytest.raises(FileNotFoundError, match="run once"):
        export_module.counts_for_day("2026-11-20", missing, missing, "rank4-routine-v1")
    legacy = Path(sheet["tmp"] / "legacy.sqlite3")
    with sqlite3.connect(legacy) as conn:
        conn.execute("create table something_else (id integer)")
    with pytest.raises(RuntimeError, match="Phase 3"):
        export_module.counts_for_day("2026-11-20", legacy, missing, "rank4-routine-v1")


def cli_main(module, *extra) -> int:
    """Call the script's `main()` with argv, so the CLI is exercised through argparse rather
    than around it: the exit codes and the day validation live in `main`, and a test that
    reached for `export_day` directly would leave both untested."""
    return module.main(list(extra))


def test_cli_writes_the_sheet_and_returns_the_breach_code(sheet, capsys):
    """The exit code is the automation hook: a cron line that prints a breach and returns 0 is a
    breach nobody receives."""
    export_module = sheet["export"]
    seed_day(sheet, "2026-11-20", visitors=10, paid=10, disputes=1, fraud=1)
    argv = ["--day", "2026-11-20", "--csv", str(sheet["csv"]), "--offline"]
    assert cli_main(export_module, *argv) == 0
    assert "ceiling" in capsys.readouterr().out.lower()
    assert sheet["csv"].is_file()
    assert cli_main(export_module, *argv, "--fail-on-ceiling") == 2


def test_cli_rejects_a_malformed_day(sheet):
    """A mistyped `--day` would otherwise export a phantom day and report it as a zero day."""
    with pytest.raises(SystemExit) as raised:
        cli_main(sheet["export"], "--day", "20-11-2026", "--csv", str(sheet["csv"]),
                 "--offline")
    assert raised.value.code == 2


def test_cli_reports_a_missing_database_as_exit_one(sheet, monkeypatch):
    """Exit 1 means "nothing was exported", which a cron wrapper has to tell apart from exit 2,
    "exported, and the kill metric tripped".

    The env vars are cleared rather than pointed somewhere: the `sheet` fixture creates the
    billing database on purpose (a zero-traffic day is a real, empty, instrumented day, not a
    missing file), and this test wants the opposite world, the one where the funnel has never
    run. Clearing the paths falls back on nothing else to stay quiet about; a second
    `monkeypatch.setenv` naming an unused path would be a fact that has to stay true on its own.
    """
    monkeypatch.delenv("FUNNEL_BILLING_DB_PATH", raising=False)
    monkeypatch.delenv("FUNNEL_DB_PATH", raising=False)
    assert cli_main(sheet["export"], "--day", "2026-11-20",
                    "--csv", str(sheet["tmp"] / "out.csv"), "--offline") == 1


def test_stdout_mode_prints_the_header_once(sheet, capsys):
    """Piping the row somewhere wants header + rows, and exactly one header across a multi-day
    backfill."""
    export_module = sheet["export"]
    seed_day(sheet, "2026-11-20", visitors=6, paid=3)
    seed_day(sheet, "2026-11-19", visitors=4, paid=1)
    assert cli_main(export_module, "--since", "2026-11-19", "--until", "2026-11-20",
                    "--csv", str(sheet["csv"]), "--stdout", "--offline") == 0
    printed = capsys.readouterr().out
    assert printed.count("day,date_utc,offer_id") == 1, printed
    assert len(printed.strip().splitlines()) == 3, printed


def test_json_mode_carries_the_detail_a_monitor_needs(sheet, capsys):
    export_module = sheet["export"]
    seed_day(sheet, "2026-11-20", visitors=10, paid=10, disputes=1)
    assert cli_main(export_module, "--day", "2026-11-20", "--csv", str(sheet["csv"]),
                    "--json", "--offline") == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["day"] == "2026-11-20"
    assert "breaches" in payload and "metrics" in payload, sorted(payload)
    assert payload["breaches"], "a dispute on 10 charges must report a breach"


# ===========================================================================
# 9. Spend posture: Phase 3 must not have armed anything
# ===========================================================================

def test_phase_three_ships_with_charges_off_and_no_spend_surface(tele_client):
    """Phase 3 adds instrumentation, which means the same defaults that gated Phase 2 still
    hold: no card moves, nothing is sent, and no worker got enabled as a side effect of touching
    billing code."""
    state = tele_client.main.billing.status()
    assert state["charges_enabled"] is False, state
    assert tele_client.main.billing.client is None, state
    reason = tele_client.main.billing.rehearsal_reason
    assert reason, "the reason no money moves is stated"
    # Either half of the two-lock gate holds the money still, and which one the app *names*
    # depends on the order it checks them in: it reports the missing key first, because a key is
    # what a fix looks like, while a switch that is off was switched off deliberately. Naming the
    # actual gate is the thing under test, so this accepts whichever lock is holding and rejects
    # a reason that names neither, which is the failure mode that matters: a checkout that says
    # "not right now" and points at nothing an operator can act on.
    assert any(name in reason for name in
               ("STRIPE_SECRET_KEY_FUNNEL", "FUNNEL_CHARGES_ENABLED")), reason


def test_no_funnel_setting_defaults_a_spend_switch_on():
    """Every setting Phase 3 reads has to default to the inert position. Asserted against the
    live module rather than a copy of the expectation, because a copied expectation goes stale
    the day someone changes the default and stays green."""
    config = importlib.import_module("config")
    settings = config.FunnelSettings.from_env()
    assert settings.charges_enabled is False
    for name, value in vars(settings).items():
        if isinstance(value, bool) and any(
                hint in name for hint in ("spend", "supervisor", "agent", "auto")):
            assert not value, f"{name} defaults to enabled"


def test_supervisor_stays_disabled_by_default():
    """`SUPERVISOR_ENABLED` defaults OFF and nothing in code or SQL enforces the single-writer
    rule, so a Phase 3 change that started reading settings more widely must not have flipped
    it. Asserted against the live source rather than a copy of the expectation."""
    finance_agent = REPO_ROOT / "backend" / "finance_agent.py"
    if not finance_agent.is_file():
        pytest.skip("backend/ is not present in this checkout")
    source = finance_agent.read_text(encoding="utf-8")
    # The flag is read through the module's own `env()` helper, which is `os.getenv` with the
    # empty-string-is-unset rules applied, so grepping for `environ` here asserted a spelling
    # rather than the property: a flag routed through the house helper looked, to this test, like
    # a flag that had stopped being read at all. Match the read, and then match the one thing
    # that actually matters about it, which is that no default is passed.
    line = next((line.strip() for line in source.splitlines()
                 if "SUPERVISOR_ENABLED" in line and "supervisor_enabled" in line), "")
    assert line, "SUPERVISOR_ENABLED is no longer read into supervisor_enabled"
    assert "env(\"SUPERVISOR_ENABLED\")" in line or "env('SUPERVISOR_ENABLED')" in line, (
        f"the flag is no longer read with no default, so re-check that absent means off: {line}")
    assert line.rstrip().endswith("in {\"1\", \"true\", \"yes\", \"on\"},"), (
        f"the flag is no longer an explicit-opt-in set membership: {line}")


def test_dry_run_stays_the_settings_key_the_spec_names():
    """§2.4 names `engine.dry_run` as a settings-table key rather than an environment variable,
    which is exactly why an agent that can write settings can arm automated spend. Phase 3
    touches billing code and must not have added a second, easier switch.

    Asserted where the switch actually lives, the `backend/engine/` package, rather than at a
    `backend/engine.py` that never existed: a test that skips because of a filename is a test that
    has stopped guarding anything while still counting as run. The `settings` row is the live
    switch and `ENGINE_DRY_RUN` is an environment *default* that loses to it (see
    `engine/config.py`'s own startup warning), so the env spelling is allowed to exist; what must
    not exist is a read of the row that can be bypassed. `True` on both reads is the part that
    matters, since a default that flips to live by accident is the incident this guards.
    """
    settings_py = REPO_ROOT / "backend" / "engine" / "settings.py"
    db_py = REPO_ROOT / "backend" / "engine" / "db.py"
    if not settings_py.is_file():
        pytest.skip("backend/engine/ is not present in this checkout")
    settings_src, db_src = settings_py.read_text(encoding="utf-8"), db_py.read_text(
        encoding="utf-8")
    assert "engine.dry_run" in settings_src, \
        "the dry-run switch is no longer named as the spec names it"
    assert 'self.bool("engine.dry_run", True)' in settings_src, \
        "the settings-table read no longer defaults to dry-run"
    assert 'self.setting("engine.dry_run", True)' in db_src, \
        "the second reader of the same key no longer defaults to dry-run"
    # Two readers of one key is tolerated; a third spelling of the key is not, because the row
    # the agent writes is identified by its name.
    assert settings_src.count('"engine.dry_run"') + db_src.count('"engine.dry_run"') == 2, \
        "the key name is spelled in more than the two places that read it"


def test_env_example_documents_the_phase_three_variables():
    """A variable that exists only in code is a variable nobody sets, and the two that matter
    here (where the metrics sheet goes, and a read-only key for the cross-check) are exactly the
    kind of thing an operator meets at 11pm on a launch day."""
    documented = (FUNNEL_ROOT / ".env.example").read_text(encoding="utf-8")
    root = REPO_ROOT / ".env.example"
    combined = documented + (root.read_text(encoding="utf-8") if root.is_file() else "")
    assert "FUNNEL_METRICS_CSV" in combined
    assert "STRIPE_READ_KEY_FUNNEL" in combined
