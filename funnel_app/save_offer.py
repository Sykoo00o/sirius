"""The post-cancel save-offer sequence, and the states a save offer can put you in.

Sequence, from `LANE_C_PHASE0_STEP1-2.md` §2.3, in the only order that is not obstruction:

    [Account] -> "Cancel subscription" (one click)
       CANCEL IS NOW DONE. State = cancelled_at_end_of_period, shown on screen.
    [Save-offer screen]   skippable; "No thanks" is already true
       A. Pause 30 days        B. Downgrade to $19/mo
       C. 50% off for 2 months  D. No thanks, keep me cancelled
    [2-question survey]   optional, never blocking
    [Confirmation email]

Why this is a module rather than branches in `main.py`: the enforceable part of the spec is
the *ordering*, and ordering is only enforceable if one place owns it. Rule 1 is "action
precedes persuasion", so `routes.account_cancel` calls `billing.cancel_now` and renders
`CANCELLED` before any offer text is chosen. Every option here is an **opt-back-in** against a
baseline of "cancelled". Nothing on this screen can leave the customer subscribed by failing to
act, which is the double-negative pattern rule 2 bans ("Keep saving me money" as the
affirmative button for staying).

Two rules constrain what may appear on the screen at all:

  * **Rule 4, one reachable state per offer.** Option B must be a tier we would sell cold, and
    B and C must be reachable together, since the customer is entitled to combine the two saves
    they accepted rather than being told to cancel twice for the second one.
  * **Rule 3, one offer per cancel event.** The four options are a *menu* presented once. This
    module deliberately exposes no "next offer" concept: a maze is what the corpus calls a Roach
    Motel, and a maze is easy to add by accident.

Rule 6, instrumentation: `OUTCOME_EVENTS` is the closed vocabulary that `BillingStore.log_event`
accepts. `SAVE` names are mapped to those event names here, so a screen edit that renames an
option cannot invent a fifth event name and quietly split the cohort.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from config import FunnelSettings
from funnel_copy import Copy

#: The four options, in spec order. A/B/C are conditional on their config being real.
PAUSE = "pause"
DOWNGRADE = "downgrade"
DISCOUNT = "discount"
KEEP_CANCELLED = "keep_cancelled"

#: Option -> the stable dotted event name (§2.3 rule 6). Closed set: anything outside it is a
#: bug at the call site, and `BillingStore.log_event` raises rather than storing a typo.
OUTCOME_EVENTS: dict[str, str] = {
    PAUSE: "pause_selected",
    DOWNGRADE: "downgrade_selected",
    DISCOUNT: "discount_selected",
    KEEP_CANCELLED: "cancel_confirmed",
}

#: Accepting any of these means billing resumes, so the period-end cancel must be reversed.
REVVERSIBLE: tuple[str, ...] = (PAUSE, DOWNGRADE, DISCOUNT)


@dataclass(frozen=True)
class Offer:
    """One menu entry, as displayed. `verb` is what the button says, so a template cannot
    invent a persuasive synonym for a state change."""

    key: str
    title: str
    detail: str
    verb: str


@dataclass(frozen=True)
class OfferMenu:
    """What the save-offer screen may show, decided by config rather than by optimists."""

    offers: tuple[Offer, ...]

    @property
    def any_live(self) -> bool:
        return bool(self.offers)

    def get(self, key: str) -> Offer | None:
        return next((offer for offer in self.offers if offer.key == key), None)

    def keys(self) -> tuple[str, ...]:
        return tuple(offer.key for offer in self.offers)


def offer_menu(settings: FunnelSettings, copy: Copy, *,
               today: date | None = None) -> OfferMenu:
    """Build the menu from what is actually configured, in spec order.

    An option whose processor object is missing does not appear, rather than appearing and
    failing on click. A "50% off" that Stripe rejects after acceptance is worse than no offer:
    the customer believes they saved money, and the invoice says otherwise.
    """
    offers: list[Offer] = []
    if not settings.save_offer_enabled:
        return OfferMenu(())

    offers.append(Offer(
        key=PAUSE,
        title=f"Pause for {settings.pause_days} days",
        detail=(
            "Nothing is charged while paused, and it restarts by itself, so the plan is still "
            f"here when you are back. You keep access to {copy.settings.brand_name} in the "
            "meantime."),
        verb=f"Pause for {settings.pause_days} days",
    ))

    offers.append(Offer(
        key=DOWNGRADE,
        title=f"Switch to the {copy.lite_per_month} lite plan",
        detail=(
            "Same four-week plan and the same exports, without the weekly adjustments or the "
            "new cycle written from your own month of data. Cheaper, and still something we "
            "would sell you cold."),
        verb=f"Switch to {copy.lite_per_month}",
    ))

    if settings.discount_coupon_id:
        # Half of the *full* price, addressed as "the full plan", because this menu is
        # presented after a cancel and the cancel path does not switch tiers on its own. A
        # member already on lite taking this offer keeps their tier at half of lite; see
        # `accepted_outcome` and `BillingService._lite_discount_price`.
        offers.append(Offer(
            key=DISCOUNT,
            title=f"Stay on the full plan at {copy.half_price}/month",
            detail=(
                "Half price for two months, applied to your next invoice. After that it goes "
                f"back to {copy.rebill_per_month}, and you can cancel again in one click."),
            verb=f"Take {copy.half_price} for 2 months",
        ))

    return OfferMenu(tuple(offers))


@dataclass(frozen=True)
class Outcome:
    """What accepting an option means, in words the confirmation screen can print verbatim.

    The screen states the new amount and date rather than "you're all set", because a save
    offer the customer cannot restate after taking it is an offer they will dispute later.
    """

    event: str
    headline: str
    detail: str
    #: Fields to write to the subscription row. Owned here so the caller cannot apply half an
    #: outcome, e.g. logging `downgrade_selected` while the row still says "full".
    fields: dict[str, object]


def accepted_outcome(key: str, copy: Copy, settings: FunnelSettings, *,
                     renewal_date: str, resumed: bool,
                     already_lite: bool = False,
                     paused_until: str = "") -> Outcome:
    """Describe an accepted offer, given what the subscription already is.

    `already_lite` and `already_discounted` carry the prior saves, which is how rule 4 holds:
    taking the discount while on lite keeps you on lite at half the lite price rather than
    silently upgrading you back to the plan you just declined.
    """
    if key == PAUSE:
        return Outcome(
            event=OUTCOME_EVENTS[PAUSE],
            headline=f"Paused until {paused_until or 'you restart it'}.",
            detail=(
                "No charges while paused, and it resumes by itself. Your plan is untouched and "
                "stays exportable."),
            fields={"status": "paused", "paused_until": paused_until,
                    "save_offer_outcome": PAUSE},
        )
    if key == DOWNGRADE:
        return Outcome(
            event=OUTCOME_EVENTS[DOWNGRADE],
            headline=f"You are on the lite plan at {copy.lite_per_month}.",
            detail=f"{copy.next_bill_heading(plan='lite')} Cancel or switch back any time.",
            fields={"plan": "lite", "save_offer_outcome": DOWNGRADE},
        )
    if key == DISCOUNT:
        base = copy.lite_per_month if already_lite else copy.rebill_per_month
        shown = copy.half_lite_price if already_lite else copy.half_price
        return Outcome(
            event=OUTCOME_EVENTS[DISCOUNT],
            headline=f"Half price for two months: {shown}.",
            detail=(
                f"Applied to your next invoice. After two months it returns to {base}. "
                f"You stay on the {'lite' if already_lite else 'full'} plan."),
            # The tier is never mentioned in these fields, so a lite member who takes the
            # discount is not upgraded by a field they did not ask about (rule 4). The number
            # shown is half of what *this* member pays, which for a lite member is half of
            # lite; `BillingService._lite_discount_price` makes the invoice say the same.
            fields={"save_offer_outcome": DISCOUNT, "discount_months": 2},
        )
    return Outcome(
        event=OUTCOME_EVENTS[KEEP_CANCELLED],
        headline="You are cancelled.",
        detail=(
            f"Last charge was already paid; nothing more is coming. Access runs to "
            f"{renewal_date}."
            if not resumed else
            "You are cancelled again, same as before the offer."),
        fields={"status": "cancelled_at_period_end", "save_offer_outcome": KEEP_CANCELLED},
    )


# ---- survey ---------------------------------------------------------------

#: Question 1: why. Free choice, no leading scale, and the labels describe situations
#: rather than feelings, because "how satisfied are you" invites a rating rather than a
#: reason anyone can act on.
SURVEY_REASONS: tuple[tuple[str, str], ...] = (
    ("cost", "Cost, once the trial ended"),
    ("got_result", "I had what I needed from the plan"),
    ("no_time", "Life got busy and I could not run the week"),
    ("quality", "The plan was not what I wanted"),
    ("forgot", "I forgot it was running"),
    ("other", "Something else"),
)

#: Question 2: would a change have kept you. The free text is optional and the question
#: says so twice, because a blocked survey turns into a bounce and a bounce is a lost
#: response, not a refusal.
SURVEY_HEADLINE = "Would anything have made you stay?"


def survey_labels(reason: str) -> str:
    """Human label for a stored survey reason, or the raw value if it is off-menu."""
    return dict(SURVEY_REASONS).get(reason, reason)
