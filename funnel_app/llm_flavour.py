"""Optional LLM decoration of the deterministic plan: a coach note and two strings.

The LLM adds voice, never structure. `plan_builder` decides the week; this module asks
the `fast` tier for a short coach note about the routine the builder already produced,
and validates what comes back against `claims.FORBIDDEN_TERMS` before it is displayed.
That check is the point of the module: a model answering a wellness prompt will drift
toward outcome language unprompted, and the guardrail has to sit on the output, not in
the instruction.

Routing note: this imports `backend/llm_router.py` directly rather than calling the
engine over HTTP, so the funnel runs with the engine stopped. Both processes read the
same env-derived provider config, so there is one source of truth for which provider
serves the `fast` tier, and no second copy of the provider ladder to drift.

Failure posture: any error, timeout, empty response, or guardrail violation returns
None and the caller renders the builder's own note. Phase 1 shows no payment form
errors to the user, so the preview screen must not be able to fail.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any

import httpx

import claims
from plan_builder import Plan, Profile

log = logging.getLogger("funnel.llm")

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
_ROUTER_SPEC = _BACKEND / "llm_router.py"
_ROUTER_LOADED = False

MAX_COACH_CHARS = 420

SYSTEM_RULES = (
    "You write short, warm, plain English for a wellness routine planner. "
    "Rules you must not break: never mention medical conditions, diagnosis, treatment, "
    "symptoms, hormones, metabolism, immune function, or detox. Never promise or imply a "
    "weight, fat-loss or body-composition result. Never use the words guarantee, cure, "
    "heal, treat, therapy, clinical, bloating, or detox. Talk only about routine "
    "structure, time, consistency, meal organisation, and how the week is arranged. "
    "Two or three sentences, no preamble, no headings, no bullet points, no emoji."
)


def load_router() -> Any:
    """Import `backend/llm_router.py` by path, without importing the engine package.

    Returns the module, or None if the file or httpx is unavailable. Loaded once and
    cached in sys.modules so a repeated call is cheap.
    """
    global _ROUTER_LOADED
    if "funnel_llm_router" in sys.modules:
        return sys.modules["funnel_llm_router"]
    if not _ROUTER_SPEC.is_file():
        log.warning("llm_router.py not found at %s, preview runs builder-only", _ROUTER_SPEC)
        return None
    spec = importlib.util.spec_from_file_location("funnel_llm_router", _ROUTER_SPEC)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["funnel_llm_router"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken router must not take the funnel down
        log.warning("llm_router import failed: %s", exc)
        del sys.modules["funnel_llm_router"]
        return None
    _ROUTER_LOADED = True
    return module


def _profile_digest(profile: Profile, plan: Plan) -> str:
    """Compact factual brief handed to the model.

    Hand-built rather than a dump of the profile, so the model sees the same
    product-truth vocabulary the guardrail enforces, and cannot invent an input the
    user never gave.
    """
    return (
        f"Week structure: {plan.days_per_week} training days, sessions of about "
        f"{plan.session_minutes} minutes. Split: {plan.split_label}.\n"
        f"Sessions this week: "
        + ", ".join(d.session.name for d in plan.days if d.is_training_day) + ".\n"
        f"Meal approach: {profile.diet.replace('highprotein', 'higher protein')}, "
        f"cooking style {profile.cook_style.replace('_', ' ')}.\n"
        f"What usually derails them: {profile.blocker.replace('_', ' ')}.\n"
        f"Stated focus: {profile.goal}."
    )


def _tidy(text: str) -> str:
    """Strip markdown scaffolding and surrounding quotes a chat model likes to add."""
    text = re.sub(r"[*_#>`]", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.MULTILINE)
    text = " ".join(text.split())
    return text.strip().strip('"“”\'')


async def coach_note(profile: Profile, plan: Plan, *, timeout_seconds: int = 25) -> str | None:
    """Ask the `fast` tier for a coach note about this plan. None means "use ours"."""
    router_module = load_router()
    if router_module is None:
        return None
    try:
        router = router_module.get_router()
    except Exception as exc:
        log.info("llm router unavailable: %s", exc)
        return None

    prompt = (
        f"{SYSTEM_RULES}\n\n"
        "Write the opening coach note for the first week of this routine plan. Address "
        "the reader as 'you'. Say what the week is built around and one thing to notice "
        f"in week 1. Here is the plan:\n\n{_profile_digest(profile, plan)}"
    )
    try:
        completion = await asyncio.wait_for(
            router.complete(
                [{"role": "user", "content": prompt}],
                tier="fast",
                temperature=0.4,
                max_tokens=220,
            ),
            timeout=timeout_seconds,
        )
    except (TimeoutError, asyncio.TimeoutError) as exc:
        log.info("coach note timed out after %ss: %s", timeout_seconds, exc)
        return None
    except httpx.HTTPError as exc:
        log.info("coach note network error: %s", exc)
        return None
    except Exception as exc:
        # LLMNotConfigured / AllProvidersFailed / LLMRequestRejected, plus anything
        # unexpected. All of them mean the same thing here: render the builder's note.
        log.info("coach note unavailable (%s): %s", type(exc).__name__, str(exc)[:200])
        return None

    note = _tidy(getattr(completion, "text", "") or "")
    if len(note) < 40:
        log.info("coach note too short to be useful (%d chars)", len(note))
        return None
    note = note[:MAX_COACH_CHARS]

    flagged = claims.violations(note)
    if flagged:
        # Drop it rather than scrub word-by-word: a sentence about fat loss with the
        # words removed is still a sentence about fat loss.
        log.warning("coach note rejected by copy guardrail: %s", flagged[0])
        return None
    return note


def builder_coach_note(profile: Profile, plan: Plan) -> str:
    """Deterministic fallback voice, written by a human and scanned by the tests."""
    return (
        f"This week is built around {plan.days_per_week} training days of about "
        f"{plan.session_minutes} minutes, placed in the window you told us you have. "
        "Week 1 is a baseline, not a test: follow the structure, log the three check-in "
        "metrics, and tell us what felt wrong. The plan changes based on that."
    )
