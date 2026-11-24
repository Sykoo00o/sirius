"""Command line for the marketing agent. Every command prints JSON.

    python -m marketing_agent status
    python -m marketing_agent brief draft [--niche N] [--concept NAME] [--ai]
    python -m marketing_agent brief audit BRIEF_ID
    python -m marketing_agent brief approve BRIEF_ID --actor NAME
    python -m marketing_agent brief show BRIEF_ID
    python -m marketing_agent patterns ingest FILE [--min-days 30]
    python -m marketing_agent patterns fetch --search-terms T --countries DE,FR [--niche N] [--limit 50]
    python -m marketing_agent assets manifest BRIEF_ID
    python -m marketing_agent assets stage VARIANT_CODE
    python -m marketing_agent funnel-health [--day yesterday]
    python -m marketing_agent attribution [--since 7daysago] [--until today]
    python -m marketing_agent telemetry evaluate [FILE] [--source file|api] [--day yesterday]
                                                [--since D --until D] [--skip-funnel]
    python -m marketing_agent report ads [--insights FILE | --source api] [--since 7daysago] [--until yesterday]

`--source api` is the only way any command reaches Meta, and only with GET (`graph.py`).

Exit codes: 0 ok, 1 flagged / refused / error, 2 the funnel tripped a kill metric (same meaning as
the metrics script's exit 2).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import adspy, ai, assets, briefs, compliance, funnel_metrics, graph, loader, meta, report
from .config import MarketingSettings, ad_library_token
from .offer import load_offer
from .store import MarketingStore


def _print(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def resolve_day(value: str) -> str:
    """YYYY-MM-DD, or today / yesterday / Ndaysago, as a UTC date. Anything else raises."""
    today = datetime.now(timezone.utc).date()
    text = value.strip().lower().replace(" ", "")
    if text == "today":
        return today.isoformat()
    if text == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    match = re.fullmatch(r"(\d+)days?ago", text)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    datetime.strptime(value, "%Y-%m-%d")
    return value


class Agent:
    def __init__(self, settings: MarketingSettings, *, completer: ai.Completer | None = None,
                 transport: graph.Transport | None = None) -> None:
        self.settings = settings
        self.store = MarketingStore(settings.db_path)
        self.claims = loader.claims_module(settings.app_dir)
        self.quiz = loader.quiz_module(settings.app_dir)
        self.offer = load_offer(settings.app_dir)
        self._completer = completer
        self._transport = transport

    # ---- AI (gated, fail-closed) -------------------------------------------

    def ai_completer(self) -> ai.Completer | None:
        if not self.settings.ai_enabled:
            return None
        if self._completer is None:
            try:
                self._completer = ai.router_completer()
            except ai.AIUnavailable:
                return None
        return self._completer

    def audit(self, payload: dict[str, Any]) -> compliance.AuditResult:
        reviewer = (ai.make_reviewer(self.ai_completer(), price_line=self.offer.price_line(self.claims))
                    if self.settings.ai_enabled else None)
        return compliance.audit_brief(
            payload, quiz=self.quiz, claims=self.claims, offer=self.offer,
            references=self.store.reference_texts(payload.get("niche", "")), reviewer=reviewer)

    def _save_and_audit(self, payload: dict[str, Any], concept: str) -> dict[str, Any]:
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

    def draft(self, *, niche: str, concept: str, use_ai: bool = False) -> dict[str, Any]:
        payload = briefs.draft_brief(app=self.settings.app_dir.name, niche=niche, quiz=self.quiz,
                                     claims=self.claims, offer=self.offer, concept_name=concept)
        result = self._save_and_audit(payload, concept)
        if use_ai:
            result["ai"] = self.ai_draft(payload, niche=niche)
        return result

    def ai_draft(self, base: dict[str, Any], *, niche: str) -> dict[str, Any]:
        """A second brief with model hooks, or a failure record. Never replaces the base brief."""
        if not self.settings.ai_enabled:
            return {"status": "disabled", "reason": "MARKETING_AI_ENABLED is off"}
        completer = self.ai_completer()
        if completer is None:
            return {"status": "unavailable", "reason": "no model reachable"}
        try:
            payload = ai.draft_hooks(base, completer=completer,
                                     price_line=self.offer.price_line(self.claims),
                                     patterns=self.store.patterns(niche))
        except Exception as exc:  # noqa: BLE001 - any failure means no AI brief, never a partial one
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            self.store.log(event="marketing.ai_draft_failed", level="warning", message=reason)
            return {"status": "failed", "reason": reason}
        return {"status": "drafted", **self._save_and_audit(payload, payload["concept_name"])}

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

    # ---- Meta reads (GET only, on request) ---------------------------------

    def _reader(self, token: str) -> graph.GraphReader:
        return graph.GraphReader(token, version=self.settings.graph_api_version,
                                 transport=self._transport)

    def fetch_patterns(self, *, niche: str, search_terms: str, countries: list[str],
                       limit: int, min_days: int) -> dict[str, Any]:
        token = ad_library_token()
        if not token:
            return {"error": "set META_AD_LIBRARY_ACCESS_TOKEN (or META_ACCESS_TOKEN) to read the "
                             "Ad Library; or use `patterns ingest FILE`"}
        try:
            stats = adspy.ingest_ad_library(adspy.AdLibrarySource(self._reader(token)), self.store,
                                            niche=niche, search_terms=search_terms,
                                            countries=countries, limit=limit,
                                            min_days_active=min_days)
        except (graph.GraphError, ValueError) as exc:
            return {"error": str(exc)}
        self.store.log(event="marketing.patterns_fetched", message=f"ads_archive {search_terms!r}",
                       context={"niche": niche, "countries": countries, **stats})
        return stats

    def insights_source(self, *, source: str, path: Path | None, since: str, until: str):
        if source == "file":
            if path is None:
                raise ValueError("--source file needs an insights FILE")
            return meta.FixtureInsightsSource(path)
        token = os.getenv("META_ACCESS_TOKEN", "").strip()
        account = os.getenv("META_AD_ACCOUNT_ID", "").strip()
        if not (token and account):
            raise ValueError("--source api needs META_ACCESS_TOKEN and META_AD_ACCOUNT_ID")
        return meta.GraphInsightsSource(self._reader(token), account, since=since, until=until)

    def ad_report(self, *, source: str, path: Path | None, since: str, until: str) -> dict[str, Any]:
        rows = self.insights_source(source=source, path=path, since=since, until=until).rows()
        return report.ad_report(settings=self.settings, store=self.store, stats=rows,
                                since=since, until=until)

    # ---- assets ------------------------------------------------------------

    def manifest_dir(self, bid: str) -> Path:
        return self.settings.state_dir / "manifests" / bid

    def build_manifests(self, bid: str) -> dict[str, Any]:
        brief = self.store.get_brief(bid)
        if brief is None:
            return {"brief_id": bid, "built": False, "problems": ["no such brief"]}
        try:
            manifests = assets.build_manifests(
                bid, brief, quiz=self.quiz, claims=self.claims, offer=self.offer,
                out_dir=self.manifest_dir(bid),
                references=self.store.reference_texts(brief["payload"].get("niche", "")))
        except assets.ManifestRefused as exc:
            self.store.log(event="marketing.manifest_refused", level="warning",
                           message=f"brief {bid}", context={"problems": exc.problems})
            return {"brief_id": bid, "built": False, "problems": exc.problems}
        for manifest in manifests:
            self.store.set_variant_status(manifest["variant_code"], "MANIFESTED")
        self.store.log(event="marketing.manifests_built", message=f"brief {bid}",
                       context={"variants": [m["variant_code"] for m in manifests]},
                       idempotency_key=f"manifests:{bid}:" + ",".join(
                           sorted(m["manifest_sha256"][:8] for m in manifests)))
        return {"brief_id": bid, "built": True, "rendered": False,
                "manifests": [{"variant_code": m["variant_code"], "path": m["path"],
                               "manifest_sha256": m["manifest_sha256"]} for m in manifests]}

    def stage(self, variant_code: str) -> dict[str, Any]:
        variant = self.store.variant(variant_code)
        bid = variant["brief_id"] if variant else variant_code.split(":", 1)[0]
        receipts = assets.stage(variant_code, settings=self.settings, store=self.store,
                                out_dir=self.manifest_dir(bid))
        return {"variant_code": variant_code, "executed": False, "receipts": receipts}

    # ---- telemetry ---------------------------------------------------------

    def evaluate_telemetry(self, path: Path | None, *, day: str, skip_funnel: bool,
                           source: str = "file", since: str = "", until: str = "") -> dict[str, Any]:
        health = None if skip_funnel else funnel_metrics.snapshot(self.settings, day)
        halt = "; ".join(health.reasons) if health and health.halt_scaling else ""
        verified = bool(health and health.available)
        if halt:
            self.store.log(event="marketing.funnel_halt", level="warning",
                           message=f"funnel kill metric on {health.day}: {halt}",
                           context=health.to_dict(), idempotency_key=f"halt:{health.day}")
        client = meta.UnarmedMetaClient(self.settings, self.store)
        out = []
        rows = self.insights_source(source=source, path=path, since=since or day,
                                    until=until or day).rows()
        for stats in rows:
            self.store.save_telemetry(stats.to_row())
            rec = meta.evaluate(stats, self.settings.rules, funnel_halt=halt,
                                funnel_verified=verified)
            self.store.save_recommendation(meta_ad_id=rec.meta_ad_id, window_end=rec.window_end,
                                           action=rec.action, reason=rec.reason)
            executed = "none"
            if rec.action in (meta.PAUSE, meta.HALT):
                executed = client.pause_ad(rec.meta_ad_id, rec.reason)
            out.append({**rec.to_dict(), "executed": executed})
        return {"funnel": health.to_dict() if health else None, "halt": bool(halt),
                "funnel_verified": verified, "recommendations": out,
                "dry_run": self.settings.dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketing_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    brief = sub.add_parser("brief").add_subparsers(dest="action", required=True)
    draft = brief.add_parser("draft")
    draft.add_argument("--niche", default="")
    draft.add_argument("--concept", default="quiz-preview-baseline")
    draft.add_argument("--ai", action="store_true",
                       help="also draft a model-hooked brief (needs MARKETING_AI_ENABLED)")
    for name in ("audit", "show"):
        brief.add_parser(name).add_argument("brief_id")
    approve = brief.add_parser("approve")
    approve.add_argument("brief_id")
    approve.add_argument("--actor", required=True)
    patterns = sub.add_parser("patterns").add_subparsers(dest="action", required=True)
    ingest = patterns.add_parser("ingest")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--min-days", type=int, default=30)
    fetch = patterns.add_parser("fetch")
    fetch.add_argument("--search-terms", required=True)
    fetch.add_argument("--countries", required=True, help="comma-separated, e.g. DE,FR,GB")
    fetch.add_argument("--niche", default="")
    fetch.add_argument("--limit", type=int, default=50)
    fetch.add_argument("--min-days", type=int, default=30)
    asset_cmd = sub.add_parser("assets").add_subparsers(dest="action", required=True)
    asset_cmd.add_parser("manifest").add_argument("brief_id")
    asset_cmd.add_parser("stage").add_argument("variant_code")
    health = sub.add_parser("funnel-health")
    health.add_argument("--day", default="yesterday")
    attribution = sub.add_parser("attribution")
    attribution.add_argument("--since", default="7daysago")
    attribution.add_argument("--until", default="today")
    telemetry = sub.add_parser("telemetry").add_subparsers(dest="action", required=True)
    evaluate = telemetry.add_parser("evaluate")
    evaluate.add_argument("file", type=Path, nargs="?")
    evaluate.add_argument("--source", choices=("file", "api"), default="file")
    evaluate.add_argument("--day", default="yesterday")
    evaluate.add_argument("--since", default="")
    evaluate.add_argument("--until", default="")
    evaluate.add_argument("--skip-funnel", action="store_true")
    report_cmd = sub.add_parser("report").add_subparsers(dest="action", required=True)
    ads = report_cmd.add_parser("ads")
    ads.add_argument("--insights", type=Path, default=None)
    ads.add_argument("--source", choices=("file", "api"), default="file")
    ads.add_argument("--since", default="7daysago")
    ads.add_argument("--until", default="yesterday")
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
            result = agent.draft(niche=args.niche or settings.app_dir.name, concept=args.concept,
                                 use_ai=args.ai)
            _print(result)
            ok = result["audit"]["status"] == compliance.PASSED_CHECKS
            if args.ai:
                ok = ok and result["ai"].get("audit", {}).get("status") == compliance.PASSED_CHECKS
            return 0 if ok else 1
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
        if args.action == "ingest":
            _print(adspy.ingest_file(args.file, agent.store, min_days_active=args.min_days))
            return 0
        countries = [c for c in args.countries.split(",") if c.strip()]
        result = agent.fetch_patterns(niche=args.niche or settings.app_dir.name,
                                      search_terms=args.search_terms, countries=countries,
                                      limit=args.limit, min_days=args.min_days)
        _print(result)
        return 1 if "error" in result else 0
    if args.command == "assets":
        if args.action == "manifest":
            result = agent.build_manifests(args.brief_id)
            _print(result)
            return 0 if result["built"] else 1
        result = agent.stage(args.variant_code)
        _print(result)
        refused = any(r["status"].startswith("refused") for r in result["receipts"])
        return 1 if refused else 0
    try:
        if args.command == "report":
            if args.source == "file" and args.insights is None:
                raise ValueError("report ads needs --insights FILE or --source api")
            result = agent.ad_report(source=args.source, path=args.insights,
                                     since=resolve_day(args.since), until=resolve_day(args.until))
            _print(result)
            return 2 if result["halt_all_scaling"] else 0
        day = resolve_day(args.day)
        result = agent.evaluate_telemetry(
            args.file, day=day, skip_funnel=args.skip_funnel, source=args.source,
            since=resolve_day(args.since) if args.since else "",
            until=resolve_day(args.until) if args.until else "")
    except (ValueError, graph.GraphError) as exc:
        _print({"error": str(exc)})
        return 1
    _print(result)
    return 2 if result["halt"] else 0


if __name__ == "__main__":
    sys.exit(main())
