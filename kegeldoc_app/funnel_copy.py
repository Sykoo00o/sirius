"""funnel_copy: all funnel wording, in one module, with prices interpolated from config.

Why code and not Jinja: the Phase 0 disclosure checklist has ten numbered slots, and
the failure mode it is defending against is a stale number in one slot after a price
change. A template string typed in three places is exactly how that happens. Here,
every money amount comes from `FunnelSettings`, so there is one place a price exists,
and `claims.disclosure_lines` can assert the required lines actually render.

Copy rules for this file (kegeldoc fork): pelvic floor exercise guidance, session structure,
consistency. No diagnosis, no cure or treatment claim, no endorsement, no promised result. `tests/test_copy_guardrails.py`
scans every string in this module against `claims.FORBIDDEN_TERMS`, so a well-meant
edit that adds "guaranteed results" fails locally instead of on a landing page.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

import claims
from config import FunnelSettings

#: Product truth: what the trial actually unlocks. Kept as data so the paywall can be
#: diffed against it, which is the control the trap table prescribes for copy drift.
ARTIFACT_FREE = (
    "Week 1, day by day: every hold, rest and rep, from your own answers",
    "Your starting level, starting hold and the daily drill for your goal",
    "Safety notes and lifestyle notes drawn from what you told us",
)

ARTIFACT_PAID = (
    "Weeks 2, 3 and 4, with hold times stepped up from your day-7 check-in",
    "A guided timer that cues every squeeze and release",
    "Export to PDF and plain text",
    "A re-test at week 4 and a maintenance routine written from your own log",
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
     "Every question changes something in the output: your level, your hold time, your "
     "daily drill or a safety note. Nothing here is a fake progress bar."),
)


def money(minor: int, currency: str) -> str:
    """Whole-dollar phrasing. Minor units stay the storage format; this is only display."""
    amount = minor / 100
    symbol = "$" if currency == "usd" else f"{currency.upper()} "
    return f"{symbol}{amount:.2f}".rstrip("0").rstrip(".")


def human_date(day: date) -> str:
    """Spelled-out day, the one format the renewal disclosure is allowed to use."""
    return f"{calendar.day_name[day.weekday()]} {day.strftime('%d %B %Y')}"


def iso_to_local_date(iso: str) -> date | None:
    """Parse a stored ISO timestamp to a calendar date in local civil time.

    Stored in UTC, displayed in the browser's/visitor's day, because a customer's "the 8th"
    is their day and not ours. Returns None on anything unparseable rather than raising: the
    caller's alternative is to fall back to the estimated date, and an account page that 500s
    because a timestamp arrived in an unexpected shape is worse than one that says "about".
    """
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().date()


def epoch_to_local_date(seconds: int) -> date | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(int(seconds), timezone.utc).astimezone().date()


def next_bill_date(period_end: date | None, trial_days: int, *,
                   today: date | None = None) -> tuple[date, bool]:
    """The date to show as "next billing", and whether it is the processor's real date.

    ``(real_period_end or estimated)``, with the boolean saying which. Two states rather
    than one because the funnel has to display a renewal date before the processor has
    answered (checkout, before a session exists), and checklist slot 4 does not accept
    "we will tell you later". The caller must not present the estimated date as the real
    one, so templates qualify it, and `/health` fails the account-page check if a
    real-backed date ever renders as an estimate.
    """
    if period_end is not None:
        return period_end, True
    day = today or date.today()
    return day + timedelta(days=trial_days), False


@dataclass(frozen=True)
class Copy:
    """Rendered copy for one settings snapshot.

    Immutable, and `for_renewal_date` derives a variant rather than mutating: the renewal
    date is per-subscription while every other field is per-settings, and a mutated shared
    `Copy` would leak one visitor's renewal date into the next visitor's landing page under
    a threadpool.
    """

    settings: FunnelSettings
    trial_amount: str
    rebill_per_month: str
    refund_days: int
    trial_days: int
    today: str
    renewal_date: str
    #: True when `renewal_date` comes from the processor's period end, false when it is the
    #: checkout-time estimate. Templates must not claim precision the boolean denies.
    renewal_is_real: bool = False

    def for_renewal_date(self, renewal_date: str, *, is_real: bool = True) -> "Copy":
        """A copy object whose renewal sentences name a specific date.

        Used by the receipt email, the dashboard and the renewal notice, all of which know
        the actual date and none of which should re-estimate it from today.
        """
        return replace(self, renewal_date=renewal_date, renewal_is_real=is_real)

    def renewal_warning(self) -> str:
        """The next-charge sentence, for surfaces *after* the trial payment.

        The mandated `renewal_line` speaks from checkout, where the trial charge has not
        happened yet. Repeated verbatim on a receipt it contradicts the "Charged today: $1"
        line two rows above it, because the mandated amounts are the trial amount and the
        rebill amount in one sentence. Rather than edit a mandated disclosure, post-payment
        surfaces say the same fact in their own tense, with the amounts split across two
        dated rows so nothing reads as a third price.
        """
        return (
            f"That {self.trial_amount} was the whole cost until {self.renewal_date}. From "
            f"then it is {self.rebill_per_month} each month until you cancel, and cancelling "
            "is one click from your account page."
        )

    def slots(self) -> claims.Slots:
        """The disclosure inputs for this copy, one construction site.

        Existed as three near-identical `claims.Slots(...)` calls (here, in
        `disclosure_coverage`, and it would have been a fourth in the email layer), and each
        one was a place a new disclosure input could be forgotten. A missed field there is a
        disclosure rendered without its amount.
        """
        return claims.Slots(
            trial_amount=self.trial_amount,
            rebill_per_month=self.rebill_per_month,
            trial_days=self.trial_days,
            refund_days=self.refund_days,
            renewal_date=self.renewal_date,
        )

    def disclosure(self, slot: str) -> str:
        return self.disclosures[slot]

    # ---- landing -----------------------------------------------------------

    @property
    def hero_headline(self) -> str:
        return "Strengthen the muscles you never think about"

    @property
    def hero_sub(self) -> str:
        return (
            "Kegel plans for men and women. Answer a short quiz about your goal, what you "
            "notice now and how much time you have, and we write your first week of guided "
            f"pelvic floor sessions before you pay anything. Then {self.trial_amount} gets you "
            "weeks 2 to 4 and the guided timer."
        )

    @property
    def primary_cta(self) -> str:
        return "Get my plan"

    @property
    def cta_fine_print(self) -> str:
        return (
            f"About a 4-minute quiz. Plans from {self.trial_amount} for the first "
            f"{self.trial_days} days, then {self.rebill_per_month} until you cancel."
        )

    @property
    def how_it_works(self) -> tuple[tuple[str, str, str], ...]:
        return (
            ("1", "Answer a short quiz",
             "Your goal, what you notice now, whether you have tried kegels before, a quick "
             "squeeze test, and how much time you really have. No account."),
            ("2", "See your plan",
             "Seven days of sessions with exact holds, rests and reps, built from your "
             "answers, before you choose a plan."),
            ("3", "Pick 1, 4 or 12 weeks",
             f"From {self.trial_amount} for the first {self.trial_days} days of the full plan "
             f"and the guided timer. Then {self.rebill_per_month} a month, and one click ends "
             "it."),
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
        return {key: text for key, text, _where in claims.rendered_disclosures(self.slots())}

    @property
    def price_line(self) -> str:
        """Checklist 1's exact sentence for this copy's amounts and trial length.

        Delegates to `claims.price_line` rather than re-spelling it, so the checkout screen
        and the payment email cannot word it differently.
        """
        return claims.price_line(self.slots())

    @property
    def renewal_after_line(self) -> str:
        """Consequence sentence that follows the mandated renewal line."""
        return (
            "Change your mind and it is one click from your account page, any time "
            "before then."
        )

    # ---- checkout, live mode -----------------------------------------------

    @property
    def card_stub_note(self) -> str:
        return (
            "Phase 1 rehearsal: no card is charged on this screen and nothing is stored. "
            "This funnel is not taking money yet."
        )

    @property
    def pay_button_label(self) -> str:
        return f"Start my trial for {self.trial_amount}"

    @property
    def checkout_legal_note(self) -> str:
        """The agreement line under the button, live mode.

        Auto-renewal is on by default and *labelled* here, which is what checklist slot 3
        permits; the pattern ICPEN flagged was making auto-renewal impossible to decline,
        so the state is stated in words next to the button rather than left as a checkbox
        inference. Checkout is where the disclosure obligation sits, so the account page
        repeats the date and amount rather than repeating this sentence.
        """
        return (
            f"Auto-renews monthly at {self.rebill_per_month} after the {self.trial_days}-day "
            f"trial. By starting the trial you agree to the terms and confirm you have read "
            "the refund policy."
        )

    @property
    def disclaimer(self) -> str:
        """The one-line framing note that sits in every footer. Exact text is listed in
        `claims.DISCLAIMER_EXEMPTIONS`, since it is the sentence whose job is to say what
        the plan is not."""
        return (
            "Pelvic floor exercise guidance only. Not medical advice, not a diagnosis, and "
            "no promised result. See the terms page."
        )

    # ---- account and cancellation ------------------------------------------

    def next_bill_heading(self, *, plan: str = "full", discounted_months: int = 0,
                          discounted_price: str = "") -> str:
        """Plain statement of the next charge, for the account page headline slot.

        `discounted_price` is what the discount offer actually quoted this member (half of
        full, or half of lite). Left empty it falls back to `half_price`, and a caller that
        knows the member's tier should pass it: guessing the wrong one states a price on the
        account page that the next invoice will not match.
        """
        if discounted_months > 0:
            price = discounted_price or self.half_price
            base = self.lite_per_month if plan == "lite" else self.rebill_per_month
            return (
                f"Next charge: {price} for the next {discounted_months} months, then "
                f"{base} on {self.renewal_date}."
            )
        if plan == "lite":
            return f"Next charge: {self.lite_per_month} on {self.renewal_date}."
        return f"Next charge: {self.rebill_per_month} on {self.renewal_date}."

    @property
    def half_price(self) -> str:
        """Half the rebill, for save-offer C.

        Computed from the minor amount rather than typed, so a price change cannot leave a
        "50% off" badge quoting yesterday's discount. Rounded up to the cent: a customer
        shown "half price" and charged one cent under it is fine, one cent over is a
        complaint, and rounding up means we never charge more than we showed.
        """
        half = math.ceil(self.settings.rebill_amount_minor / 2)
        return money(half, self.settings.currency)

    @property
    def lite_per_month(self) -> str:
        return f"{money(self.settings.lite_amount_minor, self.settings.currency)}/month"

    @property
    def half_lite_price(self) -> str:
        """Half the lite price, for a lite member who takes the discount offer.

        Its own property rather than an argument on `half_price`, so a template cannot pass the
        wrong tier by position and quietly show a member a price the invoice will not match.
        """
        return money(math.ceil(self.settings.lite_amount_minor / 2), self.settings.currency)

    # ---- legal -------------------------------------------------------------

    def policy_pages(self) -> dict[str, tuple[str, tuple[str, ...]]]:
        """Terms, privacy and refund copy. One source, and the refund page is the text
        any badge must match (checklist slot 8: badge text equals the linked policy)."""
        return {
            "terms": (
                "Terms",
                (
                    f"{self.settings.brand_name} sells a pelvic floor exercise planner. You "
                    "answer questions about your goal, what you notice and your routine; we "
                    "return a suggested four-week kegel plan.",
                    "kegeldoc gives general pelvic floor exercise guidance for adults. It is not "
                    "medical advice, it is not a diagnosis, and it does not treat any condition. "
                    "If you have pain, bleeding, are pregnant, have recently given birth or had "
                    "pelvic surgery, speak to a doctor or pelvic health physiotherapist before "
                    "starting.",
                    "The name is a brand name. No doctor reviews individual plans, and nothing "
                    "on this site is endorsed by a medical body.",
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
                    "We store what you tell us in the questionnaire: your goal, what you notice, "
                    "your answers to the squeeze and hold tests, the safety check, your lifestyle "
                    "answers and your routine. Some of these answers are about your sexual and "
                    "bladder health, so they are treated as sensitive: they are used only to "
                    "build your plan and to count, in aggregate, how often people finish.",
                    # Whole line is an exemption in claims.DISCLAIMER_EXEMPTIONS.
                    "We store no name, no address and no medical record on this page, and we ask "
                    "for none. Payment details go to the payment processor and never to us; we "
                    "keep the last four digits and the card brand so a receipt is recognisable.",
                    "A small signed cookie remembers your answers between pages so a refresh "
                    "does not lose your progress. It identifies your browser to us, nothing more.",
                    "We do not sell your answers and we do not use them for advertising.",
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


def build_copy(settings: FunnelSettings, *, today: date | None = None,
               period_end_iso: str = "") -> Copy:
    """Render copy for one settings snapshot, and optionally for one subscription.

    `period_end_iso` is the processor's period end (or trial end, during a trial). With it,
    the renewal sentences carry the real date and `renewal_is_real` is True, which is what
    every post-payment screen must use. Without it, the date is the estimate today + trial
    length, and the copy says "about" because checklist slot 4 wants a renewal date
    reachable from checkout while honesty forbids presenting a guess as a promise.
    """
    day = today or date.today()
    real = iso_to_local_date(period_end_iso)
    renewal, is_real = next_bill_date(real, settings.trial_days, today=day)
    return Copy(
        settings=settings,
        trial_amount=money(settings.trial_amount_minor, settings.currency),
        rebill_per_month=f"{money(settings.rebill_amount_minor, settings.currency)}/month",
        refund_days=settings.refund_window_days,
        trial_days=settings.trial_days,
        today=day.strftime("%d %B %Y"),
        renewal_date=human_date(renewal),
        renewal_is_real=is_real,
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
    slots = copy.slots()
    rows: list[tuple[str, bool, str]] = []
    for key, expected, _where in claims.rendered_disclosures(slots):
        actual = rendered.get(key, "")
        if key == "renewal_line":
            present = actual.startswith(expected)
        else:
            present = actual == expected
        rows.append((key, present, expected))
    return rows
