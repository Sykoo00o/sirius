"""LLM calls through OpenRouter, fail-closed, with spend accounted as it happens.

Three behaviours are deliberate:

1. Fail-closed on budget. `budget_ok()` returns False on a DB error, not True.
   A database blip must never look like permission to keep spending.
2. Retries are keyed by an idempotency key, so a retry after a timeout cannot
   double-bill an agent's daily cap.
3. Cost is read from the response (`usage` + OpenRouter's `usage.cost`) and
   written to `engine.log_llm_call()`, which returns whether the agent is still
   under its cap. The response body is never persisted: prompts contain vendor
   prices and customer data we do not want replicated into a log table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config, get_config

log = logging.getLogger("engine.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Price per token used when the provider returns no cost: a rough blend that
# still moves the needle, so an unreported cost is not silently free.
FALLBACK_COST_PER_MTOKEN = 3.0


class LLMUnavailable(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    attempts: int
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def cache_key(prefix: str, *parts: Any) -> str:
    """Stable key for an LLM call's result, so a retry does not pay twice."""
    blob = "\x1f".join(str(p) for p in parts)
    digest = hashlib.sha256(blob.encode()).hexdigest()[:32]
    return f"{prefix}:{digest}"


class LLMClient:
    def __init__(self, db: Any, cfg: Config | None = None) -> None:
        self.db = db
        self.cfg = cfg or get_config()
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.llm_timeout_seconds, connect=15),
                limits=httpx.Limits(max_connections=20),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------- budget gate
    def spend_today(self, agent_handle: str) -> float:
        row = self.db.read_one(
            """
            select coalesce(sum(cost_usd), 0)::float as spent
              from engine.agent_spend_daily d
              join engine.agents a on a.id = d.agent_id
             where a.handle = %s
               and d.day = (now() at time zone 'UTC')::date
            """,
            (agent_handle,),
        )
        return float(row["spent"]) if row else 0.0

    def budget_ok(self, agent_handle: str) -> bool:
        """False when this agent or the engine has hit its daily LLM cap.

        Fail-closed: any exception returns False, because the safe reading of
        'we could not determine spend' is 'do not spend more'.
        """
        try:
            cap_total = float(self.db.setting("llm.daily_cap_usd", self.cfg.llm_daily_cap_usd))
            agent_cap = self.db.scalar(
                "select llm_budget_usd_day from engine.agents where handle = %s",
                (agent_handle,),
                default=0,
            )
            spent_agent = self.spend_today(agent_handle)
            spent_total = float(
                self.db.scalar(
                    "select coalesce(sum(cost_usd),0)::float from engine.agent_spend_daily "
                    "where day = (now() at time zone 'UTC')::date",
                    default=0,
                )
            )
            if spent_total >= cap_total:
                return False
            effective_agent_cap = float(agent_cap or 0)
            if effective_agent_cap > 0 and spent_agent >= effective_agent_cap:
                return False
            return True
        except Exception as exc:
            log.error("budget check failed for %s, assuming exhausted: %s", agent_handle, exc)
            return False

    # ------------------------------------------------------------------ calls
    async def complete(
        self,
        agent_handle: str,
        system: str,
        user: str,
        *,
        strong: bool = False,
        json_schema: dict[str, Any] | None = None,
        cache_key_: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> LLMResult:
        """One chat completion, with budget gate, cache and retry ladder."""
        if not self.cfg.openrouter_api_key:
            raise LLMUnavailable("OPENROUTER_API_KEY is unset")

        # Replay guard: a retried identical call returns the stored answer.
        key = cache_key_ or cache_key(agent_handle, system, user, strong)
        if cache_key_:
            cached = self._cache_get(key)
            if cached is not None:
                log.debug("llm cache hit %s", key)
                return cached

        if not self.budget_ok(agent_handle):
            raise BudgetExhausted(
                f"{agent_handle} is at or over its LLM cap for today; refusing another call"
            )

        models = self._models(strong)
        last_error: Exception | None = None

        for attempt in range(1, self.cfg.llm_max_attempts + 1):
            model = models[(attempt - 1) % len(models)]
            try:
                return await self._one_call(
                    agent_handle, model, system, user,
                    json_schema=json_schema, temperature=temperature,
                    max_tokens=max_tokens, attempts=attempt, key=key if cache_key_ else None,
                )
            except BudgetExhausted:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                log.warning("llm attempt %d/%d on %s failed: %s",
                            attempt, self.cfg.llm_max_attempts, model, exc)
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                last_error = exc
                if code in (402, 429):
                    # Out of credits, or throttled: back off and change model.
                    log.warning("llm %d from %s; rotating model", code, model)
                elif 500 <= code < 600:
                    log.warning("llm upstream %d on %s", code, model)
                else:
                    # 4xx on our request shape will not fix itself by retrying.
                    raise LLMUnavailable(
                        f"openrouter rejected the request ({code}): "
                        f"{exc.response.text[:300]}"
                    ) from exc
            await asyncio.sleep(min(2 ** attempt, 15))

        raise LLMUnavailable(
            f"all {self.cfg.llm_max_attempts} llm attempts failed: {last_error}"
        )

    def _models(self, strong: bool) -> list[str]:
        primary = self.cfg.llm_model_strong if strong else self.cfg.llm_model_cheap
        chain = [primary, *self.cfg.llm_fallback_chain]
        # Dedupe, preserve order.
        seen: set[str] = set()
        return [m for m in chain if not (m in seen or seen.add(m))]

    async def _one_call(
        self, agent_handle: str, model: str, system: str, user: str, *,
        json_schema: dict[str, Any] | None, temperature: float,
        max_tokens: int, attempts: int, key: str | None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Ask OpenRouter to report spend, otherwise usage.cost is absent and
            # we fall back to a guess that drifts from what the invoice says.
            "usage": {"include": True},
        }
        if json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "engine_response", "strict": True, "schema": json_schema}}

        http = await self._http()
        headers = {
            "Authorization": f"Bearer {self.cfg.openrouter_api_key}",
            "HTTP-Referer": self.cfg.public_base_url,
            "X-Title": "Ghost X2 engine",
        }
        resp = await http.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()

        choice = (body.get("choices") or [{}])[0]
        usage = body.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        raw_cost = usage.get("cost")
        if raw_cost is None:
            raw_cost = (in_tok + out_tok) / 1_000_000 * FALLBACK_COST_PER_MTOKEN
        cost = round(float(raw_cost), 6)

        # Account the spend. log_llm_call returns False once the cap is crossed,
        # so the next call in the loop is refused before it costs anything.
        still_under = True
        try:
            still_under = bool(self.db.call(
                "log_llm_call", agent_handle, model, in_tok, out_tok, cost,
                json.dumps({"attempts": attempts}),
            ))
        except Exception as exc:
            # We spent the money; record the fact that we could not account it.
            log.error("failed to account llm spend of $%s on %s: %s", cost, agent_handle, exc)

        result = LLMResult(
            text=(choice.get("message") or {}).get("content") or "",
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            attempts=attempts,
            finish_reason=choice.get("finish_reason") or "",
        )
        if key:
            self._cache_put(key, result)
        if not still_under:
            raise BudgetExhausted(f"{agent_handle} crossed its LLM cap during this call")
        return result

    # ---------------------------------------------------------------- cache
    def _cache_get(self, key: str) -> LLMResult | None:
        try:
            row = self.db.read_one(
                "select value from engine.settings where key = %s", (f"llm.cache.{key}",)
            )
            if not row:
                return None
            v = row["value"]
            v = json.loads(v) if isinstance(v, str) else v
            return LLMResult(**v)
        except Exception:
            return None

    def _cache_put(self, key: str, result: LLMResult) -> None:
        try:
            self.db.set_setting(f"llm.cache.{key}", result.__dict__, actor="llm")
        except Exception as exc:
            log.debug("could not cache llm result: %s", exc)

    async def json_complete(
        self, agent_handle: str, system: str, user: str, schema: dict[str, Any], **kw: Any
    ) -> dict[str, Any]:
        """Complete and parse, raising rather than guessing on malformed output."""
        result = await self.complete(agent_handle, system, user, json_schema=schema, **kw)
        text = (result.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.index("{"):] if "{" in text else text
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(
                f"model returned non-JSON on {result.model}: {text[:200]!r}"
            ) from exc
