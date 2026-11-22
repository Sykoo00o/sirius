"""The kegeldoc onboarding quiz: screens, branching, validation, resume position.

Forked from the Rank-4 funnel's twelve-question quiz and grown to the reference length
(~30 screens on quiz.kegel-plan.com before its consent wall; 39 here for men, 35-36 for women).
Three things are new compared with the routine funnel, and all three keep the original's
progress model intact rather than inventing a second one:

* **Branching.** The bank is a superset; `flow_for(answers)` returns the screens this visitor
  sees, in order. A screen's `show` predicate reads earlier answers only, so the flow up to any
  answered screen never changes underneath the visitor.
* **Kinds beyond single/multi.** `scale` stores "1".."5". `fact` and `milestone` are information
  screens: they store the value "seen" when acknowledged. Treating an acknowledged screen as an
  answer is what lets `next_unanswered` stay "first screen with nothing stored", so resume, the
  forward-jump guard and the per-step response rows work exactly as they did.
* **Sourced facts.** Study screens cite real, published sources by author, journal and year.
  There are no invented percentages and no reviews; `claims.FORBIDDEN_TERMS` bans the shapes.

Answers are plain strings keyed by `key`, validated against `options`, and turned into a
`Profile` by `profile.profile_from_answers` at preview time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

Answers = dict[str, list[str]]

SEEN = "seen"
SCALE_VALUES: tuple[str, ...] = ("1", "2", "3", "4", "5")


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    shown: str
    #: Bars with the same group render under one heading.
    group: str = ""
    #: "accent" for the trained/after result, "muted" for the comparison.
    tone: str = "accent"


@dataclass(frozen=True)
class Chart:
    title: str
    bars: tuple[Bar, ...]
    #: The value a full-width bar stands for.
    scale_max: float
    #: Who was studied and how, so the chart is read as that study and not as a promise.
    note: str


#: Charts for the study screens. Every number is the published figure from the source the screen
#: cites (checked against the papers on 2026-11-22), rounded only where `shown` says so. A new
#: chart needs its own source line on the screen that shows it.
CHARTS: dict[str, Chart] = {
    # Dorey et al., BJGP 2004;54:819-825, abstract: 22 (40.0%) normal function, 19 (34.5%)
    # improved, 14 (25.5%) failed to improve, n=55.
    "dorey": Chart(
        title="Erection outcome for the 55 men in the trial",
        bars=(Bar("Normal erectile function", 40.0, "40%"),
              Bar("Improved", 34.5, "34.5%"),
              Bar("No improvement", 25.5, "25.5%", tone="muted")),
        scale_max=100,
        note="Randomised trial of pelvic floor exercises with a physiotherapist, biofeedback and "
             "home practice. Results vary from person to person."),
    # Pastore et al., Ther Adv Urol 2014;6(3):83-88: mean IELT 31.7 s at baseline, 146.2 s
    # after 12 weeks; 33 of 40 gained control. Uncontrolled.
    "pastore": Chart(
        title="Average time to ejaculation, 40 men",
        bars=(Bar("Before the programme", 31.7, "32 s", tone="muted"),
              Bar("After 12 weeks", 146.2, "146 s")),
        scale_max=160,
        note="Clinic programme of pelvic floor exercises, electrical stimulation and "
             "biofeedback; 33 of the 40 men gained control. There was no comparison group."),
    # Dumoulin et al., Cochrane 2018, CD005654: stress incontinence, cure 56% vs 6% (RR 8.38);
    # cure or improvement 74% vs 11% (RR 6.33).
    "cochrane": Chart(
        title="Women with leaks on effort, at the end of training",
        bars=(Bar("Pelvic floor training", 56, "56%", group="Leaks stopped"),
              Bar("No training", 6, "6%", group="Leaks stopped", tone="muted"),
              Bar("Pelvic floor training", 74, "74%", group="Leaks stopped or improved"),
              Bar("No training", 11, "11%", group="Leaks stopped or improved", tone="muted")),
        scale_max=100,
        note="Pooled from randomised trials comparing pelvic floor training with no training. "
             "Most trials were small and followed women for under a year."),
}


@dataclass(frozen=True)
class Question:
    key: str
    section: str
    title: str
    help: str = ""
    kind: str = "single"  # "single" | "multi" | "scale" | "fact" | "milestone"
    options: tuple[tuple[str, str], ...] = ()
    allow_note: bool = False
    required: bool = True
    #: Scale end labels.
    low: str = ""
    high: str = ""
    #: Fact screens: eyebrow, body text, optional citation, illustration name.
    eyebrow: str = ""
    body: str = ""
    source: str = ""
    art: str = "floor"
    #: Study screens: the key of a chart in `CHARTS`, drawn from the cited paper's own figures.
    chart: str = ""
    #: Single-choice: render the tall icon rows (the track picker).
    icons: bool = False
    #: Multi-select: the value that clears every other choice ("None of these").
    exclusive: str = ""
    #: Subtitles under option labels, keyed by option value.
    subs: dict[str, str] = field(default_factory=dict)
    #: Whether this screen is in the visitor's flow, from earlier answers.
    show: Callable[[Answers], bool] | None = None

    @property
    def option_values(self) -> tuple[str, ...]:
        if self.kind == "scale":
            return SCALE_VALUES
        if self.kind in ("fact", "milestone"):
            return (SEEN,)
        return tuple(value for value, _ in self.options)

    @property
    def is_info(self) -> bool:
        """A screen that informs rather than asks. Excluded from the summary read-back."""
        return self.kind in ("fact", "milestone")


def _v(answers: Answers, key: str) -> str:
    values = answers.get(key) or []
    return values[0] if values else ""


SKIP = ("skip", "I'd rather not say")
FREQ: tuple[tuple[str, str], ...] = (
    ("never", "Never"), ("rarely", "Rarely"), ("sometimes", "Sometimes"),
    ("often", "Often"), ("most", "Most of the time"), SKIP,
)

SECTIONS: tuple[tuple[str, str], ...] = (
    ("about", "About you"),
    ("goal", "Your goal"),
    ("erections", "Erections"),
    ("control", "Control"),
    ("bladder", "Bladder"),
    ("lifestage", "Life stage"),
    ("sexlife", "Your sex life"),
    ("health", "Health"),
    ("experience", "Your experience"),
    ("safety", "Safety check"),
    ("lifestyle", "Lifestyle"),
    ("routine", "Your routine"),
    ("info", "Good to know"),
)


def _men(a: Answers) -> bool:
    return _v(a, "track") != "women"


def _women(a: Answers) -> bool:
    return _v(a, "track") == "women"


BANK: tuple[Question, ...] = (
    # ---- about you -----------------------------------------------------------------------
    Question("track", "about", "Who is this plan for?",
             "Pelvic floor anatomy differs, so the exercises and cues do too.",
             options=(("men", "Men"), ("women", "Women")), icons=True,
             subs={"men": "Erections, control, bladder", "women": "Leaks, recovery, sensation"}),
    Question("age", "about", "How old are you?",
             "Muscle tone changes with age. It changes your starting holds, not whether this "
             "works for you.",
             options=(("18", "18–29"), ("30", "30–39"), ("40", "40–49"), ("50", "50–59"),
                      ("60", "60 or over"))),

    # ---- men -----------------------------------------------------------------------------
    Question("goal_men", "goal", "What's your main goal?",
             "Pick the one that matters most. The plan adds a daily drill for it.",
             options=(("erection", "Firmer, longer-lasting erections"),
                      ("ejac", "More control over when I finish"), ("both", "Both of those"),
                      ("bladder", "Better bladder control"), ("general", "Overall pelvic strength")),
             show=_men),
    Question("erect_type", "erections", "Which best describes what you notice?",
             "Answers stay private to this plan.",
             options=(("getting", "Getting an erection is hard"), ("keeping", "Keeping it is hard"),
                      ("both", "Both"), ("none", "Neither, I'm training ahead"), SKIP),
             show=_men),
    Question("erect_freq", "erections", "How often does it happen?",
             "Think about the last three months.", options=FREQ,
             show=lambda a: _men(a) and _v(a, "erect_type") != "none"),
    Question("erect_since", "erections", "How long has it been going on?",
             "Gradual changes and sudden ones point to different things.",
             options=(("lt1", "Less than a year"), ("1to5", "1 to 5 years"),
                      ("gt5", "More than 5 years"), SKIP),
             show=lambda a: _men(a) and _v(a, "erect_type") != "none"),
    Question("morning", "erections", "How often do you wake up with an erection?",
             "Morning erections are a rough sign that blood flow and nerves are working.",
             options=FREQ, show=_men),
    Question("fact_men_erect", "info", "The muscles at the base of the penis help hold an erection",
             kind="fact", eyebrow="Study fact", chart="dorey",
             body="Two pelvic floor muscles wrap the root of the penis. When they contract they "
                  "help keep blood in place. Pelvic floor exercise has been tested in a "
                  "randomised trial with men who had erection problems.",
             source="Dorey et al., British Journal of General Practice, 2004.", show=_men),
    Question("firm_sex", "erections", "How firm are your erections during sex?", "Rate from 1 to 5.",
             kind="scale", low="Not firm", high="Very firm", show=_men),
    Question("sat", "sexlife", "How satisfied are you with your sex life in the last 3 months?",
             "Rate from 1 to 5.", kind="scale", low="Not satisfied", high="Very satisfied",
             show=_men),
    Question("worry", "sexlife", "How much does this worry you?",
             "Worry itself can make erections harder, so it shapes the relaxation work.",
             kind="scale", low="I don't worry", high="A lot", show=_men),
    Question("relation", "sexlife", "How much does it affect your relationship?",
             "If you are single, answer 1.", kind="scale", low="Not at all", high="A lot",
             show=_men),
    Question("confidence_m", "sexlife", "Does it affect how confident you feel?",
             "A common, honest answer is yes.",
             options=(("yes", "Yes"), ("bit", "A little"), ("no", "No"), SKIP), show=_men),
    Question("sex_length", "control", "How long does sex usually last for you?",
             "From penetration to finishing, roughly.",
             options=(("lt2", "Less than 2 minutes"), ("2to7", "2 to 7 minutes"),
                      ("7to15", "7 to 15 minutes"), ("gt15", "More than 15 minutes"), SKIP),
             show=_men),
    Question("early", "control", "How often do you finish sooner than you would like?",
             "Answers stay private to this plan.", options=FREQ, show=_men),
    Question("control_want", "control", "Would you like more control over when you finish?",
             "If yes, the plan adds a slow-release drill.",
             options=(("yes", "Yes, I'd like that"), ("maybe", "Maybe"),
                      ("no", "It's not a concern")), show=_men),
    Question("fact_men_ejac", "info", "The same muscles are involved in ejaculation",
             kind="fact", eyebrow="Study fact", chart="pastore",
             body="Learning to feel, hold and deliberately relax the pelvic floor has been "
                  "studied as a way to build control over timing, with sessions much like the "
                  "ones in your plan.",
             source="Pastore et al., Therapeutic Advances in Urology, 2014.", show=_men),
    Question("dribble", "bladder", "Do you notice a few drops after you have finished urinating?",
             "Very common, and one of the easiest things for a pelvic floor routine to target.",
             options=FREQ, show=_men),
    Question("urge_m", "bladder", "Do you get sudden urges where you need to go right now?",
             "Urgency gets its own drill.", options=FREQ, show=_men),
    Question("prostate", "health", "Have you had prostate surgery or treatment?",
             "Pelvic floor exercise is often part of recovery, but timing matters.",
             options=(("no", "No"), ("old", "Yes, more than 3 months ago"),
                      ("recent", "Yes, in the last 3 months")), show=_men),
    Question("libido_m", "sexlife", "How would you rate your sex drive in the last 3 months?",
             "Rate from 1 to 5.", kind="scale", low="Low", high="High", show=_men),
    Question("meds", "health", "Do you take medication to help with erections?",
             "This changes nothing about your exercises. It is here so the plan talks to you "
             "honestly.",
             options=(("always", "Yes, every time"), ("sometimes", "Sometimes"), ("no", "No"), SKIP),
             show=_men),
    Question("fact_men_meds", "info", "Exercise works alongside medication, not instead of it",
             kind="fact", eyebrow="Good to know", art="meds",
             body="If you take medication, keep taking it the way your doctor told you. Pelvic "
                  "floor exercise is "
                  "something you add. Any change to medication is a conversation with your "
                  "doctor, not with a quiz.",
             show=lambda a: _men(a) and _v(a, "meds") in ("always", "sometimes")),

    # ---- women ---------------------------------------------------------------------------
    Question("goal_women", "goal", "What's your main goal?",
             "Pick the one that matters most. The plan adds a daily drill for it.",
             options=(("leaks", "Fewer leaks when I cough, laugh or move"),
                      ("intimacy", "More sensation and comfort during sex"),
                      ("birth", "Recovery after pregnancy and birth"),
                      ("menopause", "Support through menopause"),
                      ("general", "Overall pelvic strength")),
             show=_women),
    Question("leak_effort", "bladder", "Do you leak when you cough, sneeze or laugh?",
             "Even a little counts. This is the most common reason women start kegels.",
             options=FREQ, show=_women),
    Question("leak_exercise", "bladder", "Do you leak when you run, jump or lift something?",
             "Impact and heavy lifting load the pelvic floor most.",
             options=FREQ + (("na", "I don't do these"),), show=_women),
    Question("urge", "bladder", "Do you get sudden urges where you need to go right now?",
             "Urgency gets its own drill, separate from leaks on effort.", options=FREQ,
             show=_women),
    Question("night", "bladder", "How many times do you usually get up at night to urinate?",
             "Once is common. More often is worth knowing about.",
             options=(("0", "Not at all"), ("1", "Once"), ("2", "Twice"), ("3", "3 times or more")),
             show=_women),
    Question("leak_amount", "bladder", "When you do leak, how much is it?",
             "Sets how much effort-timing practice goes into week 1.",
             options=(("none", "I don't leak"), ("drops", "A few drops"), ("small", "A small amount"),
                      ("more", "Enough to change clothes"), SKIP),
             show=_women),
    Question("pads", "bladder", "Do you wear pads or liners because of leaks?",
             "A practical measure of how much this shapes your day.",
             options=(("never", "Never"), ("sometimes", "Sometimes, just in case"),
                      ("daily", "Every day"), SKIP),
             show=_women),
    Question("fact_women_leaks", "info", "Two kinds of leaks, two different drills",
             kind="fact", eyebrow="Good to know", art="leaks",
             body="Leaks on a cough or a jump happen when the floor is not quick or strong enough "
                  "under sudden pressure. Leaks with a sudden urge are about a bladder that "
                  "squeezes too early. Your plan trains each one differently.",
             show=_women),
    Question("births", "lifestage", "Have you given birth?",
             "Pregnancy and birth stretch the pelvic floor. Recent births start gentler.",
             options=(("none", "No"), ("one", "Yes, once"), ("two", "Yes, twice"),
                      ("three", "Three times or more"), ("pregnant", "I'm pregnant now")),
             show=_women),
    Question("birth_when", "lifestage", "When was your most recent birth?",
             "Most guidance suggests waiting for your postnatal check before harder exercise.",
             options=(("under6w", "Less than 6 weeks ago"), ("under1y", "6 weeks to a year ago"),
                      ("over1y", "More than a year ago")),
             show=lambda a: _women(a) and _v(a, "births") in ("one", "two", "three")),
    Question("meno", "lifestage", "Where are you with menopause?",
             "Changes at menopause affect pelvic tissue, so endurance work gets more time.",
             options=(("pre", "Not there yet"), ("peri", "Perimenopause"), ("post", "Post-menopause"),
                      ("unsure", "Not sure")),
             show=_women),
    Question("heaviness", "lifestage", "Do you ever feel heaviness, dragging or a bulge down below?",
             "This can be a sign of prolapse, which a physiotherapist should check.",
             options=FREQ, show=_women),
    Question("sensation", "sexlife", "How much sensation do you feel during sex?",
             "Rate from 1 to 5.", kind="scale", low="Very little", high="A lot", show=_women),
    Question("sat_w", "sexlife", "How satisfied are you with your sex life in the last 3 months?",
             "Rate from 1 to 5.", kind="scale", low="Not satisfied", high="Very satisfied",
             show=_women),
    Question("sexpain_w", "sexlife", "Do you feel pain during or after sex?",
             "Pain changes the plan: relaxation comes before strength.", options=FREQ,
             show=_women),
    Question("confidence_w", "sexlife", "How much do these things affect your confidence?",
             "Rate from 1 to 5.", kind="scale", low="Not at all", high="A lot", show=_women),
    Question("libido_w", "sexlife", "How would you rate your sex drive in the last 3 months?",
             "Rate from 1 to 5.", kind="scale", low="Low", high="High", show=_women),
    Question("fact_women_first", "info", "Pelvic floor exercise is a recommended first step for leaks",
             kind="fact", eyebrow="Study fact", chart="cochrane",
             body="UK guidance for clinicians recommends at least three months of supervised "
                  "pelvic floor muscle training as the first step for leaks on effort, and a large "
                  "review found women who trained were more likely to report their leaks had "
                  "stopped or improved than women who did not.",
             source="NICE guideline NG123, 2019; Dumoulin et al., Cochrane review, 2018.",
             show=_women),

    # ---- both tracks ---------------------------------------------------------------------
    Question("milestone", "info", "Your plan so far", kind="milestone"),
    Question("tried", "experience", "Have you done kegels before?",
             "Most people have tried. Few were ever shown how.",
             options=(("never", "Never"), ("unsure", "Yes, but I'm not sure I did them right"),
                      ("sometimes", "Now and then"), ("regular", "Yes, regularly"))),
    Question("fact_find", "info", "Finding the right muscles", kind="fact", eyebrow="How to",
             art="anatomy",
             body="Imagine stopping yourself passing wind and, at the same time, stopping the flow "
                  "of urine. That squeeze-and-lift is a kegel. Use the image only: stopping urine "
                  "mid-flow as a regular exercise is not recommended."),
    Question("feel", "experience",
             "Try it now: gently squeeze and lift, then let go. Could you feel it?",
             "Keep your belly, thighs and buttocks relaxed. You should feel a lift inward, not a "
             "push down.",
             options=(("yes", "Yes, clearly"), ("some", "A little"), ("no", "Not really"))),
    Question("strength", "experience", "How strong did that squeeze feel?",
             "A guess is fine. Your plan re-tests it on day 7.", kind="scale",
             low="Barely there", high="Very strong"),
    Question("hold", "experience", "Squeeze again and hold it, counting slowly. When did it fade?",
             "This sets your starting hold length.",
             options=(("2", "Within 2 seconds"), ("5", "At 3 to 5 seconds"), ("9", "At 6 to 9 seconds"),
                      ("10", "I held 10 seconds or more"), ("0", "I couldn't feel it"))),
    Question("fact_relax", "info", "Letting go is half of every rep", kind="fact",
             eyebrow="Good to know", art="relax",
             body="A muscle that never fully relaxes gets tired and tight, not strong. Every hold "
                  "in your plan is followed by a release at least as long as the squeeze."),
    Question("safety", "safety", "Do any of these apply to you right now?",
             "Select all that apply. This changes how your plan starts.", kind="multi",
             options=(("pain", "Pelvic, genital or rectal pain"), ("blood", "Blood in urine"),
                      ("burning", "Burning when urinating"),
                      ("surgery", "Pelvic surgery in the last 3 months"), ("none", "None of these")),
             exclusive="none"),
    Question("alcohol", "lifestyle", "How often do you drink alcohol?",
             "Alcohol affects both the bladder and erections.",
             options=(("daily", "Every day"), ("weekly_plus", "Several times a week"),
                      ("weekly", "About once a week"), ("rarely", "Once a month or less"),
                      ("never", "I don't drink"))),
    Question("smoke", "lifestyle", "Do you smoke or vape?",
             "Nicotine narrows blood vessels; a smoker's cough loads the pelvic floor.",
             options=(("yes", "Yes"), ("sometimes", "Sometimes"), ("quit", "I quit"), ("no", "No"))),
    Question("diet", "lifestyle", "How healthy is your diet?",
             "Straining with constipation is one of the biggest everyday loads on the pelvic floor.",
             kind="scale", low="Not healthy", high="Very healthy"),
    Question("activity", "lifestyle", "How active are you day to day?",
             "Running and lifting load the pelvic floor, so active plans add standing work sooner.",
             kind="scale", low="Mostly sitting", high="Very active"),
    Question("sitting", "lifestyle", "How many hours a day do you spend sitting?",
             "Long sitting is the easiest place to hide a set.",
             options=(("lt4", "Less than 4 hours"), ("4to8", "4 to 8 hours"),
                      ("gt8", "More than 8 hours"))),
    Question("stress", "lifestyle", "How would you rate your daily stress?",
             "Stress often shows up as a tight, tired pelvic floor.", kind="scale",
             low="Low", high="High"),
    Question("minutes", "routine", "How much time can you give it each day?",
             "Short and daily beats long and occasional.",
             options=(("3", "3 minutes"), ("5", "5 minutes"), ("10", "10 minutes")),
             subs={"3": "One round", "5": "Recommended", "10": "Two rounds plus a drill"}),
    Question("when", "routine", "When would a session fit best?",
             "We anchor it to something you already do, so you remember.",
             options=(("morning", "Right after I wake up"), ("commute", "On my commute"),
                      ("desk", "At my desk"), ("evening", "In bed at night")),
             allow_note=True),
)

#: The full bank, every branch. Kept under the routine funnel's name because the telemetry and
#: billing tests size fixtures from it.
QUESTIONS: tuple[Question, ...] = BANK
QUESTION_BY_KEY: dict[str, Question] = {q.key: q for q in BANK}

#: Display names for stored values, keyed by question then value.
OPTION_LABELS: dict[str, dict[str, str]] = {
    q.key: ({v: v for v in SCALE_VALUES} if q.kind == "scale" else dict(q.options)) for q in BANK
}

SECTION_KEYS: tuple[str, ...] = tuple(key for key, _label in SECTIONS)
SECTION_TITLES: dict[str, str] = dict(SECTIONS)

NOTE_SUFFIX = "_note"
MAX_NOTE_LENGTH = 280


def flow_for(answers: Answers) -> list[Question]:
    """The screens this visitor sees, in order.

    Predicates read only earlier answers, so for every answered screen the flow before it is
    fixed: changing the track resets the branch by design, and `track` is screen one.
    """
    return [q for q in BANK if q.show is None or q.show(answers)]


def section_of(key: str) -> str:
    return QUESTION_BY_KEY[key].section


def index_of(key: str, answers: Answers | None = None) -> int:
    return next(i for i, q in enumerate(flow_for(answers or {})) if q.key == key)


def parse_answer(question: Question, posted_single: str | None,
                 posted_fields: dict[str, list[str]],
                 raw_note: str | None) -> tuple[list[str], str, str | None]:
    """Validate one submitted answer. Returns ``(values, note, error)``.

    Single, scale, fact and milestone screens post one `answer` field. Multi-select inputs are
    named after the question key (and a literal `answers` field is accepted too, as in the
    routine funnel). Unknown options are dropped rather than trusted.
    """
    note = (raw_note or "").strip()[:MAX_NOTE_LENGTH]

    if question.kind != "multi":
        value = (posted_single or "").strip()
        if value not in question.option_values:
            if question.is_info:
                return [], note, "Tap the button to continue."
            if question.kind == "scale":
                return [], note, "Choose a number from 1 to 5 to continue."
            return [], note, "Choose one option to continue."
        return [value], note, None

    posted = list(posted_fields.get(question.key, []))
    posted += posted_fields.get("answers", [])
    values = [v.strip() for v in posted if v.strip() in question.option_values]
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if question.exclusive and question.exclusive in unique:
        unique = [question.exclusive]
    if not unique:
        return [], note, "Pick at least one option to continue."
    return unique, note, None


def _answered(question: Question, answers: Answers) -> bool:
    values = answers.get(question.key) or []
    return any(v in question.option_values for v in values)


def answered_count(answers: Answers) -> int:
    return sum(1 for q in flow_for(answers) if _answered(q, answers))


def next_unanswered(answers: Answers) -> int:
    """Index (into this visitor's flow) of the first screen with nothing valid stored, or the
    last index when the flow is complete. The quiz's resume position."""
    flow = flow_for(answers)
    for position, question in enumerate(flow):
        if not _answered(question, answers):
            return position
    return len(flow) - 1


def is_complete(answers: Answers) -> bool:
    return all(_answered(q, answers) for q in flow_for(answers))


def progress_pct(answers: Answers) -> int:
    flow = flow_for(answers)
    return round(answered_count(answers) / len(flow) * 100) if flow else 0
