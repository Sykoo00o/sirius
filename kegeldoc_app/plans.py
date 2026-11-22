"""The paywall's three plans: 1 week, 4 weeks, 12 weeks. There is no free plan.

Each plan is one up-front charge that covers its length, then the same monthly rebill until
cancelled. That is the shape the existing checkout already sells (an entry charge, a
`trial_end`, a recurring price), so a plan is expressed as a *settings variant*: the same
`FunnelSettings` with `trial_amount_minor` and `trial_days` swapped for the plan's. Every
disclosure, receipt, Stripe line item and renewal date then follows the chosen plan through the
code that already renders them, rather than through a second pricing path that could drift.

A stored subscription records its entry amount, not a plan key, so `for_amount` maps it back.
`audit` refuses two plans at one price for that reason, discounted prices included.

The limited-time discount is a second set of the same three plans with a lower entry amount and
`full_minor` holding the price it is struck through against. Whether a visitor gets it is
decided by `offer_active` from a server-side start time, never by the browser.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from config import FunnelSettings

DEFAULT_KEY = "4w"


@dataclass(frozen=True)
class PlanOption:
    key: str
    weeks: int
    name: str
    amount_minor: int
    days: int
    popular: bool = False
    #: The undiscounted entry price, when this option is the discounted one; 0 otherwise.
    full_minor: int = 0

    @property
    def discounted(self) -> bool:
        return self.full_minor > self.amount_minor

    def per_day_minor(self) -> float:
        return self.amount_minor / self.days


def discount_enabled(settings: FunnelSettings) -> bool:
    return 0 < settings.intro_discount_bps < 10000 and settings.intro_discount_minutes > 0


def discount_percent(settings: FunnelSettings) -> str:
    """"35.5" for 3550 bps, "40" for 4000."""
    return f"{settings.intro_discount_bps / 100:.2f}".rstrip("0").rstrip(".")


def discounted_minor(settings: FunnelSettings, amount_minor: int) -> int:
    return round(amount_minor * (10000 - settings.intro_discount_bps) / 10000)


def options(settings: FunnelSettings, *, discounted: bool = False) -> tuple[PlanOption, ...]:
    base = (
        PlanOption("1w", 1, "1-week plan", settings.trial_amount_minor, settings.trial_days),
        PlanOption("4w", 4, "4-week plan", settings.plan_4w_amount_minor, 28, popular=True),
        PlanOption("12w", 12, "12-week plan", settings.plan_12w_amount_minor, 84),
    )
    if not (discounted and discount_enabled(settings)):
        return base
    return tuple(replace(o, amount_minor=discounted_minor(settings, o.amount_minor),
                         full_minor=o.amount_minor) for o in base)


def find(settings: FunnelSettings, key: str | None, *,
         discounted: bool = False) -> PlanOption | None:
    return next((o for o in options(settings, discounted=discounted)
                 if o.key == (key or "").strip()), None)


def for_amount(settings: FunnelSettings, amount_minor: int) -> PlanOption:
    """The plan a stored subscription was sold, from its entry amount; 1-week if unknown."""
    every = options(settings) + options(settings, discounted=True)
    return next((o for o in every if o.amount_minor == int(amount_minor or 0)),
                options(settings)[0])


def offer_deadline(settings: FunnelSettings, started_epoch: float) -> float:
    return started_epoch + settings.intro_discount_minutes * 60


def offer_active(settings: FunnelSettings, started_epoch: float | None, now: float, *,
                 grace_seconds: float = 0) -> bool:
    """Whether the discount still applies. No start time means the paywall was never seen."""
    if not discount_enabled(settings) or started_epoch is None:
        return False
    return now < offer_deadline(settings, started_epoch) + grace_seconds


def settings_for(settings: FunnelSettings, option: PlanOption) -> FunnelSettings:
    return replace(settings, trial_amount_minor=option.amount_minor, trial_days=option.days)


def audit(settings: FunnelSettings) -> list[str]:
    problems = []
    amounts = [o.amount_minor for o in options(settings)]
    if discount_enabled(settings):
        amounts += [o.amount_minor for o in options(settings, discounted=True)]
    if len(set(amounts)) != len(amounts):
        problems.append("two plans share one price, so a stored subscription cannot name its plan")
    if any(a <= 0 for a in amounts):
        problems.append("every plan needs a price above zero: there is no free plan")
    return problems
