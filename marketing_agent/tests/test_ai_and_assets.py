"""AI hooks (off by default, add-only, fail-closed) and asset manifests / staging receipts."""

from __future__ import annotations

import json

import pytest

from marketing_agent import ai, assets, compliance, policy
from marketing_agent.cli import Agent, main
from marketing_agent.config import MarketingSettings

GOOD_HOOKS = {"hooks": [
    {"archetype": "question", "spoken_text": "What would your week look like if it fit your mornings?",
     "on_screen_caption": "See your week first", "visual_prompt": "Calm kitchen table at dawn, 9:16"},
    {"archetype": "process", "spoken_text": "Answer a short quiz, then look at the plan before checkout.",
     "on_screen_caption": "Preview, then decide", "visual_prompt": "Notebook on a desk, daylight, 9:16"},
]}
PASS = json.dumps({"verdict": "pass", "findings": []})


def scripted(draft_reply, review_reply=PASS):
    """A fake model: answers the drafter and the reviewer by prompt type, records every call."""
    calls = []

    def complete(prompt, max_tokens):
        calls.append(prompt)
        reply = review_reply if "policy reviewer" in prompt else draft_reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    complete.calls = calls
    return complete


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setenv("MARKETING_AI_ENABLED", "true")
    monkeypatch.setattr(ai, "load_router", lambda: None)  # never a real model in tests


# ---- defaults ---------------------------------------------------------------

def test_ai_is_off_by_default_and_never_called(app):
    completer = scripted(json.dumps(GOOD_HOOKS))
    agent = Agent(MarketingSettings.from_env(), completer=completer)
    result = agent.draft(niche="n", concept="c", use_ai=True)
    assert result["ai"]["status"] == "disabled"
    assert completer.calls == []
    assert "ai_review" not in result["audit"]["layers"]


def test_legacy_flag_name_still_enables(monkeypatch):
    monkeypatch.setenv("MARKETING_LLM_ENABLED", "true")
    assert MarketingSettings.from_env().ai_enabled


# ---- drafter ----------------------------------------------------------------

def test_ai_draft_is_a_separate_brief_through_the_full_gate(app, ai_on):
    agent = Agent(MarketingSettings.from_env(), completer=scripted(json.dumps(GOOD_HOOKS), PASS))
    result = agent.draft(niche="n", concept="c", use_ai=True)
    drafted = result["ai"]
    assert drafted["status"] == "drafted" and drafted["brief_id"] != result["brief_id"]
    assert drafted["audit"]["status"] == compliance.PASSED_CHECKS, drafted["audit"]
    assert "ai_review" in drafted["audit"]["layers"]
    payload = agent.store.get_brief(drafted["brief_id"])["payload"]
    base = agent.store.get_brief(result["brief_id"])["payload"]
    assert [h["hook_id"] for h in payload["hooks"]] == ["hook_ai_01", "hook_ai_02"]
    for key in ("destination_url", "price_disclosure", "static_quiz_cards", "core_body"):
        assert payload[key] == base[key], f"the model never sets {key}"


def test_ai_hooks_that_break_rules_are_flagged_not_fixed(app, ai_on):
    bad = {"hooks": [{"archetype": "authority", "spoken_text": "A doctor explains why your stamina is weak",
                      "on_screen_caption": "Results in 7 days", "visual_prompt": "Specialist in scrubs, 9:16"}]}
    agent = Agent(MarketingSettings.from_env(), completer=scripted(json.dumps(bad), PASS))
    drafted = agent.draft(niche="n", concept="c", use_ai=True)["ai"]
    rules = {f["rule"] for f in drafted["audit"]["findings"]}
    assert drafted["audit"]["status"] == compliance.FLAGGED
    assert {"impersonation", "timeframe"} <= rules
    stored = agent.store.get_brief(drafted["brief_id"])["payload"]["hooks"][0]["spoken_text"]
    assert stored == bad["hooks"][0]["spoken_text"]


@pytest.mark.parametrize("reply", [
    "not json", json.dumps({"hooks": []}), json.dumps({"hooks": [{"archetype": "x"}]}),
    json.dumps({"hooks": [{**GOOD_HOOKS["hooks"][0], "spoken_text": "x" * 500}]}),
    TimeoutError("model down"),
])
def test_malformed_or_failed_drafts_create_no_brief(app, ai_on, reply):
    agent = Agent(MarketingSettings.from_env(), completer=scripted(reply, PASS))
    result = agent.draft(niche="n", concept="c", use_ai=True)
    assert result["ai"]["status"] == "failed"
    assert agent.store.log_rows("marketing.ai_draft_failed")


def test_cli_ai_draft_failure_exits_nonzero(app, ai_on, capsys):
    assert main(["brief", "draft", "--ai"]) == 1, "AI on with no model: no AI brief, exit 1"
    assert json.loads(capsys.readouterr().out)["ai"]["status"] == "unavailable"


# ---- reviewer ---------------------------------------------------------------

@pytest.mark.parametrize("reply, rule", [
    ("I think it is fine", "ai_review_unclear"),
    (json.dumps({"verdict": "pass", "findings": [{"field": "x", "detail": "hmm"}]}), "ai_review"),
    (json.dumps({"verdict": "flag", "findings": []}), "ai_review_unclear"),
    (json.dumps({"verdict": "ok", "findings": []}), "ai_review_unclear"),
    (json.dumps({"verdict": "flag", "findings": [{"field": "hooks[0]", "detail": "implies a cure"}]}),
     "ai_review"),
])
def test_only_an_exact_clean_pass_adds_nothing(reply, rule):
    assert rule in {f.rule for f in ai.parse_review(reply)}
    assert ai.parse_review(PASS) == []


def test_reviewer_errors_and_absence_flag(app):
    draft = Agent(MarketingSettings.from_env()).store  # noqa: F841 - ensures DB init
    from marketing_agent import briefs
    payload = briefs.draft_brief(app=app["name"], niche="n", quiz=app["quiz"], claims=app["claims"],
                                 offer=app["offer"])
    line = app["offer"].price_line(app["claims"])
    for reviewer, rule in ((ai.make_reviewer(scripted(PASS, TimeoutError("x")), price_line=line),
                            "ai_review_error"),
                           (ai.make_reviewer(None, price_line=line), "ai_review_unavailable")):
        result = compliance.audit_brief(payload, quiz=app["quiz"], claims=app["claims"],
                                        offer=app["offer"], reviewer=reviewer)
        assert result.status == compliance.FLAGGED and rule in {f.rule for f in result.findings}


def test_reviewer_cannot_clear_a_deterministic_finding(app):
    from marketing_agent import briefs
    payload = briefs.draft_brief(app=app["name"], niche="n", quiz=app["quiz"], claims=app["claims"],
                                 offer=app["offer"])
    payload["hooks"][0]["spoken_text"] = "Guaranteed results"
    result = compliance.audit_brief(payload, quiz=app["quiz"], claims=app["claims"],
                                    offer=app["offer"], reviewer=lambda strings: [])
    assert result.status == compliance.FLAGGED and "claims" in {f.rule for f in result.findings}


def test_ai_enabled_without_a_model_blocks_approval(app, ai_on):
    agent = Agent(MarketingSettings.from_env())
    result = agent.draft(niche="n", concept="c")
    assert result["audit"]["status"] == compliance.FLAGGED
    assert agent.approve(result["brief_id"], "operator")["approved"] is False


# ---- assets -----------------------------------------------------------------

def _approved(agent):
    bid = agent.draft(niche="n", concept="c")["brief_id"]
    assert agent.approve(bid, "operator")["approved"]
    return bid


def test_manifests_need_an_approved_brief(app):
    agent = Agent(MarketingSettings.from_env())
    bid = agent.draft(niche="n", concept="c")["brief_id"]
    result = agent.build_manifests(bid)
    assert result["built"] is False and "APPROVED" in result["problems"][0]


def test_card_and_video_manifests_carry_real_quiz_and_exact_disclosure(app):
    agent = Agent(MarketingSettings.from_env())
    bid = _approved(agent)
    result = agent.build_manifests(bid)
    assert result["built"], result
    line = app["offer"].price_line(app["claims"])
    questions = {q.key: q for q in app["quiz"].QUESTIONS}
    seen = set()
    for entry in result["manifests"]:
        manifest = json.loads(open(entry["path"], encoding="utf-8").read())
        seen.add(manifest["asset_type"])
        assert f"utm_content=" in manifest["landing_url"]
        if manifest["asset_type"] == "STATIC_QUIZ_CARD":
            layers = {layer["kind"]: layer for layer in manifest["layers"]}
            assert layers["disclosure"]["text"] == line
            real = {label for _, label in questions[manifest["question_key"]].options}
            assert set(layers["options"]["items"]) <= real
            assert layers["options"]["style"] == "plain_text_list"
            assert manifest["canvas"]["aspect"] == "4:5"
        else:
            end = manifest["timeline"][-1]
            assert end["segment"] == "end_card" and end["disclosure"] == line
            assert manifest["canvas"]["aspect"] == "9:16"
    assert seen == {"STATIC_QUIZ_CARD", "VIDEO_HOOK_COMBO"}
    assert {v["status"] for v in agent.store.variants(bid)} == {"MANIFESTED"}


def test_a_tampered_approved_brief_gets_no_manifest(app):
    agent = Agent(MarketingSettings.from_env())
    bid = _approved(agent)
    with agent.store._connect() as conn:
        row = conn.execute("select payload_json from creative_briefs where brief_id = ?",
                           (bid,)).fetchone()
        payload = json.loads(row["payload_json"])
        payload["hooks"][0]["visual_prompt"] = "A doctor in scrubs points at a clickable button, 9:16"
        conn.execute("update creative_briefs set payload_json = ? where brief_id = ?",
                     (json.dumps(payload), bid))
    result = agent.build_manifests(bid)
    assert result["built"] is False
    assert any("impersonation" in p for p in result["problems"])
    assert any("fake_ui" in p for p in result["problems"])


def test_staging_without_a_manifest_is_refused(app):
    agent = Agent(MarketingSettings.from_env())
    receipts = agent.stage("nope:card_x")["receipts"]
    assert receipts[0]["status"] == "refused_no_manifest" and receipts[0]["executed"] is False


def test_staging_by_default_returns_dry_run_receipts(app, capsys):
    agent = Agent(MarketingSettings.from_env())
    bid = _approved(agent)
    code = agent.build_manifests(bid)["manifests"][0]["variant_code"]
    assert main(["assets", "stage", code]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["executed"] is False
    assert [(r["action"], r["status"]) for r in out["receipts"]] == [("render", "dry_run"),
                                                                     ("publish", "dry_run")]
    assert all(r["executed"] is False for r in out["receipts"])
    assert len(Agent(MarketingSettings.from_env()).store.log_rows("marketing.receipt")) == 2


def test_dry_run_off_without_flags_is_not_armed(app, monkeypatch):
    agent = Agent(MarketingSettings.from_env())
    code = agent.build_manifests(_approved(agent))["manifests"][0]["variant_code"]
    monkeypatch.setenv("MARKETING_DRY_RUN", "false")
    receipts = Agent(MarketingSettings.from_env()).stage(code)["receipts"]
    assert {r["status"] for r in receipts} == {"not_armed"}


def test_fully_armed_staging_is_refused_not_executed(app, monkeypatch):
    agent = Agent(MarketingSettings.from_env())
    code = agent.build_manifests(_approved(agent))["manifests"][0]["variant_code"]
    for key, value in {"MARKETING_DRY_RUN": "false", "MARKETING_RENDER_ENABLED": "true",
                       "MARKETING_PUBLISH_ENABLED": "true", "META_ACCESS_TOKEN": "x",
                       "META_AD_ACCOUNT_ID": "1"}.items():
        monkeypatch.setenv(key, value)
    receipts = Agent(MarketingSettings.from_env()).stage(code)["receipts"]
    assert {r["status"] for r in receipts} == {"refused_not_built"}
    assert all(r["executed"] is False for r in receipts)
    assert main(["assets", "stage", code]) == 1
