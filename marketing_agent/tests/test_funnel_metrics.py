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
