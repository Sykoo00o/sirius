"""Human approval, idempotent state, and recommendation-only telemetry."""

from __future__ import annotations

import json

from marketing_agent import compliance, meta
from marketing_agent.cli import Agent, main
from marketing_agent.config import AdRules, MarketingSettings

from .conftest import load_funnel_module

RULES = AdRules()


def _stats(**kw):
    base = {"meta_ad_id": "ad_1", "window_end": "2026-11-24T00:00:00Z"}
    return meta.AdStats.from_dict({**base, **kw})


def test_draft_is_idempotent_and_audited(app):
    agent = Agent(MarketingSettings.from_env())
    first = agent.draft(niche="n", concept="c")
    second = agent.draft(niche="n", concept="c")
    assert first["brief_id"] == second["brief_id"]
    assert first["created"] and not second["created"]
    assert agent.store.get_brief(first["brief_id"])["status"] == compliance.PASSED_CHECKS
    assert len(agent.store.log_rows("marketing.brief_drafted")) == 1


def test_approval_needs_a_passing_audit_and_a_named_human(app):
    agent = Agent(MarketingSettings.from_env())
    bid = agent.draft(niche="n", concept="c")["brief_id"]
    assert agent.approve(bid, "  ")["approved"] is False
    result = agent.approve(bid, "operator")
    assert result["approved"] and result["rendered"] is False
    assert result["variants_planned"], "approval plans body x hook and card variants"
    audit_rows = agent.store.log_rows("marketing.brief_approved")
    assert len(audit_rows) == 1 and audit_rows[0]["is_audit"] == 1
    assert all(v["status"] == "PLANNED" for v in agent.store.variants(bid))


def test_flagged_brief_cannot_be_approved(app):
    agent = Agent(MarketingSettings.from_env())
    bid = agent.draft(niche="n", concept="c")["brief_id"]
    agent.store.record_audit(bid, {"status": compliance.FLAGGED, "layers": ["x"], "findings": []})
    assert agent.approve(bid, "operator")["approved"] is False


def test_log_idempotency_key_dedupes(tmp_path):
    from marketing_agent.store import MarketingStore
    store = MarketingStore(tmp_path / "s.sqlite3")
    assert store.log(event="e", idempotency_key="k") is True
    assert store.log(event="e", idempotency_key="k") is False
    assert store.log(event="e") and store.log(event="e")
    assert len(store.log_rows("e")) == 3


def test_recommendations():
    assert meta.evaluate(_stats(), RULES).action == meta.INSUFFICIENT
    low_thumb = _stats(spend_minor=3000, impressions=2000, video_3s_views=300)
    assert meta.evaluate(low_thumb, RULES).action == meta.PAUSE
    low_ctr = _stats(spend_minor=4000, impressions=3000, video_3s_views=1500, outbound_clicks=10)
    assert "LOW_INTENT_CLICK" in meta.evaluate(low_ctr, RULES).reason
    low_roas = _stats(spend_minor=6000, impressions=3000, video_3s_views=1500,
                      outbound_clicks=90, revenue_minor=1000, hours_live=50)
    assert "ROAS" in meta.evaluate(low_roas, RULES).reason
    winner = _stats(spend_minor=6000, impressions=3000, video_3s_views=1500, outbound_clicks=90,
                    revenue_minor=6000, purchases=4, hours_live=60)
    assert meta.evaluate(winner, RULES).action == meta.WINNER
    assert meta.evaluate(winner, RULES, funnel_halt="dispute_cb_pct=2%").action == meta.HALT


def test_telemetry_evaluate_records_intents_only(app, tmp_path, capsys):
    rows = [{"meta_ad_id": "ad_low", "window_end": "w1", "spend_minor": 3000, "impressions": 2000,
             "video_3s_views": 200},
            {"meta_ad_id": "ad_win", "window_end": "w1", "spend_minor": 6000, "impressions": 3000,
             "video_3s_views": 1500, "outbound_clicks": 90, "revenue_minor": 6000, "purchases": 4,
             "hours_live": 60}]
    path = tmp_path / "insights.json"
    path.write_text(json.dumps(rows))
    assert main(["telemetry", "evaluate", str(path), "--skip-funnel"]) == 0
    out = json.loads(capsys.readouterr().out)
    by_ad = {r["meta_ad_id"]: r for r in out["recommendations"]}
    assert by_ad["ad_low"]["executed"] == "intent_recorded"
    # --skip-funnel means the risk ceilings were not checked, so a winner is withheld.
    assert by_ad["ad_win"]["action"] == meta.KEEP and "withheld" in by_ad["ad_win"]["reason"]
    assert by_ad["ad_win"]["executed"] == "none"
    agent = Agent(MarketingSettings.from_env())
    assert all(r["executed"] == 0 for r in agent.store.recommendations())
    # Re-running the same window is one row per ad, not two.
    main(["telemetry", "evaluate", str(path), "--skip-funnel"])
    capsys.readouterr()
    assert len(agent.store.recommendations()) == 2


def test_status_command(capsys):
    assert main(["status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["can_pause_ads"] is False


def test_patterns_ingest(app, tmp_path, capsys):
    path = tmp_path / "p.json"
    path.write_text(json.dumps([
        {"source_ref": "a", "niche": "n", "days_active": 45, "pattern": {"hook_type": "q", "x": 1}},
        {"source_ref": "b", "niche": "n", "days_active": 3},
        {"niche": "n"},
    ]))
    assert main(["patterns", "ingest", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"stored": 1, "duplicate": 0, "too_young": 1,
                                                    "invalid": 1}


def test_approval_emits_attributed_landing_urls(app):
    """Each planned variant gets the destination the funnel can join back to a trial."""
    from urllib.parse import parse_qs, urlparse
    agent = Agent(MarketingSettings.from_env())
    bid = agent.draft(niche="n", concept="c")["brief_id"]
    result = agent.approve(bid, "operator")
    assert set(result["landing_urls"]) == set(result["variants_planned"])
    telemetry = load_funnel_module("telemetry", app["name"])
    for code, url in result["landing_urls"].items():
        parsed = urlparse(url)
        assert url.startswith(app["offer"].public_url)
        flat = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert telemetry.attribution_from_query(flat)["utm_content"] == code, \
            "the funnel must keep the variant code intact for the join"
        assert flat["utm_campaign"] == bid
