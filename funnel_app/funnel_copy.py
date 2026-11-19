"""funnel_copy: all funnel wording, in one module, with prices interpolated from config.

Why code and not Jinja: the Phase 0 disclosure checklist has ten numbered slots, and
the failure mode it is defending against is a stale number in one slot after a price
change. A template string typed in three places is exactly how that happens. Here,
every money amount comes from `FunnelSettings`, so there is one place a price exists,
and `claims.disclosure_lines` can assert the required lines actually render.

Copy rules for this file: routine structure, meal organisation, consistency. No
diagnosis, no treatment, no body-composition promise. `tests/test_copy_guardrails.py`
scans every string in this module against `claims.FORBIDDEN_TERMS`, so a well-meant
edit that adds "guaranteed results" fails locally instead of on a landing page.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

import claims
from config import FunnelSettings

#: Product truth: what the trial actually unlocks. Kept as data so the paywall can be
#: diffed against it, which is the control the trap table prescribes for copy drift.
ARTIFACT_FREE = (
    "Week 1, day by day: session structure, meal slots, and three check-in metrics",
    "The fallback rule for the blocker you told us about",
    "A copy of your answers and the reasoning behind the week's layout",
)

ARTIFACT_PAID = (
    "Weeks 2, 3 and 4, with the load and session swaps written from week 1's log",
    "Export to PDF and plain text for the fridge door",
    "Weekly adjustments when a week goes sideways, which is when it matters",
    "A new four-week cycle written from four weeks of your own data",
)

TRUST_POINTS = (
    ("Cancel in one click",
     "One button on your account page. No phone call, no email hoops, no cancellation "
     "fee, no retention chat."),
    ("You see the recurring price before you pay",
     "On the same screen as the payment button, not in terms you have to go looking for."),
    ("Refund window on the trial charge",
     "Ask inside the window and the trial charge comes back. No form, no interrogation."),
    ("Your answers build your plan",
     "Twelve questions, and every one of them changes something in the output. Nothing "
     "here is a fake progress bar."),
)


def money(minor: int, currency: str) -> str:
    """Whole-dollar phrasing. Minor units stay the storage format; this is only display."""
    amount = minor / 100
    symbol = "$" if currency == "usd" else f"{currency.upper()} "
    return f"{symbol}{amount:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Copy:
    """Rendered copy for one settings snapshot."""

    settings: FunnelSettings
    trial_amount: str
    rebill_per_month: str
    refund_days: int
    trial_days: int
    today: str
    renewal_date: str

    # ---- landing -----------------------------------------------------------

    @property
    def hero_headline(self) -> str:
        return "A four-week routine that fits the week you actually have"

    @property
    def hero_sub(self) -> str:
        return (
            "Answer twelve questions about your schedule, your food and the thing that "
            "usually breaks your streak. We build the first week around your answers and "
            f"show it to you before you pay anything. Then {self.trial_amount} gets you "
            f"weeks 2 to 4, exports and weekly adjustments, which is the part that keeps "
            "a routine alive past month one."
        )

    @property
    def primary_cta(self) -> str:
        return "Build your 4-week routine plan"

    @property
    def cta_fine_print(self) -> str:
        return (
            f"Takes about 4 minutes. {self.trial_amount} for the first {self.trial_days} "
            f"days, then {self.rebill_per_month} until you cancel. See week 1 free first."
        )

    @property
    def how_it_works(self) -> tuple[tuple[str, str, str], ...]:
        return (
            ("1", "Twelve questions",
             "Goal, days you can protect, session length that is realistic, how you eat, "
             "what you can get to, sleep, and the blocker. Four minutes, no account."),
            ("2", "Week 1, free, before any card",
             "Day by day: the session structure, the meal slots, the metrics to log, and "
             "the fallback rule. Real output, not a teaser image."),
            ("3", "Weeks 2 to 4, if week 1 was worth something",
             f"{self.trial_amount} for {self.trial_days} days of the full plan, exports "
             "and weekly adjustments. Then "
             f"{self.rebill_per_month} a month, and one click ends it."),
        )

    # ---- checkout ----------------------------------------------------------

    @property
    def disclosures(self) -> dict[str, str]:
        """The four mandated sentences, generated in `claims`, keyed by slot name.

        Rendered from data rather than written into a template so that changing a price
        in `.env` moves the checkout card, the badge, the refund page and the tests in
        the same operation. Templates iterate this dict; nothing else writes these
        sentences.
        """
        slots = claims.Slots(
            trial_amount=self.trial_amount,
            rebill_per_month=self.rebill_per_month,
            trial_days=self.trial_days,
            refund_days=self.refund_days,
            renewal_date=self.renewal_date,
        )
        return {key: text for key, text, _where in claims.rendered_disclosures(slots)}

    def disclosure(self, slot: str) -> str:
        return self.disclosures[slot]

    @property
    def renewal_after_line(self) -> str:
        """Consequence sentence that follows the mandated renewal line."""
        return (
            "Change your mind and it is one click from your account page, any time "
            "before then."
        )

    @property
    def disclaimer(self) -> str:
        """The one-line framing note that sits in every footer. Exact text is listed in
        `claims.DISCLAIMER_EXEMPTIONS`, since it is the sentence whose job is to say what
        the plan is not."""
        return (
            "Routine structure and meal organisation only. Not medical advice, not a "
            "diagnosis, and no body-composition promise. See the terms page."
        )

    @property
    def card_stub_note(self) -> str:
        return (
            "Phase 1 rehearsal: no card is charged on this screen and nothing is stored. "
            "This funnel is not taking money yet."
        )

    # ---- legal -------------------------------------------------------------

    def policy_pages(self) -> dict[str, tuple[str, tuple[str, ...]]]:
        """Terms, privacy and refund copy. One source, and the refund page is the text
        any badge must match (checklist slot 8: badge text equals the linked policy)."""
        return {
            "terms": (
                "Terms",
                (
                    f"{self.settings.brand_name} sells a routine-planning tool. You give "
                    "us information about your schedule, preferences and habits; we return "
                    "a suggested structure for four weeks.",
                    "The plan is a planning aid for organising your own week. It is not "
                    "medical advice, it is not a diagnosis, and it does not treat or manage "
                    "any condition. If you are pregnant, managing a health condition, or "
                    "recovering from something, talk to a clinician about what to do before "
                    "starting a new routine.",
                    f"The trial costs {self.trial_amount} and lasts {self.trial_days} days. "
                    f"After that it renews at {self.rebill_per_month} per month until you "
                    "cancel. Billing runs from the date your first payment succeeds.",
                    "You can cancel at any time from your account page, with no "
                    "cancellation fee. Cancelling stops future charges; it does not remove "
                    "access to the days you already paid for.",
                    "You keep your plan. Whatever you generated while subscribed stays "
                    "exportable.",
                ),
            ),
            "privacy": (
                "Privacy",
                (
                    "We store what you tell us in the questionnaire: your goal, your "
                    "schedule, food preferences, equipment access, sleep pattern, the "
                    "blocker you named, and any note you typed. That is the whole input.",
                    # Whole line is an exemption in claims.DISCLAIMER_EXEMPTIONS: it is a
                    # negation, and the scanner matches per line. Reformat it there too if
                    # you reword it, or the scan starts failing on a correct sentence.
                    "We store no name, no address and no health history on this page, and we "
                    "ask for none. Payment details go to the payment processor and never to "
                    "us; we keep the last four digits and the card brand so a receipt is "
                    "recognisable.",
                    "A small signed cookie remembers your answers between the four pages so a "
                    "refresh does not lose your progress. It identifies your browser to us, "
                    "nothing more.",
                    "We use your responses to build your plan and, in aggregate, to see how "
                    "often people finish the questionnaire. We do not sell your answers.",
                    "Delete request: write to the address in the footer and the responses tied "
                    "to your visitor key are removed.",
                ),
            ),
            "refund": (
                "Refund policy",
                (
                    f"Trial charge: request a refund within {self.refund_days} days of the "
                    f"charge and the full {self.trial_amount} comes back. No form, no "
                    "explanation required.",
                    "Monthly charges: refundable within 14 days of the charge. After that, "
                    "cancelling stops the next one, which is why the cancel button is one "
                    "click and always visible.",
                    "Refunds go back to the original payment method and appear within the "
                    "processor's usual window, typically five to ten business days.",
                    "If a charge on your statement does not look like this funnel, ask before "
                    "escalating it. A refund is quicker for you and cheaper for everyone, and "
                    "we would rather issue one than argue about it.",
                ),
            ),
        }


def build_copy(settings: FunnelSettings, *, today: date | None = None) -> Copy:
    """Render copy for a settings snapshot.

    The renewal date is a display estimate anchored on the trial length. Phase 1 shows
    it because checklist slot 4 wants a renewal date reachable from checkout; the real
    anchor will be the processor's subscription period once charges exist, and that swap
    is one function here rather than a hunt through templates.
    """
    day = today or date.today()
    renewal = day + timedelta(days=settings.trial_days)
    # Day-level display, spelled out, so "renews about" cannot read as a precise promise
    # from a number that is an estimate.
    renewal_label = f"{calendar.day_name[renewal.weekday()]} {renewal.strftime('%d %B')}"
    return Copy(
        settings=settings,
        trial_amount=money(settings.trial_amount_minor, settings.currency),
        rebill_per_month=f"{money(settings.rebill_amount_minor, settings.currency)}/month",
        refund_days=settings.refund_window_days,
        trial_days=settings.trial_days,
        today=day.strftime("%d %B %Y"),
        renewal_date=renewal_label,
    )


def guardrail_scan(copy: Copy) -> list[tuple[str, str]]:
    """Scan every rendered string this module can emit. Used by the tests and by the
    `/health` view, so a copy edit is checked before it is argued about."""
    text = "\n".join(
        str(value)
        for name in dir(copy)
        if not name.startswith("_") and name != "settings"
        for value in ([getattr(copy, name)] if not isinstance(getattr(copy, name), (tuple, dict))
                      else getattr(copy, name))
    )
    text += "\n".join(str(line) for lines in copy.policy_pages().values() for line in lines[1])
    text += "\n".join(f"{a} {b}" for a, b in TRUST_POINTS)
    text += "\n".join(ARTIFACT_FREE + ARTIFACT_PAID)
    return claims.violations(text)


def disclosure_coverage(copy: Copy) -> list[tuple[str, bool, str]]:
    """Check that each mandated sentence the funnel *says* matches what `claims` says it
    must say. Returns ``(slot, present, expected)``.

    The renewal line is checked by prefix: its mandated sentence is the opening of a
    longer paragraph in the card, and asserting the whole paragraph here would mean two
    places own the trailing copy.
    """
    rendered = copy.disclosures
    slots = claims.Slots(
        trial_amount=copy.trial_amount, rebill_per_month=copy.rebill_per_month,
        trial_days=copy.trial_days, refund_days=copy.refund_days,
        renewal_date=copy.renewal_date,
    )
    rows: list[tuple[str, bool, str]] = []
    for key, expected, _where in claims.rendered_disclosures(slots):
        actual = rendered.get(key, "")
        if key == "renewal_line":
            present = actual.startswith(expected)
        else:
            present = actual == expected
        rows.append((key, present, expected))
    return rows
