"""Base class for a Hustle worker.

A worker is a loop with three obligations, and this class owns all three so no
worker reimplements them badly:

1. Report a heartbeat every tick, including a failing one. A worker that dies
   quietly is indistinguishable from a worker that is merely slow.
2. Ask before spending. `_spend()` goes through the finance gatekeeper, so a
   worker physically cannot put a card charge on without a tier decision and a
   ledger entry. There is no `execute_sql("insert into ledger")` shortcut here.
3. Die loud. An exception in one iteration is logged and counted; three in a
   row flips the reported status to `error`, which the supervisor observes and
   acts on. A crash that stops the process is caught at the top, so the last
   heartbeat says 'error' rather than going silent.

Subclasses implement `work()` and return a dict of metrics for the heartbeat.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import signal
import time
from typing import Any

from .audit import AuditLog
from .config import Config, get_config
from .db import Database
from .finance_gatekeeper import FinanceGatekeeper, SpendIntent
from .llm import LLMClient

log = logging.getLogger("engine.worker")

CONSECUTIVE_ERRORS_BEFORE_UNWELL = 3


class Worker(abc.ABC):
    #: Override in each subclass: must match a row in engine.agents.
    handle: str = "worker"
    #: Optional hustle label, used to tag ledger rows and targets.
    hustle: str | None = None

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self.db = Database(self.cfg)
        self.audit = AuditLog(self.db, self.handle)
        self.finance = FinanceGatekeeper(self.db, self.cfg, self.audit)
        self.llm = LLMClient(self.db, self.cfg)
        self._seq = 0
        self._errors = 0
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------- lifecycle
    @abc.abstractmethod
    async def work(self) -> dict[str, Any]:
        """One unit of work. Return metrics for the heartbeat. Raise on failure."""

    async def on_start(self) -> None:
        """Optional setup: acquire a lease, warm a browser, load a vendor key."""

    async def on_stop(self) -> None:
        """Optional teardown. Always runs, including after a crash."""

    def tick_seconds(self) -> int:
        row = self.db.read_one(
            "select tick_seconds from engine.agents where handle = %s", (self.handle,))
        return int(row["tick_seconds"]) if row else self.cfg.worker_tick_seconds

    # ------------------------------------------------------------ spend gate
    async def spend(
        self, title: str, cost_minor: int, *, rationale: str = "",
        source_url: str | None = None, expected_roi: str | None = None,
        vendor: str | None = None, idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> SpendIntent:
        """Ask the gatekeeper to authorise a purchase. Returns the decision.

        Awaits the async facade: `preflight` opens two or three Postgres
        round-trips (envelope read, tier read, insert) and each one is ~127 ms to
        this region. Calling it directly would park the event loop for a third of a
        second, and that same loop also carries this worker's heartbeat timer and
        the Telegram sender it shares with the supervisor.
        """
        intent = await self.finance.apreflight(
            self.handle, title, cost_minor,
            rationale=rationale, source_url=source_url, expected_roi=expected_roi,
            hustle=self.hustle, vendor=vendor, idempotency_key=idempotency_key,
            context=context,
        )
        if intent.needs_human:
            await self.audit.ainfo(
                "worker.awaiting_approval",
                f"queued for a human tap: {title}",
                approval_id=intent.approval_id, cost_minor=cost_minor,
            )
        return intent

    def settle(self, intent: SpendIntent, *, ok: bool, description: str = "",
               external_id: str | None = None, provider: str = "manual",
               metadata: dict[str, Any] | None = None) -> int | None:
        """Record what happened after acting on an intent."""
        return self.finance.record_result(
            intent, ok=ok, description=description, external_id=external_id,
            external_provider=provider, metadata=metadata,
        )

    # ------------------------------------------------------------ heartbeat
    def heartbeat(self, status: str, message: str, metrics: dict[str, Any]) -> dict[str, Any]:
        """Report in and return the supervisor's directive for this agent."""
        self._seq += 1
        try:
            response = self.db.call(
                "report_heartbeat", self.handle, status, self._seq, message[:1000],
                int(metrics.get("duration_ms") or 0),
                int(metrics.get("spend_delta_minor") or 0),
                int(metrics.get("tokens_delta") or 0),
                str(metrics),
            )
        except Exception as exc:
            # Losing the heartbeat is not a reason to abandon the work loop.
            log.error("heartbeat write failed: %s", exc)
            return {}
        # Postgres hands back the desired state, which is how a pause reaches a
        # running worker without the worker polling a second query. report_heartbeat
        # returns jsonb, so psycopg hands us a dict; the fallback keeps a driver or
        # function change from crashing the work loop over a log line.
        return response if isinstance(response, dict) else {}

    async def aheartbeat(self, status: str, message: str,
                         metrics: dict[str, Any]) -> dict[str, Any]:
        """Heartbeat off the event loop.

        This is the steady-state case: every worker calls it once per tick, forever.
        `db.call` is a blocking round-trip to another region, ~127 ms measured, so
        leaving it on the loop would park the sleep timer, the Telegram sender and
        any concurrent work for that long, every tick, in all three workers. It is
        sequentially coupled (the caller needs the directive), which is exactly the
        shape `await asyncio.to_thread(...)` expresses without any redesign.
        """
        return await asyncio.to_thread(self.heartbeat, status, message, metrics)

    # ---------------------------------------------------------------- loop
    async def run_forever(self) -> None:
        log.info("worker %s starting (tick=%ss)", self.handle, self.tick_seconds())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:  # pragma: no cover
                pass

        await self.on_start()
        try:
            while not self._stopping.is_set():
                started = time.monotonic()
                status = "running"
                message = "ok"
                metrics: dict[str, Any] = {}
                try:
                    metrics = await self.work() or {}
                    self._errors = 0
                except Exception as exc:
                    self._errors += 1
                    status = "error" if self._errors >= CONSECUTIVE_ERRORS_BEFORE_UNWELL else "running"
                    message = f"{type(exc).__name__}: {exc}"
                    metrics = {}
                    log.exception("work iteration failed (%d in a row)", self._errors)
                    await self.audit.aerror("worker.iteration_failed",
                                     f"{type(exc).__name__}: {exc}", consecutive=self._errors)
                metrics["duration_ms"] = int((time.monotonic() - started) * 1000)
                metrics["consecutive_errors"] = self._errors
                directive = await self.aheartbeat(status, message, metrics)

                # A supervisor-set pause takes effect on the next sleep, not
                # mid-iteration: cutting a purchase in half is worse than a
                # few late seconds.
                if directive.get("desired_state") in ("paused", "disabled"):
                    log.info("supervisor wants %s; exiting cleanly after this tick",
                             directive["desired_state"])
                    await self.audit.ainfo("worker.stopping",
                                    f"desired_state={directive['desired_state']}")
                    break

                interval = self.tick_seconds()
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=max(5, interval))
                except TimeoutError:
                    continue
        finally:
            try:
                await self.on_stop()
            except Exception as exc:
                log.warning("on_stop failed: %s", exc)
            await self.llm.aclose()
            self.db.close()

    def run(self) -> None:
        from .logging_setup import setup_logging

        setup_logging(self.handle.replace(":", "-"))
        try:
            asyncio.run(self.run_forever())
        except KeyboardInterrupt:
            pass
