"""Ad telemetry evaluation: recommendations only.

`evaluate()` turns one ad's stats into one of:

  halt_all_scaling   the funnel itself tripped a kill metric; overrides everything below
  insufficient_data  below every rule's spend/impression/time floor
  recommend_winner   day-0 ROAS and purchases above the winner bar
  recommend_pause    thumbstop, outbound CTR or day-0 ROAS below its floor, past its floor
  keep               none of the above

Two Insights sources share one `rows()` contract: `FixtureInsightsSource` (an exported JSON file,
the default) and `GraphInsightsSource` (read-only `GET /act_{id}/insights` through `graph.py`, used
only with `--source api`). `UnarmedMetaClient` is the only place a pause or a
publish could happen, and in v1 it records an intent while dry-run and raises `NotBuilt` otherwise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
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


def evaluate(stats: AdStats, rules: AdRules, *, funnel_halt: str = "",
             funnel_verified: bool = True) -> Recommendation:
    """`funnel_halt` (a tripped dispute, fraud or prepaid ceiling) beats every rule. When the
    funnel's risk check could not run or was skipped (`funnel_verified=False`), a winner is
    withheld and reported as `keep`: scaling is never recommended on unverified risk."""
    def rec(action: str, reason: str) -> Recommendation:
        return Recommendation(stats.meta_ad_id, stats.window_end, action, reason)

    if funnel_halt:
        return rec(HALT, f"funnel kill metric: {funnel_halt}")
    result = _rules(stats, rules, rec)
    if result.action == WINNER and not funnel_verified:
        return rec(KEEP, f"winner withheld: funnel risk check not verified ({result.reason})")
    return result


def _rules(stats: AdStats, rules: AdRules, rec) -> Recommendation:

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


INSIGHTS_FIELDS = ("ad_id", "ad_name", "spend", "impressions", "actions", "action_values",
                   "outbound_clicks", "video_p50_watched_actions", "date_start", "date_stop")
#: Action types counted as a purchase, in preference order (pixel first, then the generic one).
PURCHASE_TYPES = ("offsite_conversion.fb_pixel_purchase", "purchase", "omni_purchase")
VARIANT_CODE = re.compile(r"[0-9a-f]{16}:[A-Za-z0-9_]+(?::[A-Za-z0-9_]+)?")


def _action(items: Any, *types: str) -> float:
    """First matching action type's value from a Graph `actions`-style list, else 0."""
    if not isinstance(items, list):
        return 0.0
    by_type = {str(i.get("action_type")): i.get("value") for i in items if isinstance(i, dict)}
    for action_type in types:
        if action_type in by_type:
            try:
                return float(by_type[action_type] or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def stats_from_insight(row: dict[str, Any]) -> AdStats:
    """One `level=ad` Insights row -> AdStats.

    Money arrives in the account currency's major units as a string; stored as minor units. The
    variant code is read from the ad *name*, so name each ad with its variant code when uploading.
    `hours_live` is the reporting window's length (date_start..date_stop inclusive), an upper bound
    on time live inside the window, not the ad's age.
    """
    start, stop = str(row.get("date_start", "")), str(row.get("date_stop", ""))
    try:
        days = (datetime.fromisoformat(stop) - datetime.fromisoformat(start)).days + 1
    except ValueError:
        days = 0
    name = str(row.get("ad_name", ""))
    match = VARIANT_CODE.search(name)
    return AdStats.from_dict({
        "meta_ad_id": str(row.get("ad_id", "")),
        "window_end": stop,
        "variant_code": match.group(0) if match else "",
        "spend_minor": round(float(row.get("spend") or 0) * 100),
        "impressions": int(float(row.get("impressions") or 0)),
        "video_3s_views": int(_action(row.get("actions"), "video_view")),
        "video_p50_views": int(_action(row.get("video_p50_watched_actions"), "video_view")),
        "outbound_clicks": int(_action(row.get("outbound_clicks"), "outbound_click")),
        "purchases": int(_action(row.get("actions"), *PURCHASE_TYPES)),
        "revenue_minor": round(_action(row.get("action_values"), *PURCHASE_TYPES) * 100),
        "hours_live": max(0, days) * 24,
    })


class GraphInsightsSource:
    """Read-only `GET /act_{id}/insights` at ad level over one date window, same `rows()` contract
    as `FixtureInsightsSource`. Rows without an ad id are dropped rather than guessed."""

    def __init__(self, reader, account_id: str, *, since: str, until: str) -> None:
        account = account_id.strip()
        account = account if account.startswith("act_") else f"act_{account}"
        if not re.fullmatch(r"act_\d+", account):
            raise ValueError("META_AD_ACCOUNT_ID must be numeric (with or without act_)")
        for value in (since, until):
            datetime.strptime(value, "%Y-%m-%d")
        self.reader, self.account, self.since, self.until = reader, account, since, until

    def rows(self) -> list[AdStats]:
        params = {"level": "ad", "fields": ",".join(INSIGHTS_FIELDS),
                  "time_range": json.dumps({"since": self.since, "until": self.until}),
                  "limit": "200"}
        out = []
        for row in self.reader.paged(f"{self.account}/insights", params):
            if row.get("ad_id") and row.get("date_stop"):
                out.append(stats_from_insight(row))
        return out


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
