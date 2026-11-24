"""Creative briefs: the contract, strict validation, and deterministic drafting.

A brief is one core body, up to ten hooks and up to five static quiz cards (the web2app blueprint's
modular formula). Two rules make it fit this funnel rather than any funnel:

* Quiz cards mirror the funnel's **real** questions. A card names a `question_key` that exists in
  the app's `quiz.QUESTIONS` and only shows option labels that question really offers, so the ad
  previews Screen 1 instead of inventing one.
* Any price the brief names is the funnel's own disclosure sentence (`claims.price_line`).

Drafting here is deterministic (templates + the real quiz). A model-backed drafter can replace
`draft_brief` later; its output goes through the same `validate_payload` and compliance gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .offer import OfferTerms

MAX_HOOKS = 10
MAX_CARDS = 5
MAX_CARD_OPTIONS = 4


class BriefInvalid(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def brief_id(payload: dict[str, Any]) -> str:
    """Content hash: the same draft twice is the same brief, which is the store's dedupe key."""
    return hashlib.sha256(canonical(payload).encode()).hexdigest()[:16]


def _askable(quiz) -> list[Any]:
    return [q for q in quiz.QUESTIONS
            if getattr(q, "kind", "single") in ("single", "multi") and len(q.options) >= 2]


def iter_strings(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Every human-visible or media-prompt string, with a dotted field path."""
    out: list[tuple[str, str]] = []
    body = payload.get("core_body") or {}
    for key in ("spoken_script", "ui_broll_cue"):
        out.append((f"core_body.{key}", str(body.get(key, ""))))
    for i, hook in enumerate(payload.get("hooks") or []):
        for key in ("spoken_text", "on_screen_caption", "visual_prompt"):
            out.append((f"hooks[{i}].{key}", str(hook.get(key, ""))))
    for i, card in enumerate(payload.get("static_quiz_cards") or []):
        for key in ("header_text", "footer_text", "background_prompt"):
            out.append((f"static_quiz_cards[{i}].{key}", str(card.get(key, ""))))
        for j, option in enumerate(card.get("options") or []):
            out.append((f"static_quiz_cards[{i}].options[{j}]", str(option)))
    out.append(("concept_name", str(payload.get("concept_name", ""))))
    if payload.get("price_disclosure"):
        out.append(("price_disclosure", str(payload["price_disclosure"])))
    return out


def validate_payload(payload: dict[str, Any], *, quiz, claims, offer: OfferTerms) -> None:
    """Raise `BriefInvalid` listing every shape problem. Content rules live in `compliance`."""
    problems: list[str] = []
    for key in ("app", "niche", "concept_name", "destination_url", "core_body", "hooks"):
        if not payload.get(key):
            problems.append(f"missing {key}")
    body = payload.get("core_body") or {}
    if not isinstance(body, dict) or not body.get("body_id") or not body.get("spoken_script"):
        problems.append("core_body needs body_id and spoken_script")
    hooks = payload.get("hooks") or []
    if not isinstance(hooks, list) or not 1 <= len(hooks) <= MAX_HOOKS:
        problems.append(f"hooks must be a list of 1-{MAX_HOOKS}")
    else:
        ids = [h.get("hook_id") for h in hooks if isinstance(h, dict)]
        if len(ids) != len(hooks) or not all(ids) or len(set(ids)) != len(ids):
            problems.append("every hook needs a unique hook_id")
        for i, hook in enumerate(hooks):
            if isinstance(hook, dict) and not hook.get("spoken_text"):
                problems.append(f"hooks[{i}] has no spoken_text")
    cards = payload.get("static_quiz_cards") or []
    if not isinstance(cards, list) or len(cards) > MAX_CARDS:
        problems.append(f"static_quiz_cards must be a list of 0-{MAX_CARDS}")
        cards = []
    by_key = {q.key: q for q in quiz.QUESTIONS}
    for i, card in enumerate(cards):
        question = by_key.get(card.get("question_key", ""))
        if question is None:
            problems.append(f"static_quiz_cards[{i}] names no real quiz question")
            continue
        labels = {label for _, label in question.options}
        options = card.get("options") or []
        if not options or len(options) > MAX_CARD_OPTIONS:
            problems.append(f"static_quiz_cards[{i}] needs 1-{MAX_CARD_OPTIONS} options")
        invented = [o for o in options if o not in labels]
        if invented:
            problems.append(f"static_quiz_cards[{i}] shows options the quiz does not offer: {invented}")
    public = offer.public_url
    destination = str(payload.get("destination_url", ""))
    if public and urlsplit(destination).netloc != urlsplit(public).netloc:
        problems.append(f"destination_url must be on the funnel's own host ({public})")
    disclosure = payload.get("price_disclosure")
    if disclosure and disclosure != offer.price_line(claims):
        problems.append("price_disclosure differs from the funnel's price_line")
    if problems:
        raise BriefInvalid(problems)


def draft_brief(*, app: str, niche: str, quiz, claims, offer: OfferTerms,
                concept_name: str = "quiz-preview-baseline", max_cards: int = 3) -> dict[str, Any]:
    """A deterministic brief built only from the funnel's real quiz and real price terms."""
    askable = _askable(quiz)
    branching = hasattr(quiz, "flow_for")
    count = "" if branching else f"{len(askable)} "
    price_line = offer.price_line(claims)
    first = askable[0].title if askable else "A few questions about your week"
    host = urlsplit(offer.public_url).netloc or offer.public_url

    hooks = [
        {"hook_id": "hook_01_question", "archetype": "question",
         "spoken_text": first,
         "on_screen_caption": f"Answer {count}questions, see your first week",
         "visual_prompt": "Person reading a phone at a kitchen table, morning light, candid, 9:16"},
        {"hook_id": "hook_02_preview_first", "archetype": "process",
         "spoken_text": f"Answer {count}questions and see your first week before you are asked to pay.",
         "on_screen_caption": "Preview first, decide after",
         "visual_prompt": "Over-the-shoulder shot of a paper weekly planner on a desk, soft daylight, 9:16"},
        {"hook_id": "hook_03_terms_upfront", "archetype": "transparency",
         "spoken_text": f"{price_line}. Cancel in one click from your account page.",
         "on_screen_caption": price_line,
         "visual_prompt": "Calm desk scene with a notebook and a cup of coffee, warm tones, 9:16"},
        {"hook_id": "hook_04_same_answers", "archetype": "specificity",
         "spoken_text": "Your answers set the layout of the week. Same answers, same plan, every time.",
         "on_screen_caption": "Built from your answers",
         "visual_prompt": "Hand writing a short list in a notebook, close-up, natural light, 9:16"},
    ]
    cards = []
    for question in askable[:max_cards]:
        cards.append({
            "card_id": f"card_{question.key}",
            "question_key": question.key,
            "header_text": question.title,
            "options": [label for _, label in question.options[:MAX_CARD_OPTIONS]],
            "footer_text": f"{offer.brand_name} quiz at {host}",
            "background_prompt": "Plain soft gradient background, no text, no interface elements, 4:5",
        })
    return {
        "app": app,
        "niche": niche,
        "concept_name": concept_name,
        "destination_url": f"{offer.public_url}/",
        "core_body": {
            "body_id": "body_01",
            "spoken_script": (
                f"Answer {count}questions about your goal and your week. {offer.brand_name} lays out "
                f"your first week from those answers and shows it to you before checkout. "
                f"{price_line}. Cancel in one click from your account page."),
            "ui_broll_cue": "Unedited screen recording of the real quiz, the summary and the Week 1 preview",
        },
        "hooks": hooks,
        "static_quiz_cards": cards,
        "price_disclosure": price_line,
    }


@dataclass(frozen=True)
class VariantPlan:
    variant_code: str
    asset_type: str


def planned_variants(bid: str, payload: dict[str, Any]) -> list[VariantPlan]:
    """Body × hook video combos and one static per card. Planned only: nothing is rendered."""
    body = payload["core_body"]["body_id"]
    plans = [VariantPlan(f"{bid}:{body}:{hook['hook_id']}", "VIDEO_HOOK_COMBO")
             for hook in payload.get("hooks", [])]
    plans += [VariantPlan(f"{bid}:{card['card_id']}", "STATIC_QUIZ_CARD")
              for card in payload.get("static_quiz_cards", [])]
    return plans


def landing_url(payload: dict[str, Any], plan: VariantPlan) -> str:
    """The ad's destination with the attribution the funnel records on `lp_view`.

    `utm_content` is the variant code, the join key from an ad to that visitor's trial rows;
    `utm_campaign` is the brief. The funnel keeps only these `utm_*` keys and caps each at 100
    characters (`telemetry.attribution_from_query`), which a variant code fits well inside.
    """
    from urllib.parse import urlencode
    query = urlencode({"utm_source": "meta", "utm_medium": "paid_social",
                       "utm_campaign": plan.variant_code.split(":", 1)[0],
                       "utm_content": plan.variant_code})
    base = str(payload["destination_url"])
    return f"{base}{'&' if '?' in base else '?'}{query}"
