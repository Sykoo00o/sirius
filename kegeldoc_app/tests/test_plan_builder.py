"""The kegel plan builder: determinism, the safety branches, levels and drills.

The routine funnel's builder suite was mostly about meals and food exclusions. This one guards
the decisions that matter for a pelvic floor plan, in order of how much harm getting them wrong
would do: pain and recent births must start gentle and say "see a clinician first", the level
must follow the tests the visitor actually did, and the same answers must always give the same
plan (the signature in the footer is a receipt).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

import claims                                                    # noqa: E402
import plan_builder as PB                                        # noqa: E402
from plan_builder import Profile, build_plan                     # noqa: E402
from profile import profile_from_answers                         # noqa: E402


def test_same_answers_give_identical_plans():
    profile = Profile(track="women", goal="leaks", minutes=10)
    assert build_plan(profile) == build_plan(profile)


def test_signature_is_stable_but_answers_sensitive():
    base = Profile(track="men", goal="erection")
    assert base.signature() == Profile(track="men", goal="erection").signature()
    assert base.signature() != Profile(track="men", goal="ejac").signature()


def test_week_is_seven_days_monday_to_sunday_with_a_relax_day():
    plan = build_plan(Profile())
    assert [d.short for d in plan.days] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert [d.day for d in plan.days if d.is_rest] == ["Thursday"]


@pytest.mark.parametrize("minutes,rounds", [(3, 1), (5, 1), (10, 2)])
def test_daily_minutes_set_the_rounds(minutes: int, rounds: int):
    plan = build_plan(Profile(minutes=minutes))
    assert plan.rounds == rounds and plan.minutes == minutes
    assert f"{rounds} round" in plan.days[1].steps[1]


# ---------------------------------------------------------------------------
# Safety branches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [
    Profile(safety=frozenset({"pain"})),
    Profile(track="women", sexpain="often"),
    Profile(track="women", sexpain="most"),
], ids=["pelvic-pain", "painful-sex-often", "painful-sex-most"])
def test_pain_gives_a_relax_first_week_capped_at_five_minutes(profile: Profile):
    plan = build_plan(Profile(**{**profile.__dict__, "minutes": 10}))
    assert plan.level_name == "Relax first" and plan.minutes == 5
    assert plan.drill_name == PB.PAIN_DRILL[0]
    for day in plan.days:
        assert day.minutes <= 5, day
        if day.day not in ("Thursday", "Sunday"):
            assert day.title == "Relax-first session"
    assert plan.safety_note


@pytest.mark.parametrize("profile", [
    Profile(track="women", births="one", birth_when="under6w"),
    Profile(track="women", births="two", birth_when="under1y"),
    Profile(track="men", prostate="recent"),
    Profile(safety=frozenset({"surgery"})),
], ids=["birth-6w", "birth-1y", "prostate-recent", "pelvic-surgery"])
def test_recent_birth_surgery_or_treatment_forces_the_foundation_level(profile: Profile):
    strong_inputs = dict(tried="regular", feel="yes", strength=5, hold="10")
    plan = build_plan(Profile(**{**profile.__dict__, **strong_inputs}))
    assert plan.level_name == "Foundation"


@pytest.mark.parametrize("profile,needle", [
    (Profile(safety=frozenset({"blood"})), "Blood in urine"),
    (Profile(safety=frozenset({"burning"})), "Burning"),
    (Profile(track="women", births="pregnant"), "midwife"),
    (Profile(track="women", births="one", birth_when="under6w"), "physiotherapist"),
    (Profile(track="women", heaviness="often"), "heaviness"),
    (Profile(track="men", prostate="recent"), "physiotherapist"),
])
def test_answers_that_warrant_a_clinician_produce_a_safety_note(profile: Profile, needle: str):
    note = build_plan(profile).safety_note
    assert note and needle.lower() in note.lower(), note


def test_no_safety_note_when_nothing_warrants_one():
    assert build_plan(Profile(safety=frozenset({"none"}))).safety_note == ""


def test_a_stale_birth_answer_cannot_make_the_plan_gentle():
    """`birth_when` stays stored if the visitor goes back and changes `births` to No."""
    answers = {"track": ["women"], "births": ["none"], "birth_when": ["under6w"],
               "tried": ["regular"], "feel": ["yes"], "strength": ["5"], "hold": ["10"]}
    profile = profile_from_answers(answers)
    assert profile.birth_when == ""
    assert build_plan(profile).level_name == "Strong"


# ---------------------------------------------------------------------------
# Level and drill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("inputs,level", [
    (dict(tried="never", feel="no", strength=1, hold="0"), "Foundation"),
    (dict(tried="sometimes", feel="some", strength=3, hold="2"), "Builder"),
    (dict(tried="regular", feel="yes", strength=4, hold="9"), "Strong"),
])
def test_level_follows_the_tests_the_visitor_did(inputs: dict, level: str):
    assert build_plan(Profile(**inputs)).level_name == level


@pytest.mark.parametrize("track,goal", [("men", g) for g in ("erection", "ejac", "both", "bladder", "general")]
                         + [("women", g) for g in ("leaks", "birth", "intimacy", "menopause", "general")])
def test_every_goal_reaches_its_own_drill(track: str, goal: str):
    plan = build_plan(Profile(track=track, goal=goal))
    assert (plan.drill_name, plan.drill_text) == PB.DRILLS[goal]
    assert plan.focus == PB.GOAL_NAMES[goal]


def test_frequent_urgency_swaps_in_the_urge_drill_except_for_sexual_goals():
    assert build_plan(Profile(track="women", goal="leaks", urge="often")).drill_name == PB.URGE_DRILL[0]
    assert build_plan(Profile(track="men", goal="bladder", urge_m="most")).drill_name == PB.URGE_DRILL[0]
    assert build_plan(Profile(track="men", goal="erection", urge_m="most")).drill_name == "Endurance set"


def test_every_hold_rests_at_least_as_long_as_it_squeezes():
    for level in (*PB.LEVELS, PB.RELAX_FIRST):
        assert level["rest"] >= level["hold"], level


def test_audit_reports_no_problems():
    assert PB.audit_tables() == []


# ---------------------------------------------------------------------------
# Notes and degradation
# ---------------------------------------------------------------------------

def test_lifestyle_notes_follow_the_answers():
    quiet = build_plan(Profile())
    loud = build_plan(Profile(smoke="yes", alcohol="daily", diet=1, sitting="gt8", stress=5))
    assert quiet.tips == ()
    assert len(loud.tips) == 5


def test_new_erection_changes_prompt_a_check_up_note_for_men_only():
    note = "check-up"
    men = build_plan(Profile(track="men", morning="never", erect_since="lt1"))
    women = build_plan(Profile(track="women", morning="never", erect_since="lt1"))
    assert any(note in tip for tip in men.tips)
    assert not any(note in tip for tip in women.tips)


def test_locked_weeks_describe_change_without_promising_outcomes():
    for arc in PB.WEEK_ARCS:
        assert not claims.violations(arc["arc"]), arc


def test_unknown_answers_degrade_instead_of_raising():
    profile = profile_from_answers({"track": ["robot"], "minutes": ["99"], "strength": ["11"],
                                    "safety": ["lightsaber"], "goal_men": ["flying"]})
    plan = build_plan(profile)
    assert plan.minutes == 5 and plan.level_name == "Foundation" and plan.focus == "Overall strength"
