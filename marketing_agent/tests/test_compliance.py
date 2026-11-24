"""The gate: deterministic rules on top of each funnel's own claims, reject-only."""

from __future__ import annotations

import copy

import pytest

from marketing_agent import briefs, compliance, policy


def _draft(app):
    return briefs.draft_brief(app=app["name"], niche=app["name"], quiz=app["quiz"],
                              claims=app["claims"], offer=app["offer"])


def _audit(app, payload, **kw):
    return compliance.audit_brief(payload, quiz=app["quiz"], claims=app["claims"],
                                  offer=app["offer"], **kw)


def _rules(result):
    return {f.rule for f in result.findings}


def test_deterministic_draft_passes_and_is_reproducible(app):
    payload = _draft(app)
    assert _audit(app, payload).status == compliance.PASSED_CHECKS
    assert briefs.brief_id(payload) == briefs.brief_id(_draft(app))


def test_draft_carries_the_funnels_exact_disclosure(app):
    payload = _draft(app)
    line = app["offer"].price_line(app["claims"])
    assert payload["price_disclosure"] == line
    assert line in payload["core_body"]["spoken_script"]


def test_gate_never_approves(app):
    assert _audit(app, _draft(app)).status != "APPROVED"


@pytest.mark.parametrize("text, rule", [
    ("Are you struggling in bed?", "personal_attribute"),
    ("Fix your weak stamina", "personal_attribute"),
    ("See results in 3 days", "timeframe"),
    ("Start free today", "free"),
    ("Only $5 to begin", "price"),
    ("Just $1 to start", "price"),
])
def test_ad_rules_fire_on_spoken_copy(app, text, rule):
    payload = _draft(app)
    payload["hooks"][0]["spoken_text"] = text
    result = _audit(app, payload)
    assert result.status == compliance.FLAGGED
    assert rule in _rules(result)


def test_funnel_claims_apply_to_ads(app):
    payload = _draft(app)
    payload["hooks"][0]["on_screen_caption"] = "Guaranteed results"
    assert "claims" in _rules(_audit(app, payload))


def test_medical_professional_avatar_is_rejected(app):
    payload = _draft(app)
    payload["hooks"][0]["visual_prompt"] = ("Photorealistic 40-year-old male urology specialist in "
                                            "dark navy scrubs inside a medical office --ar 9:16")
    assert "impersonation" in _rules(_audit(app, payload))


def test_fake_buttons_on_static_cards_are_rejected(app):
    payload = _draft(app)
    payload["static_quiz_cards"][0]["background_prompt"] = "header + 4 clickable-style selection buttons"
    assert "fake_ui" in _rules(_audit(app, payload))


def test_real_quiz_screen_recording_is_allowed(app):
    payload = _draft(app)
    payload["core_body"]["ui_broll_cue"] = "Screen recording of the real quiz progress bar"
    assert "fake_ui" not in _rules(_audit(app, payload))


def test_cards_must_mirror_real_questions(app):
    payload = _draft(app)
    payload["static_quiz_cards"][0]["options"] = ["An option the quiz never offers"]
    assert "shape" in _rules(_audit(app, payload))
    payload = _draft(app)
    payload["static_quiz_cards"][0]["question_key"] = "made_up"
    assert "shape" in _rules(_audit(app, payload))


def test_destination_must_be_the_funnels_own_host(app):
    payload = _draft(app)
    payload["destination_url"] = "https://elsewhere.example/quiz"
    assert "shape" in _rules(_audit(app, payload))


def test_disclosure_must_match_configured_prices(app):
    payload = _draft(app)
    payload["price_disclosure"] = "$1 today for 7 days, then $9/month until you cancel"
    assert "shape" in _rules(_audit(app, payload))


def test_copied_competitor_text_is_rejected(app):
    reference = "most men ignore the one muscle group that holds their whole core together every day"
    payload = _draft(app)
    payload["hooks"][1]["spoken_text"] = ("Truth is most men ignore the one muscle group that holds "
                                          "their whole core together.")
    assert "copied_text" in _rules(_audit(app, payload, references=[reference]))


def test_flagged_copy_is_never_rewritten(app):
    payload = _draft(app)
    payload["hooks"][0]["spoken_text"] = "Guaranteed results in 3 days"
    before = copy.deepcopy(payload)
    _audit(app, payload)
    assert payload == before


def test_classifier_is_add_only_and_fails_closed(app):
    payload = _draft(app)
    assert _audit(app, payload, classifier=lambda f, t: []).passed
    flagged = _audit(app, payload, classifier=lambda f, t: [policy.Finding("llm", f, "x")]
                     if f == "concept_name" else [])
    assert flagged.status == compliance.FLAGGED

    def broken(field, text):
        raise TimeoutError("model down")
    assert "classifier_error" in _rules(_audit(app, payload, classifier=broken))

    bad = _draft(app)
    bad["hooks"][0]["spoken_text"] = "Are you struggling in bed?"
    assert not _audit(app, bad, classifier=lambda f, t: []).passed


def test_pasted_blueprint_example_brief_is_flagged(app):
    """The example brief in web2app_marketing_agentic_setup_blueprint.md, pointed at this funnel."""
    payload = _draft(app)
    payload["core_body"]["spoken_script"] = (
        "Most men ignore the pubococcygeus muscle group. When these deep pelvic floor fibers weaken "
        "from years of sitting, lower-body stamina and control drop fast.")
    payload["hooks"] = [
        {"hook_id": "hook_01_metaphor", "archetype": "Visual Metaphor",
         "spoken_text": "Training stamina without strengthening your pelvic floor is like firing a "
                        "cannon from a canoe.",
         "on_screen_caption": "Weak base = zero control", "visual_prompt": "braided rope --ar 9:16"},
        {"hook_id": "hook_02_authority", "archetype": "Specialist Explainer",
         "spoken_text": "If you think stamina is just cardio, your core is hiding something critical.",
         "on_screen_caption": "90% of men miss this muscle",
         "visual_prompt": "Photorealistic male urology specialist in navy scrubs --ar 9:16"},
    ]
    result = _audit(app, payload)
    assert result.status == compliance.FLAGGED
    assert {"impersonation", "personal_attribute"} <= _rules(result)
