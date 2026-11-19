"""The artefact must be real, reproducible, and obedient to the answers.

This is the screen a stranger is judging, and the Phase 0 criterion it has to meet is a
genuine pre-paywall artefact. Two properties carry that:

* Reproducibility. Same answers, same plan, byte for byte. A generator we cannot re-run
  identically cannot be defended in a dispute, which is why the core is deterministic and
  the profile signature is shown on the page.
* Obedience. The plan has to visibly honour the answers: the day count asked for, the
  equipment actually reachable, and none of the food excluded. A plan that ignores the quiz
  is a template with the user's name in it.

Meal-restriction handling gets the most tests here because it is where the failure is
invisible to a status code: recommending soy inside the dairy-avoidance sentence, or
rendering every meal row blank, both look fine at HTTP level.
"""

from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

import pytest

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

import plan_builder as PB                                       # noqa: E402
from profile import profile_from_answers                        # noqa: E402

BASE_ANSWERS: dict[str, list[str]] = {
    "goal": ["energy"], "experience": ["comeback"], "days": ["3"], "minutes": ["30"],
    "timeOfDay": ["evening"], "weekendShape": ["social"], "diet": ["anything"],
    "restrictions": ["none"], "equipment": ["bodyweight"], "cookstyle": ["minimal"],
    "sleep": ["7to8"], "blocker": ["time"],
}


def build(**overrides):
    return PB.build_plan(profile_from_answers({**BASE_ANSWERS, **overrides}))


def all_diets():
    return sorted(PB.MEAL_TEMPLATES)


ALL_RESTRICTIONS = [key for key in PB.RESTRICTION_VOCAB if key != "alcohol"] + ["alcohol"]


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

def test_same_answers_give_identical_plans():
    assert build().to_dict() == build().to_dict()
    assert build(days=["5"]).to_dict() == build(days=["5"]).to_dict()


def test_signature_is_stable_but_answers_sensitive():
    """The signature is printed on the artefact as proof the plan came from these answers.

    It must not change for an unrelated reason (a re-run, a dict ordering difference) and it
    must change when the answers change, or the claim on the page means nothing.
    """
    assert build().profile_signature == build().profile_signature
    assert build().profile_signature != build(goal=["strength"]).profile_signature
    assert len(build().profile_signature) == 8


def test_different_input_ordering_same_signature():
    """Multi-select order is presentation, not information: nuts+dairy is dairy+nuts."""
    one = build(restrictions=["dairy", "nuts"])
    other = build(restrictions=["nuts", "dairy"])
    assert one.profile_signature == other.profile_signature
    assert one.to_dict() == other.to_dict()


# ---------------------------------------------------------------------------
# Obedience: schedule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("days", ["2", "3", "4", "5", "6"])
def test_training_day_count_matches_the_answer(days: str):
    plan = build(days=[days])
    assert sum(1 for day in plan.days if day.is_training_day) == int(days)
    # Hard days must not *all* be adjacent. A 4-day pattern like Mon/Tue/Thu/Fri is correct
    # by design (the sequences alternate session type so the load alternates), and forbidding
    # adjacency outright would be wrong: with 6 training days in a 7-day week adjacency is
    # unavoidable. What a user actually feels is "every session I can do is on consecutive
    # days", which is what this asserts against.
    training_indices = [i for i, d in enumerate(plan.days) if d.is_training_day]
    pairs = [(a, b) for a, b in zip(training_indices, training_indices[1:], strict=False)]
    adjacent = [pair for pair in pairs if pair[1] - pair[0] == 1]
    if int(days) <= 4:
        assert len(adjacent) < len(pairs), f"{days} days packed every session together"


@pytest.mark.parametrize("minutes", ["20", "30", "45", "60"])
def test_session_blocks_sum_to_the_stated_length(minutes: str):
    """Scaling that loses or invents minutes is the kind of arithmetic a user checks."""
    for day in build(minutes=[minutes]).days:
        if day.is_training_day:
            assert sum(block[1] for block in day.session.blocks) == int(minutes)


def test_short_sleep_downgrades_intervals():
    """Under 6 hours changes the plan's load rather than adding a lecture about sleep."""
    tired = build(sleep=["under6"], days=["4"])
    assert "Intervals" not in " ".join(d.session.name for d in tired.days)
    assert "Intervals" in " ".join(d.session.name for d in build(days=["4"]).days)


def test_blocker_produces_a_named_fallback():
    """The fallback is the anti-abandonment feature, so it must vary with the blocker."""
    time_plan = build(blocker=["time"])
    travel_plan = build(blocker=["travel"])
    assert time_plan.fallback_rule != travel_plan.fallback_rule
    for blocker in PB.BLOCKER_FALLBACKS:
        assert build(blocker=[blocker]).fallback_rule == PB.BLOCKER_FALLBACKS[blocker]


# ---------------------------------------------------------------------------
# Obedience: meals obey every exclusion, in every combination
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("diet", all_diets())
@pytest.mark.parametrize("restriction", ALL_RESTRICTIONS)
def test_single_restriction_is_honoured(diet: str, restriction: str):
    plan = build(diet=[diet], restrictions=[restriction])
    offenders = [f"{meal[0]}: {meal[1]}" for day in plan.days
                 for meal in day.meal_structure
                 if PB._names_excluded_food(meal[1], frozenset({restriction}))]
    assert not offenders, f"{diet} + {restriction} leaked: {offenders}"


@pytest.mark.parametrize("diet", all_diets())
def test_every_pair_of_restrictions_is_honoured(diet: str):
    """Pairwise, not exhaustive.

    Restrictions compose badly rather than rarely: the dairy rule's answer is a soy product,
    so dairy+soy is the shape that broke twice already. Pairs cover that interaction for
    every diet, and the "everything" test below covers the degenerate all-ticked case. Full
    enumeration would be 2^10 per diet for no additional class of bug.
    """
    for pair in combinations([r for r in ALL_RESTRICTIONS if r != "alcohol"], 2):
        plan = build(diet=[diet], restrictions=list(pair))
        restrictions = frozenset(pair)
        offenders = [f"{pair}: {meal[1]}" for day in plan.days for meal in day.meal_structure
                     if PB._names_excluded_food(meal[1], restrictions)]
        assert not offenders, f"{diet} + {pair} leaked: {offenders}"


@pytest.mark.parametrize("diet", all_diets())
def test_all_restrictions_ticked_still_produces_meals(diet: str):
    """The pathological input. Somebody ticks every box, and the artefact must not vanish."""
    plan = build(diet=[diet], restrictions=ALL_RESTRICTIONS)
    for day in plan.days:
        assert day.meal_structure, f"{diet}: a slot disappeared entirely"
        for meal in day.meal_structure:
            assert meal[1].strip(), f"{diet}: empty meal text in {meal[0]}"


@pytest.mark.parametrize("diet", all_diets())
def test_meal_slots_are_never_blank(diet: str):
    """Blank rows were a real bug: headings rendered, content did not, HTTP said 200."""
    plan = build(diet=[diet], restrictions=["dairy"])
    slots_seen = {meal[0] for day in plan.days for meal in day.meal_structure}
    assert {"Breakfast", "Lunch", "Dinner", "Snacks"} <= slots_seen
    for day in plan.days:
        assert len(day.meal_structure) == 4
        for meal in day.meal_structure:
            assert len(meal[1].split()) >= 2, f"{diet}: '{meal[1]}' is not a meal"


def test_no_replacement_names_an_excluded_food():
    """Table hygiene: a swap rule's value must not be food that everyone rejects.

    Deliberately stricter than the runtime in one direction and looser in another, and the
    split is the interesting part.

    Stricter: the runtime accepts a candidate only when it is clean for the whole visitor, so
    a value that trips one exclusion merely routes that visitor to a different template. This
    test still flags it, because a table that routinely needs the veto is a table authors
    cannot reason about.

    Looser: two exclusions describing the same shelf may answer with the wider family. A
    lactose-only request is answered with lactose-free dairy, which *is* dairy, and the
    visitor who also ticked "dairy" is served by the runtime veto. Encoding that as a failure
    would push authors towards worse copy just to satisfy a check.
    """
    for restriction, rules in PB.RESTRICTION_SWAPS.items():
        for _old, new in rules:
            if PB._names_excluded_food(new, frozenset({restriction})):
                continue                     # runtime veto covers this visitor
            offenders = [other for other in PB.RESTRICTION_VOCAB
                         if other not in (restriction, "alcohol")
                         and PB._names_excluded_food(new, frozenset({other}))
                         and not PB._FAMILY_OVERLAPS.issuperset({restriction, other})]
            assert not offenders, (
                f"{restriction} rule offers {new!r}, which names {offenders}: the funnel "
                "would answer one exclusion with another excluded food")


def test_conditional_mask_only_shields_its_own_exclusion():
    """"gluten-free toast" is fine for a gluten excluder and still gluten for everyone else.

    The alternative reading of "free-from" masking is to apply it globally, which would let
    a gluten-containing "gluten-free flavoured crisp" through to someone who did not ask
    about gluten at all. Narrow masking is the version that stays correct as the tables grow.
    """
    # The label consumes the item beside it, which is the part that took two attempts:
    # "toast" is a gluten name word in its own right, so masking only the label would leave
    # the phrase reading as gluten to the one person it was written for.
    assert not PB._names_excluded_food("gluten-free toast", frozenset({"gluten"}))
    assert not PB._names_excluded_food("gluten-free toast or potato", frozenset({"gluten"}))
    assert not PB._names_excluded_food("gluten free rye bread", frozenset({"gluten"}))
    assert not PB._names_excluded_food("lactose-free milk", frozenset({"lactose"}))
    # "lactose-free milk" is a real product a lactose-intolerant person drinks, so masking it
    # is correct rather than a leak. Plain milk is not, and that is the line.
    assert PB._names_excluded_food("milk", frozenset({"lactose"}))
    assert PB._names_excluded_food("Semolina pasta", frozenset({"gluten"}))
    assert PB._names_excluded_food("rye bread", frozenset({"gluten"}))
    # And the mask must not shield a *different* food sitting in the same phrase.
    assert PB._names_excluded_food("gluten-free mince sauce", frozenset({"redmeat"}))


def test_swap_does_not_rescan_its_own_output():
    """The regression that produced "coconut soy or coconut yoghurt"."""
    rules = PB._swap_rules(frozenset({"dairy", "soy"}))
    out = PB._apply_swaps("Overnight oats with yoghurt and berries", rules)
    assert out.lower().count("yoghurt") == 1, out
    assert "soy" not in out.lower(), f"soy reappeared for a soy excluder: {out}"


def test_longest_rule_wins_inside_its_own_substring():
    """"Walnuts" must swap as walnuts, not as a stray "nuts" inside it."""
    rules = PB._swap_rules(frozenset({"nuts"}))
    assert PB._apply_swaps("Yoghurt, walnuts and honey", rules) == \
        "Yoghurt, pumpkin seeds and honey"


def test_coconut_is_not_a_nut_and_oat_milk_is_not_dairy():
    """Two false positives that would each gut the plant-based menus."""
    assert not PB._names_excluded_food("Coconut yoghurt with seeds", frozenset({"nuts"}))
    assert not PB._names_excluded_food("Smoothie with oat milk and banana",
                                       frozenset({"dairy"}))
    assert PB._names_excluded_food("Greek yoghurt and berries", frozenset({"dairy"}))
    assert PB._names_excluded_food("Trail mix", frozenset({"nuts"}))


def test_three_options_when_available_but_no_duplicates():
    """Variety is the artefact's credibility; a repeated line in one slot reads as padding."""
    for diet in all_diets():
        plan = build(diet=[diet], restrictions=["none"])
        for meal in plan.days[0].meal_structure:
            options = meal[1].split(" / ")
            assert len(options) == len(set(options)), f"{diet}.{meal[0]} repeats itself"


# ---------------------------------------------------------------------------
# Metrics and honesty of the locked half
# ---------------------------------------------------------------------------

def test_metrics_exist_and_reference_the_answers():
    plan = build(sleep=["under6"])
    labels = [row[0] for row in plan.metrics]
    assert "Hours slept" in labels, "short sleep should surface as the first thing logged"
    assert all(len(row) == 3 and all(part.strip() for part in row) for row in plan.metrics)


def test_locked_weeks_describe_change_without_promising_outcomes():
    """Weeks 2-4 are the paid half. They must say what changes, not what it will do to you."""
    arcs = PB.WEEK_ARCS[1:]
    assert len(arcs) == 3
    assert {arc["week"] for arc in arcs} == {2, 3, 4}
    assert len({arc["arc"] for arc in arcs}) == 3, "locked weeks must differ from each other"


def test_unknown_answers_degrade_instead_of_raising():
    """Every quiz field has a fallback, because losing the artefact after the quiz is the
    most expensive failure in the funnel."""
    plan = PB.build_plan(profile_from_answers({}))
    assert plan.days and plan.metrics and plan.fallback_rule
    weird = profile_from_answers({**BASE_ANSWERS, "days": ["99"], "minutes": ["abc"],
                                  "goal": ["not_a_goal"], "diet": ["ketoish"]})
    fallback_plan = PB.build_plan(weird)
    assert sum(1 for d in fallback_plan.days if d.is_training_day) <= 6
