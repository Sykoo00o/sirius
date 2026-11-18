"""Tiered LLM router over OpenAI-compatible providers: OpenRouter and NVIDIA NIM.

Two providers, one request shape. Both speak the OpenAI chat-completions dialect,
so the only real difference between them is the base URL and how the key rides
along. That keeps the router small and keeps a third provider a config line
rather than a code change.

Deliberate properties:

1. Tier is a routing decision, not a model name the caller must remember. Callers
   ask for `tier="fast"` or `tier="reasoning"` and get whatever is configured for
   that job, then a rotation chain inside the same tier. `model=` overrides when a
   caller truly means one model.
2. Chains are keyed by (provider, tier), not by tier alone, because the two
   catalogues sell different things: NVIDIA has no Anthropic slugs, OpenRouter sells
   nearly everything by marketplace name. Each provider is offered its own chain for
   the tier, walked provider-by-provider. That keeps reasoning on OpenRouter without
   spending a billed 404 on NIM, and an OpenRouter outage still falls through to NIM
   as soon as the first chain is exhausted.
3. No key, no candidate. A provider with an unset API key is dropped from the
   chain entirely, so the retry ladder never spends its attempts proving a key is
   missing, and the final error names the config to fix.
4. Errors are typed. `AllProvidersFailed` (transient: upstream down, timeouts,
   throttling, cross-provider rotation exhausted) versus `LLMRequestRejected`
   (permanent: a 4xx on our request shape, or nothing configured). A caller that
   can degrade to a canned reply degrades on the first and alerts on the second.
5. Usage is accounted per call. Providers disagree about cost fields, so we record
   tokens and price at a documented fallback rate when no cost came back. An
   unreported cost is never silently free.

This module is standalone like `finance_agent.py`: stdlib + httpx, no import of
the `engine` package, so `engine/llm.py` (which adds the per-agent budget gate)
and this one cannot pull each other's dependencies in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("llm_router")

TIER_FAST = "fast"
TIER_REASONING = "reasoning"
TIERS = (TIER_FAST, TIER_REASONING)

# OpenAI-compatible base URLs. `/chat/completions` is appended to these.
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
NIM_BASE = "https://integrate.api.nvidia.com/v1"

# What we price a call at when the provider reports no cost. A rough blend that
# still moves the needle: undercharging is how a loop becomes a surprise invoice.
FALLBACK_COST_PER_MTOKEN = 3.0

# Which models to try, per PROVIDER and per tier, in order.
#
# The unit is (provider, tier), not tier, because "what serves llama-3.3-70b" and
# "what serves Claude" are different questions per catalogue: NVIDIA has no
# Anthropic slugs at all, and OpenRouter serves almost anything by marketplace name.
# A single shared chain would spend a billed attempt on a guaranteed 404 on every
# call, which looks like flaky routing and is really a slug nobody sells.
#
# Env overrides, all comma-separated, all appended to (not replacing) these
# defaults, with `LLM_ROUTER_*` winning over the older engine names:
#   LLM_ROUTER_FAST_MODELS / LLM_ROUTER_REASONING_MODELS      every provider
#   LLM_ROUTER_OPENROUTER_MODELS / LLM_ROUTER_NVIDIA_MODELS   one provider
# Plus the two names `engine/config.py` has always addressed to OpenRouter, folded
# into the OpenRouter chains so one `.env` drives both routers consistently:
#   LLM_MODEL_CHEAP -> openrouter fast, LLM_MODEL_STRONG + LLM_FALLBACK_CHAIN ->
#   openrouter reasoning.
FAST_SLUGS_NVIDIA = (
    "meta/llama-3.3-70b-instruct",
    "meta/llama3.1-70b-instruct",
)
FAST_SLUGS_OPENROUTER = (
    "meta/llama-3.3-70b-instruct",
    "openai/gpt-5-mini",
)
REASONING_SLUGS_NVIDIA = (
    "meta/llama-3.3-70b-instruct",
    "moonshotai/kimi-k2-instruct",
)
REASONING_SLUGS_OPENROUTER = (
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-sonnet-4.5",
)
DEFAULT_MODELS: dict[str, dict[str, tuple[str, ...]]] = {
    "nvidia": {TIER_FAST: FAST_SLUGS_NVIDIA, TIER_REASONING: REASONING_SLUGS_NVIDIA},
    "openrouter": {TIER_FAST: FAST_SLUGS_OPENROUTER, TIER_REASONING: REASONING_SLUGS_OPENROUTER},
}


class LLMError(RuntimeError):
    """Base for everything this router raises."""


class LLMNotConfigured(LLMError):
    """Permanent: no provider has an API key, so no call can ever succeed."""


class LLMRequestRejected(LLMError):
    """Permanent: the provider answered 4xx on our request shape (bad model name,
    malformed tools, unsupported response_format). Retrying cannot help.

    `credential_failure` marks the 401/403 case specifically. It is still a
    rejection, but the repair is a key, not a different model, so the retry loop
    stops on it instead of billing four attempts to the same dead credential.
    """

    def __init__(self, message: str, *, credential_failure: bool = False) -> None:
        super().__init__(message)
        self.credential_failure = credential_failure


class AllProvidersFailed(LLMError):
    """Transient: every candidate in the chain timed out, errored, or refused with
    a retryable status. Safe to degrade to a canned reply and alert."""


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible endpoint. `scheme` is 'bearer' or 'api_key' (NIM
    accepts both; bearer is what the docs show, so it is the default)."""

    name: str
    base_url: str
    api_key: str = field(repr=False)
    scheme: str = "bearer"

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if self.scheme == "api_key":
            headers = {"Authorization": f"Api-Key {self.api_key}"}
        else:
            headers = {"Authorization": f"Bearer {self.api_key}"}
        return {**headers, **(extra or {})}


@dataclass
class Candidate:
    """A (provider, model) pair, the unit of one attempt."""

    provider: Provider
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider.name}/{self.model}"


@dataclass
class ToolCall:
    """One function call the model asked for.

    `ok` is False when the arguments were not valid JSON (a truncated stream mid-
    arguments is the common cause). Callers must check it: running a half-written
    tool call with `{}` silently performs the action with no arguments, which for
    `approve_transaction` means no transaction.
    """

    name: str
    arguments: dict[str, Any]
    id: str = ""
    raw_arguments: str = ""
    parse_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.parse_error


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    tier: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    attempts: int
    finish_reason: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        log.warning("%s is not an integer; using %d", name, default)
        return default


def _clean(value: str | None) -> str:
    """Treat the placeholder values that .env files carry as unset, so the router
    reports 'not configured' instead of sending a literal 'changeme' as a key."""
    v = (value or "").strip().strip("'\"")
    if not v or v.lower() in {"changeme", "changeme-", "none", "null", "todo", "xxx"}:
        return ""
    if v.startswith(("your-", "your_", "<", "${")):
        return ""
    return v


def _split_models(raw: str, fallback: Sequence[str]) -> tuple[str, ...]:
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or tuple(fallback)


class LLMRouter:
    """Async client with tier routing, cross-provider rotation and tool calling.

    Every argument falls back to the environment when left unset, so `LLMRouter()`
    with no arguments is the production shape and explicit arguments are for tests
    and for a caller that wants a provider nothing else sees.
    """

    def __init__(
        self,
        *,
        providers: Sequence[Provider] | None = None,
        tier_models: dict[str, tuple[str, ...]] | None = None,
        models: dict[str, dict[str, tuple[str, ...]]] | None = None,
        timeout_seconds: int | None = None,
        max_attempts: int | None = None,
        referer: str = "",
        title: str = "Ghost X2",
    ) -> None:
        """`models` is the full (provider -> tier -> slugs) table.

        `tier_models` is the shorthand for tests and single-provider callers: the
        same tier chains for every provider, which is exactly the shape a test wants
        and never what a two-provider deployment should rely on.
        """
        self._injected_providers = providers is not None
        self.providers = tuple(p for p in (providers if providers is not None
                                           else providers_from_env()) if p.api_key)
        self._injected_chains: dict[str, dict[str, tuple[str, ...]]] | None = models
        if tier_models is not None and self._injected_chains is None:
            names = {p.name for p in self.providers} or set(DEFAULT_MODELS)
            self._injected_chains = {n: dict(tier_models) for n in names}
        self.timeout_seconds = max(10, int(
            timeout_seconds if timeout_seconds is not None
            else _int_env("LLM_TIMEOUT_SECONDS", 90)))
        self.max_attempts = max(1, int(
            max_attempts if max_attempts is not None
            else _int_env("LLM_MAX_ATTEMPTS", 4)))
        self.referer = referer or _clean(os.getenv("PUBLIC_BASE_URL"))
        self.title = title
        self._client: httpx.AsyncClient | None = None
        self.stats: dict[str, int] = {"calls": 0, "attempts": 0, "failures": 0}
        if not self.providers:
            log.warning(
                "LLM router has no provider with an API key: set OPENROUTER_API_KEY "
                "or NVIDIA_API_KEY. Calls will raise LLMNotConfigured."
            )

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(float(self.timeout_seconds), connect=15.0),
                limits=httpx.Limits(max_connections=20),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------ routing
    def candidates(
        self, tier: str = TIER_FAST, *, model: str | None = None
    ) -> list[Candidate]:
        """Ordered attempt list for a tier, across every configured provider.

        An explicit `model` wins outright (single candidate). Otherwise: provider
        outer, tier model inner, deduped by (provider, model). See the class
        docstring for why the nesting is this way round.
        """
        if tier not in TIERS:
            raise LLMError(f"unknown tier {tier!r}; expected one of {TIERS}")
        if model:
            order = self._order(tier)
            if not order:
                raise LLMNotConfigured("no LLM provider configured")
            return [Candidate(order[0], model)]

        chains = self._chains()
        out: list[Candidate] = []
        for provider in self._order(tier):
            for name in chains.get(provider.name, {}).get(tier, ()):
                if not self._serves(provider, name):
                    continue
                out.append(Candidate(provider, name))
        if not out:
            raise LLMNotConfigured(
                f"tier {tier!r} has no candidate for providers "
                f"{[p.name for p in self._order(tier)]}: no model chain resolves. "
                "Set LLM_ROUTER_<PROVIDER>_MODELS, or check "
                "LLM_ROUTER_ALLOWED_MODELS if it is narrowing the list away."
            )
        return out

    def _chains(self) -> dict[str, dict[str, tuple[str, ...]]]:
        """(provider, tier) -> slugs. Injected chains win; otherwise env, re-read per
        call so a `.env` edit picked up by a restart-cheap process applies at once."""
        if self._injected_chains is not None:
            return self._injected_chains
        return models_from_env()

    def _order(self, tier: str) -> list[Provider]:
        """Providers best-first for this tier. An injected provider list (tests, or
        a caller pinning one endpoint) is used as given; otherwise the env-derived
        preference applies. Re-derived per call, so a key added to the environment
        takes effect on the next request without a restart."""
        if self._injected_providers:
            return list(self.providers)
        return [p for p in provider_order(tier) if p.api_key]

    def _serves(self, provider: Provider, model: str) -> bool:
        """Whether to offer `model` to `provider`.

        OpenRouter is a marketplace and takes almost anything by name, so allowlist
        it. NIM 400s on unknown slugs, and burning an attempt per bad slug is noise.
        `LLM_ROUTER_ALLOWED_MODELS` (comma-separated substrings) tightens the
        OpenRouter side when a deployment wants a fixed surface.
        """
        if provider.name != "openrouter":
            return True
        allowed = _ALLOWED_OPENROUTER
        if not allowed:
            return True
        return any(a in model for a in allowed)

    # -------------------------------------------------------------------- calls
    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tier: str = TIER_FAST,
        model: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1000,
        json_mode: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> Completion:
        """One completion, rotating candidates until one answers.

        `tool_choice` follows the OpenAI contract and defaults to "auto" whenever
        `tools` are passed: a provider that ignores `tool_choice` and always
        force-calls would otherwise loop the tool turn forever.
        """
        if not self._order(tier):
            raise LLMNotConfigured(
                "no LLM provider configured: set OPENROUTER_API_KEY or NVIDIA_API_KEY"
            )
        chain = self.candidates(tier, model=model)
        body = self._payload(
            messages, chain[0].model, tools=tools, tool_choice=tool_choice,
            temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
            extra_body=extra_body,
        )

        errors: list[str] = []
        permanent: LLMRequestRejected | None = None
        for attempt, cand in enumerate(chain[: self.max_attempts], start=1):
            body["model"] = cand.model
            self.stats["attempts"] += 1
            started = time.monotonic()
            try:
                result = await self._call(cand, body)
            except LLMRequestRejected as exc:
                # A malformed request fails identically on every candidate that
                # shares the shape, but slugs differ, so keep rotating and surface
                # the last rejection if nothing else worked.
                errors.append(f"{cand.label}: {exc}")
                if exc.credential_failure:
                    # A dead key is not a per-model problem, and three more attempts
                    # against the same provider cannot fix it. Surface it now, with
                    # the provider named: this is the config fault to repair. The
                    # flag is carried forward so callers can tell "swap the key" from
                    # "fix the model name" without parsing the message.
                    raise LLMRequestRejected(
                        f"{cand.provider.name} refused our credentials: {exc}",
                        credential_failure=True) from exc
                # Otherwise keep rotating: a 400 on one provider's slug rules may
                # still land on one that accepts the same model name.
                log.warning("llm attempt %d %s rejected: %s", attempt, cand.label, exc)
                permanent = exc
                continue
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                errors.append(f"{cand.label}: {type(exc).__name__}: {str(exc)[:200]}")
                log.warning("llm attempt %d/%d on %s failed after %.1fs: %s",
                            attempt, min(len(chain), self.max_attempts), cand.label,
                            time.monotonic() - started, str(exc)[:200])
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                    # Wrong slug for this provider: rotate, but do not log a stack.
                    continue
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            except _BadResponse as exc:
                errors.append(f"{cand.label}: {exc}")
                log.warning("llm attempt %d on %s returned an unusable body: %s",
                            attempt, cand.label, exc)
                continue

            self.stats["calls"] += 1
            result.attempts = attempt
            result.tier = tier
            log.debug("llm ok via %s in %.1fs, %d/%d tokens, $%s",
                      cand.label, time.monotonic() - started, result.input_tokens,
                      result.output_tokens, result.cost_usd)
            return result

        self.stats["failures"] += 1
        detail = "; ".join(errors[-3:])
        # One candidate, permanently rejected: a config fault (bad slug, malformed
        # tools) must read like one, not like an outage the caller should retry.
        # `chain[:max_attempts]` is the list actually tried, and max_attempts is the
        # smaller number in production, so the test is against what ran.
        if permanent is not None and len(chain[: self.max_attempts]) == 1:
            raise permanent
        raise AllProvidersFailed(
            f"all {min(len(chain), self.max_attempts)} llm candidate(s) for tier "
            f"{tier!r} failed: {detail}"
        )

    async def get_completion(
        self,
        prompt: str,
        *,
        system: str = "",
        messages: Sequence[dict[str, Any]] | None = None,
        tier: str = TIER_FAST,
        **kw: Any,
    ) -> Completion:
        """Convenience shim: a prompt (plus optional system) becomes the message list.

        Named `get_completion` for callers that reach for the older
        `get_llm_completion` name, and re-exported from `finance_agent` under that
        name so the plain-English path reads naturally.
        """
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        if messages:
            msgs.extend(messages)
        elif prompt:
            msgs.append({"role": "user", "content": prompt})
        if not msgs:
            raise LLMError("get_completion needs prompt and/or messages")
        return await self.complete(msgs, tier=tier, **kw)

    def _payload(
        self, messages: Sequence[dict[str, Any]], model: str, *,
        tools: Sequence[dict[str, Any]] | None, tool_choice: Any,
        temperature: float, max_tokens: int, json_mode: bool,
        extra_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        if json_mode:
            # Plain json_object, not response_format json_schema: NIM supports the
            # former broadly and the latter inconsistently, and callers that want a
            # guarantee validate the parse rather than trusting the flag.
            body["response_format"] = {"type": "json_object"}
        # OpenRouter bills nothing back unless asked; NIM ignores the key outright.
        body["usage"] = {"include": True}
        if extra_body:
            body.update(extra_body)
        return body

    async def _call(self, cand: Candidate, body: dict[str, Any]) -> Completion:
        http = await self._http()
        headers = {"Content-Type": "application/json"}
        if cand.provider.name == "openrouter":
            # Attribution headers OpenRouter uses for rate-limit identity; harmless
            # elsewhere, so gated rather than global.
            headers["HTTP-Referer"] = self.referer or "http://localhost:3200"
            headers["X-Title"] = self.title
        resp = await http.post(
            cand.provider.url, json=body, headers=cand.provider.headers(headers))
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            if resp.status_code == 404:
                raise httpx.HTTPStatusError(
                    f"404 model not found: {detail}", request=resp.request, response=resp)
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 409, 425, 429):
                # 401/403: credentials. 400 and friends: our request shape, which a
                # differently-ruled provider may still accept.
                raise LLMRequestRejected(
                    f"HTTP {resp.status_code}: {detail}",
                    credential_failure=resp.status_code in (401, 403))
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {detail}", request=resp.request, response=resp)
        try:
            parsed = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise _BadResponse(f"non-JSON body: {resp.text[:160]!r}") from exc
        if not isinstance(parsed, dict) or "choices" not in parsed:
            raise _BadResponse(f"body without choices: {str(parsed)[:160]!r}")
        return _parse_completion(parsed, cand, attempts=0)

    # ------------------------------------------------------------------ helpers
    def describe(self) -> dict[str, Any]:
        """Non-secret view of the routing table, for /health and debugging."""
        return {
            "providers": [
                {"name": p.name, "base_url": p.base_url, "key_present": bool(p.api_key)}
                for p in self.providers
            ],
            # Per provider, because two providers serving different slugs for one
            # tier is the normal case. A flat tier list here would hide which
            # catalogue a deployment is actually pointed at.
            "models": {name: {t: list(ms) for t, ms in tiers.items()}
                       for name, tiers in self._chains().items()
                       if name in {p.name for p in self.providers}},
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "stats": dict(self.stats),
        }


class _BadResponse(Exception):
    pass


def _error_detail(resp: httpx.Response) -> str:
    """Best readable message out of an error body, which differs per provider."""
    snippet = resp.text[:300].replace("\n", " ")
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return snippet
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err is not None:
            return str(err)[:300]
        if data.get("detail"):
            return str(data["detail"])[:300]
    return snippet


def _parse_arguments(raw: str) -> tuple[dict[str, Any], str]:
    """(arguments, parse_error). Empty arguments with no error is a legitimate
    zero-argument call; a JSON error is not, and the caller decides what to do."""
    if not raw.strip():
        return {}, ""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"arguments are not valid JSON: {exc.msg} at offset {exc.pos}"
    if isinstance(value, dict):
        return value, ""
    return {"value": value}, ""


def _parse_completion(body: dict[str, Any], cand: Candidate, *, attempts: int) -> Completion:
    choices = body.get("choices") or []
    if not choices:
        raise _BadResponse("empty choices array")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    msg = choice.get("message") or {}
    usage = body.get("usage") or {}
    details = body.get("usage_details") or {}  # OpenRouter's alternative spelling
    in_tok = int(usage.get("prompt_tokens") or details.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or details.get("completion_tokens") or 0)
    raw_cost = usage.get("cost")
    if raw_cost is None:
        raw_cost = details.get("cost")
    if raw_cost is None:
        raw_cost = (in_tok + out_tok) / 1_000_000 * FALLBACK_COST_PER_MTOKEN

    calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = str(fn.get("arguments") or "")
        args, parse_error = _parse_arguments(raw_args)
        calls.append(ToolCall(
            name=str(fn.get("name") or ""), arguments=args,
            id=str(tc.get("id") or ""), raw_arguments=raw_args, parse_error=parse_error))

    text = msg.get("content")
    if not isinstance(text, str):
        # Some providers return None (tool call only) or a content-part list.
        if isinstance(text, list):
            text = "".join(
                str(p.get("text") or "") for p in text if isinstance(p, dict))
        else:
            text = ""

    return Completion(
        text=text,
        model=str(body.get("model") or cand.model),
        provider=cand.provider.name,
        tier="",
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=round(float(raw_cost), 6),
        attempts=attempts,
        finish_reason=str(choice.get("finish_reason") or ""),
        tool_calls=calls,
        raw=body,
    )


def providers_from_env() -> list[Provider]:
    """Build providers from env. Order is the rotation order within a tier.

    NVIDIA first: NIM's free-tier keys absorb the high-volume fast-tier traffic
    (every Telegram message), and OpenRouter, which costs money, then carries the
    reasoning tier and whatever NIM refuses.
    """
    nim_key = _clean(os.getenv("NVIDIA_API_KEY")) or _clean(os.getenv("NIM_API_KEY"))
    or_key = _clean(os.getenv("OPENROUTER_API_KEY"))
    nim_base = _clean(os.getenv("NVIDIA_NIM_BASE_URL")) or NIM_BASE
    or_base = _clean(os.getenv("OPENROUTER_BASE_URL")) or OPENROUTER_BASE

    providers: list[Provider] = []
    if nim_key:
        providers.append(Provider("nvidia", nim_base, nim_key))
    if or_key:
        providers.append(Provider("openrouter", or_base, or_key))
    return providers


def provider_order(tier: str) -> list[Provider]:
    """Configured providers, best-first for one tier.

    `candidates()` walks this order provider-outer, so whichever provider heads it
    sees that tier's whole model chain first. Reasoning prefers OpenRouter, where
    `anthropic/claude-3.5-sonnet` resolves; the fast tier prefers NIM, whose free
    llama slugs should absorb the high-volume traffic. With one provider
    configured, both tiers land on it and the preference is inert.

    Env: `LLM_PROVIDER_ORDER` (comma list, applies to every tier) or
    `LLM_FAST_PROVIDER` / `LLM_REASONING_PROVIDER`.
    """
    providers = providers_from_env()
    by_name = {p.name: p for p in providers}
    ordered: list[Provider] = []

    want_csv = _clean(os.getenv("LLM_PROVIDER_ORDER"))
    if want_csv:
        wanted = [p.strip().lower() for p in want_csv.split(",") if p.strip()]
        ordered = [by_name[n] for n in wanted if n in by_name]
    else:
        want = _clean(os.getenv("LLM_PROVIDER")) or ""
        if not want:
            want = (_clean(os.getenv("LLM_FAST_PROVIDER")) or "nvidia") if tier == TIER_FAST \
                else (_clean(os.getenv("LLM_REASONING_PROVIDER")) or "openrouter")
        if want in by_name:
            ordered.append(by_name[want])
    ordered.extend(p for p in providers if p not in ordered)
    return ordered


def models_from_env() -> dict[str, dict[str, tuple[str, ...]]]:
    """Chains to try, keyed by provider then tier, defaults plus env additions.

    Precedence within a chain, highest first:

      1. `LLM_ROUTER_<PROVIDER>_MODELS`, because a provider-specific line means the
         operator is naming what that catalogue sells.
      2. `LLM_ROUTER_FAST_MODELS` / `LLM_ROUTER_REASONING_MODELS`, which say "whatever
         you route to, try these first".
      3. For OpenRouter only, the names `engine/config.py` has always addressed there:
         `LLM_MODEL_CHEAP` into fast, `LLM_MODEL_STRONG` and `LLM_FALLBACK_CHAIN` into
         reasoning. Honouring them means one `.env` drives `engine/llm.py` and this
         router to the same model, which is what an operator reading `.env` expects.
      4. The built-in defaults for that provider, so a fresh checkout works.

    Provider-agnostic overrides land at the front of every provider's chain, and a
    slug a provider does not sell simply 404s and rotates: that is a wasted attempt,
    not a wrong answer, which is the right trade for letting one env line retarget a
    whole deployment.
    """
    global_slugs = {
        TIER_FAST: _split_models(_clean(os.getenv("LLM_ROUTER_FAST_MODELS")), ()),
        TIER_REASONING: _split_models(_clean(os.getenv("LLM_ROUTER_REASONING_MODELS")), ()),
    }
    chains: dict[str, dict[str, tuple[str, ...]]] = {}
    for provider in sorted(set(DEFAULT_MODELS) | {"nvidia", "openrouter"}):
        per_provider = _split_models(
            _clean(os.getenv(f"LLM_ROUTER_{provider.upper()}_MODELS")), ())
        base = DEFAULT_MODELS.get(provider, {})
        built: dict[str, tuple[str, ...]] = {}
        for tier in TIERS:
            parts: list[str] = list(per_provider) + list(global_slugs[tier])
            if provider == "openrouter":
                if tier == TIER_FAST:
                    parts += _split_models(_clean(os.getenv("LLM_MODEL_CHEAP")), ())
                else:
                    parts += _split_models(_clean(os.getenv("LLM_MODEL_STRONG")), ())
                    parts += _split_models(_clean(os.getenv("LLM_FALLBACK_CHAIN")), ())
            parts += list(base.get(tier, ()))
            built[tier] = tuple(dict.fromkeys(x for x in parts if x))
        chains[provider] = built
    return chains


def tier_models_from_env(provider: str = "openrouter") -> tuple[str, ...]:
    """One provider's chain for one tier-less call site, kept for compatibility."""
    return models_from_env().get(provider, {}).get(TIER_FAST, ())


def _allowed_openrouter() -> tuple[str, ...]:
    raw = _clean(os.getenv("LLM_ROUTER_ALLOWED_MODELS"))
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


_ALLOWED_OPENROUTER: tuple[str, ...] = ()


def configure(model_env_prefix: str = "") -> None:
    """Re-read the env-derived allowlist. Exists for tests and for a process that
    loads .env after import (finance_agent does exactly that in its lifespan)."""
    global _ALLOWED_OPENROUTER
    _ALLOWED_OPENROUTER = _allowed_openrouter()


configure()

_ROUTER: LLMRouter | None = None


def get_router() -> LLMRouter:
    """Process-wide router, built lazily so a missing key is visible at first call
    rather than at import (an import-time raise would take the whole API down for a
    config gap that only affects chat replies)."""
    global _ROUTER
    if _ROUTER is None:
        configure()
        _ROUTER = LLMRouter()
    return _ROUTER


async def aclose_router() -> None:
    global _ROUTER
    if _ROUTER is not None:
        await _ROUTER.aclose()
        _ROUTER = None


async def get_llm_completion(
    prompt: str,
    *,
    system: str = "",
    tier: str = TIER_FAST,
    messages: Sequence[dict[str, Any]] | None = None,
    tools: Sequence[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    **kw: Any,
) -> Completion:
    """Module-level entry point on the shared router."""
    return await get_router().get_completion(
        prompt, system=system, tier=tier, messages=messages,
        tools=tools, tool_choice=tool_choice, **kw)
