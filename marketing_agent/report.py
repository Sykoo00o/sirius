"""Read-only ad x trial summary: Meta delivery joined to funnel outcomes, under the kill switch.

Join path: an ad's **name** carries its variant code (`meta.stats_from_insight`), the funnel's
`lp_view` carries the same code as `utm_content` (`telemetry.attribution_from_query`), and
`visitor_key` ties that landing view to the visitor's `trial_started` / `trial_paid` rows
(`scripts/export_lane_c_metrics.py --by-ad`). So per variant this reports what Meta says it spent
and delivered next to what the funnel says it earned, and `funnel_roas` is the funnel's own
captured revenue over Meta spend rather than Meta's pixel estimate.

The kill switch is applied to every row: if any day in the window tripped a dispute, fraud
(Radar early warning) or prepaid-share ceiling, every recommendation is `halt_all_scaling`; if the
funnel check could not run, winners are withheld. Nothing is saved and nothing is executed.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import funnel_metrics, meta

UNTAGGED = "(untagged)"


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def ad_report(*, settings, store, stats: list[meta.AdStats], since: str,
              until: str) -> dict[str, Any]:
    health = funnel_metrics.window_snapshot(settings, since, until)
    halt = "; ".join(health.reasons) if health.halt_scaling else ""
    attribution = funnel_metrics.by_ad(settings, since, until)
    funnel_rows = {ad["utm_content"]: ad for ad in attribution.get("ads", [])}

    delivery: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "meta_ad_ids": [], "spend_minor": 0, "impressions": 0, "outbound_clicks": 0,
        "meta_purchases": 0, "meta_revenue_minor": 0, "recommendations": []})
    unnamed = 0
    for row in stats:
        if not row.variant_code:
            unnamed += 1
        bucket = delivery[row.variant_code or UNTAGGED]
        bucket["meta_ad_ids"].append(row.meta_ad_id)
        bucket["spend_minor"] += row.spend_minor
        bucket["impressions"] += row.impressions
        bucket["outbound_clicks"] += row.outbound_clicks
        bucket["meta_purchases"] += row.purchases
        bucket["meta_revenue_minor"] += row.revenue_minor
        rec = meta.evaluate(row, settings.rules, funnel_halt=halt,
                            funnel_verified=health.available)
        bucket["recommendations"].append(rec.to_dict())

    variants = []
    for code in sorted(set(delivery) | set(funnel_rows), key=lambda c: (c == UNTAGGED, c)):
        d = delivery.get(code) or {"meta_ad_ids": [], "spend_minor": 0, "impressions": 0,
                                   "outbound_clicks": 0, "meta_purchases": 0,
                                   "meta_revenue_minor": 0, "recommendations": []}
        f = funnel_rows.get(code) or {}
        planned = store.variant(code) if code != UNTAGGED else None
        spend = d["spend_minor"]
        trials, paid = int(f.get("trial_started", 0)), int(f.get("trial_paid", 0))
        revenue = int(f.get("revenue_minor", 0))
        variants.append({
            "variant_code": code, "known_variant": planned is not None,
            "brief_id": planned["brief_id"] if planned else "",
            **{k: d[k] for k in ("meta_ad_ids", "spend_minor", "impressions", "outbound_clicks",
                                 "meta_purchases", "meta_revenue_minor")},
            "lp_view": int(f.get("lp_view", 0)), "quiz_completed": int(f.get("quiz_completed", 0)),
            "trial_started": trials, "trial_paid": paid, "funnel_revenue_minor": revenue,
            "refunds_count": int(f.get("refunds_count", 0)),
            "disputes_count": int(f.get("disputes_count", 0)),
            "cost_per_trial_minor": round(spend / trials) if trials else None,
            "cost_per_paid_minor": round(spend / paid) if paid else None,
            "funnel_roas": _ratio(revenue, spend),
            "recommendations": d["recommendations"],
        })
    return {
        "since": since, "until": until, "read_only": True,
        "halt_all_scaling": bool(halt), "halt_reasons": list(health.reasons) if halt else [],
        "funnel_verified": health.available,
        "funnel_note": "" if health.available else
        f"funnel risk check unavailable ({'; '.join(health.reasons)}): winners withheld",
        "attribution_available": bool(attribution.get("available")),
        "unattributed_events": attribution.get("unattributed_events", 0),
        "ads_without_variant_code": unnamed,
        "variants": variants,
    }
