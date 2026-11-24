"""Graph reads (Ad Library, Insights) through a fake transport: no network, no real credentials."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from marketing_agent import adspy, compliance, graph, meta
from marketing_agent.cli import Agent, main
from marketing_agent.config import MarketingSettings
from marketing_agent.store import MarketingStore

TOKEN = "fake-test-token-not-real"


class FakeTransport:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _reader(transport):
    return graph.GraphReader(TOKEN, transport=transport)


# ---- graph.py ---------------------------------------------------------------

def test_reader_needs_a_token():
    with pytest.raises(graph.GraphError):
        graph.GraphReader("")


def test_paging_follows_next_on_the_graph_host_only():
    nxt = "https://graph.facebook.com/v21.0/ads_archive?after=x&access_token=" + TOKEN
    t = FakeTransport([{"data": [{"id": "1"}], "paging": {"next": nxt}}, {"data": [{"id": "2"}]}])
    assert [a["id"] for a in _reader(t).paged("ads_archive", {"q": "1"})] == ["1", "2"]
    assert t.calls[0][1]["access_token"] == TOKEN and t.calls[1][1] == {}

    evil = FakeTransport([{"data": [], "paging": {"next": "https://evil.example/steal"}}])
    with pytest.raises(graph.GraphError, match="non-Graph"):
        list(_reader(evil).paged("ads_archive", {}))
    assert len(evil.calls) == 1, "the token is never sent to another host"


def test_page_limit_is_enforced():
    nxt = "https://graph.facebook.com/v21.0/ads_archive?after=x"
    t = FakeTransport([{"data": [{"id": str(i)}], "paging": {"next": nxt}} for i in range(5)])
    reader = graph.GraphReader(TOKEN, transport=t, max_pages=3)
    assert len(list(reader.paged("ads_archive", {}))) == 3


def test_errors_never_echo_the_token():
    t = FakeTransport([RuntimeError(f"boom access_token={TOKEN}&x=1 and {TOKEN}")])
    with pytest.raises(graph.GraphError) as exc:
        list(_reader(t).paged("ads_archive", {}))
    assert TOKEN not in str(exc.value)
    t = FakeTransport([{"error": {"code": 190, "message": f"bad token {TOKEN}"}}])
    with pytest.raises(graph.GraphError) as exc:
        list(_reader(t).paged("ads_archive", {}))
    assert TOKEN not in str(exc.value) and "190" in str(exc.value)


def test_paths_and_versions_are_validated():
    with pytest.raises(graph.GraphError):
        graph.GraphReader(TOKEN, version="latest")
    with pytest.raises(graph.GraphError):
        _reader(FakeTransport([])).url("../me?fields=x")


# ---- Ad Library -------------------------------------------------------------

AD = {"id": "987", "ad_delivery_start_time": "2026-09-01",
      "ad_creative_bodies": ["Why do mornings feel rushed? Take the two-minute quiz and see your plan."],
      "ad_creative_link_titles": ["Take the quiz"], "publisher_platforms": ["facebook", "INSTAGRAM"]}
NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_normalised_pattern_holds_types_not_copy():
    item = adspy.normalize_archive_ad(AD, niche="routine", now=NOW)
    assert item["source_ref"] == "adlib:987" and item["days_active"] == 84
    assert item["pattern"] == {"hook_type": "question", "cta_style": "quiz_entry",
                               "quiz_entry_angle": "quiz_first", "copy_length": "short",
                               "platforms": "facebook,instagram"}
    body_words = AD["ad_creative_bodies"][0].lower().split()
    flat = json.dumps(item["pattern"]).lower()
    for i in range(len(body_words) - 2):
        assert " ".join(body_words[i:i + 3]) not in flat
    assert AD["ad_creative_bodies"][0] in item["reference_text"]


def test_fetch_stores_patterns_and_arms_the_copied_text_rule(app, tmp_path):
    store = MarketingStore(tmp_path / "m.sqlite3")
    young = {**AD, "id": "988", "ad_delivery_start_time": "2026-11-20"}
    t = FakeTransport([{"data": [AD, young, {"id": ""}]}])
    stats = adspy.ingest_ad_library(adspy.AdLibrarySource(_reader(t)), store, niche=app["name"],
                                    search_terms="morning routine", countries=["de", "FR"], now=NOW)
    assert stats == {"stored": 1, "duplicate": 0, "too_young": 1, "invalid": 1, "fetched": 3}
    params = t.calls[0][1]
    assert json.loads(params["ad_reached_countries"]) == ["DE", "FR"]
    assert "ad_creative_bodies" in params["fields"]

    payload = __import__("marketing_agent.briefs", fromlist=["x"]).draft_brief(
        app=app["name"], niche=app["name"], quiz=app["quiz"], claims=app["claims"],
        offer=app["offer"])
    payload["hooks"][1]["spoken_text"] = "Why do mornings feel rushed? Take the two-minute quiz and see"
    result = compliance.audit_brief(payload, quiz=app["quiz"], claims=app["claims"],
                                    offer=app["offer"], references=store.reference_texts(app["name"]))
    assert "copied_text" in {f.rule for f in result.findings}


@pytest.mark.parametrize("countries", [[], ["USA"], ["1x"]])
def test_countries_must_be_iso_codes(countries):
    with pytest.raises(ValueError):
        adspy.AdLibrarySource(_reader(FakeTransport([]))).search(search_terms="x", countries=countries)


def test_fetch_without_a_token_never_calls_out(app, capsys):
    assert main(["patterns", "fetch", "--search-terms", "x", "--countries", "DE"]) == 1
    assert "META_AD_LIBRARY_ACCESS_TOKEN" in json.loads(capsys.readouterr().out)["error"]


def test_free_text_pattern_values_are_dropped(tmp_path):
    store = MarketingStore(tmp_path / "m.sqlite3")
    adspy.ingest_items([{"source_ref": "a", "niche": "n", "days_active": 40,
                         "pattern": {"hook_type": "Copy this exact line from their ad",
                                     "cta_style": "quiz_entry"}}], store)
    assert store.patterns("n") == [{"cta_style": "quiz_entry"}]


# ---- Insights ---------------------------------------------------------------

CODE = "0123456789abcdef:body_01:hook_01_question"
INSIGHT = {"ad_id": "ad_9", "ad_name": f"Routine {CODE}", "spend": "60.00", "impressions": "3000",
           "actions": [{"action_type": "video_view", "value": "1500"},
                       {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "4"}],
           "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "60.0"}],
           "outbound_clicks": [{"action_type": "outbound_click", "value": "90"}],
           "date_start": "2026-11-20", "date_stop": "2026-11-22"}


def test_insight_rows_map_onto_ad_stats():
    stats = meta.stats_from_insight(INSIGHT)
    assert (stats.meta_ad_id, stats.variant_code, stats.window_end) == ("ad_9", CODE, "2026-11-22")
    assert (stats.spend_minor, stats.impressions, stats.video_3s_views) == (6000, 3000, 1500)
    assert (stats.outbound_clicks, stats.purchases, stats.revenue_minor) == (90, 4, 6000)
    assert stats.hours_live == 72


def test_insights_source_reads_one_window_at_ad_level():
    t = FakeTransport([{"data": [INSIGHT, {"ad_name": "no id"}]}])
    rows = meta.GraphInsightsSource(_reader(t), "123", since="2026-11-20", until="2026-11-22").rows()
    assert len(rows) == 1
    url, params = t.calls[0]
    assert url.endswith("/act_123/insights") and params["level"] == "ad"
    assert json.loads(params["time_range"]) == {"since": "2026-11-20", "until": "2026-11-22"}
    with pytest.raises(ValueError):
        meta.GraphInsightsSource(_reader(t), "act_12x", since="2026-11-20", until="2026-11-22")


def test_api_telemetry_is_still_recommendation_only(app, monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "123")
    t = FakeTransport([{"data": [INSIGHT]}])
    agent = Agent(MarketingSettings.from_env(), transport=t)
    result = agent.evaluate_telemetry(None, day="2026-11-22", skip_funnel=True, source="api")
    rec = result["recommendations"][0]
    assert rec["action"] == meta.KEEP and "withheld" in rec["reason"], "risk unverified -> no winner"
    assert all(r["executed"] == 0 for r in agent.store.recommendations())
    assert TOKEN not in json.dumps(result)


def test_api_source_without_credentials_is_an_error(app, capsys):
    assert main(["telemetry", "evaluate", "--source", "api", "--skip-funnel"]) == 1
    assert "META_ACCESS_TOKEN" in json.loads(capsys.readouterr().out)["error"]
