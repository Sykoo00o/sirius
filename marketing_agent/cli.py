"""Command line for the marketing agent. Every command prints JSON.

    python -m marketing_agent status
    python -m marketing_agent brief draft [--niche N] [--concept NAME]
    python -m marketing_agent brief audit BRIEF_ID
    python -m marketing_agent brief approve BRIEF_ID --actor NAME
    python -m marketing_agent brief show BRIEF_ID
    python -m marketing_agent patterns ingest FILE [--min-days 30]
    python -m marketing_agent funnel-health [--day yesterday]
    python -m marketing_agent attribution [--since 7daysago] [--until today]
    python -m marketing_agent telemetry evaluate FILE [--day yesterday] [--skip-funnel]

Exit codes: 0 ok, 1 flagged / refused / error, 2 the funnel tripped a kill metric (same meaning as
the metrics script's exit 2).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import adspy, briefs, compliance, funnel_metrics, loader, meta
from .config import MarketingSettings
from .offer import load_offer
from .store import MarketingStore


def _print(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


class Agent:
    def __init__(self, settings: MarketingSettings) -> None:
        self.settings = settings
        self.store = MarketingStore(settings.db_path)
        self.claims = loader.claims_module(settings.app_dir)
        self.quiz = loader.quiz_module(settings.app_dir)
        self.offer = load_offer(settings.app_dir)

    def audit(self, payload: dict[str, Any]) -> compliance.AuditResult:
        return compliance.audit_brief(
            payload, quiz=self.quiz, claims=self.claims, offer=self.offer,
            references=self.store.reference_texts(payload.get("niche", "")))

    def draft(self, *, niche: str, concept: str) -> dict[str, Any]:
        payload = briefs.draft_brief(app=self.settings.app_dir.name, niche=niche, quiz=self.quiz,
                                     claims=self.claims, offer=self.offer, concept_name=concept)
        bid = briefs.brief_id(payload)
        created = self.store.save_brief(bid, payload)
        self.store.log(event="marketing.brief_drafted", message=f"brief {bid} drafted",
                       context={"brief_id": bid, "concept": concept},
                       idempotency_key=f"draft:{bid}")
        result = self.audit(payload)
        self.store.record_audit(bid, result.to_dict())
        self.store.log(event="marketing.brief_audited", level="info" if result.passed else "warning",
                       message=f"brief {bid} {result.status}",
                       context={"brief_id": bid, **result.to_dict()})
        return {"brief_id": bid, "created": created, "audit": result.to_dict()}

    def reaudit(self, bid: str) -> dict[str, Any]:
        brief = self.store.get_brief(bid)
        if brief is None:
            return {"error": "no such brief"}
        result = self.audit(brief["payload"])
        self.store.record_audit(bid, result.to_dict())
        return {"brief_id": bid, "audit": result.to_dict()}

    def approve(self, bid: str, actor: str) -> dict[str, Any]:
        ok, message = self.store.approve(bid, actor=actor)
        planned: list[str] = []
        urls: dict[str, str] = {}
        if ok:
            payload = self.store.get_brief(bid)["payload"]
            for plan in briefs.planned_variants(bid, payload):
                self.store.plan_variant(plan.variant_code, bid, plan.asset_type)
                planned.append(plan.variant_code)
                urls[plan.variant_code] = briefs.landing_url(payload, plan)
        return {"brief_id": bid, "approved": ok, "message": message,
                "variants_planned": planned, "landing_urls": urls, "rendered": False,
                "note": "rendering and upload are not built; variants are PLANNED only"}

    def attribution(self, *, since: str, until: str) -> dict[str, Any]:
        """Funnel counts per ad, each row marked with the brief that planned its variant code.

        A `utm_content` this agent never planned is kept and marked `known_variant: False`: it is
        a hand-tagged link or a typo, and either way the operator should see it, not lose it.
        """
        result = funnel_metrics.by_ad(self.settings, since, until)
        for ad in result.get("ads", []):
            variant = self.store.variant(ad["utm_content"])
            ad["known_variant"] = variant is not None
            ad["brief_id"] = variant["brief_id"] if variant else ""
            ad["asset_type"] = variant["asset_type"] if variant else ""
        return result

    def evaluate_telemetry(self, path: Path, *, day: str, skip_funnel: bool) -> dict[str, Any]:
        health = None if skip_funnel else funnel_metrics.snapshot(self.settings, day)
        halt = "; ".join(health.reasons) if health and health.halt_scaling else ""
        if halt:
            self.store.log(event="marketing.funnel_halt", level="warning",
                           message=f"funnel kill metric on {health.day}: {halt}",
                           context=health.to_dict(), idempotency_key=f"halt:{health.day}")
        client = meta.UnarmedMetaClient(self.settings, self.store)
        out = []
        for stats in meta.FixtureInsightsSource(path).rows():
            self.store.save_telemetry(stats.to_row())
            rec = meta.evaluate(stats, self.settings.rules, funnel_halt=halt)
            self.store.save_recommendation(meta_ad_id=rec.meta_ad_id, window_end=rec.window_end,
                                           action=rec.action, reason=rec.reason)
            executed = "none"
            if rec.action in (meta.PAUSE, meta.HALT):
                executed = client.pause_ad(rec.meta_ad_id, rec.reason)
            out.append({**rec.to_dict(), "executed": executed})
        return {"funnel": health.to_dict() if health else None, "halt": bool(halt),
                "recommendations": out, "dry_run": self.settings.dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketing_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    brief = sub.add_parser("brief").add_subparsers(dest="action", required=True)
    draft = brief.add_parser("draft")
    draft.add_argument("--niche", default="")
    draft.add_argument("--concept", default="quiz-preview-baseline")
    for name in ("audit", "show"):
        brief.add_parser(name).add_argument("brief_id")
    approve = brief.add_parser("approve")
    approve.add_argument("brief_id")
    approve.add_argument("--actor", required=True)
    patterns = sub.add_parser("patterns").add_subparsers(dest="action", required=True)
    ingest = patterns.add_parser("ingest")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--min-days", type=int, default=30)
    health = sub.add_parser("funnel-health")
    health.add_argument("--day", default="yesterday")
    attribution = sub.add_parser("attribution")
    attribution.add_argument("--since", default="7daysago")
    attribution.add_argument("--until", default="today")
    telemetry = sub.add_parser("telemetry").add_subparsers(dest="action", required=True)
    evaluate = telemetry.add_parser("evaluate")
    evaluate.add_argument("file", type=Path)
    evaluate.add_argument("--day", default="yesterday")
    evaluate.add_argument("--skip-funnel", action="store_true")
    args = parser.parse_args(argv)

    settings = MarketingSettings.from_env()
    if args.command == "status":
        _print(settings.describe())
        return 0
    if args.command == "funnel-health":
        result = funnel_metrics.snapshot(settings, args.day)
        _print(result.to_dict())
        return 2 if result.halt_scaling else 0

    agent = Agent(settings)
    if args.command == "brief":
        if args.action == "draft":
            result = agent.draft(niche=args.niche or settings.app_dir.name, concept=args.concept)
            _print(result)
            return 0 if result["audit"]["status"] == compliance.PASSED_CHECKS else 1
        if args.action == "audit":
            result = agent.reaudit(args.brief_id)
            _print(result)
            return 0 if result.get("audit", {}).get("status") == compliance.PASSED_CHECKS else 1
        if args.action == "show":
            found = agent.store.get_brief(args.brief_id)
            _print(found or {"error": "no such brief"})
            return 0 if found else 1
        result = agent.approve(args.brief_id, args.actor)
        _print(result)
        return 0 if result["approved"] else 1
    if args.command == "attribution":
        result = agent.attribution(since=args.since, until=args.until)
        _print(result)
        return 0 if result.get("available") else 1
    if args.command == "patterns":
        _print(adspy.ingest_file(args.file, agent.store, min_days_active=args.min_days))
        return 0
    result = agent.evaluate_telemetry(args.file, day=args.day, skip_funnel=args.skip_funnel)
    _print(result)
    return 2 if result["halt"] else 0


if __name__ == "__main__":
    sys.exit(main())
