"""Router contract: tier routing, wire format, error taxonomy, usage accounting.

These assert the behaviours the finance chat path leans on, not internals:

* a tier resolves to the provider that can actually serve it (NIM has no
  Anthropic slugs, so the reasoning tier must reach OpenRouter);
* a dead model, timeout, or throttle rotates while a bad credential aborts at
  once, because three more attempts against a dead key is waste, not resilience;
* a provider with no key is dropped, so the chain never spends attempts proving a
  key is missing;
* cost is recorded even when the provider reports none, since an unreported cost
  that defaults to zero is how a loop quietly outruns its budget.

MockTransport drives the real request-building and parsing code, so URL, auth
header and body shape are all exercised.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import llm_router as R

OK_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "sure"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.00001},
}

ENV_KEYS = {
    "NVIDIA_API_KEY": "nk",
    "NIM_API_KEY": "",
    "OPENROUTER_API_KEY": "ok",
    "NVIDIA_NIM_BASE_URL": "",
    "OPENROUTER_BASE_URL": "",
    "LLM_PROVIDER_ORDER": "",
    "LLM_PROVIDER": "",
    "LLM_FAST_PROVIDER": "",
    "LLM_REASONING_PROVIDER": "",
    "LLM_ROUTER_FAST_MODELS": "",
    "LLM_ROUTER_REASONING_MODELS": "",
    "LLM_ROUTER_NVIDIA_MODELS": "",
    "LLM_ROUTER_OPENROUTER_MODELS": "",
    "LLM_MODEL_CHEAP": "",
    "LLM_MODEL_STRONG": "",
    "LLM_FALLBACK_CHAIN": "",
    "LLM_ROUTER_ALLOWED_MODELS": "",
    "LLM_TIMEOUT_SECONDS": "",
    "LLM_MAX_ATTEMPTS": "",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ENV_KEYS.items():
        monkeypatch.setenv(key, value)
    R.configure()


def client_for(*responses: tuple[int, Any]) -> httpx.AsyncClient:
    """Sequential canned responses; the last one repeats if asked for again."""
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        status, payload = responses[i]
        if status == 0:
            raise httpx.ConnectTimeout("connect timed out")
        if payload is None:
            return httpx.Response(status, text="<html>bad gateway</html>")
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def router_with(client: httpx.AsyncClient, **kw: Any) -> R.LLMRouter:
    router = R.LLMRouter(**kw)
    router._client = client
    return router


# ------------------------------------------------------------------ tier routing
def test_reasoning_tier_reaches_openrouter_and_fast_reaches_nvidia() -> None:
    router = R.LLMRouter()
    assert next(p.name for p in router._order(R.TIER_REASONING)) == "openrouter"
    assert next(p.name for p in router._order(R.TIER_FAST)) == "nvidia"
    assert router.candidates(R.TIER_REASONING)[0].label.startswith("openrouter/anthropic")
    assert router.candidates(R.TIER_FAST)[0].label.startswith("nvidia/meta/llama")


def test_missing_key_drops_that_provider_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    router = R.LLMRouter()
    assert [p.name for p in router.providers] == ["openrouter"]
    # Both tiers still resolve, onto the one live provider.
    assert {c.provider.name for c in router.candidates(R.TIER_FAST)} == {"openrouter"}
    assert {c.provider.name for c in router.candidates(R.TIER_REASONING)} == {"openrouter"}


def test_placeholder_key_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A .env with OPENROUTER_API_KEY=changeme must read as unconfigured."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "changeme")
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    with pytest.raises(R.LLMNotConfigured):
        R.LLMRouter().candidates(R.TIER_FAST)


def test_no_keys_raises_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(R.LLMNotConfigured):
        R.LLMRouter().candidates(R.TIER_FAST)


def test_env_overrides_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_FAST_MODELS", "my-custom-fast")
    monkeypatch.setenv("LLM_REASONING_PROVIDER", "nvidia")
    router = R.LLMRouter()
    assert router.candidates(R.TIER_FAST)[0].model == "my-custom-fast"
    assert next(p.name for p in router._order(R.TIER_REASONING)) == "nvidia"


def test_provider_specific_override_outranks_the_generic_one(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """One line can retarget a single catalogue without disturbing the other."""
    monkeypatch.setenv("LLM_ROUTER_FAST_MODELS", "universal-first")
    monkeypatch.setenv("LLM_ROUTER_NVIDIA_MODELS", "nvidia-only")
    chains = R.models_from_env()
    assert chains["nvidia"][R.TIER_FAST][0] == "nvidia-only"
    assert "nvidia-only" not in chains["openrouter"][R.TIER_FAST]
    assert chains["openrouter"][R.TIER_FAST][0] == "universal-first"


def test_engine_env_names_land_on_openrouter_only(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """This repo's .env sets LLM_MODEL_STRONG=anthropic/claude-sonnet-5.5.

    Those names have always addressed OpenRouter (`engine/config.py`). Folding them
    into the OpenRouter chain is what lets one `.env` drive both routers to the same
    model; letting them onto NVIDIA would mean a 404 on every reasoning call, because
    NIM sells no Anthropic slugs.
    """
    monkeypatch.setenv("LLM_MODEL_CHEAP", "anthropic/claude-haiku-5.5")
    monkeypatch.setenv("LLM_MODEL_STRONG", "anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("LLM_FALLBACK_CHAIN", "anthropic/claude-haiku-5.5,openai/gpt-5-mini")
    chains = R.models_from_env()
    assert chains["openrouter"][R.TIER_FAST][0] == "anthropic/claude-haiku-5.5"
    assert chains["openrouter"][R.TIER_REASONING][0] == "anthropic/claude-sonnet-5.5"
    assert "anthropic/claude-haiku-5.5" in chains["openrouter"][R.TIER_REASONING]
    nvidia_slugs = [m for tier in chains["nvidia"].values() for m in tier]
    assert not any("anthropic" in m for m in nvidia_slugs)


def test_allowlist_narrows_the_marketplace(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter takes nearly any slug by name, so a fixed surface is opt-in."""
    monkeypatch.setenv("LLM_ROUTER_ALLOWED_MODELS", "anthropic/")
    R.configure()
    router = R.LLMRouter()
    narrowed = [c for c in router.candidates(R.TIER_FAST) if c.provider.name == "openrouter"]
    assert narrowed == [], "every default openrouter fast slug is filtered away"
    # A matching slug comes back as soon as it is configured for that provider.
    monkeypatch.setenv("LLM_ROUTER_OPENROUTER_MODELS", "anthropic/claude-3.5-sonnet")
    router2 = R.LLMRouter()
    assert [c.label for c in router2.candidates(R.TIER_FAST)
            if c.provider.name == "openrouter"] == ["openrouter/anthropic/claude-3.5-sonnet"]


def test_unknown_tier_is_a_programmer_error() -> None:
    with pytest.raises(R.LLMError):
        R.LLMRouter().candidates("turbo")


# ------------------------------------------------------------------- wire format
async def test_openrouter_request_shape() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["title"] = request.headers.get("x-title")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await router.get_completion("hello there", system="be terse", tier=R.TIER_REASONING)

    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer ok"
    assert seen["title"] == "Ghost X2"
    body = seen["body"]
    assert body["model"].startswith("anthropic/claude")
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["messages"][1]["content"] == "hello there"
    assert body["usage"] == {"include": True}
    assert "tools" not in body
    assert "tool_choice" not in body
    assert result.text == "sure"
    assert result.tier == R.TIER_REASONING


async def test_nvidia_request_shape() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=OK_BODY)

    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await router.get_completion("hi", tier=R.TIER_FAST)
    assert seen["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert seen["auth"] == "Bearer nk"


async def test_tools_and_tool_choice_defaults_to_auto() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await router.get_completion("hi", tools=[{"type": "function",
                                             "function": {"name": "x"}}])
    assert captured["tool_choice"] == "auto"
    assert captured["tools"][0]["function"]["name"] == "x"


async def test_explicit_tool_choice_and_json_mode_pass_through() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await router.get_completion("hi", tools=[{"type": "function", "function": {"name": "x"}}],
                                tool_choice="required", json_mode=True, temperature=0.9,
                                max_tokens=42)
    assert captured["tool_choice"] == "required"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["temperature"] == 0.9
    assert captured["max_tokens"] == 42


# ------------------------------------------------------------------- error taxonomy
async def test_timeout_then_throttle_then_success_rotates() -> None:
    client = client_for((0, None), (429, {"error": {"message": "rate limited"}}),
                        (200, OK_BODY))
    router = router_with(client, max_attempts=4)
    result = await router.get_completion("hi")
    assert result.attempts == 3
    assert result.text == "sure"


async def test_unknown_slug_404_rotates_to_next_model() -> None:
    client = client_for((404, {"error": {"message": "model not found"}}), (200, OK_BODY))
    router = router_with(client, max_attempts=4)
    assert (await router.get_completion("hi")).attempts == 2


async def test_html_gateway_body_rotates_instead_of_crashing() -> None:
    client = client_for((200, None), (200, OK_BODY))
    router = router_with(client, max_attempts=4)
    assert (await router.get_completion("hi")).attempts == 2


async def test_exhausted_chain_raises_transient_error() -> None:
    client = client_for((500, {"error": {"message": "upstream exploded"}}))
    router = router_with(client, max_attempts=3)
    with pytest.raises(R.AllProvidersFailed):
        await router.get_completion("hi")
    assert router.stats["failures"] == 1


async def test_bad_credential_aborts_without_burning_the_chain() -> None:
    """401 is a key problem, not a model problem: one attempt, then say so."""
    client = client_for((401, {"error": {"message": "invalid api key"}}))
    router = router_with(client, max_attempts=4)
    with pytest.raises(R.LLMRequestRejected) as excinfo:
        await router.get_completion("hi")
    assert excinfo.value.credential_failure
    assert "nvidia" in str(excinfo.value)


async def test_max_attempts_caps_the_number_of_requests() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": "boom"}})

    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                         max_attempts=2)
    with pytest.raises(R.AllProvidersFailed):
        await router.get_completion("hi")
    assert calls["n"] == 2


# ---------------------------------------------------------------------- accounting
async def test_cost_recorded_when_provider_reports_none() -> None:
    body = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}
    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=body))))
    result = await router.get_completion("hi")
    # 2000 tokens at the documented fallback rate, never zero.
    assert result.cost_usd == pytest.approx(0.006)
    assert result.total_tokens == 2000


async def test_openrouter_usage_details_spelling_is_read() -> None:
    body = {"choices": [{"message": {"content": "ok"}}],
            "usage_details": {"prompt_tokens": 20, "completion_tokens": 5, "cost": 0.001}}
    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=body))))
    result = await router.get_completion("hi")
    assert (result.input_tokens, result.output_tokens, result.cost_usd) == (20, 5, 0.001)


async def test_tool_calls_parsed_including_truncated_arguments() -> None:
    good = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "approve_transaction",
                                  "arguments": '{"transaction_id": "abc"}'}}]},
        "finish_reason": "tool_calls"}]}
    router = router_with(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=good))))
    result = await router.get_completion("hi")
    assert result.text == ""
    assert result.wants_tools
    call = result.tool_calls[0]
    assert call.ok
    assert call.name == "approve_transaction"
    assert call.arguments == {"transaction_id": "abc"}

    cut = {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"id": "c2", "function": {"name": "approve_transaction",
                                  "arguments": '{"transaction_id": "ab'}}]},
        "finish_reason": "length"}]}
    router2 = router_with(httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=cut))))
    truncated = (await router2.get_completion("hi")).tool_calls[0]
    # A half-written call must not look like a zero-argument call.
    assert not truncated.ok
    assert truncated.arguments == {}


async def test_describe_exposes_no_secrets() -> None:
    router = R.LLMRouter()
    text = json.dumps(router.describe())
    assert "nk" not in text.replace("nvidia", "")
    assert "ok" not in text.replace("nvidia", "")
    described = router.describe()
    assert described["providers"][0]["key_present"] is True
    # Both catalogues appear separately: an operator must be able to see which
    # slugs each provider will actually be asked for.
    assert set(described["models"]) == {"nvidia", "openrouter"}
