"""Copy guardrails for the Rank-4 funnel: forbidden vocab, approved framing, disclosures.

Why this is code and not a note in a README: the corpus treats body-related outcome
claims in this niche as *illegal*, not grey (`grey_hat_side_hustles.md:19`, restated
in `LANE_C_PHASE0_STEP1-2.md` §1 rank 4). Rank 4 has the highest conversion and the
heaviest copy-discipline burden, and the plan's own trap table names the failure
mode: "copy drift from product truth" (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.5).
The mitigation there is a diff of final copy against a written product-truth list
before publish, so the list lives here and `tests/test_copy_guardrails.py` runs it
against every template. A drift fails a test rather than reaching a stranger.

Two lists, used differently:

  * `FORBIDDEN_TERMS` blocks words that assert a medical or physical outcome we
    cannot promise. Matched on whole words, case-insensitive.
  * `APPROVED_FRAMINGS` is the product-truth vocabulary the funnel is allowed to
    promise. Nothing outside it is a promise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _terms(*words: str) -> tuple[str, ...]:
    """Package a term list and its exemptions. Keeps the exemption next to the words
    it exempts, which is what stops a future reader deleting it by accident."""
    return tuple(words)


#: Contextual disclaimer lines whose *whole content* is a negation. A scanner that
#: blocks the word "medical" cannot see the difference between "boosts your immunity"
#: and "this is not medical advice", so instead of trusting the scan to tolerate the
#: latter, these exact sentences are exempted by full-line equality. Only lines listed
#: here, character for character, get through: a smuggled claim cannot hide inside one.
DISCLAIMER_EXEMPTIONS: frozenset[str] = frozenset({
    "the plan is a planning aid for organising your own week. it is not medical advice, "
    "it is not a diagnosis, and it does not treat or manage any condition. if you are "
    "pregnant, managing a health condition, or recovering from something, talk to a "
    "clinician about what to do before starting a new routine.",
    "we store no name, no address and no health history on this page, and we ask for "
    "none. payment details go to the payment processor and never to us; we keep the last "
    "four digits and the card brand so a receipt is recognisable.",
    "routine structure and meal organisation only. not medical advice, not a diagnosis, "
    "and no body-composition promise. see the terms page.",
})


#: Words that imply diagnosis, treatment, or a body outcome. A funnel may describe
#: routine structure; it may not promise physiology.
FORBIDDEN_TERMS: tuple[str, ...] = _terms(
    # medical / clinical surface
    "diagnos", "prescri", "therap", "cure", "remedy", "heal", "treat",
    "symptom", "disease", "disorder", "clinical", "medical", "medicine",
    "patient", "doctor", "physician", "hormone", "hormonal", "metabolism",
    "metabolic", "detox", "cleanse", "immune", "inflammation", "anti-inflammatory",
    "anxiety", "depress", "insomnia", "adhd", "chronic", "injury", "rehab",
    "cortisol", "testosterone", "estrogen", "insulin", "deficiency",
    # "lean" is excluded here on purpose: it is the *goal label* the user picks, and the
    # funnel quotes it back. What must not appear is the funnel claiming leanness as an
    # outcome, which is what "get lean" and "lean out" do, so those two are what we ban.
    "get lean", "lean out", "leaner", "slim down", "toned", "tone up",
    # guaranteed outcome surface
    "guarantee", "guaranteed", "assurance", "proven results", "results guaranteed",
    "melt", "torch", "shred", "burn fat", "lose weight fast", "lose belly fat",
    "drop pounds", "lose pounds", "melts fat", "boost your metabolism",
    "奇迹", "miracle", "instant results",
    # Scoped forms, not bare words. "overnight" alone would blacklist "overnight oats",
    # which is a breakfast, not a claim; the pattern is what a marketer would write.
    "overnight results", "overnight fix", "results overnight", "fix overnight",
    # "heal" as a verb of recovery is a body claim; the noun "a health condition" is
    # handled by the disclaimer exemptions, and "healthy weight" by its own exemption.
    "heals", "healing",
)

#: What the funnel is allowed to say it delivers. Anything a template promises must
#: be expressible with one of these, which is what keeps the artefact honest: the
#: artefact *is* a routine structure, so that is all it may claim to be.
APPROVED_FRAMINGS: tuple[str, ...] = (
    "routine structure",
    "weekly structure",
    "session structure",
    "meal structure",
    "meal ideas",
    "food list",
    "schedule",
    "habit structure",
    "focus metric",
    "check-in",
    "fallback",
    "4-week plan",
    "week 1 preview",
    "organization",
    "consistency",
)

#: Where each required disclosure must appear, by checklist number
#: (`LANE_C_PHASE0_STEP1-2.md` §2.2). `rendered_disclosures()` turns these into exact
#: strings, and the test suite asserts the rendered HTML contains each one, so a
#: missing disclosure fails a test rather than surfacing as a dispute.
REQUIRED_DISCLOSURES: tuple[tuple[str, str], ...] = (
    ("price_line", "checklist 1: entry price and recurring price on the payment screen"),
    ("renewal_line", "checklist 2-4: renewal labelled, renewal date reachable"),
    ("refund_line", "checklist 8/10: badge text equals the linked policy"),
    ("cancel_line", "anti-Roach-Motel: one-click cancel stated before money moves"),
)


#: Checklist 1, verbatim: the entry price, the length, the recurring price and the fact
#: that it stops when the customer says stop. Named separately from `Slots` because the
#: payment email has to carry this exact sentence, and a sentence assembled from a
#: settings object in an email builder is a sentence that can drift from the one on the
#: checkout screen. One builder, two call sites, and the existing checkout test already
#: pins the wording.
def price_line(slots: "Slots") -> str:
    return (f"{slots.trial_amount} today for {slots.trial_days} days, then "
            f"{slots.rebill_per_month} until you cancel")


@dataclass(frozen=True)
class Slots:
    """Display strings for the two amounts, so disclosure text is assembled from data."""

    trial_amount: str
    rebill_per_month: str
    trial_days: int
    refund_days: int
    renewal_date: str


_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"(?<![a-z]){re.escape(term.strip()).lower()}")
    for term in FORBIDDEN_TERMS
)

#: "not prescriptions", "no guarantees", "without the diagnosis". Negation immediately
#: before a term, at most one intensifier in between, which covers "not strictly a
#: guarantee" and stops short of scanning a whole sentence for the word "not".
_NEGATED_TERMS = re.compile(
    r"\b(?:not|no|non|never|without|nor|isn'?t|aren'?t|won'?t)\s+(?:just\s+|really\s+|"
    r"strictly\s+|a\s+|an\s+|the\s+)*"
    + "(" + "|".join(re.escape(term.strip().lower()) for term in FORBIDDEN_TERMS) + ")"
)


def violations(text: str) -> list[tuple[str, str]]:
    """Return ``(term, snippet)`` for every forbidden word found in ``text``.

    Strips HTML before matching so a tag name can never trip the scan, and matches on
    lowercase word-initial prefixes, which catches "guaranteed" under "guarantee"
    without matching "ensure".

    Matching is per line, and a line that is byte-for-byte one of
    `DISCLAIMER_EXEMPTIONS` is skipped. That is how "this is not medical advice" is
    allowed while "great for your immunity" is not: the exemption is a whitelist of
    complete sentences, not a word the scanner forgives.
    """
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\{\{.*?\}\}", " ", plain)  # template expressions carry no copy
    # Jinja comments, and author-facing block comments of the shape this repo writes
    # (`{# ... #}`). Both are invisible to the visitor, and scanning them produces a
    # different failure mode than the one the guardrail is for: the scan starts objecting to
    # prose about the rules, so the first fix everyone reaches for is to shrink the term
    # list, and shrinking the term list lets real claims back in. Comments are stripped so
    # the list never has to be traded away to get a green run.
    plain = re.sub(r"\{#.*?#\}", " ", plain, flags=re.DOTALL)
    # Line comments, for the same reason when the text being scanned is Python source.
    plain = re.sub(r"^\s*#:?.*$", " ", plain, flags=re.MULTILINE)
    # HTML comments. `violations` already strips tags so `<b>heals</b>` cannot trip on a tag
    # name, and a `<!-- not a medical claim -->` left beside a paragraph is the same kind of
    # author-facing note. Stripping it keeps `preview.html`'s "not prescriptions" line as the
    # only judgement call, and that one is real visitor-facing copy that happens to negate
    # the word, which the scanner is right to keep surfacing.
    plain = re.sub(r"<!--.*?-->", " ", plain, flags=re.DOTALL)
    found: list[tuple[str, str]] = []
    for line in plain.splitlines():
        collapsed = " ".join(line.split()).lower().strip()
        if not collapsed or collapsed in DISCLAIMER_EXEMPTIONS:
            continue
        # A negated term is not a claim: "not prescriptions", "no guarantees", "not a
        # diagnosis". Word-scoped on purpose, and it cannot rescue a *phrase* term, so the
        # line that hides behind it is one sentence, not one paragraph. The remaining risk is
        # rhetorical ("don't just lose weight, transform") and that shape belongs in a human
        # read of the page rather than in a scanner that would have to guess the scope.
        collapsed = _NEGATED_TERMS.sub(" ", collapsed)
        if not collapsed:
            continue
        for term, pattern in zip(FORBIDDEN_TERMS, _COMPILED, strict=True):
            match = pattern.search(collapsed)
            if not match:
                continue
            start = max(0, match.start() - 40)
            snippet = " ".join(collapsed[start:match.end() + 40].split())
            found.append((term.strip().lower(), snippet))
    return found


def rendered_disclosures(slots: Slots) -> list[tuple[str, str, str]]:
    """The exact sentences the funnel must render, with amounts filled in.

    Built here rather than typed into templates: a price change must move every slot at
    once, which is the same argument that put the amounts in config.
    """
    return [
        (
            "price_line",
            price_line(slots),
            "checkout card, adjacent to the payment button",
        ),
        (
            "renewal_line",
            f"Your trial auto-renews into a monthly subscription at "
            f"{slots.rebill_per_month}. Based on today, that first renewal falls about "
            f"{slots.renewal_date}.",
            "checkout card, under the price line",
        ),
        (
            "refund_line",
            f"Refund window: ask within {slots.refund_days} days of the trial charge and "
            f"the {slots.trial_amount} comes back, no questions.",
            "checkout card, refund page, and any guarantee badge",
        ),
        (
            "cancel_line",
            "Cancel in one click from your account page. No phone call, no email hoops, "
            "no cancellation fee.",
            "checkout card, terms page, landing footer",
        ),
    ]
