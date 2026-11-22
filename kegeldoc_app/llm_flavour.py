"""Optional LLM decoration of the deterministic plan: a coach note and two strings.

The LLM adds voice, never structure. `plan_builder` decides the week; this module asks
the `fast` tier for a short coach note about the kegel plan the builder already produced,
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
    "You write short, warm, plain English for a pelvic floor exercise planner. "
    "Rules you must not break: never mention or name medical conditions, diagnosis, "
    "treatment, medication or hormones. Never promise or imply any sexual, bladder or body "
    "result, and never say a doctor or study backs the plan. Never use the words guarantee, "
    "cure, heal, treat, proven, clinical, erectile dysfunction, premature ejaculation, "
    "incontinence, bigger, harder or tighter. Talk only about the exercises, time, "
    "consistency, breathing, relaxing between holds, and how the week is arranged. "
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
    """Compact factual brief handed to the model, in the product-truth vocabulary only, so the
    model cannot invent an input the user never gave."""
    return (
        f"Plan for {'women' if profile.women else 'men'}. Focus: {plan.focus}.\n"
        f"Daily time: {plan.minutes} minutes, done {plan.when_label}.\n"
        f"Starting level: {plan.level_name}, holds of {plan.hold_seconds} seconds with "
        f"{plan.rest_seconds} seconds of full relax, {plan.reps} reps.\n"
        f"Daily drill: {plan.drill_name}.\n"
        f"Week 1 sessions: " + ", ".join(d.title for d in plan.days) + "."
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
        "Write the opening coach note for the first week of this pelvic floor exercise plan. "
        "Address the reader as 'you'. Say what the week is built around and one thing to "
        "notice in week 1, such as keeping the belly and thighs soft or relaxing fully between "
        "holds. Do not promise any result, do not mention medication, and do not claim any "
        f"doctor is involved. Here is the plan:\n\n{_profile_digest(profile, plan)}"
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
        f"This week is built around {plan.minutes} minutes a day, {plan.when_label}, starting "
        f"with {plan.hold_seconds}-second holds. Week 1 is a baseline, not a test: find the "
        "muscle, keep your belly and thighs soft, let go fully after every squeeze, and time "
        "your longest comfortable hold on day 7. Week 2 is built from that number."
    )
