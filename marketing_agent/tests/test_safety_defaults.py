"""The agent must be inert until a human arms it, and stay inert in v1 even then."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from marketing_agent import meta
from marketing_agent.config import MarketingSettings
from marketing_agent.store import MarketingStore

PACKAGE = Path(__file__).resolve().parents[1]


def test_defaults_are_inert():
    s = MarketingSettings.from_env()
    assert s.dry_run is True
    assert (s.auto_pause, s.publish_enabled, s.llm_enabled) == (False, False, False)
    assert not s.can_pause_ads() and not s.can_publish()


@pytest.mark.parametrize("value", ["", "ture", "False ", "maybe", "TRUE", "1"])
def test_only_an_explicit_false_leaves_dry_run(monkeypatch, value):
    monkeypatch.setenv("MARKETING_DRY_RUN", value)
    expected = value.strip().lower() == "false"
    assert MarketingSettings.from_env().dry_run is (not expected)


def test_credentials_alone_do_not_arm(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "placeholder-not-a-real-token")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "act_placeholder")
    s = MarketingSettings.from_env()
    assert not s.can_pause_ads() and not s.can_publish()
    monkeypatch.setenv("MARKETING_AUTO_PAUSE", "true")
    assert not MarketingSettings.from_env().can_pause_ads(), "dry-run still on"


def test_describe_never_prints_credentials(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "placeholder-secret-value")
    assert "placeholder-secret-value" not in json.dumps(MarketingSettings.from_env().describe())


def test_dry_run_mutations_only_record_intents(tmp_path):
    s = MarketingSettings.from_env()
    store = MarketingStore(tmp_path / "m.sqlite3")
    client = meta.UnarmedMetaClient(s, store)
    assert client.pause_ad("ad_1", "LOW_THUMBSTOP") == "intent_recorded"
    assert client.publish("brief:card_goal") == "intent_recorded"
    assert len(store.log_rows("marketing.intent_recorded")) == 2


def test_fully_armed_still_cannot_mutate_in_v1(monkeypatch, tmp_path):
    for key, value in {"MARKETING_DRY_RUN": "false", "MARKETING_AUTO_PAUSE": "true",
                       "MARKETING_PUBLISH_ENABLED": "true", "META_ACCESS_TOKEN": "x",
                       "META_AD_ACCOUNT_ID": "y"}.items():
        monkeypatch.setenv(key, value)
    s = MarketingSettings.from_env()
    assert s.can_pause_ads() and s.can_publish()
    client = meta.UnarmedMetaClient(s, MarketingStore(tmp_path / "m.sqlite3"))
    with pytest.raises(meta.NotBuilt):
        client.pause_ad("ad_1", "x")
    with pytest.raises(meta.NotBuilt):
        client.publish("v1")


def test_package_makes_no_network_calls_and_imports_no_engine():
    """Stdlib-only and offline: the only subprocess is the operator's metrics script, --offline."""
    banned = re.compile(r"^\s*(?:import|from)\s+(httpx|requests|urllib\.request|socket|http\.client|"
                        r"aiohttp|smtplib|backend|engine|fastapi)\b", re.M)
    for path in PACKAGE.glob("*.py"):
        assert not banned.search(path.read_text()), path.name
    metrics_src = (PACKAGE / "funnel_metrics.py").read_text()
    assert '"--offline"' in metrics_src


def test_agent_never_touches_engine_or_funnel_switches():
    for path in PACKAGE.glob("*.py"):
        text = path.read_text()
        for name in ("SUPERVISOR_ENABLED", "ENGINE_DRY_RUN", "FUNNEL_CHARGES_ENABLED", "engine.dry_run"):
            assert f'"{name}"' not in text and f"'{name}'" not in text, (path.name, name)
