"""Read-only views, one registry per agent. Each view returns `(state, summary)`.

Rule: a view may only *read*. Marketing views call the agent's own read paths
(`MarketingSettings.describe`, `funnel_metrics.snapshot` / `by_ad`, which run the metrics export
with `--offline` and never write the operator sheet). Finance views are HTTP `GET`s against the
finance agent's `/health` and `/spend/envelope`, plus one `POST /chat/ask`: a read-only chat turn
whose only tool lists pending transactions (enforced in the finance agent, not here). Nothing here
approves, spends, pauses, publishes, renders, or changes a switch, and no view takes a file path.

An agent may name a `fallback` view: free text that is not a view name goes there whole
(`message`), with earlier user/assistant turns as `history`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import GatewaySettings, finance_chat_token

CHAT_PATH = "/chat/ask"

ViewResult = tuple[dict[str, Any], str]
Handler = Callable[[dict[str, str]], Awaitable[ViewResult]]

_SECRETISH = re.compile(r"token|secret|password|api_?key|authorization|cookie", re.I)
_HANDLE = re.compile(r"[a-z0-9][a-z0-9:_-]{0,63}")


class ViewError(ValueError):
    """Bad parameters or an unreachable upstream. The message is safe to show the user."""


def redact(value: Any) -> Any:
    """Drop any mapping key that looks like a credential, recursively. Belt and braces: the
    upstream payloads carry none today, and this keeps a future field from leaking one."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if not _SECRETISH.search(str(k))}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


@dataclass(frozen=True)
class View:
    name: str
    description: str
    params: tuple[str, ...]
    handler: Handler
    #: Free text, not key=value params: the gateway passes `message` and `history`.
    free_text: bool = False


def _day(params: dict[str, str], key: str, default: str) -> str:
    from marketing_agent.cli import resolve_day  # noqa: PLC0415 - stdlib-only, loaded on use
    raw = params.get(key, default)
    try:
        return resolve_day(raw)
    except ValueError:
        raise ViewError(f"{key} must be YYYY-MM-DD, today, yesterday or Ndaysago") from None


# ---- marketing --------------------------------------------------------------

def marketing_views() -> dict[str, View]:
    from marketing_agent import funnel_metrics  # noqa: PLC0415
    from marketing_agent.config import MarketingSettings  # noqa: PLC0415

    async def status(params: dict[str, str]) -> ViewResult:
        data = MarketingSettings.from_env().describe()
        armed = [k for k in ("can_pause_ads", "can_publish", "can_render") if data.get(k)]
        summary = (f"Marketing agent: dry-run {'on' if data['dry_run'] else 'OFF'}, "
                   f"AI {'on' if data['ai_enabled'] else 'off'}, "
                   f"{'armed: ' + ', '.join(armed) if armed else 'nothing armed'}.")
        return data, summary

    async def funnel_health(params: dict[str, str]) -> ViewResult:
        day = _day(params, "day", "yesterday")
        health = await asyncio.to_thread(funnel_metrics.snapshot, MarketingSettings.from_env(), day)
        data = health.to_dict()
        if not health.available:
            return data, f"Funnel health for {day}: unavailable ({'; '.join(health.reasons)})."
        if health.halt_scaling:
            return data, f"Funnel health for {day}: HALT scaling, {'; '.join(health.reasons)}."
        return data, f"Funnel health for {day}: within ceilings."

    async def attribution(params: dict[str, str]) -> ViewResult:
        since, until = _day(params, "since", "7daysago"), _day(params, "until", "today")
        data = await asyncio.to_thread(funnel_metrics.by_ad, MarketingSettings.from_env(),
                                       since, until)
        if not data.get("available"):
            return data, f"Attribution {since}..{until}: unavailable ({data.get('reason', '')})."
        ads = data.get("ads", [])
        trials = sum(int(a.get("trial_started", 0)) for a in ads)
        paid = sum(int(a.get("trial_paid", 0)) for a in ads)
        return data, (f"Attribution {since}..{until}: {len(ads)} ad bucket(s), "
                      f"{trials} trial(s) started, {paid} paid.")

    return {v.name: v for v in (
        View("status", "switches and what is armed (never credentials)", (), status),
        View("funnel_health", "dispute, fraud and prepaid ceilings for one day", ("day",),
             funnel_health),
        View("attribution", "funnel counts per ad (utm_content), first touch", ("since", "until"),
             attribution),
    )}


# ---- finance ----------------------------------------------------------------

def finance_views(settings: GatewaySettings,
                  transport: httpx.AsyncBaseTransport | None = None) -> dict[str, View]:
    async def get(path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=settings.finance_url, transport=transport,
                                         timeout=settings.http_timeout,
                                         follow_redirects=False) as client:
                response = await client.get(path, params=query)
        except httpx.HTTPError as exc:
            raise ViewError(f"finance agent unreachable ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise ViewError(f"finance agent answered HTTP {response.status_code} on {path}")
        try:
            body = response.json()
        except ValueError:
            raise ViewError(f"finance agent sent non-JSON on {path}") from None
        if not isinstance(body, dict):
            raise ViewError(f"finance agent sent an unexpected shape on {path}")
        return redact(body)

    async def health(params: dict[str, str]) -> ViewResult:
        data = await get("/health")
        llm = data.get("llm") or {}
        return data, (f"Finance agent: {'up' if data.get('ok') else 'NOT ok'}, dry-run "
                      f"{'on' if data.get('dry_run') else 'OFF'}, chat model "
                      f"{'configured' if llm.get('configured') else 'not configured'}.")

    async def chat(params: dict[str, Any]) -> ViewResult:
        token = finance_chat_token()
        if not token:
            raise ViewError("finance chat is off: set AGUI_FINANCE_CHAT_TOKEN (and FINANCE_CHAT_TOKEN "
                            "on the finance agent)")
        body = {"message": str(params.get("message", ""))[:1500],
                "history": list(params.get("history") or [])[-6:]}
        try:
            async with httpx.AsyncClient(base_url=settings.finance_url, transport=transport,
                                         timeout=settings.chat_timeout,
                                         follow_redirects=False) as client:
                response = await client.post(CHAT_PATH, json=body,
                                             headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise ViewError(f"finance agent unreachable ({type(exc).__name__})") from None
        if response.status_code in (401, 503):
            raise ViewError(f"finance chat refused (HTTP {response.status_code}): check "
                            "FINANCE_CHAT_TOKEN and the chat model on the finance agent")
        if response.status_code != 200:
            raise ViewError(f"finance agent answered HTTP {response.status_code} on {CHAT_PATH}")
        try:
            data = response.json()
        except ValueError:
            raise ViewError("finance agent sent non-JSON on chat") from None
        reply = data.get("reply") if isinstance(data, dict) else None
        if not isinstance(reply, str) or not reply.strip():
            raise ViewError("finance agent sent an empty chat reply")
        return {"read_only": bool(data.get("read_only", False))}, reply

    async def envelope(params: dict[str, str]) -> ViewResult:
        handle = params.get("agent_handle", settings.envelope_handles[0]).strip()
        if not _HANDLE.fullmatch(handle) or handle not in settings.envelope_handles:
            raise ViewError(f"agent_handle must be one of {', '.join(settings.envelope_handles)}")
        # ask_issuer stays false: the gateway never triggers a card-issuer call.
        data = await get("/spend/envelope", {"agent_handle": handle, "ask_issuer": "false"})
        return data, (f"Spend envelope for {handle}: "
                      f"{'allowed' if data.get('allowed') else 'blocked'}"
                      f"{' (' + str(data['reason']) + ')' if data.get('reason') else ''}.")

    return {v.name: v for v in (
        View("health", "finance agent up/down, dry-run, chat model configured", (), health),
        View("envelope", "remaining spend envelope for an allowed agent handle",
             ("agent_handle",), envelope),
        View("chat", "plain-English question about the books (read-only; anything that is not "
             "a view name)", (), chat, free_text=True),
    )}


#: Which view receives free text, per agent.
FALLBACK = {"finance": "chat"}


def registry(settings: GatewaySettings,
             finance_transport: httpx.AsyncBaseTransport | None = None) -> dict[str, dict[str, View]]:
    return {"marketing": marketing_views(), "finance": finance_views(settings, finance_transport)}
