"""Copy guardrails for kegeldoc: forbidden vocab, approved framing, disclosures.

Forked from the Rank-4 funnel's guardrail with the same API and the same reason for being
code: a sexual-health product sits in the most heavily policed corner of health copy, and
"copy drift from product truth" (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.5) here reads as an
unlicensed medical claim. `tests/test_copy_guardrails.py` runs this list against every
template, the copy module and the quiz bank.

The vocabulary differs from the routine funnel in one deliberate direction. That funnel bans
"doctor", "treat" and "symptom" outright because it never needs them. This one does: its safety
screens must send people with pain, bleeding or a recent birth to a clinician, and a guardrail
that blocks the referral is a guardrail that makes the funnel less safe. So the referral words
are allowed and the *claims* are scoped: cure and treatment verbs, endorsement language (a brand
called kegeldoc must never imply a doctor stands behind it), condition names used as promises,
and outcome guarantees.

  * `FORBIDDEN_TERMS` blocks words that assert a medical or physical outcome we cannot
    promise, or an endorsement we do not have. Matched on word-initial prefixes,
    case-insensitive, per line, with negations ("not medical advice") skipped.
  * `APPROVED_FRAMINGS` is the product-truth vocabulary the funnel may promise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _terms(*words: str) -> tuple[str, ...]:
    """Package a term list. Kept as a function so exemptions can sit next to the words."""
    return tuple(words)


#: Whole-line exemptions: disclaimer sentences whose content is a negation or a referral.
#: Matched by full-line equality after whitespace collapse and lowercasing, so a claim cannot
#: hide inside one.
DISCLAIMER_EXEMPTIONS: frozenset[str] = frozenset({
    "kegeldoc gives general pelvic floor exercise guidance for adults. it is not medical "
    "advice, it is not a diagnosis, and it does not treat any condition. if you have pain, "
    "bleeding, are pregnant, have recently given birth or had pelvic surgery, speak to a "
    "doctor or pelvic health physiotherapist before starting.",
    "we store no name, no address and no medical record on this page, and we ask for none. "
    "payment details go to the payment processor and never to us; we keep the last four "
    "digits and the card brand so a receipt is recognisable.",
    "pelvic floor exercise guidance only. not medical advice, not a diagnosis, and no "
    "promised result. see the terms page.",
})


#: Words that imply cure, treatment, endorsement or a guaranteed body outcome.
FORBIDDEN_TERMS: tuple[str, ...] = _terms(
    # cure / treatment verbs, scoped so "prostate treatment" in a history question survives
    "cure", "cures", "curing", "remedy", "remedies", "heals", "healing", "diagnos", "prescri",
    "treats", "treating", "treatment for", "treat your", "treat erect", "treat incont",
    "treat premature", "reverses", "reverse your",
    # condition names used as promises; the funnel describes what people notice instead
    "erectile dysfunction", "impotence", "impotent", "premature ejaculation",
    "incontinence cure", "prolapse repair",
    # endorsement and evidence-strength claims; the name must never imply a doctor behind it
    "doctor-approved", "doctor approved", "doctor-designed", "doctor designed",
    "doctor-recommended", "recommended by doctors", "urologist-approved",
    "urologist approved", "gynecologist-approved", "clinically proven", "clinically tested",
    "scientifically proven", "medically proven", "medical-grade", "fda",
    "proven to", "proven results", "studies prove", "science proves",
    # drug comparisons and medication advice
    "better than pills", "instead of pills", "instead of medication", "replace your medication",
    "stop taking", "drug-free alternative", "natural viagra", "viagra", "cialis",
    "testosterone", "boost your libido", "libido booster",
    # guaranteed outcomes and their marketing shapes
    "guarantee", "guaranteed", "miracle", "instant results", "overnight results",
    "permanent results", "never leak again", "no more leaks", "stop leaking",
    "last longer in bed", "last for hours", "rock hard", "rock-hard", "harder erections",
    "bigger", "enlarge", "enlargement", "penis size", "tighter vagina", "vaginal tightening",
    "in just 2 weeks", "in just two weeks", "in just 4 weeks", "in just four weeks",
    "84%", "% of men", "% of women", "out of 10 men", "out of 10 women",
)

#: What the funnel is allowed to say it delivers.
APPROVED_FRAMINGS: tuple[str, ...] = (
    "pelvic floor exercise",
    "kegel plan",
    "exercise guidance",
    "session structure",
    "guided holds",
    "daily drill",
    "hold time",
    "check-in",
    "habit structure",
    "4-week plan",
    "week 1 preview",
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
