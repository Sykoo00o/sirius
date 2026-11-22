"""Copy guardrails: no claim may reach the page, every disclosure must.

Two directions, because a funnel fails compliance in both. Too *strong* wording is a
deception risk ("heals", "guaranteed", "fat loss"). Too *thin* disclosure is a chargeback
and a card-network problem: a trial that does not say what recurs is the pattern the Phase 0
spec exists to avoid.

Why tests rather than only the `/health` page: `/health` is checked by a human who remembers
to look. These run on every edit, including the edit nobody remembers, which in this lane
means the copy edit that makes the product sound better for one second and illegal
afterwards.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

FUNNEL_ROOT = Path(__file__).resolve().parents[1]
if str(FUNNEL_ROOT) not in sys.path:
    sys.path.insert(0, str(FUNNEL_ROOT))

import claims                                    # noqa: E402
from config import FunnelSettings                # noqa: E402
from funnel_copy import (ARTIFACT_FREE, ARTIFACT_PAID, TRUST_POINTS, build_copy,  # noqa: E402
                         disclosure_coverage, guardrail_scan)

TEMPLATE_DIR = FUNNEL_ROOT / "templates"

#: Amounts this suite asserts against. Distinct from the shipped defaults on purpose: if a
#: test hardcodes $2.99 it passes while measuring nothing when the price moves, which is the
#: exact drift the single-source copy module was written to prevent.
TEST_SETTINGS = FunnelSettings(trial_amount_minor=299, rebill_amount_minor=2499,
                               trial_days=7, refund_window_days=14, currency="usd")


@pytest.fixture(scope="module")
def copy_obj():
    return build_copy(TEST_SETTINGS)


# ---------------------------------------------------------------------------
# Guardrail: nothing claim-shaped may render
# ---------------------------------------------------------------------------

def _template_texts() -> list[tuple[str, str]]:
    return [(path.name, path.read_text(encoding="utf-8"))
            for path in sorted(TEMPLATE_DIR.glob("*.html"))]


def test_every_template_exists_and_is_nonempty():
    """A renamed template is not "no violations", it is a test that stopped looking."""
    names = {path.name for path in TEMPLATE_DIR.glob("*.html")}
    for required in ("base.html", "landing.html", "quiz_step.html", "summary.html",
                     "preview.html", "checkout.html", "policy.html"):
        assert required in names, f"{required} missing: the copy scan silently shrank"
    assert _template_texts(), "no templates found at all"


@pytest.mark.parametrize("name,text", _template_texts(),
                         ids=[p.name for p in sorted(TEMPLATE_DIR.glob("*.html"))])
def test_template_source_is_claim_free(name: str, text: str):
    """Scan template prose, not only rendered output.

    Rendered output is the narrower check: a banned phrase sitting in a Jinja comment or on
    a code path no test visits is invisible there. Scanning the source means the phrase
    cannot survive by being unreachable today and reachable after the next edit.
    """
    # Jinja control tags and comments are dropped before matching: `{% if %}` is not prose,
    # and a comment explaining the guardrail mentions the words the guardrail bans by name.
    # Comments are stripped inside `claims.violations` for exactly that reason; asserting the
    # tag-strip here keeps a `{% for treatment in ... %}` from failing the build on a
    # variable name.
    prose = re.sub(r"\{%.*?%\}", " ", text, flags=re.DOTALL)
    hits = claims.violations(prose)
    assert not hits, f"{name}: forbidden claim in template prose: {hits}"


def test_jinja_comments_are_not_scanned_as_copy():
    """A comment that quotes the banned vocabulary must not fail the guardrail.

    This test is about the scanner, not the copy. Without it, the first author to write
    "no medical claims here" inside a comment gets a red run and fixes it by loosening
    `FORBIDDEN_TERMS`, which is the failure that quietly removes the protection.
    """
    assert claims.violations("{# this line mentions diagnosis and guarantees #}") == []
    assert claims.violations("<p>heals inflammation</p>")  # real copy still caught


def test_module_copy_is_claim_free(copy_obj):
    assert guardrail_scan(copy_obj) == []


def test_artifact_lists_are_claim_free():
    """Free-vs-paid bullets are the highest-risk copy in the funnel.

    They describe what the product *does*, which is precisely where a writer reaches for
    "improves", "boosts", "supports your goals".
    """
    for bullet in (*ARTIFACT_FREE, *ARTIFACT_PAID):
        assert not claims.violations(bullet), bullet
    for _title, detail in TRUST_POINTS:
        assert not claims.violations(detail), detail


def test_plan_builder_strings_are_claim_free():
    """The deterministic engine feeds the artefact screen, so its tables, and every plan it can
    emit, are copy too. Plans are generated across both tracks, every goal and the branches that
    add safety and lifestyle notes, so a note that only appears for one answer is still scanned."""
    import plan_builder
    from plan_builder import Profile, build_plan

    corpus = list(plan_builder.GOAL_NAMES.values()) + list(plan_builder.WHEN_LABELS.values())
    for name, text in [*plan_builder.DRILLS.values(), plan_builder.URGE_DRILL, plan_builder.PAIN_DRILL]:
        corpus += [name, text]
    corpus += [arc["arc"] for arc in plan_builder.WEEK_ARCS]
    loud = dict(smoke="yes", alcohol="daily", diet=1, sitting="gt8", stress=5)
    profiles = [Profile(track="men", goal=g, morning="never", erect_since="lt1", prostate="recent",
                        safety=frozenset({"blood", "burning"}), **loud)
                for g in ("erection", "ejac", "both", "bladder", "general")]
    profiles += [Profile(track="women", goal=g, urge="often", night="3", births="pregnant",
                         heaviness="often", sexpain="most", **loud)
                 for g in ("leaks", "birth", "intimacy", "menopause", "general")]
    for profile in profiles:
        plan = build_plan(profile)
        corpus += [plan.headline, plan.summary, plan.safety_note, *plan.tips]
        corpus += [step for day in plan.days for step in (day.title, *day.steps)]
        corpus += [part for row in plan.metrics for part in row]
    hits = claims.violations("\n".join(corpus))
    assert not hits, f"plan builder vocabulary carries claims: {hits}"


def test_quiz_bank_is_claim_free():
    """Study facts and question help text are what a visitor reads for four minutes."""
    import quiz

    corpus = []
    for q in quiz.BANK:
        corpus += [q.title, q.help, q.body, q.source, q.low, q.high, *q.subs.values()]
        corpus += [label for _value, label in q.options]
    hits = claims.violations("\n".join(corpus))
    assert not hits, f"quiz bank carries claims: {hits}"


def test_bare_words_are_deliberately_not_banned():
    """Scoped phrases, not words. A sexual-health funnel must be able to send people to a
    clinician and ask about prostate treatment; banning the words would make it less safe."""
    for harmless in ("healthy routine", "prostate treatment", "speak to a doctor",
                     "pelvic health physiotherapist", "not medical advice", "erections",
                     "finish sooner than you would like", "leaks on effort"):
        assert not claims.violations(harmless), f"over-broad term blocks: {harmless!r}"
    for claim in ("cures erectile dysfunction", "doctor-approved plan", "clinically proven",
                  "never leak again", "guaranteed results", "better than pills",
                  "84% of men improved", "bigger and harder erections", "treats incontinence"):
        assert claims.violations(claim), f"claim slipped through: {claim!r}"


def test_disclaimer_negations_do_not_count_as_violations():
    """The mandated disclaimer says "not medical advice, not a diagnosis".

    Exemption is granted per whole line, not per forgiven word, so a claim cannot hide on
    the same line as a sanctioned negation.
    """
    for line in claims.DISCLAIMER_EXEMPTIONS:
        assert claims.violations(line) == []
    smuggled = f"{next(iter(claims.DISCLAIMER_EXEMPTIONS))} and it guarantees fat loss"
    assert claims.violations(smuggled), "a claim hid beside a disclaimer line"


# ---------------------------------------------------------------------------
# Disclosure: the four mandated sentences must be emitted
# ---------------------------------------------------------------------------

def test_all_four_disclosures_present(copy_obj):
    coverage = disclosure_coverage(copy_obj)
    assert [key for key, _present, _expected in coverage] == [
        "price_line", "renewal_line", "refund_line", "cancel_line"]
    assert all(present for _key, present, _expected in coverage), coverage


def test_disclosures_carry_the_configured_amounts(copy_obj):
    """Amounts come from settings, never typed into a template.

    Asserted on the rendered sentences rather than on the settings object, because the bug
    worth catching is a template that renders a stale literal next to a live number.
    """
    rendered = "\n".join(copy_obj.disclosures.values())
    assert "$2.99" in rendered and "$24.99" in rendered
    assert "7 days" in rendered
    assert "14 days" in rendered


def test_price_change_moves_every_slot_at_once():
    """One price edit, zero stale slots. The reason copy lives in code."""
    moved = build_copy(FunnelSettings(trial_amount_minor=100, rebill_amount_minor=1500,
                                      trial_days=3, refund_window_days=30))
    joined = "\n".join(moved.disclosures.values())
    # `money()` drops trailing zeros, so $15.00 renders as "$15" and $1.00 as "$1": whole
    # dollars read better on a card than $15.00, and a shopper who sees "$15/month" is not
    # misled. The assertion follows the product decision rather than the minor-unit input.
    assert "$1 " in joined and "$15" in joined, joined
    assert "3 days" in joined and "30 days" in joined
    assert "2.99" not in joined and "24.99" not in joined, "a slot kept the old price"
    assert disclosure_coverage(moved) and all(
        present for _k, present, _e in disclosure_coverage(moved))


def test_refund_terms_match_the_phase_zero_spec(copy_obj):
    """14-day money-back on the trial charge, and one-click cancel. Both are spec text,
    not marketing copy, so they are asserted as facts about the offer."""
    assert "14 days" in copy_obj.disclosures["refund_line"]
    assert "one click" in copy_obj.disclosures["cancel_line"].lower()
    pages = copy_obj.policy_pages()
    assert "refund" in pages, (
        f"policy_pages lost its refund key ({sorted(pages)}), which would have made the "
        "next line a KeyError instead of a failing assertion")
    refund_page = "\n".join(pages["refund"][1])
    assert not claims.violations(refund_page)
    assert "$2.99" in refund_page


def test_trial_is_disclosed_before_payment_everywhere_it_is_offered(copy_obj):
    """Phase 0 criterion: price disclosed beside the card field, not in a footer.

    Checked against the copy the checkout screen iterates. The structural half (the price
    block sitting *inside* the form element) is covered by the walkthrough script.
    """
    assert "$2.99" in copy_obj.primary_cta or "$2.99" in copy_obj.cta_fine_print
    card_label = copy_obj.card_stub_note + copy_obj.disclosures["price_line"]
    assert "$2.99" in card_label and "$24.99" in card_label
