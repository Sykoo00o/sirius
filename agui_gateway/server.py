"""AG-UI endpoint for this project's agents. Read-only (phase 0 views + phase 1 finance chat).

    POST /agui/{agent}     body: AG-UI RunAgentInput; response: AG-UI event stream (SSE)
    GET  /healthz          liveness only, no data

Choosing a view, in order:
  1. `forwardedProps.view` (+ `forwardedProps.params`), for a dashboard button;
  2. the last user message: first word is the view, the rest `key=value` params,
     e.g. "attribution since=7daysago until=today";
  3. otherwise, if the agent has a fallback view (finance: `chat`), the whole message goes there,
     with earlier user/assistant turns of `messages` as history;
  4. otherwise the run answers with the list of views (still a successful run). `help` always does.

Every run streams the same shape:
  RUN_STARTED -> STEP_STARTED(view) -> STATE_SNAPSHOT{agent, view, read_only, data}
  -> TEXT_MESSAGE_START/CONTENT/END (one-line summary) -> STEP_FINISHED -> RUN_FINISHED
A bad parameter or an unreachable upstream ends with RUN_ERROR instead, with a message safe to show.

What phase 0 refuses, and how:
  * no bearer token configured -> 503 for every run (fail closed, never served open);
  * wrong token -> 401;
  * a run carrying `resume` (an answer to an interrupt) -> RUN_ERROR code `read_only`: there are no
    approvals here yet, so nothing may be "approved" through this door;
  * client-offered `tools` and client `state` are ignored: the browser is not an authority.
"""

from __future__ import annotations

import hmac
import logging
import shlex
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from ag_ui.core import (
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import views as views_mod
from .config import GatewaySettings, gateway_token

log = logging.getLogger("agui_gateway")


def _authorise(authorization: str | None) -> None:
    expected = gateway_token()
    if not expected:
        raise HTTPException(503, "AGUI_GATEWAY_TOKEN is not configured; the gateway serves nothing")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(),
                                                             expected.encode()):
        raise HTTPException(401, "missing or wrong bearer token",
                            headers={"WWW-Authenticate": "Bearer"})


def _last_user_text(run: RunAgentInput) -> str:
    for message in reversed(run.messages or []):
        if getattr(message, "role", "") == "user":
            content = getattr(message, "content", "")
            if isinstance(content, str):
                return content
            # Multimodal content: keep only text parts.
            return " ".join(str(getattr(p, "text", "") or "") for p in content or [])
    return ""


def chat_history(run: RunAgentInput) -> list[dict[str, str]]:
    """Earlier user/assistant text turns, oldest first, excluding the current message."""
    turns = []
    for message in run.messages or []:
        role, content = getattr(message, "role", ""), getattr(message, "content", "")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            turns.append({"role": role, "content": content[:1500]})
    if turns and turns[-1]["role"] == "user":
        turns.pop()
    return turns[-6:]


def choose_view(run: RunAgentInput) -> tuple[str, dict[str, str]]:
    props = run.forwarded_props if isinstance(run.forwarded_props, dict) else {}
    if props.get("view"):
        raw = props.get("params") if isinstance(props.get("params"), dict) else {}
        return str(props["view"]).strip().lower(), {str(k): str(v) for k, v in raw.items()}
    text = _last_user_text(run).strip()
    if not text:
        return "", {}
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    params = dict(w.split("=", 1) for w in words[1:] if "=" in w)
    return words[0].lower().lstrip("/"), params


def create_app(settings: GatewaySettings | None = None, *,
               finance_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or GatewaySettings.from_env()
    registry = views_mod.registry(settings, finance_transport)
    app = FastAPI(title="Ghost X2 AG-UI gateway", version="0.1.0", docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "phase": 0, "read_only": True}

    @app.post("/agui/{agent}")
    async def run_agent(agent: str, run: RunAgentInput, request: Request,
                        authorization: str | None = Header(default=None)) -> StreamingResponse:
        _authorise(authorization)
        if agent not in registry:
            raise HTTPException(404, f"unknown agent; available: {', '.join(sorted(registry))}")
        encoder = EventEncoder(accept=request.headers.get("accept"))
        return StreamingResponse(stream(agent, run, encoder), media_type=encoder.get_content_type())

    async def stream(agent: str, run: RunAgentInput, encoder: EventEncoder) -> AsyncIterator[str]:
        ids = {"thread_id": run.thread_id, "run_id": run.run_id}
        yield encoder.encode(RunStartedEvent(type=EventType.RUN_STARTED, **ids))
        if run.resume:
            yield encoder.encode(RunErrorEvent(
                type=EventType.RUN_ERROR, code="read_only",
                message="this gateway is read-only (phase 0): there is nothing to resume or approve"))
            return
        agent_views = registry[agent]
        name, params = choose_view(run)
        view = agent_views.get(name)
        fallback = agent_views.get(views_mod.FALLBACK.get(agent, ""))
        if view is not None and view.free_text:
            view = None  # free-text views are reached by fallback only, never by keyword
        if view is None and fallback is not None and name not in ("", "help"):
            text = _last_user_text(run).strip()
            if text:
                view, name = fallback, fallback.name
                params = {"message": text, "history": chat_history(run)}
        if view is None:
            listing = "; ".join(f"{v.name}" + (f" ({', '.join(v.params)})" if v.params else "")
                                + f": {v.description}" for v in agent_views.values())
            state = {"agent": agent, "view": "help", "read_only": True,
                     "data": {"views": [{"name": v.name, "params": list(v.params),
                                         "description": v.description}
                                        for v in agent_views.values()]}}
            async for chunk in _respond(encoder, "help", state,
                                        f"Views for {agent}: {listing}.", ids):
                yield chunk
            return
        unknown = [] if view.free_text else sorted(set(params) - set(view.params))
        if unknown:
            yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, code="bad_params",
                                               message=f"unknown parameter(s): {', '.join(unknown)}"))
            return
        try:
            data, summary = await view.handler(params)
        except views_mod.ViewError as exc:
            yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, code="view_error",
                                               message=str(exc)[:300]))
            return
        except Exception as exc:  # noqa: BLE001 - never leak internals into the stream
            log.exception("view %s/%s failed", agent, name)
            yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, code="internal",
                                               message=f"{agent}/{name} failed ({type(exc).__name__})"))
            return
        state = {"agent": agent, "view": name, "read_only": True, "data": views_mod.redact(data)}
        async for chunk in _respond(encoder, name, state, summary, ids):
            yield chunk

    return app


async def _respond(encoder: EventEncoder, step: str, state: dict[str, Any], summary: str,
                   ids: dict[str, str]) -> AsyncIterator[str]:
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    yield encoder.encode(StepStartedEvent(type=EventType.STEP_STARTED, step_name=step))
    yield encoder.encode(StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=state))
    yield encoder.encode(TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START,
                                               message_id=message_id, role="assistant"))
    yield encoder.encode(TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT,
                                                 message_id=message_id, delta=summary))
    yield encoder.encode(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=message_id))
    yield encoder.encode(StepFinishedEvent(type=EventType.STEP_FINISHED, step_name=step))
    yield encoder.encode(RunFinishedEvent(type=EventType.RUN_FINISHED, **ids))
