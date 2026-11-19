"""The onboarding quiz: questions, validation, scoring, and the summary checkpoint.

Twelve questions in four sections. Each one is required and contributes to the
plan: nothing here is filler, because the whole premise of this lane is that the
effort the user spends is genuine (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §1.C
Step 1, criterion 1). A fake progress bar is the thing we are not allowed to build.

Answers are kept as plain strings keyed by `key`, validated against `options`
(plus an optional free-text note per question), and turned into a `Profile` by
`scoring.profile_from_answers` at preview time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    key: str
    section: str
    title: str
    help: str
    kind: str  # "single" | "multi"
    options: tuple[tuple[str, str], ...]
    allow_note: bool = True
    required: bool = True

    @property
    def option_values(self) -> tuple[str, ...]:
        return tuple(value for value, _ in self.options)


SECTIONS: tuple[tuple[str, str], ...] = (
    ("goal", "What you want"),
    ("routine", "Your week, realistically"),
    ("fuel", "Food and movement"),
    ("recovery", "Rest, and what gets in the way"),
)


QUESTIONS: tuple[Question, ...] = (
    # ---- Section 1: goal -----------------------------------------------------
    Question(
        key="goal",
        section="goal",
        title="What are you actually trying to build?",
        help="Pick the one that matters most this month. One focus beats four.",
        kind="single",
        options=(
            ("energy", "More steady energy through the day"),
            ("strength", "Get stronger with weights"),
            ("lean", "Move toward a weight that suits you"),
            ("endurance", "Better cardio and stamina"),
            ("mobility", "Move freely, feel less stiff"),
            ("sleep", "Sleep and recovery habits"),
            ("consistency", "Just be consistent, whatever that means"),
        ),
    ),
    Question(
        key="experience",
        section="goal",
        title="How consistent has your last month looked?",
        help="Honest answer. The plan is built around the pattern you actually have.",
        kind="single",
        options=(
            ("new", "Barely trained at all lately"),
            ("comeback", "Used to train, taking a break or coming back"),
            ("steady", "Mostly regular, 1-3 sessions a week"),
            ("solid", "Regular, 4+ sessions a week"),
        ),
    ),
    # ---- Section 2: routine --------------------------------------------------
    Question(
        key="days",
        section="routine",
        title="How many days a week can you protect?",
        help="We plan the number you can repeat, not the number you wish for.",
        kind="single",
        options=(
            ("2", "2 days"),
            ("3", "3 days"),
            ("4", "4 days"),
            ("5", "5 days"),
            ("6", "6 or more"),
        ),
        allow_note=False,
    ),
    Question(
        key="minutes",
        section="routine",
        title="How long is a realistic session?",
        help="Including a warm-up. Understating this is the fastest way to quit.",
        kind="single",
        options=(
            ("20", "About 20 minutes"),
            ("30", "30 minutes"),
            ("45", "45 minutes"),
            ("60", "60 minutes or more"),
        ),
        allow_note=False,
    ),
    Question(
        key="timeOfDay",
        section="routine",
        title="When do you actually move?",
        help="Time blocks, not intentions. Morning works if mornings are real.",
        kind="single",
        options=(
            ("early", "Early morning, before the day starts"),
            ("midday", "Lunch break or midday"),
            ("evening", "After work, early evening"),
            ("late", "Late evening"),
            ("varies", "It varies wildly"),
        ),
    ),
    Question(
        key="weekendShape",
        section="routine",
        title="What do weekends look like for you?",
        help="Weekends decide whether a routine survives week two.",
        kind="single",
        options=(
            ("shift", "Weekends are my training window"),
            ("social", "Social, meals out, later starts"),
            ("family", "Family errands and logistics"),
            ("work", "Still working, low time"),
            ("same", "Similar to weekdays"),
        ),
    ),
    # ---- Section 3: fuel and movement ---------------------------------------
    Question(
        key="diet",
        section="fuel",
        title="How do you eat right now?",
        help="Structure preference only. No food is a moral category here.",
        kind="single",
        options=(
            ("anything", "No rules, I eat everything"),
            ("highprotein", "High protein on purpose"),
            ("plant", "Mostly or fully plant-based"),
            ("vegetarian", "Vegetarian"),
            ("mediterranean", "Mediterranean-ish"),
            ("lowcarb", "Lower carb"),
            ("pescatarian", "Fish and seafood, no meat"),
        ),
    ),
    Question(
        key="restrictions",
        section="fuel",
        title="Anything you avoid, or that disagrees with you?",
        help="Pick all that apply. This shapes the meal structure you see.",
        kind="multi",
        options=(
            ("none", "Nothing"),
            ("gluten", "Gluten"),
            ("dairy", "Dairy"),
            ("lactose", "Lactose"),
            ("nuts", "Tree nuts or peanuts"),
            ("shellfish", "Shellfish"),
            ("egg", "Egg"),
            ("soy", "Soy"),
            ("alcohol", "Alcohol"),
            ("redmeat", "Red meat"),
            ("coffee", "Caffeine"),
        ),
    ),
    Question(
        key="equipment",
        section="fuel",
        title="What can you actually get to?",
        help="We build the routine around what is nearby, not what is ideal.",
        kind="multi",
        options=(
            ("bodyweight", "Just floor space"),
            ("bands", "Resistance bands"),
            ("dumbbells", "Dumbbells or kettlebells"),
            ("homegym", "A home rack or bars"),
            ("fullgym", "A full gym"),
            ("pool", "A pool"),
            ("outdoor", "Outdoor space, stairs, a route"),
            ("classes", "Classes or a studio"),
        ),
    ),
    Question(
        key="cookstyle",
        section="fuel",
        title="How much cooking is realistic for you?",
        help="A meal structure you will not cook is decoration, not a plan.",
        kind="single",
        options=(
            ("batch", "I can batch-cook a couple of times a week"),
            ("fresh", "I cook something fresh most days"),
            ("minimal", "Minimal cooking, assembly mostly"),
            ("outside", "Mostly eaten out or premade"),
            ("shared", "Someone else cooks"),
        ),
    ),
    # ---- Section 4: recovery and blockers ------------------------------------
    Question(
        key="sleep",
        section="recovery",
        title="How does a typical night go?",
        help="Sleep is the base every habit sits on. It changes the plan's load.",
        kind="single",
        options=(
            ("under6", "Under 6 hours, regularly"),
            ("6to7", "Around 6 to 7 hours"),
            ("7to8", "7 to 8 hours"),
            ("over8", "8 or more hours"),
            ("broken", "Enough hours, broken up"),
        ),
    ),
    Question(
        key="blocker",
        section="recovery",
        title="What usually breaks the streak first?",
        help="The plan puts a named fallback where this happens.",
        kind="single",
        options=(
            ("time", "Not enough time"),
            ("energy", "Low energy by the time I could train"),
            ("motivation", "Motivation drops off"),
            ("travel", "Travel or an irregular schedule"),
            ("knowledge", "Not knowing what to do next"),
            ("soreness", "Soreness or niggles"),
            ("food", "Food falls apart first"),
            ("work", "Work load or shift patterns"),
        ),
    ),
)

QUESTION_BY_KEY: dict[str, Question] = {q.key: q for q in QUESTIONS}

#: Display names for option values, keyed by question then value. Values stay the storage
#: format (they are what the plan builder switches on) and the label lives here, so a
#: stored answer can always be shown as English without the builder learning about copy.
OPTION_LABELS: dict[str, dict[str, str]] = {
    question.key: dict(question.options) for question in QUESTIONS
}

#: Section membership and ordering, derived rather than repeated.
SECTION_KEYS: tuple[str, ...] = tuple(key for key, _label in SECTIONS)
SECTION_TITLES: dict[str, str] = dict(SECTIONS)


#: Free-text note field shown alongside most questions. Kept separate from the
#: option answers so validation of options stays simple, and so a stray semicolon
#: in prose can never be mistaken for a multi-select separator.
NOTE_SUFFIX = "_note"
MAX_NOTE_LENGTH = 280


def section_of(key: str) -> str:
    for index, question in enumerate(QUESTIONS):
        if question.key == key:
            return question.section
    raise KeyError(key)


def index_of(key: str) -> int:
    return next(i for i, q in enumerate(QUESTIONS) if q.key == key)


def parse_answer(question: Question, posted_single: str | None,
                 posted_fields: dict[str, list[str]],
                 raw_note: str | None) -> tuple[list[str], str, str | None]:
    """Validate one submitted answer.

    Returns ``(values, note, error)``; ``error`` is None when the answer is usable.

    `posted_fields` is the whole posted form as {name: [values]}. Multi-select inputs are
    named after the question key, because that is what the template emits and what a
    server-rendered form should emit, so validation has to look the field up by key
    instead of assuming one shared name. Reading a fixed name is the bug this signature
    exists to prevent: it returns an empty selection for every multi question, which then
    reads as "the user picked nothing" and bounces them off question 8 forever.

    Unknown options are dropped rather than trusted, so a hand-edited post cannot smuggle
    an arbitrary string into the plan builder.
    """
    note = (raw_note or "").strip()[:MAX_NOTE_LENGTH]

    if question.kind == "single":
        value = (posted_single or "").strip()
        if value not in question.option_values:
            return [], note, "Choose one option to continue."
        return [value], note, None

    posted = list(posted_fields.get(question.key, []))
    # A field literally named `answers` is accepted too: it is what a hand-built POST or a
    # no-JS fallback would send, and refusing it would punish a client for a naming choice
    # the funnel never advertised.
    posted += posted_fields.get("answers", [])
    values = [v.strip() for v in posted if v.strip() in question.option_values]
    # De-duplicate while preserving order, so a re-posted form does not double-count.
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if "none" in unique:
        unique = ["none"]
    if not unique:
        return [], note, "Pick at least one option to continue."
    return unique, note, None


def answered_count(answers: dict[str, list[str]]) -> int:
    return sum(1 for q in QUESTIONS if answers.get(q.key))


def next_unanswered(answers: dict[str, list[str]]) -> int:
    """Index of the first question with no stored answer, or the last index if complete.

    This is the quiz's resume position, and it is why every step posts to its own route:
    a visit to `/onboarding` after finishing lands on the checkpoint rather than looping
    back to question 1, which is what a "no answers, start over" guard alone would do.
    """
    for position, question in enumerate(QUESTIONS):
        if not answers.get(question.key):
            return position
    return len(QUESTIONS) - 1


def progress_pct(answers: dict[str, list[str]]) -> int:
    total = len(QUESTIONS)
    return round(answered_count(answers) / total * 100)
