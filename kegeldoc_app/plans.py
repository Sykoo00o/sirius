"""The paywall's three plans: 1 week, 4 weeks, 12 weeks. There is no free plan.

Each plan is one up-front charge that covers its length, then the same monthly rebill until
cancelled. That is the shape the existing checkout already sells (an entry charge, a
`trial_end`, a recurring price), so a plan is expressed as a *settings variant*: the same
`FunnelSettings` with `trial_amount_minor` and `trial_days` swapped for the plan's. Every
disclosure, receipt, Stripe line item and renewal date then follows the chosen plan through the
code that already renders them, rather than through a second pricing path that could drift.

A stored subscription records its entry amount, not a plan key, so `for_amount` maps it back.
`audit` refuses two plans at one price for that reason.
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

    def per_day_minor(self) -> float:
        return self.amount_minor / self.days


def options(settings: FunnelSettings) -> tuple[PlanOption, ...]:
    return (
        PlanOption("1w", 1, "1-week plan", settings.trial_amount_minor, settings.trial_days),
        PlanOption("4w", 4, "4-week plan", settings.plan_4w_amount_minor, 28, popular=True),
        PlanOption("12w", 12, "12-week plan", settings.plan_12w_amount_minor, 84),
    )


def find(settings: FunnelSettings, key: str | None) -> PlanOption | None:
    return next((o for o in options(settings) if o.key == (key or "").strip()), None)


def for_amount(settings: FunnelSettings, amount_minor: int) -> PlanOption:
    """The plan a stored subscription was sold, from its entry amount; 1-week if unknown."""
    return next((o for o in options(settings) if o.amount_minor == int(amount_minor or 0)),
                options(settings)[0])


def settings_for(settings: FunnelSettings, option: PlanOption) -> FunnelSettings:
    return replace(settings, trial_amount_minor=option.amount_minor, trial_days=option.days)


def audit(settings: FunnelSettings) -> list[str]:
    problems = []
    amounts = [o.amount_minor for o in options(settings)]
    if len(set(amounts)) != len(amounts):
        problems.append("two plans share one price, so a stored subscription cannot name its plan")
    if any(a <= 0 for a in amounts):
        problems.append("every plan needs a price above zero: there is no free plan")
    return problems
