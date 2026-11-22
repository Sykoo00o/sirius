"""Quiz answers -> `Profile`, the builder's input contract.

The only place untrusted form data becomes typed data. Every field falls back to a default, so a
partial or stale answer set still produces a real plan rather than an error on the preview
screen: by the time a visitor lands there they have done the work, and failing then is the worst
possible place to be strict. Values are validated against the question bank, never trusted.
"""

from __future__ import annotations

from plan_builder import Profile
from quiz import (NOTE_SUFFIX, QUESTION_BY_KEY, flow_for, is_complete,  # noqa: F401 - re-exported
                  next_unanswered)

DEFAULTS = Profile()


def _one(answers: dict[str, list[str]], key: str, default: str) -> str:
    values = answers.get(key) or []
    question = QUESTION_BY_KEY.get(key)
    if values and question and values[0] in question.option_values:
        return values[0]
    return default


def _scale(answers: dict[str, list[str]], key: str, default: int) -> int:
    raw = _one(answers, key, "")
    return int(raw) if raw else default


def profile_from_answers(answers: dict[str, list[str]]) -> Profile:
    """Reduce stored answers to a `Profile`, validating against the question bank."""
    track = _one(answers, "track", DEFAULTS.track)
    goal_key = "goal_women" if track == "women" else "goal_men"
    safety = frozenset(v for v in (answers.get("safety") or [])
                       if v in QUESTION_BY_KEY["safety"].option_values) or frozenset({"none"})
    notes = {key[: -len(NOTE_SUFFIX)]: values[0]
             for key, values in answers.items()
             if key.endswith(NOTE_SUFFIX) and values and values[0]}
    return Profile(
        track=track,
        age=_one(answers, "age", DEFAULTS.age),
        goal=_one(answers, goal_key, DEFAULTS.goal),
        tried=_one(answers, "tried", DEFAULTS.tried),
        feel=_one(answers, "feel", DEFAULTS.feel),
        strength=_scale(answers, "strength", DEFAULTS.strength),
        hold=_one(answers, "hold", DEFAULTS.hold),
        safety=safety,
        minutes=int(_one(answers, "minutes", str(DEFAULTS.minutes))),
        when=_one(answers, "when", DEFAULTS.when),
        activity=_scale(answers, "activity", DEFAULTS.activity),
        sitting=_one(answers, "sitting", DEFAULTS.sitting),
        stress=_scale(answers, "stress", DEFAULTS.stress),
        diet=_scale(answers, "diet", DEFAULTS.diet),
        smoke=_one(answers, "smoke", DEFAULTS.smoke),
        alcohol=_one(answers, "alcohol", DEFAULTS.alcohol),
        morning=_one(answers, "morning", "") if track != "women" else "",
        erect_since=_one(answers, "erect_since", "") if track != "women" else "",
        urge_m=_one(answers, "urge_m", "") if track != "women" else "",
        prostate=_one(answers, "prostate", "") if track != "women" else "",
        urge=_one(answers, "urge", "") if track == "women" else "",
        night=_one(answers, "night", "") if track == "women" else "",
        births=_one(answers, "births", "") if track == "women" else "",
        # Only while the screen is in the flow: a visitor who answered it, then went back and
        # changed "births" to No, still has the old value stored, and it must not make the plan
        # gentle for a birth that the current answers say never happened.
        birth_when=(_one(answers, "birth_when", "") if track == "women"
                    and _one(answers, "births", "") in ("one", "two", "three") else ""),
        heaviness=_one(answers, "heaviness", "") if track == "women" else "",
        sexpain=_one(answers, "sexpain_w", "") if track == "women" else "",
        notes=notes,
    )


def missing_indices(answers: dict[str, list[str]]) -> int:
    """Index of the first unanswered screen in this visitor's flow, so a partial quiz resumes in
    place instead of restarting."""
    return next_unanswered(answers)
