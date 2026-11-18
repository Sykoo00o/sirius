"""Shared setup for the backend suite.

Two jobs, both about the suite failing in a way that has nothing to do with the
code under test.

Import path: `finance_agent` and `llm_router` are top-level modules that import
each other by bare name, which is exactly how they run in production (uvicorn
starts from `backend/`). Without this, `pytest tests/` from the repo root puts
`backend/tests` on sys.path instead of `backend`, and every test dies at import
with a ModuleNotFoundError that looks like a broken module.

Environment: the suite reads real configuration, and this developer machine has a
live `.env` two directories up that `engine.config` loads on import. Tests assert
on model chains and on whether the LLM path is available, so a machine that has
keys (or a `DATABASE_URL` pointing at production) must not change the answers.
Everything a test cares about is pinned here, and the individual tests still set
what they specifically exercise, via `monkeypatch`, so intent stays local.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Nothing in the suite opens a socket. A store that tries gets this password
# rejection rather than a hang against a real host.
os.environ.setdefault("SUPABASE_DB_PASSWORD", "test-only-unreachable")

# Keys and endpoints that decide routing, blanked so a real .env cannot leak a
# provider (or a cost) into a test that did not ask for one.
NEUTRAL_ENV = {
    "OPENROUTER_API_KEY": "",
    "NVIDIA_API_KEY": "",
    "NIM_API_KEY": "",
    "OPENROUTER_BASE_URL": "",
    "NVIDIA_NIM_BASE_URL": "",
    "LLM_MODEL_CHEAP": "",
    "LLM_MODEL_STRONG": "",
    "LLM_FALLBACK_CHAIN": "",
    "LLM_ROUTER_FAST_MODELS": "",
    "LLM_ROUTER_REASONING_MODELS": "",
    "LLM_ROUTER_NVIDIA_MODELS": "",
    "LLM_ROUTER_OPENROUTER_MODELS": "",
    "LLM_ROUTER_ALLOWED_MODELS": "",
    "LLM_PROVIDER_ORDER": "",
    "LLM_PROVIDER": "",
    "LLM_FAST_PROVIDER": "",
    "LLM_REASONING_PROVIDER": "",
    "TG_LLM_ENABLED": "",
    "TELEGRAM_APPROVAL_BOT_TOKEN": "",
    "TELEGRAM_APPROVAL_CHAT_ID": "",
    "TELEGRAM_WEBHOOK_SECRET": "",
    "TELEGRAM_ALLOWED_IPS": "",
}


@pytest.fixture(autouse=True)
def _neutral_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank provider configuration for every test, then restore the router's cache.

    `llm_router` builds its provider list once and caches it, so a test that sets a
    key leaves a live-looking router behind for whichever test runs next. Re-running
    `configure()` after each test makes the cache match the restored environment
    instead of whichever test happened to run first.
    """
    for key, value in NEUTRAL_ENV.items():
        monkeypatch.setenv(key, value)
    try:
        yield
    finally:
        import llm_router

        llm_router.configure()
