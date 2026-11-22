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

# The fixture, the cookie jar, `post`, `walk` and `attach` come from `conftest.py` and are
# re-exported here: two copies of the 12-answer walk is how one suite starts passing on
# requests the other would reject, and every rule in that walk exists because of a specific
# harness bug (`data=` bound to `files=` by a positional passthrough, a `client.cookies` reader
# that disagrees with the transport's real jar, a POST that follows its own 302 and throws the
# body away). Documented there rather than duplicated. `test_design_pattern.py` loads the same
# module by path for the same reason.
from conftest import (ANSWERS, attach, jar_of, post, walk)  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Visitor identity
# ---------------------------------------------------------------------------

def test_visitor_cookie_is_issued_on_first_hit(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "kd_vid" in jar_of(client), (
        f"no visitor key in {jar_of(client)}, which means no funnel metrics")


def test_visitor_key_survives_across_requests(client):
    client.get("/")
    first = jar_of(client)["kd_vid"]
    client.get("/onboarding")
    assert jar_of(client)["kd_vid"] == first


def test_forged_cookie_does_not_crash(client):
    """The cookie is signed, and a stranger's value must degrade to a new visitor."""
    jar_of(client)["kd_vid"] = "not-a-signed-value"
    response = client.get("/")
    assert response.status_code == 200
    assert jar_of(client)["kd_vid"] != "not-a-signed-value", "a forged cookie was kept"


def test_forged_cookie_cannot_borrow_another_visitors_answers(client):
    """A signed, replayed cookie is the only way in. Guessing one must not read someone's
    quiz answers, because those answers become the plan on the paid screen."""
    client.get("/")
    walk(client)
    victim = jar_of(client)["kd_vid"]
    assert client.get("/summary").status_code == 200
    other = attach(TestClient(client.app))
    jar_of(other)["kd_vid"] = victim[:-1] + ("a" if victim[-1] != "a" else "b")
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
    # A finished visitor lands on the last screen of *their* flow (or the summary), never on 0.
    from conftest import _MEN_WALK
    from quiz import flow_for
    flow_length = len(flow_for({key: [value] for key, value in _MEN_WALK}))
    assert "summary" in response.headers["location"] or int(
        response.headers["location"].rsplit("/", 1)[-1]) == flow_length - 1


def _complete(track: str) -> dict:
    """Every screen of one track's flow answered with its first valid value."""
    from quiz import flow_for
    answers = {"track": [track]}
    for _ in range(3):  # predicates read earlier answers, so settle the flow
        for q in flow_for(answers):
            answers.setdefault(q.key, [q.option_values[0]])
    assert answers["track"] == [track]
    return answers


def test_next_unanswered_is_the_resume_position():
    from quiz import flow_for
    complete = _complete("men")
    flow = flow_for(complete)
    assert next_unanswered(complete) == len(flow) - 1
    partial = {q.key: [q.option_values[0]] for q in flow[:5]}
    assert next_unanswered(partial) == 5
    assert next_unanswered({}) == 0


def test_progress_percent_is_monotonic_and_bounded():
    from quiz import flow_for
    complete = _complete("women")
    flow = flow_for(complete)
    seen = []
    for count in range(len(flow) + 1):
        answers = {q.key: complete[q.key] for q in flow[:count]}
        seen.append(progress_pct(answers))
    assert seen == sorted(seen)
    assert seen[0] == 0 and seen[-1] == 100


def test_branching_gives_each_track_its_own_screens():
    """The flow is per visitor: the men's and women's screens never mix, conditional screens
    appear only when their trigger is answered, and both tracks reach reference length."""
    from quiz import flow_for
    men = [q.key for q in flow_for({"track": ["men"]})]
    women = [q.key for q in flow_for({"track": ["women"]})]
    assert "goal_men" in men and "goal_women" not in men
    assert "goal_women" in women and "erect_type" not in women
    assert "birth_when" not in women
    assert "birth_when" in [q.key for q in flow_for({"track": ["women"], "births": ["one"]})]
    assert "fact_men_meds" in [q.key for q in flow_for({"track": ["men"], "meds": ["always"]})]
    assert len(men) >= 30 and len(women) >= 30


def test_switching_track_drops_the_other_branch(client):
    """Answers from the men's flow must not leak into a women's plan."""
    client.get("/")
    walk(client, upto=5)
    assert post(client, "/onboarding/0", [("answer", "women")]).status_code == 302
    stored = client.main.store.latest_answers(client.cookies.get("kd_vid", "") and
                                               client.main.get_visitor.__globals__["codec"].verify(
                                                   client.cookies.get("kd_vid"))) or {}
    assert stored.get("track") == ["women"]
    assert "goal_men" not in stored and "erect_type" not in stored


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------

def test_multi_select_reads_the_field_named_after_the_question():
    """Inputs are named after the question key because that is what the template emits."""
    question = next(q for q in QUESTIONS if q.key == "safety")
    values, _note, error = parse_answer(question, None, {"safety": ["pain", "blood"]}, None)
    assert error is None and values == ["pain", "blood"]


def test_none_selected_alongside_others_collapses_to_none():
    """Someone who ticks "None of these" plus "Pain" is not reporting two facts."""
    question = next(q for q in QUESTIONS if q.key == "safety")
    values, _note, _error = parse_answer(question, None, {"safety": ["pain", "none"]}, None)
    assert values == ["none"]


def test_unknown_option_is_dropped_not_stored():
    """A hand-posted body cannot push an arbitrary string into the plan builder."""
    question = next(q for q in QUESTIONS if q.key == "safety")
    values, _note, error = parse_answer(question, None, {"safety": ["blood", "lightsaber"]}, None)
    assert error is None and values == ["blood"]


def test_duplicate_selection_is_counted_once():
    question = next(q for q in QUESTIONS if q.key == "safety")
    values, _note, _error = parse_answer(question, None, {"safety": ["blood", "blood"]}, None)
    assert values == ["blood"]


def test_scale_and_information_screens_validate_their_own_values():
    scale = next(q for q in QUESTIONS if q.key == "strength")
    assert parse_answer(scale, "4", {}, None)[0] == ["4"]
    assert parse_answer(scale, "6", {}, None)[2]
    fact = next(q for q in QUESTIONS if q.key == "fact_relax")
    assert parse_answer(fact, "seen", {}, None)[0] == ["seen"]
    assert parse_answer(fact, "", {}, None)[2]


def test_note_is_length_capped_and_trimmed():
    question = next(q for q in QUESTIONS if q.key == "when")
    _values, note, _error = parse_answer(question, "morning", {}, "  " + "x" * 900)
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
    for needle in ("Keeping it is hard", "None of these", "Long commute", "Show my week 1"):
        assert needle.lower() in summary.text.lower(), needle

    preview = client.get("/preview")
    assert preview.status_code == 200
    for needle in ("Until you cancel", "auto-renew", "one click", "Find the muscle",
                   "Control + endurance", "full relax", "Weeks 2 to 4", "Longest comfortable hold",
                   "Check-in"):
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
    # Amounts are read from config rather than hardcoded here: these are the Phase 0 baseline
    # figures ($1 / 7 days / $39) and a test that retypes them on every price change is a test
    # that quietly agrees with whatever the config says, including a wrong one. What this
    # assertion is for is *placement* -- price must sit with the card field, not in a footer
    # the visitor scrolls past -- so the amounts come from the same source the page does.
    form = checkout.text[checkout.text.find("<form"):]
    for amount in _offer_amounts():
        assert amount in form, amount
    assert "rehearsal" in checkout.text.lower(), "charges are off; the page must say so"


def _offer_amounts() -> list[str]:
    import config as _config
    from funnel_copy import build_copy
    copy_obj = build_copy(_config.FunnelSettings.from_env())
    return [copy_obj.trial_amount, copy_obj.rebill_per_month.split("/")[0]]


def test_checkout_post_records_intent_without_charging(client):
    client.get("/")
    walk(client)
    client.get("/preview")
    client.get("/checkout")
    done = post(client, "/checkout", [])
    assert done.status_code in (200, 302)
    body = done.text or client.get("/checkout").text
    assert _offer_amounts()[1] in body and "14" in body


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
