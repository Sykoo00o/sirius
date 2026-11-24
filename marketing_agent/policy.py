"""Deterministic ad-copy rules, layered on top of the target funnel's own `claims.violations`.

Every rule returns findings; none rewrites text. A flagged brief stays flagged and a new one must
be drafted, which is the funnel's reject-not-scrub rule (`funnel_app/llm_flavour.py`) applied to
ads. Rules, each named so an audit row says which one fired:

  claims            the funnel's forbidden outcome vocabulary (one list per niche, site and ads)
  personal_attribute "you/your" next to a condition or insecurity (ad platforms reject these, and
                    they assert something about the viewer we cannot know)
  timeframe         promised results inside a stated time ("in 3 days", "by next week")
  impersonation     a medical professional as speaker or in a visual prompt
  fake_ui           non-functional buttons, play icons, notifications, "tap below" in ad media
  free              "free" anything, when the trial costs money
  price             a price is named without the funnel's exact disclosure sentence, or a price
                    that is not one of the funnel's configured amounts
  copied_text       an 8-word run shared with an ingested competitor reference
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .offer import OfferTerms


@dataclass(frozen=True)
class Finding:
    rule: str
    field: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "field": self.field, "detail": self.detail}


_CONDITION_WORDS = (
    r"weak|struggl\w*|embarrass\w*|insecur\w*|ashamed|problem\w*|dysfunction|leak\w*|"
    r"overweight|fat|belly|stamina|performance|in bed|lasting|last longer|anxious|lonely|"
    r"broke|debt|failing|lazy|out of shape|unattractive"
)
PERSONAL_ATTRIBUTE = re.compile(
    rf"\b(?:you|you're|youre|your|are you|do you|does your)\b[^.?!]{{0,40}}\b(?:{_CONDITION_WORDS})\b",
    re.I,
)
TIMEFRAME = re.compile(
    r"\b(?:in|within|after just|after only|by)\s+(?:\d+|one|two|three|a|seven|ten|fourteen)\s+"
    r"(?:minutes?|hours?|days?|nights?|weeks?)\b[^.?!]{0,40}\b(?:results?|change|fix\w*|"
    r"stronger|better|gone|transform\w*|see|feel)\b"
    r"|\b(?:results?|fix\w*|transform\w*)\b[^.?!]{0,30}\b(?:in|within)\s+\d+\s+(?:days?|weeks?|hours?)\b",
    re.I,
)
IMPERSONATION = re.compile(
    r"\b(?:doctor|dr\.|physician|urologist|gyn(?:a)?ecologist|dermatologist|clinician|nurse|"
    r"surgeon|therapist|specialist|scrubs|lab coat|stethoscope|medical office|clinic)\b",
    re.I,
)
FAKE_UI = re.compile(
    r"\b(?:tap|click|press|swipe)\s+(?:below|here|the button|a button|an option|to start)\b"
    r"|\bclickable\b|\bbuttons?\b|\bplay (?:button|icon)\b|\bnotification\b|\bcheckbox\b|"
    r"\bprogress bar\b|\bloading spinner\b",
    re.I,
)
FREE = re.compile(r"\bfree\b(?!\s*(?:-|of|from)\b)", re.I)
MONEY = re.compile(r"(?:\$|usd\s?|£|€)\s?\d+(?:[.,]\d{1,2})?", re.I)

#: Fields that are pictures or b-roll, where UI and impersonation rules apply with full force.
MEDIA_FIELDS = ("visual_prompt", "background_prompt", "ui_broll_cue")


def _norm_money(text: str) -> str:
    digits = re.sub(r"[^\d.]", "", text)
    try:
        value = float(digits)
    except ValueError:
        return text
    return f"{value:.2f}"


def check_text(field: str, text: str, *, claims, offer: OfferTerms,
               references: Iterable[str] = ()) -> list[Finding]:
    """All rules for one string. `field` is a dotted path like `hooks[2].spoken_text`."""
    findings: list[Finding] = []
    if not text:
        return findings
    leaf = field.rsplit(".", 1)[-1]
    is_media = leaf in MEDIA_FIELDS

    for term, snippet in claims.violations(text):
        findings.append(Finding("claims", field, f"{term!r} in {snippet!r}"))
    if not is_media:
        match = PERSONAL_ATTRIBUTE.search(text)
        if match:
            findings.append(Finding("personal_attribute", field, match.group(0)))
        match = TIMEFRAME.search(text)
        if match:
            findings.append(Finding("timeframe", field, match.group(0)))
    match = IMPERSONATION.search(text)
    if match:
        findings.append(Finding("impersonation", field, match.group(0)))
    # The core body's b-roll cue legitimately shows the real quiz UI as a screen recording; any
    # other media field (a generated picture) must not draw interface elements.
    if leaf != "ui_broll_cue":
        match = FAKE_UI.search(text)
        if match:
            findings.append(Finding("fake_ui", field, match.group(0)))
    if offer.trial_amount_minor > 0 and FREE.search(text):
        findings.append(Finding("free", field, "the trial is paid; 'free' misstates it"))

    allowed = {_norm_money(offer.trial_amount), _norm_money(offer.money(offer.rebill_amount_minor))}
    prices = MONEY.findall(text)
    for price in prices:
        if _norm_money(price) not in allowed:
            findings.append(Finding("price", field, f"{price} is not a configured funnel price"))
    if prices and leaf != "price_disclosure" and offer.price_line(claims) not in text:
        findings.append(Finding("price", field,
                                "names a price without the funnel's full disclosure sentence"))

    lowered = _shingles(text)
    for reference in references:
        overlap = lowered & _shingles(reference)
        if overlap:
            findings.append(Finding("copied_text", field,
                                    f"shares an 8-word run with a competitor reference: "
                                    f"{sorted(overlap)[0]!r}"))
            break
    return findings


def _shingles(text: str, size: int = 8) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}
