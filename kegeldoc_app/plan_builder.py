"""Deterministic kegel plan: starting level, daily drill, week 1 day by day, safety notes.

Forked from the Rank-4 funnel's routine builder and kept to the same three rules, for the same
reasons: the pre-paywall artefact must be reproducible to be defensible, every string in it is
human-written so it stays inside the claims guardrail, and it must never fail on the screen where
a visitor has just answered thirty-odd questions. The optional LLM touches only the coach note.

What it decides, in order:

1. **Gentle start.** Pain (from the safety check or from painful sex), a birth in the last year,
   prostate treatment in the last three months or pelvic surgery forces the Foundation level,
   and pain additionally switches the whole week to relax-first sessions capped at five minutes.
2. **Level.** Otherwise experience, the feel test, the squeeze rating and the hold test score
   into Foundation, Builder or Strong, each with fixed hold, rest, rep and quick-squeeze counts.
3. **Daily drill** for the stated goal, overridden by an urge drill when urgency is frequent (and
   the goal is not erections or control) and by a down-training breath when there is pain.
4. **Safety note** whenever an answer warrants a clinician first, and **lifestyle notes** that
   are general, well-established observations rather than promises.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any

DAY_NAMES: tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                              "Saturday", "Sunday")

OFTEN = frozenset({"often", "most"})

GOAL_NAMES: dict[str, str] = {
    "bladder": "Bladder control", "ejac": "Ejaculation control", "erection": "Erection support",
    "both": "Erections and control", "leaks": "Fewer leaks", "birth": "Post-birth recovery",
    "intimacy": "Sensation & comfort", "menopause": "Menopause support",
    "general": "Overall strength",
}

LEVELS: tuple[dict[str, Any], ...] = (
    {"name": "Foundation", "hold": 3, "rest": 6, "reps": 8, "quick": 5},
    {"name": "Builder", "hold": 5, "rest": 5, "reps": 10, "quick": 10},
    {"name": "Strong", "hold": 8, "rest": 8, "reps": 10, "quick": 15},
)
RELAX_FIRST: dict[str, Any] = {"name": "Relax first", "hold": 2, "rest": 10, "reps": 6, "quick": 0}

DRILLS: dict[str, tuple[str, str]] = {
    "bladder": ("After-drip squeeze", "After urinating, wait a moment, then give one strong squeeze "
                "and lift to clear the last drops."),
    "ejac": ("Control hold", "One long hold at about half effort for as long as you can keep "
             "breathing easily, then a slow 5-second release."),
    "erection": ("Endurance set", "Three holds of twice your normal length at about 70% effort, "
                 "with a full relax between each."),
    "both": ("Control + endurance", "Two long holds at 70% effort, then one half-effort hold "
             "released over a slow count of five."),
    "leaks": ("The knack", "Squeeze and lift just before you cough, sneeze or pick something up. "
              "Practise it on three fake coughs."),
    "birth": ("Breath connection", "Breathe in and let the floor drop; breathe out and gently "
              "lift. Ten slow breaths, no forcing."),
    "intimacy": ("Awareness squeeze", "Slow squeeze, notice where you feel it, then a deliberate "
                 "slow release. Five reps, eyes closed."),
    "menopause": ("Endurance set", "Three holds of twice your normal length at about 70% effort, "
                  "with a full relax between each."),
    "general": ("Elevator lift", "Lift in three steps (a third, two thirds, full), then lower in "
                "three steps. Three reps."),
}
URGE_DRILL = ("Urge pause", "When an urge hits: stand still, five quick squeezes, slow breath, "
              "then walk calmly to the toilet.")
PAIN_DRILL = ("Down-training breath", "Lying down, knees bent: breathe in deeply and let the floor "
              "soften and drop. Eight breaths, before anything else.")

WHEN_LABELS: dict[str, str] = {
    "morning": "right after you wake up", "commute": "on your commute", "desk": "at your desk",
    "evening": "in bed before sleep",
}

#: Weeks 2-4 stay locked behind the trial, so the strings say what changes, honestly, and no more.
WEEK_ARCS: tuple[dict[str, Any], ...] = (
    {"week": 1, "label": "Find and baseline",
     "arc": "Everything is a first pass: find the muscle, learn the release, and set a baseline "
            "hold on day 7."},
    {"week": 2, "label": "Longer holds",
     "arc": "Starts from your day-7 hold time and adds about a second a week."},
    {"week": 3, "label": "Real-life positions",
     "arc": "Standing and moving sets, with your daily drill built into everyday moments."},
    {"week": 4, "label": "Make it automatic",
     "arc": "Fewer reminders, a maintenance routine, and a re-test against day 1."},
)


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """Validated quiz answers reduced to the fields the builder uses."""

    track: str = "men"
    age: str = "30"
    goal: str = "general"
    tried: str = "never"
    feel: str = "some"
    strength: int = 2
    hold: str = "2"
    safety: frozenset[str] = frozenset({"none"})
    minutes: int = 5
    when: str = "morning"
    activity: int = 3
    sitting: str = "4to8"
    stress: int = 3
    diet: int = 3
    smoke: str = "no"
    alcohol: str = "rarely"
    # men
    morning: str = ""
    erect_since: str = ""
    urge_m: str = ""
    prostate: str = ""
    # women
    urge: str = ""
    night: str = ""
    births: str = ""
    birth_when: str = ""
    heaviness: str = ""
    sexpain: str = ""
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def women(self) -> bool:
        return self.track == "women"

    @property
    def flags(self) -> frozenset[str]:
        return frozenset(self.safety) - {"none"}

    @property
    def pain(self) -> bool:
        return "pain" in self.flags or self.sexpain in OFTEN

    @property
    def gentle(self) -> bool:
        return (self.pain or self.birth_when in ("under6w", "under1y")
                or self.prostate == "recent" or "surgery" in self.flags)

    def signature(self) -> str:
        """Stable short hash of the inputs, shown in the plan footer as a receipt that the same
        answers always produce the same plan."""
        payload = json.dumps(asdict(self), sort_keys=True, default=sorted)
        return hashlib.sha256(payload.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class DayPlan:
    day: str
    title: str
    minutes: int
    steps: tuple[str, ...]
    is_rest: bool = False

    @property
    def short(self) -> str:
        return self.day[:3]


@dataclass(frozen=True)
class Plan:
    headline: str
    summary: str
    track: str
    focus: str
    level_name: str
    hold_seconds: int
    rest_seconds: int
    reps: int
    quick: int
    rounds: int
    minutes: int
    when_label: str
    drill_name: str
    drill_text: str
    days: tuple[DayPlan, ...]
    metrics: tuple[tuple[str, str, str], ...]
    safety_note: str
    tips: tuple[str, ...]
    week_arc: dict[str, Any]
    profile_signature: str
    source: str  # "builder" | "builder+llm"
    coach_note: str = ""

    def with_coach_note(self, note: str, *, source: str | None = None) -> "Plan":
        return replace(self, coach_note=note, source=source or self.source)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["days"] = [asdict(d) for d in self.days]
        return data


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def _level(profile: Profile) -> dict[str, Any]:
    if profile.pain:
        return RELAX_FIRST
    if profile.gentle:
        return LEVELS[0]
    score = {"never": 0, "unsure": 1, "sometimes": 1, "regular": 2}.get(profile.tried, 0)
    score += {"no": 0, "some": 1, "yes": 2}.get(profile.feel, 0)
    score += 2 if profile.strength >= 4 else 1 if profile.strength >= 3 else 0
    score += {"0": 0, "2": 0, "5": 1, "9": 2, "10": 3}.get(profile.hold, 0)
    return LEVELS[2] if score >= 6 else LEVELS[1] if score >= 3 else LEVELS[0]


def _drill(profile: Profile) -> tuple[str, str]:
    if profile.pain:
        return PAIN_DRILL
    urge = profile.urge if profile.women else profile.urge_m
    if urge in OFTEN and profile.goal not in ("erection", "ejac", "both"):
        return URGE_DRILL
    return DRILLS.get(profile.goal, DRILLS["general"])


def _safety_note(profile: Profile) -> str:
    flags = profile.flags
    needs = (flags or profile.births == "pregnant" or profile.birth_when == "under6w"
             or profile.prostate == "recent" or profile.heaviness in OFTEN or profile.pain)
    if not needs:
        return ""
    note = ("Based on your answers, please check with a doctor or pelvic health physiotherapist "
            "before you start, and stop any exercise that causes pain.")
    if "blood" in flags:
        note += " Blood in urine should always be checked by a doctor."
    if "burning" in flags:
        note += " Burning when you urinate can be an infection, which needs looking at first."
    if profile.heaviness in OFTEN:
        note += " A feeling of heaviness or bulging is worth an examination."
    if profile.births == "pregnant":
        note += (" Pelvic floor exercise is commonly recommended in pregnancy, but your midwife or "
                 "doctor should confirm what suits you.")
    return note


def _tips(profile: Profile) -> tuple[str, ...]:
    tips: list[str] = []
    if profile.smoke in ("yes", "sometimes"):
        tips.append("Smoking narrows blood vessels, including the ones erections and pelvic tissue "
                    "rely on, and a smoker's cough loads the pelvic floor. Cutting down helps more "
                    "than any exercise can.")
    if profile.alcohol in ("daily", "weekly_plus"):
        tips.append("Alcohol irritates the bladder and can blunt erections. Notice how you feel on "
                    "your lighter days.")
    if profile.diet <= 2:
        tips.append("Straining on the toilet is a big everyday load on the pelvic floor. Fibre and "
                    "water keep things moving.")
    if profile.sitting == "gt8":
        tips.append("You sit a lot, so your sitting set doubles as a movement break. Stand up "
                    "after it.")
    if profile.stress >= 4:
        tips.append("High stress often shows up as a tight pelvic floor. Your Thursday relax day "
                    "matters as much as the holds.")
    if profile.women and profile.night in ("2", "3"):
        tips.append("Getting up twice or more a night is worth mentioning to your doctor, "
                    "especially if it is new.")
    if (not profile.women and profile.morning in ("never", "rarely")
            and profile.erect_since == "lt1"):
        tips.append("Erection changes that are new can be an early sign of heart or blood-vessel "
                    "problems. A routine check-up is worth booking.")
    return tuple(tips)


def _metrics(profile: Profile, level: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    rows = [
        ("Sessions done", "7 of 7 days, short is fine",
         "Daily beats long. Week 1 only asks whether the plan fits your day."),
        ("Longest comfortable hold", f"Timed on day 7, starting from {level['hold']} seconds",
         "The number weeks 2 to 4 are built from."),
        ("Squeeze rating", "1 to 5, re-rated on day 7", "A felt measure, compared with your own day 1."),
    ]
    if profile.goal in ("leaks", "bladder") or profile.urge in OFTEN:
        rows.append(("Leaks or urgent trips", "A tally, no target yet",
                     "Week 1 measures before the plan asks anything of it."))
    return tuple(rows)


def build_plan(profile: Profile) -> Plan:
    """Pure function: the same `Profile` always produces the same `Plan`."""
    level = _level(profile)
    pain = profile.pain
    minutes = min(profile.minutes, 5) if pain else profile.minutes
    rounds = 2 if minutes >= 10 else 1
    drill_name, drill_text = _drill(profile)
    focus = GOAL_NAMES.get(profile.goal, GOAL_NAMES["general"])
    when_label = WHEN_LABELS.get(profile.when, "at a time you pick")
    standing = profile.activity >= 4

    set_text = (f"{rounds} round{'s' if rounds > 1 else ''}: {level['reps']} slow holds "
                f"({level['hold']}s squeeze, {level['rest']}s full relax)"
                + (f" + {level['quick']} quick squeezes" if level["quick"] else ""))
    drill_line = f"{drill_name}: {drill_text}"
    position = "Stand tall, knees soft." if standing else "Sit upright, feet flat."

    specs = [
        ("Find the muscle", 4, ("Lie on your back, knees bent.",
                                "Squeeze as if holding in wind, lift, let go. 5 gentle tries.",
                                "Notice: belly, thighs and buttocks stay soft."), False),
        ("Foundation set, lying down", minutes, ("Lie down, knees bent.", set_text,
                                                  "Relax completely between every rep."), False),
        ("Foundation set + daily drill", minutes, ("Lying down.", set_text, drill_line), False),
        ("Relax and reset", 3, ("Lying down, slow breathing.",
                                "Breathe in, let the floor drop; breathe out, soften further. "
                                "10 breaths.", "Light day: no holds."), True),
        ("Sitting set + daily drill", minutes, ("Sit upright, feet flat.", set_text, drill_line),
         False),
        (("Standing" if standing else "Sitting") + " set + daily drill", minutes,
         (position, set_text, drill_line), False),
        ("Check-in", 4, ("Lie down. Time your longest comfortable hold.",
                         "Rate the squeeze 1 to 5 again.", "This sets where week 2 starts."), False),
    ]
    days: list[DayPlan] = []
    for name, (title, mins, steps, is_rest) in zip(DAY_NAMES, specs, strict=True):
        if pain and name not in ("Thursday", "Sunday"):
            title = "Relax-first session"
            mins = min(mins, 5)
            steps = ("Lying down, knees bent.", drill_line,
                     f"Only if comfortable: {level['reps']} very light 2s squeezes, 10s release.")
        days.append(DayPlan(day=name, title=title, minutes=mins, steps=steps, is_rest=is_rest))

    who = "women" if profile.women else "men"
    headline = f"A {minutes}-minute daily kegel plan for {focus.lower()}"
    summary = (f"Built for {who} from your answers. Starting level {level['name']}, holds of "
               f"{level['hold']} seconds, done {when_label}. Daily drill: {drill_name.lower()}.")

    return Plan(
        headline=headline, summary=summary, track=profile.track, focus=focus,
        level_name=level["name"], hold_seconds=level["hold"], rest_seconds=level["rest"],
        reps=level["reps"], quick=level["quick"], rounds=rounds, minutes=minutes,
        when_label=when_label, drill_name=drill_name, drill_text=drill_text, days=tuple(days),
        metrics=_metrics(profile, level), safety_note=_safety_note(profile), tips=_tips(profile),
        week_arc=WEEK_ARCS[0], profile_signature=profile.signature(), source="builder",
    )


def audit_tables() -> list[str]:
    """Integrity of the builder's own tables, for `/health`: every goal option reaches a drill and
    a display name, every level is internally sane, and a pain answer always yields relax-first."""
    from quiz import QUESTION_BY_KEY  # local: quiz imports nothing from here, keep it that way

    problems: list[str] = []
    for key in ("goal_men", "goal_women"):
        for value, _label in QUESTION_BY_KEY[key].options:
            if value not in DRILLS:
                problems.append(f"{key}={value} has no daily drill")
            if value not in GOAL_NAMES:
                problems.append(f"{key}={value} has no display name")
    for level in LEVELS + (RELAX_FIRST,):
        if level["rest"] < level["hold"]:
            problems.append(f"level {level['name']} rests less than it holds")
    plan = build_plan(Profile(safety=frozenset({"pain"}), minutes=10))
    if plan.level_name != RELAX_FIRST["name"] or plan.minutes > 5:
        problems.append("pain did not produce a relax-first plan capped at 5 minutes")
    return problems
