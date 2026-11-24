"""Optional model hooks: a hook drafter and a secondary policy reviewer. Off by default.

`MARKETING_AI_ENABLED` (legacy alias `MARKETING_LLM_ENABLED`) turns both on. Neither can approve
anything, and both fail closed:

* **Drafter** (`draft_hooks`): proposes hooks only. The model never sets the destination, the
  price, the disclosure or the quiz cards; those come from the deterministic draft. Anything
  malformed, over-long or missing means *no AI brief* (the deterministic brief still exists).
  An AI brief is a new brief with its own id and goes through the full gate like any other.
* **Reviewer** (`review_brief`): adds findings, never removes one, and runs *after* the funnel's
  `claims.py` and the deterministic ad rules, which it cannot bypass. A reply that is not exactly
  `{"verdict": "pass", "findings": []}` is a finding; an error or timeout is a finding; AI enabled
  with no working model is a finding. So turning AI on with a broken model blocks approvals
  rather than waving briefs through.

Models are reached through `backend/llm_router.py`, loaded by file path exactly as
`funnel_app/llm_flavour.py` does, so provider keys stay in that module's environment. Prompts carry
the abstract competitor patterns (`pattern_json`), never competitor text.

Tests inject a `Completer`; no model is called in the suite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from typing import Any

from . import policy
from .config import REPO_ROOT

#: (prompt, max_tokens) -> raw model text. Raises on any failure.
Completer = Callable[[str, int], str]

ROUTER_PATH = REPO_ROOT / "backend" / "llm_router.py"
MAX_AI_HOOKS = 6
LIMITS = {"archetype": 40, "spoken_text": 220, "on_screen_caption": 80, "visual_prompt": 300}

RULES = """Rules (any breach means the ad is rejected):
- Never say or imply the viewer has a weakness, condition or problem ("you/your" + a body or health issue).
- No result promised within a time ("in 7 days", "by next week").
- No doctors, clinicians, specialists, scrubs, clinics or medical authority of any kind.
- No fake interface: no buttons, checkboxes, play icons, notifications or progress bars in visuals.
- Never say "free". If a price is mentioned it must be exactly: {price_line}
- No guarantees, cures, or weight/fat-loss claims. Describe the quiz and the preview, nothing more.
- Do not imitate any existing ad or brand."""


class AIUnavailable(RuntimeError):
    pass


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    return match.group(1) if match else text


def load_router():
    """`backend/llm_router.py` by path, or None when it or its dependencies are missing."""
    name = "marketing_llm_router"
    if name in sys.modules:
        return sys.modules[name]
    if not ROUTER_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, ROUTER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - missing httpx or config: AI is unavailable, not broken
        del sys.modules[name]
        return None
    return module


def router_completer(*, timeout_seconds: int = 45) -> Completer:
    """A Completer over the backend router's `fast` tier. Raises AIUnavailable when absent."""
    module = load_router()
    if module is None:
        raise AIUnavailable("backend/llm_router.py could not be loaded")

    def complete(prompt: str, max_tokens: int) -> str:
        async def run() -> str:
            router = module.LLMRouter()
            try:
                completion = await asyncio.wait_for(
                    router.complete([{"role": "user", "content": prompt}], tier="fast",
                                    temperature=0.4, max_tokens=max_tokens, json_mode=True),
                    timeout=timeout_seconds)
                return str(getattr(completion, "text", "") or "")
            finally:
                await router.aclose()
        return asyncio.run(run())

    return complete


# ---------------------------------------------------------------------------
# Drafter
# ---------------------------------------------------------------------------

def draft_prompt(base: dict[str, Any], *, price_line: str, patterns: list[dict[str, Any]],
                 n: int) -> str:
    cards = [c.get("header_text", "") for c in base.get("static_quiz_cards") or []]
    return (
        f"You write short video ad hooks for a quiz funnel ({base.get('niche', '')}). The product: "
        f"the viewer answers a short quiz and sees a preview of their plan before any checkout.\n"
        f"{RULES.format(price_line=price_line)}\n\n"
        f"Real quiz questions you may reference: {json.dumps(cards, ensure_ascii=False)}\n"
        f"Abstract patterns of long-running ads in this niche (types only, never copy): "
        f"{json.dumps(patterns[:10], ensure_ascii=False)}\n\n"
        f"Reply with JSON only: {{\"hooks\": [{{\"archetype\": str, \"spoken_text\": str, "
        f"\"on_screen_caption\": str, \"visual_prompt\": str}}]}} with exactly {n} hooks. "
        f"spoken_text <= 220 chars, caption <= 80, visual_prompt <= 300 and ends with ', 9:16'.")


def parse_hooks(text: str, n: int) -> list[dict[str, str]]:
    """Strict parse of the drafter's reply. Raises ValueError on anything off-contract."""
    data = json.loads(_strip_fences(text))
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, list) or not 1 <= len(hooks) <= min(n, MAX_AI_HOOKS):
        raise ValueError("hooks must be a non-empty list no longer than requested")
    out = []
    for i, hook in enumerate(hooks, 1):
        if not isinstance(hook, dict):
            raise ValueError("each hook must be an object")
        clean = {"hook_id": f"hook_ai_{i:02d}"}
        for key, limit in LIMITS.items():
            value = hook.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"hook {i} {key} missing or longer than {limit}")
            clean[key] = " ".join(value.split())
        out.append(clean)
    return out


def draft_hooks(base: dict[str, Any], *, completer: Completer, price_line: str,
                patterns: list[dict[str, Any]], n: int = 4) -> dict[str, Any]:
    """A new payload: the deterministic base with model hooks in place of its hooks."""
    hooks = parse_hooks(completer(draft_prompt(base, price_line=price_line, patterns=patterns,
                                               n=n), 900), n)
    payload = json.loads(json.dumps(base))
    payload["hooks"] = hooks
    payload["concept_name"] = f"{base.get('concept_name', 'concept')}-ai"
    return payload


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------

def review_prompt(strings: list[tuple[str, str]], *, price_line: str) -> str:
    listing = "\n".join(f"{field}: {text}" for field, text in strings if text)
    return (
        "You are a strict ad policy reviewer for a quiz funnel ad. Flag anything that breaks a rule "
        "or that you are unsure about.\n"
        f"{RULES.format(price_line=price_line)}\n\nAd fields:\n{listing}\n\n"
        'Reply with JSON only: {"verdict": "pass" | "flag", "findings": [{"field": str, '
        '"detail": str}]}. Use "pass" only with an empty findings list.')


def parse_review(text: str) -> list[policy.Finding]:
    """Map a reply to findings. Only an exact clean pass yields none."""
    try:
        data = json.loads(_strip_fences(text))
    except ValueError:
        return [policy.Finding("ai_review_unclear", "brief", "reply was not JSON")]
    if not isinstance(data, dict):
        return [policy.Finding("ai_review_unclear", "brief", "reply was not an object")]
    verdict, findings = data.get("verdict"), data.get("findings")
    if verdict == "pass" and findings == []:
        return []
    out: list[policy.Finding] = []
    if isinstance(findings, list):
        for item in findings[:20]:
            if isinstance(item, dict):
                out.append(policy.Finding("ai_review", str(item.get("field", "brief"))[:80],
                                          str(item.get("detail", ""))[:200] or "flagged"))
    if not out:
        detail = "model flagged without detail" if verdict == "flag" else \
            f"unexpected verdict {str(verdict)[:20]!r}"
        out.append(policy.Finding("ai_review_unclear", "brief", detail))
    return out


def make_reviewer(completer: Completer | None, *, price_line: str):
    """A brief-level reviewer for `compliance.audit_brief(reviewer=...)`. None completer = a
    reviewer that always flags, because AI review was asked for and cannot run."""

    def review(strings: list[tuple[str, str]]) -> list[policy.Finding]:
        if completer is None:
            return [policy.Finding("ai_review_unavailable", "brief",
                                   "AI review is enabled but no model is reachable")]
        return parse_review(completer(review_prompt(strings, price_line=price_line), 600))

    return review
