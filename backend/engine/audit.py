"""Structured logging and the human-readable audit stream.

Container stdout stays in Docker (the dashboard tails it separately). This is
the *semantic* stream: named events that answer "why did the engine spend
$50 at 03:12" without parsing a stack trace.

Secrets are scrubbed on the way in, because agent-authored context regularly
contains a URL with a token in it, and agent_logs is readable by a browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import contextmanager
from typing import Any

log = logging.getLogger("engine.audit")

# Order matters: the bearer pattern must run before the generic key=value one,
# or it leaves a half-redacted fragment behind.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{12,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|passwd|auth)=([^&\s\"']+)"),
     r"\1=[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[redacted-key]"),
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"), "[redacted-key]"),
    (re.compile(r"\br[k|sk]_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[redacted-key]"),
    (re.compile(r"\bnvapi-[A-Za-z0-9_-]{16,}\b"), "[redacted-key]"),
    (re.compile(r"\bjina_[A-Za-z0-9_-]{16,}\b"), "[redacted-key]"),
    # Telegram bot tokens are literally `\d{8,10}:[A-Za-z0-9_-]{35}`.
    (re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"), "[redacted-bot-token]"),
    (re.compile(r"postgres(?:ql)?://[^:@/\s]+:[^@\s]+@"), "postgres://[redacted]@"),
    (re.compile(r"\bsb_(?:publishable|secret)_[A-Za-z0-9_-]{8,}\b"), "[redacted-key]"),
]


def scrub(text: str) -> str:
    """Redact credentials from free text before it reaches a readable log table."""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def scrub_obj(value: Any, _depth: int = 0) -> Any:
    """Recursively scrub a context payload, bounding depth against recursive refs."""
    if _depth > 8:
        return "...truncated"
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_obj(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_obj(v, _depth + 1) for v in value]
    return value


class AuditLog:
    """Writes engine.agent_logs. Best-effort by design: a log write must never
    take down a financial operation that has already committed."""

    LEVELS = ("debug", "info", "warning", "error", "critical")

    def __init__(self, db: Any, agent_handle: str | None = None) -> None:
        self.db = db
        self.agent_handle = agent_handle
        self._agent_id: str | None = None

    def _resolve_agent(self) -> str | None:
        if self._agent_id or not self.agent_handle:
            return self._agent_id
        try:
            row = self.db.read_one(
                "select id::text as id from engine.agents where handle = %s",
                (self.agent_handle,),
            )
            self._agent_id = row["id"] if row else None
        except Exception:
            self._agent_id = None
        return self._agent_id

    def write(
        self,
        event: str,
        message: str,
        *,
        level: str = "info",
        context: dict[str, Any] | None = None,
        audit: bool = False,
    ) -> None:
        if level not in self.LEVELS:
            level = "info"
        safe_message = scrub(message)[:4000]
        safe_context = scrub_obj(context or {})
        try:
            self.db.execute(
                """
                insert into engine.agent_logs (agent_id, level, event, message, context, is_audit)
                values (
                  coalesce((select id from engine.agents where handle = %s), %s),
                  %s, %s, %s, %s::jsonb, %s
                )
                """,
                (
                    self.agent_handle,
                    self._resolve_agent(),
                    level,
                    event,
                    safe_message,
                    json.dumps(safe_context, default=str),
                    audit,
                ),
            )
        except Exception as exc:
            # Fall back to stdout: the event still happened, we just lost the row.
            log.warning("audit write failed (%s); falling back to stdout: %s | %s",
                        exc, event, safe_message)

    # ------------------------------------------------------------ async facade
    # Every write is an INSERT into another region (measured 127 ms per round-trip,
    # plus the correlated `select id from agents` in the statement itself). Called
    # straight from an `async def`, one log line therefore freezes the event loop
    # for that long, and audit calls sit on the hot paths: a failed iteration, a
    # stop signal, a fleet button press. So callers under asyncio use these, which
    # keep the exact same best-effort behaviour off the loop.
    async def awrite(self, event: str, message: str, **kw: Any) -> None:
        await asyncio.to_thread(self.write, event, message, **kw)

    async def ainfo(self, event: str, message: str, **context: Any) -> None:
        await asyncio.to_thread(self.info, event, message, **context)

    async def aerror(self, event: str, message: str, **context: Any) -> None:
        await asyncio.to_thread(self.error, event, message, **context)

    async def adanger(self, event: str, message: str, **context: Any) -> None:
        await asyncio.to_thread(self.danger, event, message, **context)

    async def aaudit(self, event: str, message: str, **context: Any) -> None:
        await asyncio.to_thread(self.audit, event, message, **context)

    def audit(self, event: str, message: str, **context: Any) -> None:
        """A decision by a human or a rail, always surfaced in the audit stream."""
        self.write(event, message, level="info", context=context, audit=True)

    def spend(self, message: str, **context: Any) -> None:
        self.write("spend", message, level="info", context=context, audit=True)

    def danger(self, event: str, message: str, **context: Any) -> None:
        """Something that should page: cap breached, webhook failed, card refused."""
        self.write(event, message, level="critical", context=context, audit=True)

    def error(self, event: str, message: str, **context: Any) -> None:
        self.write(event, message, level="error", context=context)

    def info(self, event: str, message: str, **context: Any) -> None:
        self.write(event, message, level="info", context=context)

    @contextmanager
    def timed(self, event: str, message: str, **context: Any):
        """Log duration of a block, and its failure, without swallowing the error."""
        import time

        started = time.monotonic()
        try:
            yield
        except Exception as exc:
            self.error(
                f"{event}.failed",
                f"{message} failed after {time.monotonic() - started:.2f}s: {exc}",
                **context,
            )
            raise
        else:
            self.info(
                f"{event}.done",
                f"{message} in {time.monotonic() - started:.2f}s",
                **context,
            )
