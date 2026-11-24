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
