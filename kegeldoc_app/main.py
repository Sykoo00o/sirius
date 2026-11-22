"""kegeldoc funnel: FastAPI app, server-rendered, no build step. A fork of the Lane C Rank-4
funnel (`funnel_app`, Routine Lab) with the same billing, telemetry, cancel and save-offer
machinery and a kegel quiz, plan builder and copy in place of the routine ones.

Run it:

    cd "kegeldoc_app" && python -m uvicorn main:app --port 8091

Deliberately standalone: this module imports nothing from `backend/engine` or `funnel_app`, and
the LLM layer loads `backend/llm_router.py` by file path. Port 8091 by default, beside Routine
Lab's 8090, with its own databases and its own `.env`.

Quiz difference from the routine funnel: the screen list is per visitor (`quiz.flow_for`), so
`/onboarding/{n}` indexes this visitor's flow rather than a fixed tuple. Information screens
(study facts, the timeline) are acknowledged with a POST like any answer, which keeps
resume, the forward-jump guard and per-step response rows exactly as they were.

Phase 2 scope on top of Phase 1: `/checkout` takes a card through Stripe's hosted page,
`/dashboard` is the member surface with the billing block on it, `/account/cancel` is the
one-click cancel with the save-offer menu behind it, and `/webhooks/stripe` keeps local state
in step with the processor. Charges stay off unless `FUNNEL_CHARGES_ENABLED=true` *and* a
funnel-account key is present; with either missing, the rehearsal path records intent and the
card screen says so in its own copy.

Route shape note: the quiz is one route per question rather than one big form, because
sunk effort has to be *experienced* as a sequence of commitments. Every submit is a
POST-redirect-GET, so a refresh re-posts cleanly instead of resubmitting, and every
accepted answer writes a row, which is what makes per-step drop-off measurable later
without a second analytics table.

The cancel route is the one place that breaks POST-redirect-GET on purpose, and it is worth
knowing before "fixing" it: §2.3 requires the cancellation to be *executed and confirmed*
before any retention copy is chosen, so `/account/cancel` renders the confirmation itself
rather than redirecting to a screen that would have to be handed the outcome anyway. One
extra hop would be the difference between "cancel done" and "cancel pending, here is an
offer", and the second reading is the dark pattern.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import anyio
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import billing as billing_module
import claims
import plans
import save_offer
import stripe_billing
import telemetry
from billing_store import BillingStore, human_date, plan_label
from config import FunnelSettings
from funnel_copy import (ARTIFACT_FREE, ARTIFACT_PAID, TRUST_POINTS, Copy, build_copy,
                        disclosure_coverage, guardrail_scan, iso_to_local_date,
                        money, next_bill_date)
from llm_flavour import builder_coach_note, coach_note, load_router
from mailer import Mailer
from plan_builder import WEEK_ARCS, Plan, build_plan
from plan_builder import audit_tables as plan_audit_problems
from profile import is_complete, missing_indices, profile_from_answers
from quiz import (NOTE_SUFFIX, OPTION_LABELS, QUESTIONS, SECTION_KEYS, SECTION_TITLES,
                  flow_for, next_unanswered, parse_answer, progress_pct)
from quiz import CHARTS
from session import COOKIE_NAME, SessionCodec
from storage import OFFER_ID, ResponseStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kegeldoc")

BASE_DIR = Path(__file__).resolve().parent

settings = FunnelSettings.from_env()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
#: Price formatting for templates, in the app's currency, read at call time so a test that swaps
#: `settings` gets its own currency.
templates.env.globals["money"] = lambda minor: money(int(minor), settings.currency)
templates.env.globals["charts"] = CHARTS
store = ResponseStore(settings.database_abspath)
billing_store = BillingStore(settings.billing_database_abspath)
#: Funnel telemetry lives in the *billing* database file, beside the subscription rows rather
#: than beside the quiz responses. The Day-1 export compares event counts against subscription
#: counts, and reading both from one file removes the one race that could make the two
#: disagree: two connections, one write between the reads, and a day whose paid count is
#: higher than its trial count.
events_store = telemetry.TelemetryStore(settings.billing_database_abspath)
mailer = Mailer(settings)
codec = SessionCodec(settings.session_secret)

#: `BillingService.client` is None unless charges are armed *and* a funnel key exists, so an
#: app started without money rails still serves every page and says which of the two is
#: missing. Read lazily rather than built once at import: `/health` must report what *would*
#: happen if a key appeared, and an operator deciding whether to flip `FUNNEL_CHARGES_ENABLED`
#: needs the account type and descriptor off the live account, not a stale snapshot taken
#: before the key was added. Construction errors still surface, on the page built to show them.
billing = billing_module.BillingService(settings, billing_store, mailer, None,
                                       telemetry=events_store)

#: Has this process shown a member a renewal date read from the provider? Process-local on
#: purpose, and named to be read in `billing_readiness`: after a restart the answer is no, and an
#: ops page that said "yes, probably, sometime before the last deploy" would be asserting
#: something it cannot know. A restart legitimately re-raises the issue rather than hiding it.
SUBSCRIPTION_READ = False


def billing_client() -> stripe_billing.StripeClient | None:
    """The funnel's Stripe client for this request, or None while checkout is rehearsing.

    `StripeMisconfigured` propagates: a B2B key where a funnel key belongs, or a `pk_test`
    paired with `sk_live`, is not a state to serve traffic under, and refusing loudly at the
    first checkout click beats discovering it as a charge on the wrong merchant account.
    """
    return stripe_billing.client_for(settings)

app = FastAPI(
    title=f"{settings.brand_name} funnel",
    version="0.1.0",
    docs_url="/dev/docs",
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------

def get_visitor(request: Request) -> str:
    """Visitor key from the signed cookie, minted by middleware on first sight.

    Never trusted as billing identity: lane C's durable identity is the processor's
    event id. This key exists so a half-finished quiz survives a refresh. See
    `session.py` for why the signature is verified rather than assumed.
    """
    return getattr(request.state, "visitor_key", "") or codec.issue()


@app.middleware("http")
async def visitor_middleware(request: Request, call_next):
    """Attach a verified visitor key, minting and setting a cookie when absent.

    Minting happens in the middleware rather than per-handler so every route, including
    redirects and error pages, gets the same cookie behaviour, and no handler has to
    remember to set one.
    """
    visitor_key = codec.verify(request.cookies.get(COOKIE_NAME))
    minted = visitor_key is None
    if minted:
        visitor_key = codec.issue()
    request.state.visitor_key = visitor_key
    response = await call_next(request)
    if minted:
        response.set_cookie(COOKIE_NAME, codec.sign(visitor_key), **codec.cookie_kwargs())
    return response


def page_context(request: Request, *, copy_obj: Copy | None = None, **extra: Any) -> dict[str, Any]:
    """Template context. Page chrome, offer terms and the rehearsal banner live here so
    no template has to remember them.

    `request` itself is not included: starlette injects it into the template context
    when the response is built, and a second copy here would shadow it.
    """
    return {
        "brand": settings.brand_name,
        "support_email": settings.support_email or "support@example.com",
        "charges_enabled": settings.charges_enabled,
        "visitor_key": get_visitor(request),
        "copy": copy_obj or build_copy(settings),
        **extra,
    }


def quiz_context(question: Any, index: int, stored: dict[str, list[str]], *,
                 answered: list[str], note: str, errors: list[str]) -> dict[str, Any]:
    """Everything a quiz screen needs. One function, so the GET and the invalid-submit
    re-render present identical chrome and the progress bar cannot lie on the error page."""
    flow = flow_for(stored)
    total = len(flow)
    return {
        "question": question,
        "section_title": SECTION_TITLES[question.section],
        "step": index,
        "step_number": index + 1,
        "total": total,
        # Position-based, not answered-count-based: on a back-step the bar shows where the
        # visitor is, which is what they are looking at.
        "progress": round(index / total * 100) if total else 0,
        "answered": answered,
        "saved_note": note,
        "errors": errors,
        "milestone": _milestone(stored) if question.kind == "milestone" else None,
    }


def _milestone(stored: dict[str, list[str]]) -> dict[str, Any]:
    """Dated plan steps for the timeline screen. Plan dates, not a promised result."""
    from datetime import timedelta
    profile = profile_from_answers(stored)
    plan = build_plan(profile)
    today = date.today()

    def fmt(days: int) -> str:
        day = today + timedelta(days=days)
        return f"{day.strftime('%B')} {day.day}"

    return {
        "focus": plan.focus,
        "steps": [
            (fmt(0), "Week 1 starts: find the muscle, foundation holds"),
            (fmt(7), "Week 2: longer holds, built from your day-7 check-in"),
            (fmt(14), "Week 3: standing and moving sets"),
            (fmt(27), "Re-test: compare your hold time to day 1"),
        ],
    }


def answers_for(request: Request) -> dict[str, Any]:
    """Stored answers for this visitor, with notes folded back into the answer map."""
    return store.latest_answers(get_visitor(request)) or {}


def _answer_count(stored: dict[str, Any]) -> int:
    """How many real answers (not acknowledged information screens) this visitor gave."""
    return sum(1 for q in flow_for(stored) if not q.is_info and stored.get(q.key))


def form_answers(stored: dict[str, Any]) -> dict[str, Any]:
    """Strip internal keys before persisting a snapshot."""
    return {k: v for k, v in stored.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Funnel telemetry
# ---------------------------------------------------------------------------

#: Page-view events, keyed by path prefix, with whether one is counted per visitor or per visit.
#:
#: Per-visitor (`dedupe` True) for the top of the funnel, because `sessions` and `lp_views` in
#: the §3.4 sheet mean "distinct people who saw this", and the LP→trial rate is quoted against
#: them: a visitor who refreshes the landing page four times is one session, and if they open a
#: second tab the same afternoon they are still one. The dedupe key is the visitor plus the day,
#: so a visitor returning tomorrow *is* a second session, which is what a daily sessions column
#: counts. The quiz rows are per-visit for the same reason they are per-visitor-keyed, and
#: `quiz_completed` is keyed per visitor because a finished quiz is a finished quiz however many
#: times the summary page is reloaded on the way to checkout.
PAGE_EVENTS: tuple[tuple[str, str, bool], ...] = (
    ("/", "lp_view", True),
    ("/onboarding", "quiz_started", True),
    ("/analysing", "quiz_completed", True),
    ("/preview", "preview_shown", False),
    ("/checkout", "checkout_viewed", False),
)


def count_page_view(visitor_key: str, path: str, *, redirecting: bool = False) -> None:
    """Write the funnel step for `path`, if `path` is a funnel step.

    The decision half, split from `track_page_view` so a test can drive the rules directly
    instead of reconstructing a request. `redirecting` is whether the response was a 1xx or 3xx,
    which settles the lower steps and deliberately does not settle the landing page.

    Only the surfaces listed above. A POST never reaches here (a submission is what the response
    tables already record), and neither does a 4xx or 5xx, which the middleware filters: a 404 for
    a mistyped URL is not a landing page view. `/dashboard`, `/health` and `/dev/*` are member and
    ops surfaces, not funnel steps, and counting a returning member's dashboard read as an LP view
    would put the same person in the denominator twice on two different days.

    A 3xx is excluded, except that `lp_view` is still counted when the response bounces, and that
    one exception is cheap to justify. The landing page is the sheet's denominator, and the
    arrivals that most need counting, an ad click on a campaign URL, a trailing-slash hop, an A/B
    dispatcher, are the ones that serve something *and* bounce. Suppressing them would undercount
    sessions in a way no other column revealed. A redirected lower step stays excluded because
    there a 3xx either showed nothing or bounced onward to a *different* step, and the second
    would bill two funnel steps for one screen.

    `/onboarding` needs no help from any of this: it is excluded by name in `NON_PAGE_PATHS`, a
    bare path rather than a prefix, so `/onboarding/{n}` keeps matching. That is the whole of the
    entry-hop question. What follows is the wrong answer, recorded because it is tempting: the
    claim that a browser-less walk reaches each of its twelve question pages as the follow of a
    redirect, so counting only 200s erases `quiz_started` for every test in the suite. It does not.
    POSTs never follow their own 302 and GETs follow one only when the caller opts in (both stated
    in `tests/conftest.py`), so a walk's question pages are twelve separate requests that each
    land on their own 200. Production redirect chains have the same shape: the app bounces onward,
    the browser requests the next page as its own request.
    """
    matched = matched_step(path)
    if matched is None or path in NON_PAGE_PATHS:
        return
    if redirecting and matched[0] not in REDIRECT_COUNTS_AT:
        return
    event, dedupe = matched
    if dedupe:
        day = telemetry.day_of(telemetry.utc_now())
        billing.track(event, visitor_key=visitor_key,
                      idempotency_key=f"{event}:{visitor_key}:{day}")
    else:
        billing.track(event, visitor_key=visitor_key)


def track_page_view(request: Request, *, redirecting: bool = False) -> None:
    """Count one page view, on the path the browser actually asked for.

    Thin over `count_page_view`, which holds the rules.

    Done in middleware rather than in each route for two reasons. The first is coverage: a
    route that forgets its tracking call reports a zero conversion step forever and looks like a
    marketing problem rather than a missing line of code. The second is fidelity: `request.url`
    inside a handler is the handler's mount, and after a forward jump in the quiz the visitor's
    address bar and the handler's idea of "which page" differ. Middleware sees the request that
    arrived.

    The funnel's own GETs, and only statuses that could have shown something: the middleware runs
    this only under 400, so "rendered something" is enforced there rather than in two places.
    """
    if request.method != "GET":
        return
    count_page_view(get_visitor(request), request.url.path, redirecting=redirecting)


#: Paths that share a funnel prefix but are not that step. `/checkout/complete` and
#: `/checkout/cancelled` are both *after* the card screen, and counting them as checkout views
#: would inflate the step that the checkout→trial conversion divides by, and `trial_started` is
#: written on that path, so leaving it in the denominator would divide a numerator by a surface
#: that already sits past it. `/checkout/start` is a POST in the happy path, so listing it costs
#: nothing and covers the GET that reaches it.
#:
#: `/onboarding` is here for a different reason, and it is the one a reader will want to check
#: twice. That exact path is the quiz's *entry redirect*: it renders nothing, it answers 302 to
#: the first unanswered question, and the visit that lands on the bar is the one that follows.
#: Counting the redirect as a `quiz_started` would charge the step to the hop rather than to the
#: page, and the hop happens on paths where nobody started anything, so it is excluded while
#: `/onboarding/{n}` below still matches on its prefix. The test is exact-path for that reason.
NON_PAGE_PATHS = frozenset({"/checkout/complete", "/checkout/cancelled", "/checkout/start",
                            "/onboarding"})


#: Steps that are still counted when the response was a 1xx or 3xx. See `track_page_view` for
#: why the landing page is in here and the quiz's entry hop is not.
REDIRECT_COUNTS_AT = frozenset({"lp_view"})


def matched_step(path: str) -> tuple[str, bool] | None:
    """Which funnel step, if any, this path *is*: `(event_name, count_once_per_visitor_per_day)`.

    Split out of `track_page_view` so the middleware can ask the same question the tracker does
    without performing the write. Matching is prefix-based on purpose: `/onboarding/7` is the
    eighth question, not a surface of its own, and a step that matched only its exact path would
    report a quiz nobody ever finished.
    """
    for prefix, event, dedupe in PAGE_EVENTS:
        if path == prefix or (prefix != "/" and path.startswith(prefix + "/")):
            return (event, dedupe)
    return None


@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    """Count a page view once, after the response is known, and never fail the page for it.

    The status *range* decides "showed nothing", not `response.is_redirect`. The object that
    comes back from `call_next` is starlette's `_StreamingResponse`, what wraps everything
    downstream of a middleware, and it does not carry the convenience properties the concrete
    `RedirectResponse` has. A 3xx check works on every response class and on none of them
    specially, which is the property worth having in code that runs around every request;
    reaching for `.is_redirect` there cost every funnel page view in this suite, swallowed by
    the very `except` that is there to keep instrumentation from breaking a page.

    After `call_next` rather than before: the status code is part of the definition of a view
    (a 404 is not a landing page), and an exception inside the tracker must not become a 500 on
    a page that rendered fine. `track` already swallows its own errors, so this is belt-and-
    braces for a failure raised while *computing* the key.
    """
    response = await call_next(request)
    if response.status_code < 400:
        try:
            track_page_view(request, redirecting=100 <= response.status_code < 200
                            or 300 <= response.status_code < 400)
        except Exception as exc:  # noqa: BLE001 - instrumentation never breaks a page
            log.warning("telemetry middleware failed on %s: %s", request.url.path, exc)
    return response


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    copy_obj = build_copy(settings)
    return templates.TemplateResponse(
        request, "landing.html",
        page_context(
            request, copy_obj=copy_obj,
            trust_points=TRUST_POINTS,
            steps=copy_obj.how_it_works,
            what_you_see=ARTIFACT_FREE,
            what_unlocks=ARTIFACT_PAID,
            question_count=len(flow_for({"track": ["men"]})),
        ),
    )


# ---------------------------------------------------------------------------
# Onboarding quiz
# ---------------------------------------------------------------------------

@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_start(request: Request) -> HTMLResponse:
    """Entry point lands on the first unanswered question, not on question 1.

    Deliberate: a visitor who answered eleven questions and returns tomorrow continues at
    eleven. Sending them to question 1 instead reads as a lost quiz, and a lost quiz is
    abandoned rather than redone.
    """
    return RedirectResponse(
        url=f"/onboarding/{next_unanswered(answers_for(request))}", status_code=302)


@app.get("/onboarding/{index:int}", response_class=HTMLResponse)
async def onboarding_step(request: Request, index: int) -> HTMLResponse:
    stored = answers_for(request)
    flow = flow_for(stored)
    if not 0 <= index < len(flow):
        return RedirectResponse(url="/onboarding", status_code=302)

    reached = next_unanswered(stored)
    if index > reached:
        # Forward jumps are not free. Reading question 8 before answering question 7 is
        # possible by typing a URL, and it costs the funnel the one property the whole
        # design is built on: the sense that each answer is paid for before the next ask.
        return RedirectResponse(url=f"/onboarding/{reached}", status_code=302)

    return await render_step(request, flow[index], index, stored, errors=[])


async def render_step(request: Request, question: Any, index: int,
                      stored: dict[str, list[str]], *, errors: list[str],
                      status_code: int = 200) -> HTMLResponse:
    """Render one question. Shared by GET and by the invalid-submit re-render.

    One function, because two hand-built contexts is how a progress bar starts lying on
    the error page: the error state must show the same progress the question did a second
    ago, or the visitor reads the rejection as the quiz resetting.
    """
    page = quiz_context(
        question, index, stored,
        answered=stored.get(question.key, []),
        note=(stored.get(f"{question.key}{NOTE_SUFFIX}") or [""])[0],
        errors=errors,
    )
    return templates.TemplateResponse(
        request, "quiz_step.html", page_context(request, **page), status_code=status_code)


@app.post("/onboarding/{index:int}", response_class=HTMLResponse)
async def onboarding_submit(
    request: Request,
    index: int,
    answer: str | None = Form(None),
    note: str | None = Form(None),
) -> HTMLResponse:
    """Accept one answer, persist it, move on.

    Invalid input re-renders the same step with a message rather than redirecting: the
    answer was not earned, so nothing advances, and 422 is what a smoke test asserts
    without scraping the page. Valid input writes a `started` row carrying the
    cumulative answer set.

    Declared `Form(...)` parameters are used rather than a raw `await request.form()`, and
    the reason is the failure mode. A declared parameter rejects a wrong content type at
    FastAPI's boundary with a 422 the smoke tests can assert on. A raw read accepts anything
    parseable and returns an *empty* form for a body it could not decode, which this handler
    would then report as "Choose one option to continue": a confident, wrong, user-facing
    message about a request that never contained an answer. Rejecting loudly beats guessing
    quietly, especially on the screen where a visitor has already answered eleven questions.
    """
    visitor_key = get_visitor(request)
    stored = store.latest_answers(visitor_key) or {}
    flow = flow_for(stored)
    if not 0 <= index < len(flow):
        return RedirectResponse(url="/onboarding", status_code=302)

    question = flow[index]
    reached = next_unanswered(stored)
    if index > reached:
        return RedirectResponse(url=f"/onboarding/{reached}", status_code=302)

    # Multi-select inputs are named after the question key, which varies per route, so the
    # posted form is the read for those; FastAPI has already drained the body to inject
    # `answer` and `note`, and this reads that cache rather than reparsing.
    #
    # `multi_items()` yields one (key, value) pair *per occurrence*, which is the shape
    # wanted here: repeated names accumulate into one list. Do not substitute `items()`,
    # which de-duplicates by key and silently keeps only the last checkbox ticked.
    posted: dict[str, list[str]] = {}
    for key, value in (await request.form()).multi_items():
        posted.setdefault(key, []).append(value if isinstance(value, str) else str(value))

    values, note_text, error = parse_answer(question, answer, posted, note)

    if error:
        page = quiz_context(
            question, index, stored,
            answered=values or stored.get(question.key, []),
            note=note_text,
            errors=[error],
        )
        return await render_step(request, question, index, stored, errors=[error],
                                 status_code=422)

    if question.key == "track" and (stored.get("track") or [None])[0] not in (None, values[0]):
        # Switching between the men's and women's plans changes which screens exist, so the
        # other branch's answers are dropped rather than left to leak into a plan for the
        # wrong body. Same rule the static prototype applied.
        stored = {}
    stored[question.key] = values
    if note_text and question.allow_note:
        stored[f"{question.key}{NOTE_SUFFIX}"] = [note_text]
    else:
        stored.pop(f"{question.key}{NOTE_SUFFIX}", None)

    store.append_event(
        visitor_key=visitor_key,
        status="started",
        answers=stored,
        step=index + 1,
        offer_id=OFFER_ID,
        user_agent=request.headers.get("user-agent", ""),
        referrer=request.headers.get("referer", ""),
    )

    # Every step posts to its own route, so an invalid answer can 422 the page the
    # visitor is actually on rather than the one it was submitted from.
    return RedirectResponse(
        url="/analysing" if is_complete(stored) else f"/onboarding/{next_unanswered(stored)}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# After the quiz: analysing -> welcome -> compatibility -> email -> ready -> paywall
# ---------------------------------------------------------------------------

EMAIL_KEY = "_email"
#: When this visitor first saw the paywall (epoch seconds, as a string). The discount clock runs
#: from here and is written once, so reloading the paywall never restarts it.
OFFER_KEY = "_offer_started"
#: A checkout form opened while the discount was on may be submitted this long after the clock
#: runs out and still get the price it showed. Past that, the visitor is shown the full price
#: before anything is charged, so the amount charged is always the amount last displayed.
OFFER_GRACE_SECONDS = 120


def _now() -> float:
    """The clock, as one function so tests can move it."""
    return time.time()


def _offer_started(stored: dict[str, Any]) -> float | None:
    try:
        return float((stored.get(OFFER_KEY) or [""])[0])
    except ValueError:
        return None


def _internal(stored: dict[str, Any]) -> dict[str, Any]:
    """The internal keys a later step still needs: the email and the discount start."""
    return {k: stored[k] for k in (EMAIL_KEY, OFFER_KEY) if k in stored}


def offer_state(stored: dict[str, Any], *, grace_seconds: float = 0) -> dict[str, Any]:
    """The discount as the page shows it: on or off, its percent and the seconds left."""
    started = _offer_started(stored)
    now = _now()
    active = plans.offer_active(settings, started, now, grace_seconds=grace_seconds)
    deadline = plans.offer_deadline(settings, started) if started is not None else 0
    return {"active": active, "percent": plans.discount_percent(settings),
            "deadline": int(deadline), "remaining": max(0, int(deadline - now)) if active else 0}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")

#: The analysis bars, per track. They describe what the builder really reads from the answers,
#: so the screen is a progress display for work the plan does, not theatre about work it does not.
ANALYSIS_BARS: dict[str, tuple[str, ...]] = {
    "men": ("Pelvic floor strength and control", "Erection and ejaculation answers",
            "Bladder and urgency patterns", "Lifestyle factors", "Building your personal plan"),
    "women": ("Pelvic floor strength and control", "Leaks, birth and intimacy answers",
              "Bladder and urgency patterns", "Lifestyle factors", "Building your personal plan"),
}


def _post_quiz_gate(request: Request, *, need_email: bool = False
                    ) -> tuple[dict[str, Any], RedirectResponse | None]:
    stored = answers_for(request)
    if not stored:
        return stored, RedirectResponse(url="/onboarding/0", status_code=302)
    if not is_complete(stored):
        return stored, RedirectResponse(url=f"/onboarding/{missing_indices(stored)}",
                                        status_code=302)
    if need_email and not _email_of(stored):
        return stored, RedirectResponse(url="/email", status_code=302)
    return stored, None


def _email_of(stored: dict[str, Any]) -> str:
    return (stored.get(EMAIL_KEY) or [""])[0]


def _track_of(stored: dict[str, Any]) -> str:
    return "women" if (stored.get("track") or [""])[0] == "women" else "men"


def compatibility(plan: Plan) -> dict[str, str]:
    """How well a kegel plan fits these answers. High unless an answer needs a clinician first."""
    if plan.safety_note:
        return {"level": "Good", "pct": "72",
                "note": "A kegel plan fits your goals, starting gently. One of your answers is "
                        "worth checking with a doctor or pelvic health physiotherapist first."}
    return {"level": "High", "pct": "94",
            "note": "Your answers match the people a structured, daily kegel plan is built for."}


@app.get("/analysing", response_class=HTMLResponse)
async def analysing(request: Request) -> HTMLResponse:
    stored, blocked = _post_quiz_gate(request)
    if blocked:
        return blocked
    return templates.TemplateResponse(
        request, "analysing.html",
        page_context(request, bars=ANALYSIS_BARS[_track_of(stored)], progress=100))


@app.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request) -> HTMLResponse:
    stored, blocked = _post_quiz_gate(request)
    if blocked:
        return blocked
    return templates.TemplateResponse(
        request, "welcome.html",
        page_context(request, track=_track_of(stored), total=_answer_count(stored)))


@app.get("/compatibility", response_class=HTMLResponse)
async def compatibility_page(request: Request) -> HTMLResponse:
    stored, blocked = _post_quiz_gate(request)
    if blocked:
        return blocked
    plan = build_plan(profile_from_answers(stored))
    return templates.TemplateResponse(
        request, "compatibility.html",
        page_context(request, plan=plan, score=compatibility(plan), track=_track_of(stored)))


@app.get("/email", response_class=HTMLResponse)
async def email_page(request: Request) -> HTMLResponse:
    stored, blocked = _post_quiz_gate(request)
    if blocked:
        return blocked
    return templates.TemplateResponse(
        request, "email.html", page_context(request, email=_email_of(stored), error=""))


@app.post("/email", response_class=HTMLResponse)
async def email_submit(request: Request, email: str | None = Form(None)) -> HTMLResponse:
    stored, blocked = _post_quiz_gate(request)
    if blocked:
        return blocked
    value = (email or "").strip()
    if not EMAIL_RE.fullmatch(value) or len(value) > 254:
        return templates.TemplateResponse(
            request, "email.html",
            page_context(request, email=value,
                         error="Enter a working email address so we can send your plan."),
            status_code=422)
    stored[EMAIL_KEY] = [value]
    store.append_event(
        visitor_key=get_visitor(request), status="started", answers=stored,
        step=len(QUESTIONS), offer_id=OFFER_ID,
        user_agent=request.headers.get("user-agent", ""))
    return RedirectResponse(url="/ready", status_code=303)


@app.get("/ready", response_class=HTMLResponse)
async def ready(request: Request) -> HTMLResponse:
    stored, blocked = _post_quiz_gate(request, need_email=True)
    if blocked:
        return blocked
    plan = build_plan(profile_from_answers(stored))
    return templates.TemplateResponse(
        request, "ready.html",
        page_context(request, plan=plan, email=_email_of(stored), total=_answer_count(stored)))


def plan_cards(discounted: bool = False) -> list[dict[str, Any]]:
    """One card per paywall plan, each carrying its own price and renewal disclosures."""
    cards = []
    for option in plans.options(settings, discounted=discounted):
        copy_obj = build_copy(plans.settings_for(settings, option))
        cards.append({
            "option": option,
            "terms": copy_obj,
            "price": copy_obj.trial_amount,
            "full_price": (money(option.full_minor, settings.currency)
                           if option.discounted else ""),
            "per_day": money(round(option.per_day_minor()), settings.currency),
            "selected": option.key == plans.DEFAULT_KEY,
        })
    return cards


def _plan_copy(key: str | None, *, discounted: bool = False
               ) -> tuple[plans.PlanOption, Copy] | None:
    """The chosen plan and its copy. No key means the 1-week plan; an unknown key means none."""
    option = (plans.find(settings, key, discounted=discounted) if key
              else plans.options(settings, discounted=discounted)[0])
    if option is None:
        return None
    return option, build_copy(plans.settings_for(settings, option))


# ---------------------------------------------------------------------------
# Summary checkpoint
# ---------------------------------------------------------------------------

@app.get("/summary", response_class=HTMLResponse)
async def summary(request: Request) -> HTMLResponse:
    """The sunk-effort moment, placed deliberately before the artefact.

    The user reads their own answers back in their own wording, then sees a plan that
    demonstrably used them. That ordering is the difference between an artefact that looks
    derived and one that looks decorative, and it is where a mis-click is still free to
    fix.
    """
    stored = answers_for(request)
    if not stored:
        return RedirectResponse(url="/onboarding/0", status_code=302)
    if not is_complete(stored):
        # Resume at the first gap rather than restarting: eleven answered questions should
        # not be thrown away because of one.
        return RedirectResponse(url=f"/onboarding/{missing_indices(stored)}", status_code=302)

    profile = profile_from_answers(stored)
    flow = flow_for(stored)
    # Answers store option values; the checkpoint shows the labels the visitor tapped, grouped
    # by section in flow order. Information screens are not answers and are left out.
    rows = []
    for section_key in SECTION_KEYS:
        entries = [
            {
                "question": question,
                "position": position,  # deep-link target for the Change affordance
                "labels": [OPTION_LABELS[question.key].get(v, v)
                           for v in stored.get(question.key, [])],
                "note": (stored.get(f"{question.key}{NOTE_SUFFIX}") or [""])[0],
            }
            for position, question in enumerate(flow)
            if question.section == section_key and not question.is_info
        ]
        if entries:
            rows.append({"key": section_key, "title": SECTION_TITLES[section_key],
                         "entries": entries})

    answered = sum(len(group["entries"]) for group in rows)
    return templates.TemplateResponse(
        request, "summary.html",
        page_context(request, rows=rows, profile=profile, progress=100,
                     total=answered, plan=build_plan(profile)),
    )


# ---------------------------------------------------------------------------
# Preview: the pre-paywall artefact
# ---------------------------------------------------------------------------

async def build_preview(request: Request) -> tuple[Plan, str]:
    """Generate the plan: deterministic core always, optional LLM voice on top.

    The builder runs first, so an LLM outage costs the tone and not the artefact. The
    replacement is the coach note only, and only after the copy guardrail has scanned it.
    """
    profile = profile_from_answers(answers_for(request))
    plan = build_plan(profile)

    note = builder_coach_note(profile, plan)
    source = "builder"
    if settings.preview_mode == "llm":
        generated = await coach_note(profile, plan, timeout_seconds=settings.llm_timeout_seconds)
        if generated:
            note, source = generated, "llm"
    return plan.with_coach_note(note, source=f"{plan.source}+{source}"), source


@app.get("/preview", response_class=HTMLResponse)
async def preview(request: Request) -> HTMLResponse:
    stored = answers_for(request)
    if not stored:
        return RedirectResponse(url="/onboarding/0", status_code=302)
    if not is_complete(stored):
        return RedirectResponse(url=f"/onboarding/{missing_indices(stored)}", status_code=302)

    if not _email_of(stored):
        return RedirectResponse(url="/email", status_code=302)

    if plans.discount_enabled(settings) and _offer_started(stored) is None:
        # First sight of the paywall starts the discount clock. The `previewed` row below
        # persists it, and every later read of the paywall uses this one start time.
        stored[OFFER_KEY] = [f"{_now():.0f}"]
    offer = offer_state(stored)

    plan, coach_source = await build_preview(request)
    copy_obj = build_copy(settings)
    store.append_event(
        visitor_key=get_visitor(request),
        status="previewed",
        answers=stored,
        step=len(QUESTIONS),
        plan_signature=plan.profile_signature,
        plan_source=plan.source,
        coach_source=coach_source,
        # Variant label for this row. Fixed today; when a test variable goes live it lands
        # here, and this column is where cohort splits get read from.
        offer_id=OFFER_ID,
        user_agent=request.headers.get("user-agent", ""),
    )
    return templates.TemplateResponse(
        request, "preview.html",
        page_context(
            request, copy_obj=copy_obj, plan=plan, profile=profile_from_answers(stored),
            total=_answer_count(stored), locked_weeks=WEEK_ARCS[1:],
            unlock_items=ARTIFACT_PAID, free_items=ARTIFACT_FREE,
            llm_live=(coach_source == "llm"), cards=plan_cards(offer["active"]),
            score=compatibility(plan), offer=offer,
        ),
    )


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def _checkout_gate(request: Request) -> HTMLResponse | RedirectResponse | None:
    """Checkout needs a finished quiz, and no more than that.

    Returns the redirect a handler should give back when the visitor has not earned the
    screen. Checked per handler rather than as middleware because `/checkout/complete` must
    *not* enforce it: a visitor who pays and returns in a different browser still owns that
    subscription, and bouncing them to question 1 would lose a paid customer at the finish
    line for a missing cookie.
    """
    stored = answers_for(request)
    if is_complete(stored):
        return None
    return RedirectResponse(url="/onboarding/0", status_code=302)


def _renewal_line_for(copy_obj: Copy) -> str:
    """The mandated renewal sentence, phrased for however certain the date is.

    `claims.renewal_line` says "based on today, that first renewal falls about X", which is
    right at checkout before a session exists and wrong once the processor has given a date,
    where "about" understates a day we were told. The prefix is preserved so
    `disclosure_coverage` still matches it, which is the whole argument for adjusting the
    tail rather than replacing the sentence.
    """
    line = copy_obj.disclosure("renewal_line")
    if copy_obj.renewal_is_real:
        line = line.replace("Based on today, that first renewal falls about ",
                            "Your first renewal falls on ")
    return line


@app.get("/checkout", response_class=HTMLResponse)
async def checkout(request: Request) -> HTMLResponse:
    if blocked := _checkout_gate(request):
        return blocked
    offer = offer_state(answers_for(request))
    chosen = _plan_copy(request.query_params.get("plan"), discounted=offer["active"])
    if chosen is None:
        return RedirectResponse(url="/preview", status_code=302)
    option, copy_obj = chosen
    plan_signature = profile_from_answers(answers_for(request)).signature()
    # Set when a submit arrived after the discount ended: the page now shows the full price.
    price_changed = request.query_params.get("changed") == "1"

    store.append_event(
        visitor_key=get_visitor(request),
        status="checkout_viewed",
        answers=form_answers(answers_for(request)),
        step=len(QUESTIONS),
        offer_id=OFFER_ID,
        user_agent=request.headers.get("user-agent", ""),
    )
    live = billing_client() is not None
    if not live:
        # Rehearsal keeps Phase 1's page verbatim, disabled inputs and all. Two templates
        # instead of one dual-mode template: the absence of a payment widget is itself a
        # disclosure, and a branch that can silently take the wrong side is how a rehearsal
        # screen reaches production looking live.
        return templates.TemplateResponse(
            request, "checkout.html",
            page_context(request, copy_obj=copy_obj, offer_id=OFFER_ID,
                         plan_signature=plan_signature, trust_points=TRUST_POINTS,
                         unlock_items=ARTIFACT_PAID, option=option, offer=offer,
                         price_changed=price_changed),
        )
    return templates.TemplateResponse(
        request, "checkout_live.html",
        page_context(request, copy_obj=copy_obj, offer_id=OFFER_ID,
                     plan_signature=plan_signature, trust_points=TRUST_POINTS,
                     unlock_items=ARTIFACT_PAID, renewal_line=_renewal_line_for(copy_obj),
                     email=_email_of(answers_for(request)), error="", option=option,
                     offer=offer, price_changed=price_changed),
    )


def _submitted_plan(request: Request, form: Any
                    ) -> tuple[plans.PlanOption, Copy] | RedirectResponse:
    """The plan a checkout form asks for, priced by the server, never by the form.

    The discount applies while the clock (plus a short grace) is running. The form carries the
    amount it displayed; if that is not the amount the server would charge now, nothing is
    charged and the visitor is sent back to the checkout page showing the current price.
    """
    key = str(form.get("plan") or "") or None
    offer = offer_state(answers_for(request), grace_seconds=OFFER_GRACE_SECONDS)
    chosen = _plan_copy(key, discounted=offer["active"])
    if chosen is None:
        return RedirectResponse(url="/preview", status_code=303)
    shown = str(form.get("amount") or "").strip()
    if shown and shown != str(chosen[0].amount_minor):
        return RedirectResponse(url=f"/checkout?plan={chosen[0].key}&changed=1",
                                status_code=303)
    return chosen


@app.post("/checkout", response_class=HTMLResponse)
async def checkout_rehearsal(request: Request) -> HTMLResponse:
    """Rehearsal submit: record intent, charge nothing, and say so on the resulting page.

    Arming charges makes this route redirect to the live flow instead of erroring, so a stale
    tab opened before someone flipped `FUNNEL_CHARGES_ENABLED` lands on a working checkout
    rather than on a form whose button no longer means anything.
    """
    if blocked := _checkout_gate(request):
        return blocked
    form = await request.form()
    chosen = _submitted_plan(request, form)
    if isinstance(chosen, RedirectResponse):
        return chosen
    option, plan_copy = chosen
    if billing_client() is not None:
        return RedirectResponse(url=f"/checkout?plan={option.key}", status_code=303)

    stored = answers_for(request)
    visitor_key = get_visitor(request)
    store.append_event(
        visitor_key=visitor_key,
        status="started",
        # The email and discount start are kept: this row becomes the visitor's latest
        # answers, and dropping them would send a returning visitor back to the email screen
        # and restart a discount clock that has already run.
        answers={**form_answers(stored), **_internal(stored)},
        step=len(QUESTIONS) + 1,
        plan_source="checkout_intent",
        offer_id=OFFER_ID,
        user_agent=request.headers.get("user-agent", ""),
    )
    # The funnel's commitment moment, recorded as an event and not only as a response row.
    # `checkout_complete` is what records it on the live path and this route is what a rehearsal
    # deployment actually runs, so without this line a rehearsal week reports visitors who
    # reached a card screen and then committed to nobody. `trials_started` is the denominator of
    # Trial→Paid, and an empty one makes that ratio blank rather than honest. No payment event
    # follows, because nothing was captured.
    billing.track("trial_started", visitor_key=visitor_key,
                  # Visitor and day, the same key the live path uses, so a visitor who somehow
                  # crosses both paths on one day is still one committed start.
                  idempotency_key=f"trial_started:{visitor_key}:"
                                  f"{telemetry.day_of(telemetry.utc_now())}",
                  detail={"live": False, "via": "rehearsal", "plan": option.key,
                          "discounted": option.discounted})
    return templates.TemplateResponse(
        request, "rehearsal_done.html",
        page_context(request, copy_obj=plan_copy, option=option),
    )


@app.post("/checkout/start", response_class=HTMLResponse)
async def checkout_start(request: Request) -> HTMLResponse:
    """Take the receipt email, create a Checkout session, and hand the browser to Stripe.

    Charges-off is a redirect back to the card screen rather than a 409, so a visitor who
    followed a link from before the switch flipped sees a coherent page instead of an error.
    A processor rejection is the visitor's problem only in the sense that they must retry: the
    message names what Stripe said, because "payment failed" with no reason is what makes
    people try a second card and end up with two charges.
    """
    if blocked := _checkout_gate(request):
        return blocked
    client = billing_client()
    if client is None:
        return RedirectResponse(url="/checkout", status_code=303)

    form = await request.form()
    chosen = _submitted_plan(request, form)
    if isinstance(chosen, RedirectResponse):
        return chosen
    option, copy_obj = chosen
    email = str(form.get("email") or "").strip()
    if not EMAIL_RE.fullmatch(email):
        return templates.TemplateResponse(
            request, "checkout_live.html",
            page_context(
                request, copy_obj=copy_obj, offer_id=OFFER_ID,
                plan_signature=profile_from_answers(answers_for(request)).signature(),
                trust_points=TRUST_POINTS, unlock_items=ARTIFACT_PAID,
                renewal_line=_renewal_line_for(copy_obj), email=email, option=option,
                offer=offer_state(answers_for(request)), price_changed=False,
                error="We need a working email address: the receipt is the record of what "
                      "you agreed to, and it carries your cancel link."),
            status_code=422,
        )

    stored = answers_for(request)
    plan_signature = profile_from_answers(stored).signature()
    try:
        session = await anyio.to_thread.run_sync(
            lambda: billing.create_session(
                visitor_key=get_visitor(request), email=email,
                plan_signature=plan_signature, origin=_origin(request), option=option))
    except stripe_billing.StripeError as exc:
        log.error("checkout session creation failed: %s", exc)
        return templates.TemplateResponse(
            request, "checkout_error.html",
            page_context(request, headline="We could not open the payment page.",
                         detail=f"The payment provider said: {exc}",
                         charged_note="Nothing has been charged, so trying again is safe.",
                         **{}),
            status_code=502,
        )

    url = session.get("url") or ""
    if not url:
        return templates.TemplateResponse(
            request, "checkout_error.html",
            page_context(request, headline="We could not open the payment page.",
                         detail="The payment provider returned a session with no address to "
                                "go to, which is a problem on our side.",
                         charged_note="Nothing has been charged."),
            status_code=502,
        )
    return RedirectResponse(url=url, status_code=303)


@app.get("/checkout/complete", response_class=HTMLResponse)
async def checkout_complete(request: Request, session_id: str = "",
                            plan: str = "") -> HTMLResponse:
    """Landing page after Stripe says the card took.

    Deliberately does not require the quiz cookie. The session id is looked up at the
    processor and its `client_reference_id` compared to this visitor, so the page credits a
    plan on evidence rather than on a redirect that merely *looked* successful: anyone can put
    a session id in a URL, and the two checks are what stop that crediting a stranger's
    purchase to the wrong row.
    """
    visitor_key = get_visitor(request)
    stored = answers_for(request)
    try:
        row = await anyio.to_thread.run_sync(
            lambda: billing.checkout_complete(
                visitor_key=visitor_key, session_id=session_id, email="",
                plan_signature=(profile_from_answers(stored).signature() if stored else ""),
                answers=form_answers(stored) if stored else {},
                coach_note="", plan_key=plan))
    except stripe_billing.StripeError as exc:
        log.error("checkout completion rejected (%s): %s", session_id, exc)
        return templates.TemplateResponse(
            request, "checkout_error.html",
            page_context(
                request, headline="We could not confirm that payment.",
                detail=("We reached the payment provider and it did not recognise this "
                        f"checkout as yours. What it said: {exc}"),
                charged_note=("If a charge appears on your statement, it is real and we can "
                              "find it by date and amount, so you do not need to repeat it.")),
            status_code=400,
        )

    copy_obj = billing.copy(row)
    results = billing.send_trial_receipt(row)
    store.append_event(
        visitor_key=visitor_key, status="paid",
        answers=form_answers(stored) if stored else {},
        step=len(QUESTIONS) + 1,
        plan_signature=row.get("plan_signature", ""), plan_source="checkout",
        offer_id=OFFER_ID, user_agent=request.headers.get("user-agent", ""),
    )
    delivered = [result for result in results if result.ok]
    return templates.TemplateResponse(
        request, "checkout_done.html",
        page_context(
            request, copy_obj=copy_obj, email=row.get("email", ""),
            renewal_line=_renewal_line_for(copy_obj),
            mail_note=(
                f"Receipt sent to {delivered[0].to}." if delivered else
                "We could not send the receipt email, so the four lines above are the record "
                "of what you agreed to; they repeat on your account page, which needs no "
                "email to open.")),
    )


@app.get("/checkout/cancelled", response_class=HTMLResponse)
async def checkout_cancelled(request: Request) -> HTMLResponse:
    """Visitor backed out of Stripe's page. Nothing was charged and nothing was written."""
    return templates.TemplateResponse(
        request, "checkout_error.html",
        page_context(
            request, headline="Checkout closed, nothing charged.",
            detail="You left the payment page, so no subscription exists and no money moved. "
                   "Your answers and your plan are saved, so you can pick a plan again "
                   "whenever you are ready.",
            charged_note="No charge was made and nothing was saved."),
    )


def _origin(request: Request) -> str:
    """Absolute origin for the processor's return URLs.

    From the request rather than from config, because a staging deploy behind a proxy must
    hand Stripe a URL that actually resolves for the visitor who left. `FUNNEL_PUBLIC_URL` is
    the fallback, and the config value is the one `/health` shows, so a wrong host in a return
    URL is visible before a customer meets it.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip() or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if not host:
        return settings.public_url
    return f"{scheme}://{host}".rstrip("/")


# ---------------------------------------------------------------------------
# Member area: dashboard, plan export, cancel + save offer
# ---------------------------------------------------------------------------

def _subscription_context(request: Request, row: dict[str, Any]) -> dict[str, Any]:
    """Billing block context, built the same way for every member surface.

    Centralised because checklist 6 is about one thing: the amount and the next date, always
    visible. Two places assembling that pair by hand is how a date stops showing on one of
    them, which is the soft Roach Motel the spec names.
    """
    copy_obj = billing.copy(row)
    period_end = iso_to_local_date(row.get("current_period_end", ""))
    renewal, is_real = next_bill_date(period_end, billing.plan_settings(row).trial_days)
    if is_real:
        # Charges are armed and a real period end is in hand, which is the fact `/health` says it
        # wants to know before it stops calling every renewal date an estimate. A process-local
        # flag rather than a fresh provider read: the check is "has anyone been told a real date
        # yet", and this function is the one place a real date becomes a page.
        global SUBSCRIPTION_READ
        SUBSCRIPTION_READ = True
    status = row.get("status", "")
    discounted = int(row.get("discount_months") or 0)
    price = (billing.copy(row).lite_per_month if row.get("plan") == "lite"
             else copy_obj.rebill_per_month)
    charged = _minor(row.get("trial_amount_minor") or settings.trial_amount_minor)
    return {
        "subscription": row,
        "status": status,
        "is_cancelled_state": status in ("cancelled_at_period_end", "canceled"),
        "is_paused": status == "paused",
        "copy": copy_obj,
        "renewal_label": human_date(renewal),
        "renewal_is_real": is_real,
        "plan_name": plan_label(row.get("plan", "full"), discounted=bool(discounted)),
        "plan_price": price,
        "charged_amount": charged,
        "charged_on": _charged_on(row),
        "card_note": f"{row['card_brand']} ending {row['card_last4']}"
        if row.get("card_brand") and row.get("card_last4") else "",
        "next_bill_line": copy_obj.next_bill_heading(
            plan=row.get("plan", "full"), discounted_months=discounted,
            discounted_price=(copy_obj.half_lite_price if row.get("plan") == "lite"
                              else copy_obj.half_price) if discounted else ""),
        "billing_note": _billing_note(row, copy_obj),
    }


def _minor(minor_units: int) -> str:
    return money(int(minor_units), settings.currency)


def _charged_on(row: dict[str, Any]) -> str:
    created = iso_to_local_date(row.get("created_at", ""))
    return human_date(created) if created else "at checkout"


def _billing_note(row: dict[str, Any], copy_obj: Copy) -> str:
    """One honest sentence about where the numbers came from.

    Shown because the renewal date is a disclosure, and a date that came from a rehearsal
    rather than from the processor deserves to say so on the page a member reads before their
    first real charge.
    """
    if row.get("subscription_id"):
        return (
            "Dates and amounts come from your subscription record at the payment provider, so "
            "they match the invoice you will receive.")
    return (
        "This subscription has no payment-provider record yet, so the date above is the "
        f"{billing.plan_settings(row).trial_days}-day plan length counted from checkout. It is confirmed by the "
        "receipt email as soon as a charge exists.")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """The member surface: billing block first, the unlocked 4-week plan below it.

    Reads only what was paid for. `answers_json` is the snapshot taken at checkout, so a
    member who clears cookies, switches browser, or later rebuilds a plan from different
    answers still sees the plan they bought, and an export they download matches it.
    """
    row = billing_store.get_subscription(get_visitor(request))
    if row is None:
        return templates.TemplateResponse(
            request, "dashboard.html",
            page_context(request, subscription=None, plan=None, weeks=[], total=0,
                         copy_obj=build_copy(settings), renewal_label="", status="",
                         is_cancelled_state=False, is_paused=False, renewal_is_real=False,
                         plan_name="", plan_price="", charged_amount="", charged_on="",
                         card_note="", next_bill_line="", billing_note=""),
        )

    context = _subscription_context(request, row)
    answers, coach_note = _stored_answers(row)
    plan: Plan | None = None
    if answers:
        plan = build_plan(profile_from_answers(answers))
        if coach_note:
            plan = plan.with_coach_note(coach_note, source=plan.source)
    return templates.TemplateResponse(
        request, "dashboard.html",
        page_context(
            request, plan=plan,
            weeks=_member_weeks(plan) if plan else [],
            total=_answer_count(answers) if answers else 0, **context),
    )


def _stored_answers(row: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The answers snapshot, defensively.

    Returns `({}, '')` rather than raising on anything unparseable. A member's plan disappearing
    is bad, but an account page that 500s on one malformed JSON blob takes the *billing* block
    down with it, and checklist 6 says the next date stays visible.
    """
    try:
        blob = json.loads(row.get("answers_json") or "{}")
    except json.JSONDecodeError:
        return {}, ""
    answers = blob.get("answers") or {}
    return (answers if isinstance(answers, dict) else {}), str(blob.get("coach_note") or "")


def _member_weeks(plan: Plan) -> list[dict[str, Any]]:
    """Four weeks for the paid view: week 1 from the plan itself, weeks 2-4 from their arcs.

    Week 1's lines come from the plan rather than from the arc text, because week 1 is the week
    the member has already read and a summary that contradicts it reads as a different product.
    """
    weeks: list[dict[str, Any]] = []
    for arc in WEEK_ARCS:
        number = int(arc["week"])
        if number == 1:
            lines = [
                f"{plan.minutes} minutes a day, {plan.when_label}, at the {plan.level_name} level",
                f"{plan.reps} slow holds of {plan.hold_seconds} seconds, each followed by "
                f"{plan.rest_seconds} seconds of full relax",
                f"Daily drill: {plan.drill_name}, and a check-in on day 7",
            ]
        else:
            lines = [arc["arc"]]
        weeks.append({"number": number, "label": arc["label"], "arc": arc["arc"],
                      "lines": lines})
    return weeks


@app.get("/account/plan.txt", response_class=HTMLResponse)
async def plan_export(request: Request) -> Response:
    """Plain-text plan, for the fridge door and for anybody who wants their data out.

    A `Content-Disposition` attachment, because "export" that opens in a tab is a page you now
    have to copy by hand. Terms say whatever you generated while subscribed stays exportable,
    and a terms sentence the funnel does not implement is a claim.
    """
    row = billing_store.get_subscription(get_visitor(request))
    if row is None:
        return RedirectResponse(url="/dashboard", status_code=302)
    answers, _note = _stored_answers(row)
    if not answers:
        return Response(content="Plan unavailable for this subscription.\n",
                        media_type="text/plain; charset=utf-8",
                        headers={"content-disposition":
                                 'attachment; filename="kegeldoc-plan.txt"'})
    plan = build_plan(profile_from_answers(answers))
    return Response(content=_plan_text(plan), media_type="text/plain; charset=utf-8",
                    headers={"content-disposition":
                             f'attachment; filename="kegeldoc-{plan.profile_signature}.txt"'})


@app.get("/account/plan.pdf", response_class=HTMLResponse)
async def plan_pdf(request: Request) -> Response:
    """PDF export, printed from the same text as the `.txt` by macOS' `textutil`.

    Degrades rather than fails, and the degradation is a redirect to the text export rather
    than a 200 with a text body under a PDF filename: a member who downloads "plan.pdf" and
    gets plain text in a `.pdf` has an unopenable file and no explanation. A redirect gives
    them the same content with the right name and the right type.

    A funnel's dependency on a system binary is the kind of thing that is fine until the day
    it is a 500 on a page a paying member needs.
    """
    row = billing_store.get_subscription(get_visitor(request))
    if row is None:
        return RedirectResponse(url="/dashboard", status_code=302)
    answers, _note = _stored_answers(row)
    if not answers:
        return RedirectResponse(url="/account/plan.txt", status_code=302)
    plan = build_plan(profile_from_answers(answers))
    import shutil
    import subprocess
    import tempfile
    textutil = shutil.which("textutil")
    if not textutil:
        return RedirectResponse(url="/account/plan.txt", status_code=302)
    with tempfile.TemporaryDirectory() as workdir:
        source = Path(workdir) / "plan.txt"
        target = Path(workdir) / "plan.pdf"
        source.write_text(_plan_text(plan), encoding="utf-8")
        try:
            await anyio.to_thread.run_sync(lambda: subprocess.run(
                [textutil, "-convert", "pdf", "-output", str(target), str(source)],
                check=True, capture_output=True, timeout=30))
            body = target.read_bytes()
        except Exception as exc:  # noqa: BLE001 - any printer failure ends the same way
            log.warning("pdf export fell back to text: %s", exc)
            return RedirectResponse(url="/account/plan.txt", status_code=302)
    return Response(content=body, media_type="application/pdf",
                    headers={"content-disposition":
                             f'attachment; filename="kegeldoc-{plan.profile_signature}.pdf"'})


def _plan_text(plan: Plan) -> str:
    """The artefact as a printable document. One builder for both export routes, so the PDF and
    the text file cannot describe the plan differently."""
    lines = [
        plan.headline,
        "=" * len(plan.headline),
        "",
        plan.summary,
        "",
        plan.coach_note,
        "",
        f"Level {plan.level_name} - {plan.hold_seconds}s holds, {plan.rest_seconds}s relax, "
        f"{plan.reps} reps - {plan.minutes} minutes a day, {plan.when_label}",
        f"Daily drill: {plan.drill_name}. {plan.drill_text}",
        "",
    ]
    if plan.safety_note:
        lines += ["Before you start", "-" * 16, plan.safety_note, ""]
    for day in plan.days:
        lines.append(f"{day.day}: {day.title}, {day.minutes} min")
        lines.append("-" * (len(day.day) + len(day.title) + 2))
        for step in day.steps:
            lines.append(f"  - {step}")
        lines.append("")
    lines += ["What you track", "-" * 14, ""]
    for label, target, why in plan.metrics:
        lines += [f"{label}: {target}", f"  {why}", ""]
    if plan.tips:
        lines += ["Notes from your answers", "-" * 23, ""]
        lines += [f"- {tip}" for tip in plan.tips] + [""]
    lines += ["General pelvic floor exercise guidance, not medical advice.",
              f"Plan reference {plan.profile_signature}"]
    return "\n".join(lines) + "\n"


@app.post("/account/cancel", response_class=HTMLResponse)
async def account_cancel(request: Request) -> HTMLResponse:
    """One click, executed, then confirmed, and only then the offer (§2.3).

    Renders its own confirmation instead of redirecting, because the rule is that the
    cancellation is *done* before persuasion appears. A redirect would put the save offer on a
    page rendered before the outcome existed, which is the interception the spec bans.
    """
    visitor_key = get_visitor(request)
    # `cancel_now` already reports a refused cancellation as `done=False` and swallows the
    # processor's `StripeError` to do it, so nothing on this path should raise on the provider's
    # behalf. The one gap is a service whose client is not the one `cancel_now` holds: here the
    # exception would escape and 500 a member who is, in fact, still subscribed. Rendered through
    # the ordinary not-cancelled shape rather than a crash page, because the honest sentence
    # already exists and a traceback is not it.
    try:
        result = await anyio.to_thread.run_sync(
            lambda: billing.cancel_now(visitor_key=visitor_key))
    except stripe_billing.StripeError as exc:
        log.error("cancel raised for %s: %s", visitor_key, exc)
        failed_row = billing_store.get_subscription(visitor_key)
        result = billing_module.failure_result(
            settings, exc,
            access_until=billing.access_until(failed_row) if failed_row else "")
    row = billing_store.get_subscription(visitor_key) or {}
    if result.done:
        store.append_event(
            visitor_key=visitor_key, status="cancelled",
            answers=form_answers(answers_for(request)),
            step=len(QUESTIONS) + 2, plan_source="cancel",
            offer_id=OFFER_ID, user_agent=request.headers.get("user-agent", ""))
    return templates.TemplateResponse(
        request, "cancel_done.html",
        page_context(request, result=result, email=row.get("email", ""),
                     menu=save_offer.offer_menu(settings, billing.copy(row))),
    )


@app.post("/account/cancel/offer", response_class=HTMLResponse)
async def cancel_offer(request: Request, offer: str = Form("")) -> HTMLResponse:
    """Act on a save-offer choice. Every option here is an opt-back-in.

    An unknown or unconfigured value falls through to "stay cancelled" rather than 422ing.
    Being unhelpful to someone who has just cancelled is the failure this screen cannot
    afford, and the outcome is the state they were already in.

    A processor rejection reaches here as an exception rather than as an outcome, and 500ing
    on it is the one wrong answer available: the customer has *already* cancelled, so a crash
    page tells someone who pressed Cancel that the page is broken and leaves them to guess
    whether they are subscribed. `offer_done.html` renders the outcome it is handed, so the
    rejection renders as its own failure copy through the same template, and the row stays
    cancelled either way.
    """
    visitor_key = get_visitor(request)
    try:
        outcome = await anyio.to_thread.run_sync(
            lambda: billing.accept_offer(visitor_key=visitor_key, key=offer))
    except stripe_billing.StripeError as exc:
        log.error("save offer %s raised for %s: %s", offer, visitor_key, exc)
        return templates.TemplateResponse(
            request, "offer_done.html",
            page_context(request, outcome=save_offer.Outcome(
                event="none",
                headline="That offer could not be applied.",
                detail=("Your cancellation still stands and nothing will be charged. We could "
                        "not make the change on our side, so nothing has been sold to you that "
                        "is not really in place. Go back and pick another option, or just stay "
                        "cancelled."),
                fields={})))
    if outcome is None:
        # Nothing was accepted, so nothing may be *reported* as accepted, and re-cancelling
        # here would log a second cancellation for an account that was already cancelled
        # before this request arrived. State the existing state and keep the menu live.
        row = billing_store.get_subscription(visitor_key)
        already = bool(row) and row.get("status") in (
            "cancelled_at_period_end", "canceled")
        return templates.TemplateResponse(
            request, "cancel_done.html",
            page_context(
                request,
                result=billing_module.CancelResult(
                    done=already,
                    headline="Already cancelled." if already
                    else "Cancelled. Nothing more will be charged.",
                    detail="Nothing was changed by that, and no offer has been applied.",
                    processor_ok=True,
                    access_until=billing.access_until(row) if row else "")
                if already else billing.cancel_now(visitor_key=visitor_key),
                email=(row or {}).get("email", ""),
                menu=save_offer.offer_menu(settings, billing.copy(row or {}))),
        )
    return templates.TemplateResponse(
        request, "offer_done.html", page_context(request, outcome=outcome))


@app.get("/account/survey", response_class=HTMLResponse)
async def survey_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "survey.html",
        page_context(request, headline=save_offer.SURVEY_HEADLINE,
                     reasons=save_offer.SURVEY_REASONS,
                     second_question="What would have made you stay? (optional)",
                     chosen="", note=""),
    )


@app.post("/account/survey", response_class=HTMLResponse)
async def survey_submit(request: Request, reason: str = Form(""),
                        note: str = Form("")) -> HTMLResponse:
    """Optional, never blocking. An empty submit is a complete answer."""
    await anyio.to_thread.run_sync(
        lambda: billing.record_survey(visitor_key=get_visitor(request), reason=reason.strip(),
                                      note=note.strip()))
    return templates.TemplateResponse(
        request, "offer_done.html",
        page_context(request, outcome=save_offer.Outcome(
            event="survey_reason",
            headline="Thank you, that is enough.",
            detail="Two answers recorded, nothing changed on your account, and no email you "
                   "did not ask for. Your plan stays available until the access date on your "
                   "account page.",
            fields={})),
    )


# ---------------------------------------------------------------------------
# Processor webhook
# ---------------------------------------------------------------------------

@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> Response:
    """Receive one Stripe event. Signature first, always.

    Three behaviours worth knowing, because each is a place a rewrite gets it wrong:

    * The **raw body** is verified before it is parsed. Verifying a re-serialised copy fails
      intermittently and mysteriously, since whitespace is part of what was signed.
    * With no `STRIPE_WEBHOOK_SECRET_FUNNEL` the handler accepts **unsigned** posts and logs a
      warning. That is the state the spec warns about (`§2.4`), and it is tolerable only while
      no automated action hangs off the webhook: a spoofed `invoice.paid` currently moves a
      status column and nothing else. `/health` names it instead of hiding it.
    * An event whose type this lane ignores returns 200, not 500. Stripe retries a 5xx
      indefinitely, so an unusable event type answered with an error becomes a permanent loop
      that looks like an outage.
    """
    body = await request.body()
    signature = request.headers.get("stripe-signature", "")
    keys = stripe_billing.FunnelKeys.load()

    if keys.webhook_secret:
        try:
            stripe_billing.verify_signature(body, signature, keys.webhook_secret)
        except stripe_billing.StripeError as exc:
            log.warning("rejected webhook: %s", exc)
            return Response(content=str(exc), status_code=400, media_type="text/plain")
    elif not settings.charges_enabled:
        log.warning("STRIPE_WEBHOOK_SECRET_FUNNEL unset: accepting an unsigned webhook in "
                    "rehearsal, where no automated action depends on it")
    else:
        # Charges are live and the secret is absent: accept, because the alternative is
        # refusing every event and desynchronising the very subscriptions that are now
        # real. Say so in the response so a dashboard test shows up as a visible reason.
        log.error("charges are enabled but STRIPE_WEBHOOK_SECRET_FUNNEL is unset: this "
                  "webhook is unsigned and can be forged by anyone who can reach it")

    try:
        event = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return Response(content="body is not JSON", status_code=400,
                        media_type="text/plain")

    billing.client = billing_client()
    try:
        reason = billing.handle_event(event)
    except stripe_billing.StripeError as exc:
        # A failure we cause (bad state, unreachable processor) is a 5xx on purpose: this is
        # the one case where a retry from Stripe is the right medicine.
        log.error("webhook %s failed: %s", event.get("type"), exc)
        return Response(content=str(exc), status_code=502, media_type="text/plain")
    log.info("webhook %s -> %s", event.get("type"), reason)
    return JSONResponse({"received": True, "result": reason})


# ---------------------------------------------------------------------------
# Lifecycle notices
# ---------------------------------------------------------------------------

@app.post("/ops/send-notices", response_class=HTMLResponse)
async def send_notices(request: Request) -> JSONResponse:
    """Send the renewal notices that are due (day −2, §2.1).

    A route rather than a cron job inside the app, because Phase 2 has no scheduler and a
    scheduler with no operator surface hides whether it ran. A timer, a `curl`, or a human on a
    renewal morning all reach the same idempotent function, which records each send before
    attempting it and therefore cannot tell a customer about the same charge twice.
    """
    billing.client = billing_client()
    results = await anyio.to_thread.run_sync(billing.send_due_notices)
    return JSONResponse({
        "sent": len([result for result in results if result.ok]),
        "failed": len([result for result in results if not result.ok]),
        "transport": mailer.transport,
        "detail": [f"{result.to}: {result.transport} {result.detail}" for result in results],
    })


# ---------------------------------------------------------------------------
# Policy pages
# ---------------------------------------------------------------------------

@app.get("/policy/{slug}", response_class=HTMLResponse)
async def policy(request: Request, slug: str) -> HTMLResponse:
    pages = build_copy(settings).policy_pages()
    if slug not in pages:
        return templates.TemplateResponse(
            request, "policy.html",
            page_context(request, slug="", title="Page not found", paragraphs=()),
            status_code=404,
        )
    title, paragraphs = pages[slug]
    return templates.TemplateResponse(
        request, "policy.html",
        page_context(request, slug=slug, title=title, paragraphs=paragraphs),
    )


# ---------------------------------------------------------------------------
# Ops views (not marketing surfaces)
# ---------------------------------------------------------------------------

#: A complete answer set that is deliberately awkward: women's track, a birth under six weeks ago
#: (gentle start and a safety note), frequent urgency (urge drill overrides the goal's drill), long sitting
#: and high stress (two lifestyle notes). Everything the artefact has to get right at once.
CHECK_ANSWERS: dict[str, list[str]] = {
    "track": ["women"], "age": ["30"], "goal_women": ["leaks"], "leak_effort": ["often"],
    "leak_exercise": ["sometimes"], "urge": ["often"], "night": ["1"], "leak_amount": ["drops"],
    "pads": ["sometimes"], "fact_women_leaks": ["seen"], "births": ["two"],
    "birth_when": ["under6w"], "meno": ["pre"], "heaviness": ["never"], "sensation": ["3"],
    "sat_w": ["3"], "sexpain_w": ["never"], "confidence_w": ["3"], "libido_w": ["3"],
    "fact_women_first": ["seen"], "milestone": ["seen"], "tried": ["unsure"],
    "fact_find": ["seen"], "feel": ["some"], "strength": ["2"], "hold": ["5"],
    "fact_relax": ["seen"], "safety": ["none"], "alcohol": ["rarely"], "smoke": ["no"],
    "diet": ["4"], "activity": ["2"], "sitting": ["gt8"], "stress": ["5"], "minutes": ["5"],
    "when": ["desk"],
}


def selfcheck_render() -> dict[str, Any]:
    """Render the artefact offline and report whether its content actually arrived.

    This exists because the funnel's worst failure is silent. A Jinja name collision
    (`{% for slot in ... %}` with `{{ slot.slot }}`, where tuple subscripting raises
    TypeError and Jinja converts that to Undefined) rendered every day card with an empty
    meal list and every metrics row blank, while every HTTP status said 200. The page looked
    like a plan. It was a plan-shaped skeleton.

    So: build a plan from a fixed answer set, render the real template, and require the
    strings a visitor came for. Cheap, runs on a page nobody crawls, and it fails loudly on
    the one class of bug that a status code cannot see.
    """
    report: dict[str, Any] = {"ok": None, "problems": [], "checks_run": 0,
                              "blank_slots": 0}
    try:
        plan = build_plan(profile_from_answers(CHECK_ANSWERS))
        copy_obj_for_check = build_copy(settings)
        html = templates.get_template("preview.html").render(
            **page_context(
                # No `request`: starlette injects the real one at response time, and no
                # template reads it, so an offline render needs a stand-in and nothing more.
                _OfflineRequest(), copy_obj=copy_obj_for_check, plan=plan,
                profile=profile_from_answers(CHECK_ANSWERS), total=_answer_count(CHECK_ANSWERS),
                locked_weeks=WEEK_ARCS[1:], unlock_items=ARTIFACT_PAID,
                free_items=ARTIFACT_FREE, llm_live=False, cards=plan_cards(),
                score=compatibility(plan), offer={"active": False},
            ))
        needles = [
            ("Mon", "the first day of week 1"), ("Sun", "the last day of week 1"),
            ("Find the muscle", "the day-1 session title"),
            ("Urge pause", "the urge drill, i.e. the urgency answer reached the plan"),
            ("Foundation", "the gentle starting level a recent birth forces"),
            ("full relax", "the relax half of every rep"),
            ("Before you start", "the safety-note heading"),
            ("Longest comfortable hold", "a metrics row"),
            (plan.profile_signature, "the plan reference"),
            (copy_obj_for_check.trial_amount, "the trial price on the artefact screen"),
            (copy_obj_for_check.rebill_per_month, "the recurring price"),
        ]
        report["problems"] = [label for needle, label in needles if needle not in html]
        # An empty meal row is the exact regression: headings present, content gone.
        report["blank_slots"] = html.count("<dt") - html.count("</dt>")
        report["checks_run"] = len(needles)
        report["ok"] = not report["problems"]
    except Exception as exc:  # a broken self-check must show up, not 500 the health page
        report["ok"] = False
        report["problems"] = [f"render raised {type(exc).__name__}: {exc}"]
    return report


class _OfflineRequest:
    """Stand-in for `Request` in an offline template render.

    Only `url_for` would touch it, and `preview.html` does not call that. Keeping this a
    class rather than `None` means a future template that *does* reach for `request` fails
    here with an obvious AttributeError instead of rendering a subtly wrong page.
    """

    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    query_params: dict[str, str] = {}
    #: starlette's TemplateResponse copies `request.state` into the render context, so the
    #: stub needs one even though no template reads it.
    state: Any = SimpleNamespace()

    def url(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("the artefact self-check template must not build URLs")


def plan_audit() -> list[str]:
    """Builder-table integrity: every goal reaches a drill, levels are sane, pain is relax-first."""
    try:
        return plan_audit_problems()
    except Exception as exc:
        return [f"audit raised {type(exc).__name__}: {exc}"]


def billing_readiness(state: dict[str, Any], copy_obj: Copy) -> list[str]:
    """What must be settled before this funnel faces traffic, as sentences.

    Deliberately not a reworded duplicate of `/health`'s markup: the page renders this same
    list, and `tests/test_billing.py` asserts on the *reasons* rather than on page text. A test
    that grepped the template for "Billing readiness: clean" would happily pass while the
    readiness function drifted, which is the specific way an ops page becomes decoration.
    """
    issues: list[str] = []
    if state.get("error"):
        issues.append(str(state["error"]))
    if state.get("charges_enabled") and not state.get("key_present"):
        issues.append(
            "FUNNEL_CHARGES_ENABLED is true but STRIPE_SECRET_KEY_FUNNEL is unset, so "
            "checkout cannot take a card. Either set the key or switch charges off.")
    if state.get("charges_enabled") and not state.get("webhook_secret_present"):
        issues.append(
            "Charges are enabled with STRIPE_WEBHOOK_SECRET_FUNNEL unset, so this handler "
            "accepts unsigned events: anyone who can reach the URL can mark a subscription "
            "paid or cancelled.")
    if state.get("key_present") and state.get("acct_type") not in ("standard", "express"):
        issues.append(
            f"The funnel secret key decoded as account type {state.get('acct_type')!r}, which "
            "is not a standard funnel account. Confirm it is not the B2B account's key.")
    if state.get("processor") and state["processor"].get("reachable") is False:
        issues.append(f"The payment provider rejected our key: {state['processor'].get('error')}")
    if (state.get("processor") and state.get("charges_enabled")
            and state["processor"].get("payments_enabled") is False):
        issues.append(
            "The merchant account has card payments disabled, so every checkout will fail "
            "even though our side is configured.")
    if not settings.support_email:
        issues.append(
            "FUNNEL_SUPPORT_EMAIL is empty, so receipt and cancellation emails carry no reply "
            "address. Checklist 5 asks for a monitored one, and a receipt nobody can answer "
            "turns a refund request into a dispute.")
    if not state.get("discount_coupon_configured"):
        issues.append(
            "FUNNEL_DISCOUNT_COUPON_ID is unset, so save-offer option C (half price for two "
            "months) is hidden from the cancel menu. That is the correct behaviour, not a "
            "bug: an offer the processor cannot apply is a promise the invoice breaks.")
    # Read off the flag rather than off `copy_obj.renewal_is_real`, which is what this check used
    # to test, and which could never fire: every caller passes `build_copy(settings)` with no
    # subscription, so `renewal_is_real` was False for the readiness copy in *every* configuration,
    # armed or not, and the issue was decided by `client_active` alone. That made
    # `test_no_readiness_findings_when_correctly_configured` fail on a correctly configured funnel
    # and made the warning itself unfalsifiable. The flag says what the sentence claims.
    if state.get("client_active") and not SUBSCRIPTION_READ:
        issues.append(
            "Charges are live but no subscription has been read from the provider yet in this "
            "process, so every renewal date shown so far is an estimate.")
    return issues


@app.get("/health", response_class=HTMLResponse)
async def health(request: Request) -> HTMLResponse:
    """Guardrail state on one screen: copy scan, disclosure coverage, model routing.

    A page rather than a status code because the thing worth seeing before traffic arrives
    is not "is the process up" but "did a copy edit break a rule", and that answer should
    not require running a test suite on a VPS.
    """
    copy_obj = build_copy(settings)
    router = load_router()
    routing: dict[str, Any] = {}
    if router is not None:
        try:
            routing = router.get_router().describe()
        except Exception as exc:  # a dark router is a state to display, not a 500
            routing = {"error": f"{type(exc).__name__}: {exc}"}

    billing.client = billing_client()
    try:
        billing_state = billing.status()
    except stripe_billing.StripeMisconfigured as exc:
        # A B2B key in the funnel slot is a finding to display, not a page that fails: the
        # operator looking at this screen is exactly the person who needs to be told which key
        # is wrong, and a 500 shows them a traceback instead.
        billing_state = {"charges_enabled": settings.charges_enabled, "client_active": False,
                         "rehearsal_reason": str(exc), "key_present": True, "key_type": "?",
                         "acct_type": "?", "webhook_secret_present": False, "counts": {},
                         "offer_counts": {}, "mail_transport": mailer.transport,
                         "discount_coupon_configured": bool(settings.discount_coupon_id),
                         "offer_options": (), "error": str(exc)}
    return templates.TemplateResponse(
        request, "health.html",
        page_context(
            request, copy_obj=copy_obj,
            violations=guardrail_scan(copy_obj),
            billing=billing_state,
            billing_issues=billing_readiness(billing_state, copy_obj),
            # The health page names the banned categories in its own explanation, which is
            # exactly what the scan forbids. So the sentence is assembled from the term list
            # at runtime: one source of truth, and the page proves it is reading that source.
            scan_terms_preview=", ".join(sorted({
                term.strip() for term in claims.FORBIDDEN_TERMS if " " not in term
            })[:6]),
            coverage=disclosure_coverage(copy_obj),
            artefact=selfcheck_render(),
            meal_audit=plan_audit(),
            router_available=router is not None,
            routing=routing,
            counts=store.counts(),
            db_path=str(settings.database_abspath),
            secret_ephemeral=codec.ephemeral,
            today=date.today().strftime("%d %B %Y"),
        ),
    )


@app.get("/dev/responses", response_class=HTMLResponse)
async def dev_responses(request: Request) -> HTMLResponse:
    """Local inspection of what the funnel captured. Not linked from any funnel page."""
    return templates.TemplateResponse(
        request, "dev_responses.html",
        page_context(request, counts=store.counts(), rows=store.recent(40),
                     db_path=str(settings.database_abspath)),
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
