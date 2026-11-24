"""Render manifests for approved briefs, and staging that only ever produces receipts.

A **manifest** is the complete, renderer-agnostic recipe for one planned variant: canvas, layers or
timeline, every line of text, and the tagged landing URL. It is built only from an APPROVED brief,
and before a file is written the shape check and every deterministic ad rule run again on the exact
strings the manifest carries (defence in depth: a manifest is what a renderer would actually draw).

* `STATIC_QUIZ_CARD` (1080x1350, 4:5): background prompt, the real quiz question as the header, its
  real option labels as a plain text list, footer, and the funnel's exact disclosure line.
* `VIDEO_HOOK_COMBO` (1080x1920, 9:16): hook segment 0-3 s, core body after it, burned-in captions,
  and an end card with the exact disclosure line and the funnel host.

**Staging** (`stage`) is where rendering and publishing would happen. Rendering would send a
manifest to an external, paid media service; publishing would upload to the ad account. Both return
a receipt and execute nothing: `dry_run` / `not_armed` while gated, `refused_not_built` when armed,
because no render or upload client exists. Every receipt is an `agent_logs` row.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import briefs, meta, policy
from .store import APPROVED, utc_now

CARD_CANVAS = {"width": 1080, "height": 1350, "aspect": "4:5"}
VIDEO_CANVAS = {"width": 1080, "height": 1920, "aspect": "9:16"}
HOOK_SECONDS = 3.0

#: Constant instructions for any renderer. Not ad copy, so not run through the copy rules.
RENDER_RULES = (
    "Options are drawn as a plain text list: no shapes, outlines or controls around them.",
    "Do not draw interface elements of any kind (no controls, icons, cursors, toggles, bars).",
    "No people in medical clothing or clinical settings; no logos other than the funnel's own name.",
    "The disclosure line is rendered verbatim, legible, and on screen for at least 2 seconds.",
)


class ManifestRefused(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _safe_name(variant_code: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "__", variant_code)


def _texts(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """(field, text) pairs a renderer would draw or speak, with leaf names the rules know."""
    out: list[tuple[str, str]] = []
    for layer in manifest.get("layers", []):
        kind = layer["kind"]
        if kind == "background":
            out.append(("card.background_prompt", layer["prompt"]))
        elif kind == "options":
            out += [("card.option", text) for text in layer["items"]]
        elif kind == "disclosure":
            out.append(("card.price_disclosure", layer["text"]))
        else:
            out.append((f"card.{kind}_text", layer["text"]))
    for segment in manifest.get("timeline", []):
        for key in ("spoken_text", "caption", "visual_prompt", "ui_broll_cue"):
            if segment.get(key):
                leaf = "visual_prompt" if key == "visual_prompt" else key
                out.append((f"{segment['segment']}.{leaf}", segment[key]))
        if segment.get("disclosure"):
            out.append((f"{segment['segment']}.price_disclosure", segment["disclosure"]))
    return out


def card_manifest(plan: briefs.VariantPlan, payload: dict[str, Any], card: dict[str, Any],
                  price_line: str) -> dict[str, Any]:
    return {
        "variant_code": plan.variant_code, "asset_type": plan.asset_type,
        "canvas": CARD_CANVAS,
        "layers": [
            {"kind": "background", "prompt": card.get("background_prompt", "")},
            {"kind": "header", "text": card["header_text"]},
            {"kind": "options", "items": list(card["options"]), "style": "plain_text_list"},
            {"kind": "footer", "text": card.get("footer_text", "")},
            {"kind": "disclosure", "text": price_line},
        ],
        "question_key": card["question_key"],
        "landing_url": briefs.landing_url(payload, plan),
        "render_rules": list(RENDER_RULES),
    }


def video_manifest(plan: briefs.VariantPlan, payload: dict[str, Any], hook: dict[str, Any],
                   price_line: str) -> dict[str, Any]:
    body = payload["core_body"]
    host = re.sub(r"^https?://", "", str(payload["destination_url"])).rstrip("/")
    return {
        "variant_code": plan.variant_code, "asset_type": plan.asset_type,
        "canvas": VIDEO_CANVAS, "captions": "burned_in",
        "timeline": [
            {"segment": "hook", "start_s": 0.0, "end_s": HOOK_SECONDS, "hook_id": hook["hook_id"],
             "spoken_text": hook["spoken_text"], "caption": hook.get("on_screen_caption", ""),
             "visual_prompt": hook.get("visual_prompt", "")},
            {"segment": "body", "start_s": HOOK_SECONDS, "body_id": body["body_id"],
             "spoken_text": body["spoken_script"], "ui_broll_cue": body.get("ui_broll_cue", "")},
            {"segment": "end_card", "duration_s": 3.0, "disclosure": price_line,
             "caption": host},
        ],
        "landing_url": briefs.landing_url(payload, plan),
        "render_rules": list(RENDER_RULES),
    }


def build_manifests(bid: str, brief: dict[str, Any], *, quiz, claims, offer,
                    out_dir: Path, references=()) -> list[dict[str, Any]]:
    """Manifests for every planned variant of an APPROVED brief, written as JSON files."""
    if brief.get("status") != APPROVED:
        raise ManifestRefused([f"brief is {brief.get('status')}; only APPROVED briefs get manifests"])
    payload = brief["payload"]
    problems: list[str] = []
    try:
        briefs.validate_payload(payload, quiz=quiz, claims=claims, offer=offer)
    except briefs.BriefInvalid as exc:
        problems += exc.problems
    price_line = offer.price_line(claims)
    hooks = {h["hook_id"]: h for h in payload.get("hooks", [])}
    cards = {c["card_id"]: c for c in payload.get("static_quiz_cards", [])}
    manifests = []
    for plan in briefs.planned_variants(bid, payload):
        tail = plan.variant_code.rsplit(":", 1)[-1]
        if plan.asset_type == "STATIC_QUIZ_CARD":
            manifest = card_manifest(plan, payload, cards[tail], price_line)
        else:
            manifest = video_manifest(plan, payload, hooks[tail], price_line)
        for field, text in _texts(manifest):
            for finding in policy.check_text(field, text, claims=claims, offer=offer,
                                             references=references):
                problems.append(f"{plan.variant_code} {finding.rule} {finding.field}: "
                                f"{finding.detail}")
        manifests.append(manifest)
    if problems:
        raise ManifestRefused(problems)
    out_dir.mkdir(parents=True, exist_ok=True)
    for manifest in manifests:
        body = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        manifest["manifest_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        manifest["path"] = str(out_dir / f"{_safe_name(manifest['variant_code'])}.json")
        Path(manifest["path"]).write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                          encoding="utf-8")
    return manifests


def manifest_path(out_dir: Path, variant_code: str) -> Path:
    return out_dir / f"{_safe_name(variant_code)}.json"


def _receipt(store, settings, *, action: str, target: str, status: str,
             reason: str) -> dict[str, Any]:
    receipt = {"action": action, "target": target, "status": status, "executed": False,
               "dry_run": settings.dry_run, "reason": reason, "at": utc_now()}
    store.log(event="marketing.receipt", level="info" if status in ("dry_run", "not_armed")
              else "warning", message=f"{action} {target}: {status}", context=receipt)
    return receipt


def stage(variant_code: str, *, settings, store, out_dir: Path) -> list[dict[str, Any]]:
    """Render, then publish, one variant. Neither ever executes; each returns a receipt."""
    variant = store.variant(variant_code)
    if variant is None or not manifest_path(out_dir, variant_code).is_file():
        return [_receipt(store, settings, action="stage", target=variant_code,
                         status="refused_no_manifest",
                         reason="build the manifest from an approved brief first")]
    receipts = []
    if not settings.can_render():
        why = "MARKETING_DRY_RUN is on" if settings.dry_run else "MARKETING_RENDER_ENABLED is off"
        receipts.append(_receipt(store, settings, action="render", target=variant_code,
                                 status="dry_run" if settings.dry_run else "not_armed", reason=why))
    else:
        receipts.append(_receipt(store, settings, action="render", target=variant_code,
                                 status="refused_not_built",
                                 reason="render is armed but no media renderer client exists"))
    try:
        status = meta.UnarmedMetaClient(settings, store).publish(variant_code, "stage")
        receipts.append(_receipt(store, settings, action="publish", target=variant_code,
                                 status="dry_run" if settings.dry_run else "not_armed",
                                 reason=f"{status}: publish gated"))
    except meta.NotBuilt as exc:
        receipts.append(_receipt(store, settings, action="publish", target=variant_code,
                                 status="refused_not_built", reason=str(exc)))
    return receipts
