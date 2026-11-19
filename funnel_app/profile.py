"""Quiz answers -> `Profile`, the builder's input contract.

Kept separate from `plan_builder` because this is the only place untrusted form data
becomes typed data. Every field has a fallback, so a partially-completed quiz still
produces a real plan rather than a KeyError on the preview screen: at the point the
user lands on `/preview` they have already done the work, and failing there is the
worst possible place to be strict.
"""

from __future__ import annotations

from plan_builder import Profile
from quiz import NOTE_SUFFIX, QUESTION_BY_KEY, QUESTIONS

INT_FIELDS = {"days": "days_per_week", "minutes": "session_minutes"}

SET_FIELDS = {"restrictions", "equipment"}

DEFAULTS: dict[str, str] = {
    "goal": "consistency",
    "experience": "steady",
    "days": "3",
    "minutes": "30",
    "timeOfDay": "varies",
    "weekendShape": "same",
    "diet": "anything",
    "restrictions": "none",
    "equipment": "bodyweight",
    "cookstyle": "minimal",
    "sleep": "7to8",
    "blocker": "time",
}


def first(answers: dict[str, list[str]], key: str) -> str:
    """First stored value for a key, or the default. Never raises."""
    values = answers.get(key) or []
    if values and values[0] in (QUESTION_BY_KEY[key].option_values if key in QUESTION_BY_KEY else ()):
        return values[0]
    return DEFAULTS.get(key, "")


def profile_from_answers(answers: dict[str, list[str]]) -> Profile:
    """Reduce stored answers to a `Profile`, validating against the question set."""
    def valid(key: str, value: str) -> str:
        question = QUESTION_BY_KEY.get(key)
        if question and value in question.option_values:
            return value
        return DEFAULTS.get(key, "")

    days_raw = valid("days", first(answers, "days"))
    minutes_raw = valid("minutes", first(answers, "minutes"))
    restrictions = {
        v for v in (answers.get("restrictions") or [])
        if v in QUESTION_BY_KEY["restrictions"].option_values
    } or {"none"}
    equipment = {
        v for v in (answers.get("equipment") or [])
        if v in QUESTION_BY_KEY["equipment"].option_values
    } or {"bodyweight"}
    notes = {
        key[: -len(NOTE_SUFFIX)]: value
        for key, value in answers.items()
        if key.endswith(NOTE_SUFFIX) and value
    }

    return Profile(
        goal=valid("goal", first(answers, "goal")),
        experience=valid("experience", first(answers, "experience")),
        days_per_week=int(days_raw),
        session_minutes=int(minutes_raw),
        time_of_day=valid("timeOfDay", first(answers, "timeOfDay")),
        weekend_shape=valid("weekendShape", first(answers, "weekendShape")),
        diet=valid("diet", first(answers, "diet")),
        restrictions=frozenset(restrictions),
        equipment=frozenset(equipment),
        cook_style=valid("cookstyle", first(answers, "cookstyle")),
        sleep=valid("sleep", first(answers, "sleep")),
        blocker=valid("blocker", first(answers, "blocker")),
        notes=notes,
    )


def is_complete(answers: dict[str, list[str]]) -> bool:
    """Every required question answered with a valid option."""
    for question in QUESTIONS:
        values = answers.get(question.key) or []
        if not values:
            return False
        if question.kind == "single" and values[0] not in question.option_values:
            return False
        if question.kind == "multi" and not any(
            v in question.option_values for v in values
        ):
            return False
    return True


def missing_questions(answers: dict[str, list[str]]) -> list[str]:
    """Titles of unanswered questions, for the summary screen's to-do list."""
    return [q.title for q in QUESTIONS if not answers.get(q.key)]


def missing_indices(answers: dict[str, list[str]]) -> int:
    """Index of the first unanswered question, so a partial quiz resumes in place
    instead of restarting and throwing away real effort."""
    for position, question in enumerate(QUESTIONS):
        if not answers.get(question.key):
            return position
    return 0
