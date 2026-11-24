"""Ad telemetry evaluation: recommendations only.

`evaluate()` turns one ad's stats into one of:

  halt_all_scaling   the funnel itself tripped a kill metric; overrides everything below
  insufficient_data  below every rule's spend/impression/time floor
  recommend_winner   day-0 ROAS and purchases above the winner bar
  recommend_pause    thumbstop, outbound CTR or day-0 ROAS below its floor, past its floor
  keep               none of the above

Nothing here calls Meta. `FixtureInsightsSource` reads an exported JSON file; a read-only Graph API
source can implement the same `rows()` later. `UnarmedMetaClient` is the only place a pause or a
publish could happen, and in v1 it records an intent while dry-run and raises `NotBuilt` otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AdRules, MarketingSettings

KEEP = "keep"
INSUFFICIENT = "insufficient_data"
PAUSE = "recommend_pause"
WINNER = "recommend_winner"
HALT = "halt_all_scaling"


class NotBuilt(RuntimeError):
    """Raised when an armed setting reaches a mutation this version does not implement."""


@dataclass(frozen=True)
class AdStats:
    meta_ad_id: str
    window_end: str
    spend_minor: int = 0
    impressions: int = 0
    video_3s_views: int = 0
    video_p50_views: int = 0
    outbound_clicks: int = 0
    purchases: int = 0
    revenue_minor: int = 0
    hours_live: int = 0
    variant_code: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AdStats":
        if not raw.get("meta_ad_id") or not raw.get("window_end"):
            raise ValueError("each row needs meta_ad_id and window_end")
        ints = ("spend_minor", "impressions", "video_3s_views", "video_p50_views",
                "outbound_clicks", "purchases", "revenue_minor", "hours_live")
        return cls(meta_ad_id=str(raw["meta_ad_id"]), window_end=str(raw["window_end"]),
                   variant_code=str(raw.get("variant_code", "")),
                   **{k: int(raw.get(k) or 0) for k in ints})

    @property
    def thumbstop_pct(self) -> float | None:
        return self.video_3s_views * 100 / self.impressions if self.impressions else None

    @property
    def hold_pct(self) -> float | None:
        return self.video_p50_views * 100 / self.impressions if self.impressions else None

    @property
    def outbound_ctr_pct(self) -> float | None:
        return self.outbound_clicks * 100 / self.impressions if self.impressions else None

    @property
    def day0_roas(self) -> float | None:
        return self.revenue_minor / self.spend_minor if self.spend_minor else None

    def to_row(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Recommendation:
    meta_ad_id: str
    window_end: str
    action: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


def evaluate(stats: AdStats, rules: AdRules, *, funnel_halt: str = "") -> Recommendation:
    def rec(action: str, reason: str) -> Recommendation:
        return Recommendation(stats.meta_ad_id, stats.window_end, action, reason)

    if funnel_halt:
        return rec(HALT, f"funnel kill metric: {funnel_halt}")

    roas = stats.day0_roas
    if (roas is not None and roas >= rules.winner_roas
            and stats.purchases >= rules.winner_min_purchases
            and stats.hours_live >= rules.roas_min_hours):
        return rec(WINNER, f"day-0 ROAS {roas:.2f} >= {rules.winner_roas} with "
                           f"{stats.purchases} purchases")
    thumb = stats.thumbstop_pct
    if (thumb is not None and stats.spend_minor >= rules.thumbstop_min_spend_minor
            and stats.impressions >= rules.thumbstop_min_impressions
            and stats.video_3s_views > 0 and thumb < rules.thumbstop_min_pct):
        return rec(PAUSE, f"LOW_THUMBSTOP {thumb:.1f}% < {rules.thumbstop_min_pct}%")
    ctr = stats.outbound_ctr_pct
    if (ctr is not None and stats.spend_minor >= rules.ctr_min_spend_minor
            and stats.impressions >= rules.ctr_min_impressions and ctr < rules.ctr_min_pct):
        return rec(PAUSE, f"LOW_INTENT_CLICK {ctr:.2f}% < {rules.ctr_min_pct}%")
    if (roas is not None and stats.spend_minor >= rules.roas_min_spend_minor
            and stats.hours_live >= rules.roas_min_hours and roas < rules.roas_floor):
        return rec(PAUSE, f"day-0 ROAS {roas:.2f} < {rules.roas_floor} after "
                          f"{stats.hours_live}h (check funnel checkout before acting)")
    floors = (stats.spend_minor < min(rules.thumbstop_min_spend_minor, rules.ctr_min_spend_minor)
              or stats.impressions < min(rules.thumbstop_min_impressions, rules.ctr_min_impressions))
    if floors:
        return rec(INSUFFICIENT, "below the spend/impression floor for every rule")
    return rec(KEEP, "no rule fired")


class FixtureInsightsSource:
    """Insights rows from an exported JSON file (a list of objects)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def rows(self) -> list[AdStats]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("insights file must be a JSON list")
        return [AdStats.from_dict(item) for item in data]


class UnarmedMetaClient:
    """The single mutation surface. Dry-run records an intent; armed raises, because v1 has no
    HTTP client for ad changes and arming must not silently do nothing."""

    def __init__(self, settings: MarketingSettings, store) -> None:
        self.settings = settings
        self.store = store

    def _intent(self, action: str, target: str, reason: str) -> str:
        self.store.log(event="marketing.intent_recorded", level="info",
                       message=f"{action} {target} not executed: dry-run or not armed",
                       context={"action": action, "target": target, "reason": reason,
                                "dry_run": self.settings.dry_run},
                       idempotency_key=f"intent:{action}:{target}:{reason[:60]}")
        return "intent_recorded"

    def pause_ad(self, meta_ad_id: str, reason: str) -> str:
        if not self.settings.can_pause_ads():
            return self._intent("pause_ad", meta_ad_id, reason)
        raise NotBuilt("pause_ad is armed but no Meta mutation client exists in v1")

    def publish(self, variant_code: str, reason: str = "") -> str:
        if not self.settings.can_publish():
            return self._intent("publish", variant_code, reason)
        raise NotBuilt("publish is armed but no Meta upload client exists in v1")
