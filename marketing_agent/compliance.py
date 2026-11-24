"""The gate every brief passes before anything is rendered or launched.

Outcomes: `FLAGGED` (any finding, from any layer) or `PASSED_CHECKS`. Never `APPROVED`: approval
is a named human action (`store.approve`), recorded as an audit row.

Layers:
  1. shape        `briefs.validate_payload` (real quiz questions, own host, exact disclosure)
  2. deterministic `policy.check_text` on every string (funnel claims + ad rules)
  3. classifier   optional, add-only. It may contribute findings; it cannot clear one, and an
                  exception from it is itself a finding (fail closed), never a pass.
  4. ai_review    optional brief-level reviewer (`ai.make_reviewer`), same add-only, fail-closed
                  contract. Runs after layers 1-2, so it can never bypass the funnel's `claims.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from . import briefs, policy
from .offer import OfferTerms

FLAGGED = "FLAGGED"
PASSED_CHECKS = "PASSED_CHECKS"

#: (field, text) -> findings. A model-backed policy classifier plugs in here later.
Classifier = Callable[[str, str], list[policy.Finding]]
#: [(field, text), ...] -> findings, one call per brief.
Reviewer = Callable[[list[tuple[str, str]]], list[policy.Finding]]


@dataclass(frozen=True)
class AuditResult:
    status: str
    findings: tuple[policy.Finding, ...] = ()
    layers: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.status == PASSED_CHECKS

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "layers": list(self.layers),
                "findings": [f.to_dict() for f in self.findings]}


def audit_brief(payload: dict[str, Any], *, quiz, claims, offer: OfferTerms,
                references: Iterable[str] = (),
                classifier: Classifier | None = None,
                reviewer: Reviewer | None = None) -> AuditResult:
    findings: list[policy.Finding] = []
    layers = ["shape"]
    try:
        briefs.validate_payload(payload, quiz=quiz, claims=claims, offer=offer)
    except briefs.BriefInvalid as exc:
        findings += [policy.Finding("shape", "brief", p) for p in exc.problems]

    layers.append("deterministic")
    refs = tuple(references)
    strings = briefs.iter_strings(payload)
    for field_path, text in strings:
        findings += policy.check_text(field_path, text, claims=claims, offer=offer,
                                      references=refs)

    if classifier is not None:
        layers.append("classifier")
        for field_path, text in strings:
            try:
                findings += list(classifier(field_path, text))
            except Exception as exc:  # noqa: BLE001 - a broken classifier is not a pass
                findings.append(policy.Finding("classifier_error", field_path, str(exc)[:200]))

    if reviewer is not None:
        layers.append("ai_review")
        try:
            findings += list(reviewer(strings))
        except Exception as exc:  # noqa: BLE001 - a failed review is a flag, never a pass
            findings.append(policy.Finding("ai_review_error", "brief",
                                           f"{type(exc).__name__}: {str(exc)[:180]}"))

    return AuditResult(FLAGGED if findings else PASSED_CHECKS, tuple(findings), tuple(layers))
