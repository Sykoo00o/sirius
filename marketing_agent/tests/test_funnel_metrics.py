"""Funnel health comes from the operator's metrics script, run offline against a fixture database."""

from __future__ import annotations

import pytest

from marketing_agent import funnel_metrics
from marketing_agent.config import MarketingSettings

from .conftest import REPO, load_funnel_module


@pytest.fixture
def funnel_db(tmp_path, monkeypatch):
    billing_store = load_funnel_module("billing_store")
    telemetry = load_funnel_module("telemetry")
    billing = tmp_path / "billing.sqlite3"
    billing_store.BillingStore(billing)
    events = telemetry.TelemetryStore(billing)
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(billing))
    monkeypatch.setenv("FUNNEL_DB_PATH", str(tmp_path / "responses.sqlite3"))
    monkeypatch.setenv("FUNNEL_METRICS_CSV", str(tmp_path / "operator_sheet.csv"))
    monkeypatch.setenv("MARKETING_FUNNEL_APP_DIR", str(REPO / "funnel_app"))
    monkeypatch.setenv("MARKETING_STATE_DIR", str(tmp_path / "agent_state"))
    for visitor in ("v1", "v2", "v3"):
        events.record(event="lp_view", visitor_key=visitor)
        events.record(event="quiz_started", visitor_key=visitor)
    for visitor in ("v1", "v2"):
        events.record(event="trial_paid", visitor_key=visitor, amount_minor=100,
                      gross_volume_minor=100, card_funding="credit",
                      idempotency_key=f"trial_paid:{visitor}")
    return {"events": events, "tmp": tmp_path}


def test_healthy_funnel_does_not_halt(funnel_db):
    health = funnel_metrics.snapshot(MarketingSettings.from_env(), "today")
    assert health.available and not health.halt_scaling, health.reasons
    assert health.counts, "counts come from the export script"


def test_dispute_ceiling_halts_scaling(funnel_db):
    funnel_db["events"].record(event="dispute_created", visitor_key="v1", amount_minor=100,
                               gross_volume_minor=100, idempotency_key="dispute_created:evt_1")
    health = funnel_metrics.snapshot(MarketingSettings.from_env(), "today")
    assert health.halt_scaling
    assert any("dispute" in r for r in health.reasons)


def test_operator_sheet_is_never_written(funnel_db):
    funnel_metrics.snapshot(MarketingSettings.from_env(), "today")
    assert not (funnel_db["tmp"] / "operator_sheet.csv").exists()
    assert list((funnel_db["tmp"] / "agent_state").glob("funnel_snapshot_*.csv"))


def test_prepaid_share_threshold():
    s = MarketingSettings.from_env()
    detail = {"metrics": {"bin_prepaid_share_pct": "35.0"}, "breaches": [], "counts": {}}
    assert funnel_metrics.evaluate("d", detail, s).halt_scaling
    detail["metrics"]["bin_prepaid_share_pct"] = ""
    assert not funnel_metrics.evaluate("d", detail, s).halt_scaling


def test_missing_database_is_unavailable_not_halt(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setenv("MARKETING_STATE_DIR", str(tmp_path / "s"))
    health = funnel_metrics.snapshot(MarketingSettings.from_env(), "today")
    assert not health.available and not health.halt_scaling


def _tagged_visit(events, visitor, content, *, paid=False):
    events.record(event="lp_view", visitor_key=visitor,
                  detail={"utm": {"utm_source": "meta", "utm_content": content}})
    events.record(event="quiz_started", visitor_key=visitor)
    events.record(event="trial_started", visitor_key=visitor)
    if paid:
        events.record(event="trial_paid", visitor_key=visitor, amount_minor=100,
                      gross_volume_minor=100, card_funding="credit",
                      idempotency_key=f"trial_paid:{visitor}")


def test_by_ad_splits_trials_per_variant(funnel_db):
    events = funnel_db["events"]
    _tagged_visit(events, "a1", "brief:body:hook_a", paid=True)
    _tagged_visit(events, "a2", "brief:body:hook_a")
    _tagged_visit(events, "b1", "brief:body:hook_b", paid=True)
    result = funnel_metrics.by_ad(MarketingSettings.from_env(), "today", "today")
    assert result["available"], result
    ads = {ad["utm_content"]: ad for ad in result["ads"]}
    assert ads["brief:body:hook_a"]["lp_view"] == 2
    assert ads["brief:body:hook_a"]["trial_started"] == 2
    assert ads["brief:body:hook_a"]["trial_paid"] == 1
    assert ads["brief:body:hook_a"]["revenue_minor"] == 100
    assert ads["brief:body:hook_a"]["lp_to_trial_pct"] == "100.00"
    assert ads["brief:body:hook_b"]["trial_paid"] == 1
    # The fixture's three organic visitors land in one untagged bucket, listed last.
    assert result["ads"][-1]["utm_content"] == "(untagged)"
    assert result["ads"][-1]["lp_view"] == 3 and result["ads"][-1]["trial_paid"] == 2


def test_by_ad_never_writes_any_sheet(funnel_db):
    funnel_metrics.by_ad(MarketingSettings.from_env(), "today", "today")
    assert not (funnel_db["tmp"] / "operator_sheet.csv").exists()


def test_by_ad_does_not_guess_visitors_without_a_landing_view(funnel_db):
    funnel_db["events"].record(event="refund_issued", visitor_key="ghost", amount_minor=100,
                               idempotency_key="refund_issued:evt_ghost")
    result = funnel_metrics.by_ad(MarketingSettings.from_env(), "today", "today")
    assert result["unattributed_events"] == 1
    assert sum(ad["refunds_count"] for ad in result["ads"]) == 0


def test_by_ad_without_a_database_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(tmp_path / "absent.sqlite3"))
    assert funnel_metrics.by_ad(MarketingSettings.from_env(), "today", "today")["available"] is False


def test_attribution_marks_the_brief_that_planned_each_variant(funnel_db, capsys):
    from marketing_agent.cli import Agent, main
    agent = Agent(MarketingSettings.from_env())
    bid = agent.draft(niche="n", concept="c")["brief_id"]
    approved = agent.approve(bid, "operator")
    code = approved["variants_planned"][0]
    _tagged_visit(funnel_db["events"], "p1", code, paid=True)
    _tagged_visit(funnel_db["events"], "p2", "hand-typed-tag")
    assert main(["attribution", "--since", "today", "--until", "today"]) == 0
    import json
    ads = {ad["utm_content"]: ad for ad in json.loads(capsys.readouterr().out)["ads"]}
    assert ads[code]["known_variant"] and ads[code]["brief_id"] == bid
    assert ads[code]["trial_paid"] == 1
    assert ads["hand-typed-tag"]["known_variant"] is False


# ---- ad x trial report ------------------------------------------------------

def _winner_row(code, ad_id="ad_w"):
    return {"meta_ad_id": ad_id, "window_end": "w", "variant_code": code, "spend_minor": 6000,
            "impressions": 3000, "video_3s_views": 1500, "outbound_clicks": 90,
            "revenue_minor": 6000, "purchases": 4, "hours_live": 60}


def _report(agent, rows, tmp_path):
    import json as _json
    path = tmp_path / "insights.json"
    path.write_text(_json.dumps(rows))
    return agent.ad_report(source="file", path=path, since=_today(), until=_today())


def _today():
    from marketing_agent.cli import resolve_day
    return resolve_day("today")


def test_report_joins_meta_spend_to_funnel_revenue(funnel_db):
    from marketing_agent import meta
    from marketing_agent.cli import Agent
    agent = Agent(MarketingSettings.from_env())
    bid = agent.draft(niche="n", concept="c")["brief_id"]
    code = agent.approve(bid, "operator")["variants_planned"][0]
    _tagged_visit(funnel_db["events"], "r1", code, paid=True)
    _tagged_visit(funnel_db["events"], "r2", code, paid=True)
    result = _report(agent, [_winner_row(code), _winner_row("", "ad_unnamed")], funnel_db["tmp"])
    assert result["read_only"] and result["funnel_verified"] and not result["halt_all_scaling"]
    row = next(v for v in result["variants"] if v["variant_code"] == code)
    assert row["known_variant"] and row["brief_id"] == bid
    assert (row["spend_minor"], row["trial_started"], row["trial_paid"]) == (6000, 2, 2)
    assert row["funnel_revenue_minor"] == 200 and row["funnel_roas"] == round(200 / 6000, 4)
    assert row["cost_per_trial_minor"] == 3000
    assert row["recommendations"][0]["action"] == meta.WINNER, "Meta's ROAS, funnel verified"
    assert result["ads_without_variant_code"] == 1
    assert agent.store.recommendations() == [], "the report saves nothing"


def test_report_halts_every_row_on_a_funnel_ceiling(funnel_db):
    from marketing_agent import meta
    from marketing_agent.cli import Agent, main
    funnel_db["events"].record(event="dispute_created", visitor_key="v1", amount_minor=100,
                               gross_volume_minor=100, idempotency_key="dispute_created:evt_r")
    agent = Agent(MarketingSettings.from_env())
    result = _report(agent, [_winner_row("0123456789abcdef:card_goal")], funnel_db["tmp"])
    assert result["halt_all_scaling"] and any("dispute" in r for r in result["halt_reasons"])
    actions = {r["action"] for v in result["variants"] for r in v["recommendations"]}
    assert actions == {meta.HALT}
    path = funnel_db["tmp"] / "insights.json"
    assert main(["report", "ads", "--insights", str(path), "--since", "today",
                 "--until", "today"]) == 2


def test_report_withholds_winners_when_the_funnel_cannot_be_checked(tmp_path, monkeypatch, app):
    from marketing_agent import meta
    from marketing_agent.cli import Agent
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setenv("MARKETING_STATE_DIR", str(tmp_path / "s"))
    agent = Agent(MarketingSettings.from_env())
    result = _report(agent, [_winner_row("0123456789abcdef:card_goal")], tmp_path)
    assert result["funnel_verified"] is False and "withheld" in result["funnel_note"]
    rec = result["variants"][0]["recommendations"][0]
    assert rec["action"] == meta.KEEP and "withheld" in rec["reason"]


def test_window_health_halts_on_any_day(funnel_db):
    funnel_db["events"].record(event="dispute_created", visitor_key="v1", amount_minor=100,
                               gross_volume_minor=100, idempotency_key="dispute_created:evt_w")
    health = funnel_metrics.window_snapshot(MarketingSettings.from_env(), "yesterday", "today")
    assert health.available and health.halt_scaling
    assert len(health.counts) == 2, "one entry per day in the window"
