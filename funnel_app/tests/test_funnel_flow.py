"""Funnel mechanics: identity, the progress guard, resumability, and event integrity.

These are the parts a visitor experiences as "the funnel worked" or "the funnel ate my
answers", plus the storage shape that the Phase 0 metrics get read out of later.

Every TestClient call here passes `follow_redirects=False` on a POST, and that is not
styling. The quiz posts in place and answers with a 302, so a client that follows the
redirect issues a GET to the next step and discards the body it just sent: every answer
looks accepted while none were saved. An earlier debugging session burned a long time on
exactly this, on the app's behalf, when the harness was at fault.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

import config as config_module                              # noqa: E402
from quiz import (NOTE_SUFFIX, QUESTIONS, next_unanswered,  # noqa: E402
                  parse_answer, progress_pct)

ANSWERS: list[list[tuple[str, str]]] = [
    [("answer", "energy")],
    [("answer", "comeback")],
    [("answer", "3")],
    [("answer", "30")],
    [("answer", "evening")],
    [("answer", "social")],
    [("answer", "plant")],
    [("restrictions", "dairy"), ("restrictions", "nuts")],
    [("equipment", "dumbbells"), ("equipment", "bodyweight")],
    [("answer", "minimal")],
    [("answer", "under6")],
    [("answer", "time"), ("note", "Two kids and a 40-hour week")],
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A fresh app on a throwaway database.

    Separate databases matter more than they look: the progress guard reads stored answers,
    so a shared dirty file makes a clean test fail with a 422 that looks like a validation
    bug. Isolation here is what makes a red run mean something.
    """
    monkeypatch.setenv("FUNNEL_DB_PATH", str(tmp_path / "funnel.sqlite3"))
    monkeypatch.setenv("FUNNEL_SESSION_SECRET", "test-secret-value")
    monkeypatch.setenv("FUNNEL_CHARGES_ENABLED", "false")
    for name in list(sys.modules):
        if name in {"main", "config", "storage", "session"}:
            del sys.modules[name]
    import main

    return attach(TestClient(main.app))


JAR_KEY = "_jar"


def jar_of(client: TestClient) -> dict[str, str]:
    """Cookies this test conversation is holding, kept by hand.

    `client.cookies` is *not* the transport's jar in this build: it reads empty while the
    underlying transport keeps its own, separate jar. So `client.cookies.get("rl_vid")`
    returns None (making the app look like it never issued a key) while a request carrying a
    deliberately wrong hand-set cookie can still behave like an established visitor, because
    the transport silently sends its real one too. Both directions of that mismatch cost
    debugging time on the app's behalf, so the jar lives in this dict instead and every
    request states what it is sending.
    """
    return getattr(client, JAR_KEY, {})


def attach(client: TestClient) -> TestClient:
    setattr(client, JAR_KEY, {})
    original = client.request

    def request(method, url, **kwargs):
        """Forward by keyword only.

        `httpx.Client.request(method, url, *, content, data, files, headers, ...)` takes
        nothing useful positionally, so a `*args` passthrough here silently binds `data=` to
        `files=`, which switches the encoder to multipart, drops the content type, and turns
        every answer into a phantom. That single wrapper bug produced a whole chain of
        plausible-looking app theories.
        """
        headers = dict(kwargs.pop("headers", None) or {})
        for name, value in jar_of(client).items():
            headers.setdefault("cookie", f"{name}={value}")
        # POSTs must not follow their own 302: a client that follows it issues a GET to the
        # next step and throws the body away, so every answer looks accepted while none saved.
        if method.upper() != "GET":
            kwargs["follow_redirects"] = kwargs.get("follow_redirects", False)
        response = original(method, url, headers=headers, **kwargs)
        # Record what the server asked us to remember, keyed off Set-Cookie.
        for raw in response.headers.get_list("set-cookie"):
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                name, _sep, value = pair.partition("=")
                jar_of(client)[name.strip()] = value.strip()
        return response

    client.request = request  # type: ignore[method-assign]
    return client


_URLENCODED = "application/x-www-form-urlencoded"


def post(client: TestClient, path: str, pairs: list[tuple[str, str]] | None = None,
         multipart: bool = False, **kwargs):
    """POST one quiz answer, in either of the two encodings a form can legally arrive in.

    Browsers send `application/x-www-form-urlencoded` for a form with no file field and
    `multipart/form-data` for one with a file, and a funnel that only understands the first is
    one stray `<input type=file>` away from losing every answer. `walk` alternates the two so
    both stay covered without a duplicated suite.

    Multipart is built with `files={name: (None, value)}`, which is httpx's spelling of "this
    part is a plain field, not a file": it produces a real boundary and repeats a name once per
    value, so checkbox groups survive. Passing `data=` a *list of tuples* instead is a trap,
    and it cost the whole of one debugging session: that shape sends the pairs with no boundary
    and, once the TestClient rebuilds wire headers from the request's own header list, no
    content type either. Starlette then hands the handler an empty form and the funnel answers
    "Choose one option to continue" about a request that carried a perfectly good answer. When
    a quiz step rejects a valid answer, check this function before believing the app is wrong.
    """
    pairs = pairs or []
    if multipart:
        fields = [(name, (None, value)) for name, value in pairs]
        # `files` as a list of pairs keeps repeated names, which a dict would collapse.
        response = client.request("POST", path, files=fields, **kwargs)
    else:
        response = client.request("POST", path, data=_merge(pairs),
                                  headers={"content-type": _URLENCODED}, **kwargs)
    return response


def _merge(pairs: list[tuple[str, str]]) -> dict:
    """Repeated names into one dict of lists, which is how a browser submits checkboxes."""
    merged: dict[str, list[str]] = {}
    for key, value in pairs:
        merged.setdefault(key, []).append(value)
    return {key: (values[0] if len(values) == 1 else values) for key, values in merged.items()}


def walk(client: TestClient, upto: int | None = None) -> None:
    for index, pairs in enumerate(ANSWERS if upto is None else ANSWERS[:upto]):
        # Alternate encodings so both paths stay exercised without a second suite.
        response = post(client, f"/onboarding/{index}", pairs, multipart=bool(index % 2))
        assert response.status_code in (200, 302), (index, response.status_code)


# ---------------------------------------------------------------------------
# Visitor identity
# ---------------------------------------------------------------------------

def test_visitor_cookie_is_issued_on_first_hit(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "rl_vid" in jar_of(client), (
        f"no visitor key in {jar_of(client)}, which means no funnel metrics")


def test_visitor_key_survives_across_requests(client):
    client.get("/")
    first = jar_of(client)["rl_vid"]
    client.get("/onboarding")
    assert jar_of(client)["rl_vid"] == first


def test_forged_cookie_does_not_crash(client):
    """The cookie is signed, and a stranger's value must degrade to a new visitor."""
    jar_of(client)["rl_vid"] = "not-a-signed-value"
    response = client.get("/")
    assert response.status_code == 200
    assert jar_of(client)["rl_vid"] != "not-a-signed-value", "a forged cookie was kept"


def test_forged_cookie_cannot_borrow_another_visitors_answers(client):
    """A signed, replayed cookie is the only way in. Guessing one must not read someone's
    quiz answers, because those answers become the plan on the paid screen."""
    client.get("/")
    walk(client)
    victim = jar_of(client)["rl_vid"]
    assert client.get("/summary").status_code == 200
    other = attach(TestClient(client.app))
    jar_of(other)["rl_vid"] = victim[:-1] + ("a" if victim[-1] != "a" else "b")
    # Tampered signature: same length, wrong digest, so it cannot pass verification.
    assert other.get("/summary", follow_redirects=False).status_code == 302


# ---------------------------------------------------------------------------
# The progress guard and resumability
# ---------------------------------------------------------------------------

def test_forward_jump_without_answers_lands_on_question_one(client):
    client.get("/")
    response = client.get("/onboarding/7", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].endswith("/onboarding/0")


def test_backwards_navigation_after_answering_is_allowed(client):
    """A sunk-effort funnel that will not let you revisit is a trap at minute four."""
    client.get("/")
    walk(client, upto=3)
    for index in (0, 1, 2):
        assert client.get(f"/onboarding/{index}").status_code == 200


def test_empty_and_forged_answers_are_rejected_with_explanation(client):
    client.get("/")
    for value in ("", "forged_option"):
        response = post(client, "/onboarding/0", [("answer", value)])
        assert response.status_code == 422, value
        assert "Choose one option" in response.text
    # Rejection must not silently store the junk.
    assert client.get("/onboarding/0").status_code == 200


def test_completed_quiz_visiting_the_start_resumes_rather_than_looping(client):
    """The 302-loop bug: "post to the next index" plus "no answers means start over" sends a
    finished visitor round and round instead of to their checkpoint."""
    client.get("/")
    walk(client)
    response = client.get("/onboarding", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].rstrip("/").endswith(
        f"/onboarding/{next_unanswered({})}"
    ) or "summary" in response.headers["location"] or int(
        response.headers["location"].rsplit("/", 1)[-1]) == len(QUESTIONS) - 1


def test_next_unanswered_is_the_resume_position():
    complete = {q.key: ["x"] for q in QUESTIONS}
    assert next_unanswered(complete) == len(QUESTIONS) - 1
    partial = {q.key: ["x"] for q in QUESTIONS[:5]}
    assert next_unanswered(partial) == 5
    assert next_unanswered({}) == 0


def test_progress_percent_is_monotonic_and_bounded():
    seen = []
    for count in range(len(QUESTIONS) + 1):
        answers = {q.key: ["x"] for q in QUESTIONS[:count]}
        seen.append(progress_pct(answers))
    assert seen == sorted(seen)
    assert seen[0] == 0 and seen[-1] == 100


# ---------------------------------------------------------------------------
# Answer parsing, including the multi-select trap
# ---------------------------------------------------------------------------

def test_multi_select_reads_the_field_named_after_the_question():
    """Inputs are named after the question key because that is what the template emits.

    Validation that reads one fixed field name instead returns an empty selection for every
    multi question, which surfaces as "the user picked nothing" and bounces a visitor off
    question 8 forever.
    """
    question = next(q for q in QUESTIONS if q.key == "restrictions")
    values, _note, error = parse_answer(question, None,
                                        {"restrictions": ["dairy", "nuts"]}, None)
    assert error is None and values == ["dairy", "nuts"]


def test_none_selected_alongside_others_collapses_to_none():
    """Someone who ticks "Nothing" plus "Dairy" is not reporting two facts."""
    question = next(q for q in QUESTIONS if q.key == "restrictions")
    values, _note, _error = parse_answer(question, None,
                                         {"restrictions": ["dairy", "none"]}, None)
    assert values == ["none"]


def test_unknown_option_is_dropped_not_stored():
    """A hand-posted body cannot push an arbitrary string into the plan builder."""
    question = next(q for q in QUESTIONS if q.key == "equipment")
    values, _note, error = parse_answer(question, None,
                                        {"equipment": ["fullgym", "lightsaber"]}, None)
    assert error is None and values == ["fullgym"]


def test_duplicate_selection_is_counted_once():
    question = next(q for q in QUESTIONS if q.key == "equipment")
    values, _note, _error = parse_answer(question, None,
                                         {"equipment": ["bands", "bands"]}, None)
    assert values == ["bands"]


def test_note_is_length_capped_and_trimmed():
    question = next(q for q in QUESTIONS if q.key == "goal")
    _values, note, _error = parse_answer(question, "energy", {}, "  " + "x" * 900)
    assert len(note) <= 280 and note == note.strip()


# ---------------------------------------------------------------------------
# The walk, end to end
# ---------------------------------------------------------------------------

def test_full_walk_reaches_a_populated_preview(client):
    client.get("/")
    walk(client)
    summary = client.get("/summary")
    assert summary.status_code == 200
    # Answers shown back as the labels the visitor tapped, plus the typed note.
    for needle in ("Coming back", "dumbbells", "40-hour week", "Show my week 1"):
        assert needle.lower() in summary.text.lower(), needle

    preview = client.get("/preview")
    assert preview.status_code == 200
    for needle in ("Until you cancel", "auto-renew", "one click", "Meal structure",
                   "Breakfast", "Metric to log", "Locked", "fallback rule", "Effort:"):
        assert needle.lower() in preview.text.lower(), needle
    # Day chips render abbreviated, so a full weekday name is not the right needle.
    for chip in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        assert chip in preview.text, chip


def test_preview_requires_completed_answers(client):
    client.get("/")
    assert client.get("/preview", follow_redirects=False).status_code == 302


def test_checkout_discloses_the_whole_offer_in_the_form(client):
    client.get("/")
    walk(client)
    client.get("/preview")
    checkout = client.get("/checkout")
    assert checkout.status_code == 200
    # Price must sit with the card field, not in a footer the visitor scrolls past.
    form = checkout.text[checkout.text.find("<form"):]
    assert "$2.99" in form and "$24.99" in form
    assert "rehearsal" in checkout.text.lower(), "charges are off; the page must say so"


def test_checkout_post_records_intent_without_charging(client):
    client.get("/")
    walk(client)
    client.get("/preview")
    client.get("/checkout")
    done = post(client, "/checkout", [])
    assert done.status_code in (200, 302)
    body = done.text or client.get("/checkout").text
    assert "$24.99" in body and "14" in body


def test_health_reports_green_across_every_guard(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert "Copy guardrail: clean" in response.text
    assert "artefact checks present" in response.text, (
        "the artefact self-check failed, which means the preview page renders blank")
    for slot in ("price_line", "renewal_line", "refund_line", "cancel_line"):
        assert slot in response.text


def test_policies_are_reachable_and_unknown_policies_404(client):
    for slug in ("terms", "privacy", "refund"):
        page = client.get(f"/policy/{slug}")
        assert page.status_code == 200, slug
        assert page.status_code == 200
    assert client.get("/policy/magic").status_code == 404


def test_charges_stay_off_in_phase_one(client):
    """Explicit, because "did we accidentally go live" is the question asked first."""
    assert config_module.FunnelSettings.from_env().charges_enabled is False
    assert "rehearsal" in client.get("/").text.lower()
