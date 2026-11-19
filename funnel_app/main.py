"""Lane C Rank-4 funnel: FastAPI app, server-rendered, Tailwind CDN, no build step.

Run it:

    cd "funnel_app" && python -m uvicorn main:app --port 8090

Deliberately standalone: this module imports nothing from `backend/engine`, and the LLM
layer loads `backend/llm_router.py` by file path so the funnel survives the engine being
stopped. Port 8090 by default, clear of the engine (8099) and the B2B apps.

Phase 1 scope: `/` landing, `/onboarding` quiz, `/summary` checkpoint, `/preview`
artefact, plus a prototype checkout and static policy pages. No charge is taken anywhere:
`FUNNEL_CHARGES_ENABLED` defaults false and the card screen says so in its own copy.

Route shape note: the quiz is one route per question rather than one big form, because
sunk effort has to be *experienced* as a sequence of commitments. Every submit is a
POST-redirect-GET, so a refresh re-posts cleanly instead of resubmitting, and every
accepted answer writes a row, which is what makes per-step drop-off measurable later
without a second analytics table.
"""

from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import claims
from config import FunnelSettings
from funnel_copy import (ARTIFACT_FREE, ARTIFACT_PAID, TRUST_POINTS, Copy, build_copy,
                        disclosure_coverage, guardrail_scan)
from llm_flavour import builder_coach_note, coach_note, load_router
from plan_builder import WEEK_ARCS, Plan, build_plan
from plan_builder import _audit_meal_tables as plan_audit_problems
from profile import is_complete, missing_indices, profile_from_answers
from quiz import (NOTE_SUFFIX, OPTION_LABELS, QUESTIONS, SECTION_KEYS, SECTION_TITLES,
                  next_unanswered, parse_answer, progress_pct)
from session import COOKIE_NAME, SessionCodec
from storage import OFFER_ID, ResponseStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("funnel")

BASE_DIR = Path(__file__).resolve().parent

settings = FunnelSettings.from_env()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
store = ResponseStore(settings.database_abspath)
codec = SessionCodec(settings.session_secret)

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
    """Everything a quiz step needs.

    One function because the GET path and the invalid-submit re-render must present
    identical chrome: two hand-built contexts is how a progress bar starts lying on the
    error page.
    """
    return {
        "question": question,
        "sections": [(key, SECTION_TITLES[key]) for key in SECTION_KEYS],
        "section_title": SECTION_TITLES[question.section],
        "section_index": SECTION_KEYS.index(question.section),
        "section_count": len(SECTION_KEYS),
        "step": index,
        "step_number": index + 1,
        "total": len(QUESTIONS),
        "progress": progress_pct(stored),
        "answered": answered,
        "saved_note": note,
        "errors": errors,
    }


def answers_for(request: Request) -> dict[str, Any]:
    """Stored answers for this visitor, with notes folded back into the answer map."""
    return store.latest_answers(get_visitor(request)) or {}


def form_answers(stored: dict[str, Any]) -> dict[str, Any]:
    """Strip internal keys before persisting a snapshot."""
    return {k: v for k, v in stored.items() if not k.startswith("_")}


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
            question_count=len(QUESTIONS),
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
    if not 0 <= index < len(QUESTIONS):
        return RedirectResponse(url="/onboarding", status_code=302)

    stored = answers_for(request)
    reached = next_unanswered(stored)
    if index > reached:
        # Forward jumps are not free. Reading question 8 before answering question 7 is
        # possible by typing a URL, and it costs the funnel the one property the whole
        # design is built on: the sense that each answer is paid for before the next ask.
        return RedirectResponse(url=f"/onboarding/{reached}", status_code=302)

    return await render_step(request, QUESTIONS[index], index, stored, errors=[])


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
    if not 0 <= index < len(QUESTIONS):
        return RedirectResponse(url="/onboarding", status_code=302)

    question = QUESTIONS[index]
    stored = store.latest_answers(visitor_key) or {}
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
        url="/summary" if is_complete(stored) else f"/onboarding/{next_unanswered(stored)}",
        status_code=302,
    )


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
    # Answers store option values; the checkpoint shows the labels the user tapped. A
    # stored value that is no longer a valid option shows raw rather than vanishing, so a
    # resume after a copy edit cannot silently drop an answer.
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
            for position, question in enumerate(QUESTIONS)
            if question.section == section_key
        ]
        rows.append({"key": section_key, "title": SECTION_TITLES[section_key], "entries": entries})

    return templates.TemplateResponse(
        request, "summary.html",
        page_context(request, rows=rows, profile=profile, progress=100,
                     total=len(QUESTIONS), plan=build_plan(profile)),
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
            total=len(QUESTIONS), locked_weeks=WEEK_ARCS[1:],
            unlock_items=ARTIFACT_PAID, free_items=ARTIFACT_FREE,
            llm_live=(coach_source == "llm"),
        ),
    )


# ---------------------------------------------------------------------------
# Checkout prototype
# ---------------------------------------------------------------------------

@app.get("/checkout", response_class=HTMLResponse)
async def checkout(request: Request) -> HTMLResponse:
    stored = answers_for(request)
    if not is_complete(stored):
        return RedirectResponse(url="/onboarding/0", status_code=302)
    # Recomputed, not stored: the signature is a hash of the answers, so recalculating it
    # here proves the checkout screen shows the same plan the preview did.
    plan_signature = profile_from_answers(stored).signature()

    copy_obj = build_copy(settings)
    store.append_event(
        visitor_key=get_visitor(request),
        status="checkout_viewed",
        answers=form_answers(stored),
        step=len(QUESTIONS),
        offer_id=OFFER_ID,
        user_agent=request.headers.get("user-agent", ""),
    )
    return templates.TemplateResponse(
        request, "checkout.html",
        page_context(request, copy_obj=copy_obj, offer_id=OFFER_ID,
                     plan_signature=plan_signature, trust_points=TRUST_POINTS,
                     unlock_items=ARTIFACT_PAID),
    )


@app.post("/checkout", response_class=HTMLResponse)
async def checkout_rehearsal(request: Request) -> HTMLResponse:
    """Record intent. Nothing is charged, and the page says so on its face.

    Phase 1's job is to find out whether the artefact earns a card detail, not to move
    money. When charges turn on, this handler becomes the processor call, and the row it
    writes today is the shape `trial_events` already expects (`started` -> `paid`).
    """
    stored = answers_for(request)
    if not is_complete(stored):
        return RedirectResponse(url="/onboarding/0", status_code=302)

    store.append_event(
        visitor_key=get_visitor(request),
        status="started",
        answers=form_answers(stored),
        step=len(QUESTIONS) + 1,
        plan_source="checkout_intent",
        offer_id=OFFER_ID,
        user_agent=request.headers.get("user-agent", ""),
    )
    return templates.TemplateResponse(
        request, "rehearsal_done.html",
        page_context(request, copy_obj=build_copy(settings)),
    )


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

#: A complete answer set that is deliberately awkward: three days, short sleep, a plant
#: diet with two overlapping exclusions. Everything the artefact has to get right at once.
CHECK_ANSWERS: dict[str, list[str]] = {
    "goal": ["lean"], "experience": ["comeback"], "days": ["3"], "minutes": ["30"],
    "timeOfDay": ["evening"], "weekendShape": ["social"], "diet": ["plant"],
    "restrictions": ["dairy", "nuts"], "equipment": ["dumbbells", "bodyweight"],
    "cookstyle": ["minimal"], "sleep": ["under6"], "blocker": ["time"],
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
                profile=profile_from_answers(CHECK_ANSWERS), total=len(QUESTIONS),
                locked_weeks=WEEK_ARCS[1:], unlock_items=ARTIFACT_PAID,
                free_items=ARTIFACT_FREE, llm_live=False,
            ))
        # Day names are rendered as three-letter chips (`day.day[:3]`), so the needles match
        # the abbreviation rather than the full word the way a template-editing human would
        # expect. Asserting against the whole word here would have reported a passing
        # artefact as broken, which is the opposite failure this function exists to catch.
        needles = [
            ("Mon", "the first day of week 1"), ("Sun", "the last day of week 1"),
            ("Meal structure", "the meal heading"), ("Breakfast", "a meal slot name"),
            ("Tofu scramble", "a plant breakfast, i.e. the plan honoured the diet answer"),
            ("Metric to log", "the per-day metric line"),
            ("Sessions completed", "a metrics-table row"),
            ("Lower-body", "a session title, i.e. equipment reached the plan"),
            (plan.profile_signature, "the plan reference"),
            # A rendered price, not a settings field: a disclosure that stopped rendering
            # is a compliance problem hiding behind a passing artefact check.
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
    """Meal-template integrity: does any 'free-from' variant name the food it excludes."""
    try:
        return plan_audit_problems()
    except Exception as exc:
        return [f"audit raised {type(exc).__name__}: {exc}"]


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
    return templates.TemplateResponse(
        request, "health.html",
        page_context(
            request, copy_obj=copy_obj,
            violations=guardrail_scan(copy_obj),
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
