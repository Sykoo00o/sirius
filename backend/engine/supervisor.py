"""Supervisor: the tick loop that keeps the fleet pointed at its desired state.

Responsibilities, in the order a tick performs them:

 1. refresh live settings into memory (caps, dry-run, stale threshold)
 2. expire approvals nobody answered
 3. read each agent's newest heartbeat
 4. declare dead anything silent past the stale window
 5. reconcile: where actual != desired, issue the one compose verb that closes
    the gap, then report a heartbeat for the action taken
 6. surface the tick's own heartbeat so a dead supervisor is detectable

Design choices that matter:

* Desired state lives in the database (`agents.desired_state`), actual state is
  observed from heartbeats and the Docker daemon. The supervisor converges the
  second onto the first. Nobody edits code to pause a worker.
* A tick that throws does not kill the process. Supervisor ticks run unattended
  at 03:00; a transient DB blip must cost one tick, not the fleet.
* Restart storms are bounded: a worker that keeps dying is escalated to a human
  instead of being restarted forever.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Any

from .audit import AuditLog
from .config import Config, get_config
from .db import Database, MigrationError, make_migrator
from .docker_control import HANDLE_TO_SERVICE, DockerControl
from .finance_gatekeeper import FinanceGatekeeper
from .llm import LLMClient
from .telegram import TelegramSender

log = logging.getLogger("engine.supervisor")

AGENT_HANDLE = "supervisor"
# Consecutive failures before a worker is escalated rather than restarted again.
MAX_RESTART_STREAK = 3


@dataclass
class AgentView:
    """One agent as the supervisor sees it this tick."""

    handle: str
    desired: str
    reported: str  # last status an agent claimed
    container: str | None
    seconds_since_beat: int | None
    stale: bool
    llm_cap_breached: bool

    @property
    def observed(self) -> str:
        """Effective state. A stale heartbeat is 'error': the process is not
        telling us it is alive, so we may not assume it is."""
        if self.stale:
            return "error"
        if self.llm_cap_breached and self.reported == "running":
            return "paused"
        return self.reported


@dataclass
class TickResult:
    actions: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    expired_approvals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions": self.actions, "stale_agents": self.stale,
            "expired_approvals": self.expired_approvals, "errors": self.errors,
            "duration_ms": self.duration_ms,
        }


class Supervisor:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self.db = Database(self.cfg)
        self.audit = AuditLog(self.db, AGENT_HANDLE)
        self.docker = DockerControl(self.db, self.cfg)
        self.docker.audit = self.audit
        self.finance = FinanceGatekeeper(self.db, self.cfg, self.audit)
        self.llm = LLMClient(self.db, self.cfg)
        self.digest = TelegramSender(self.cfg.digest_bot, self.cfg, "digest", self.db)
        self.approvals = TelegramSender(self.cfg.approval_bot, self.cfg, "approval", self.db)

        self._seq = 0
        self._stopping = asyncio.Event()
        # handle -> consecutive failed converge attempts, in this process only.
        self._restart_streak: dict[str, int] = {}

    # ------------------------------------------------------------------ boot
    def migrate(self) -> list[str]:
        """Bring the schema up before anything reads it. Safe to call from every
        container: the migrator holds a transaction-scoped advisory lock."""
        try:
            applied = make_migrator(self.cfg).apply_all()
        except MigrationError as exc:
            # An edited migration means someone rewrote history. Do not guess.
            log.critical("migration refused: %s", exc)
            raise
        if applied:
            self.audit.audit("schema.migrated", f"applied {len(applied)} migration(s)",
                             files=applied)
        return applied

    def boot_banner(self) -> None:
        dry_run = self.db.is_dry_run()
        log.info("supervisor starting | runtime=%s env=%s dry_run=%s",
                 self.cfg.runtime, self.cfg.env, dry_run)
        for problem in self.cfg.validate():
            log.warning("config: %s", problem)
        if not self.cfg.approval_bot:
            log.warning(
                "no approval bot token configured: tier2 approvals will queue in the database "
                "but nobody will be pinged. Set TELEGRAM_APPROVAL_BOT_TOKEN."
            )
        self.audit.info(
            "supervisor.start",
            f"supervisor up, dry_run={'true' if dry_run else 'FALSE (live money)'}",
            runtime=self.cfg.runtime, env=self.cfg.env,
        )

    async def shutdown(self) -> None:
        await self.llm.aclose()
        await self.digest.aclose()
        await self.approvals.aclose()
        self.db.close()

    # ------------------------------------------------------------------- tick
    async def tick(self) -> TickResult:
        started = time.monotonic()
        result = TickResult()

        # Step 1: live settings. Everything downstream reads these, so a cap
        # change takes effect on the next tick without a redeploy.
        stale_after = int(self.db.setting(
            "heartbeat.stale_seconds", self.cfg.heartbeat_stale_seconds))
        dry_run = self.db.is_dry_run()

        # Step 2: approvals that timed out. Awaited, not called: expire_stale is a
        # SELECT plus one decide_approval call per expired row, so five stale
        # approvals is six round-trips (~760 ms here) that would otherwise freeze
        # the loop this process shares with its Telegram polling task.
        try:
            result.expired_approvals = await self.finance.aexpire_stale()
        except Exception as exc:
            result.errors.append(f"expire_stale: {exc}")
            log.exception("approval expiry failed")

        # Step 3: read fleet state.
        try:
            views = self._load_views(stale_after)
        except Exception as exc:
            log.exception("could not read agent state")
            result.errors.append(f"load_state: {exc}")
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        # Step 4 + 5: staleness, then convergence.
        for view in views:
            if view.stale:
                result.stale.append(view.handle)
            if view.observed != view.desired:
                action = await self._converge(view, dry_run)
                if action:
                    result.actions.append(action)

        # A worker breaching its LLM cap is paused on the next tick above; we
        # still page a human, because pausing silently is a mystery at 08:00.
        for view in views:
            if view.llm_cap_breached and view.desired == "running":
                await self._notify(
                    f"⚠️ <b>{view.handle}</b> hit its LLM budget and was paused by the "
                    "supervisor. Raise the cap or wait for the daily reset.",
                    urgent=True,
                )

        # Step 6: our own heartbeat, last, so it reflects a tick that completed.
        self._seq += 1
        try:
            self.db.call(
                "report_heartbeat", AGENT_HANDLE, "running", self._seq,
                f"tick {self._seq}: {len(result.actions)} action(s), {len(result.stale)} stale",
                int((time.monotonic() - started) * 1000), 0, 0,
                str(result.as_dict()),
            )
        except Exception as exc:
            result.errors.append(f"heartbeat: {exc}")

        result.duration_ms = int((time.monotonic() - started) * 1000)
        if result.actions or result.errors:
            await self.audit.ainfo(
                )
        return result

    def _load_views(self, stale_after: int) -> list[AgentView]:
        rows = self.db.read("select * from engine.vw_agent_fleet_health")
        views: list[AgentView] = []
        for r in rows:
            seconds = r.get("seconds_since_beat")
            views.append(AgentView(
                handle=r["handle"],
                desired=r["desired_state"],
                reported=r["last_status"] or "disabled",
                container=r.get("container"),
                seconds_since_beat=seconds,
                stale=(seconds is None or int(seconds) > stale_after)
                and r["desired_state"] in ("running", "paused"),
                llm_cap_breached=bool(r.get("llm_cap_breached")),
            ))
        return views

    async def _converge(self, view: AgentView, dry_run: bool) -> str | None:
        """Issue exactly one action to close the gap between actual and desired."""
        service = HANDLE_TO_SERVICE.get(view.handle)
        desired, observed = view.desired, view.observed

        # A disabled agent that nobody has touched is not an incident.
        if desired == "disabled" and observed in ("disabled", "error") and view.stale:
            return None

        if not service:
            await self.audit.aerror("converge.no_service",
                             f"{view.handle} wants {desired} but has no compose service mapped")
            return f"skip {view.handle}: unmapped service"

        if desired == "running":
            verb = "restart" if observed == "error" else "start"
        elif desired == "paused" or desired == "disabled":
            verb = "stop"
        elif desired == "error":
            # Someone set desired_state='error' deliberately; leave it alone.
            return None
        else:
            verb = "start"

        streak = self._restart_streak.get(view.handle, 0)
        if verb == "restart" and streak >= MAX_RESTART_STREAK:
            await self.audit.adanger(
                "fleet.restart_storm",
                f"{view.handle} has failed {streak} times in a row; leaving it down and "
                "escalating instead of restarting again",
                handle=view.handle, service=service, streak=streak,
                )
            await self._notify(
                f"🛑 <b>{view.handle}</b> has crashed {streak} times running. The supervisor "
                "has stopped restarting it. Check the logs before setting it back to running.",
                urgent=True,
            )
            self.db.set_setting(f"fleet.{view.handle}.needs_human", True, actor="supervisor")
            return f"escalated {view.handle}"

        log.info("converge %s: %s -> %s via %s", view.handle, observed, desired, verb)
        converge_fn = getattr(self.docker, verb, None)
        if converge_fn is None:
            await self.audit.aerror("converge.bad_verb",
                             f"no docker action for {verb!r}", handle=view.handle)
            return f"skip {view.handle}: no action for {verb}"
        try:
            outcome = await converge_fn(service)
        except Exception as exc:
            self._restart_streak[view.handle] = streak + 1
            await self.audit.aerror("fleet.action_failed",
                             f"{verb} {service} failed: {exc}", handle=view.handle)
            return f"{verb} {view.handle} FAILED: {exc}"

        if outcome.simulated:
            note = f"{verb} {view.handle} SIMULATED (runtime={self.cfg.runtime})"
        elif not outcome.ok:
            self._restart_streak[view.handle] = streak + 1
            await self.audit.aerror(
                "fleet.action_failed",
                f"{verb} {service} exited {outcome.code}: {outcome.stderr[:300]}",
                handle=view.handle,
            )
            return f"{verb} {view.handle} exit {outcome.code}"
        else:
            self._restart_streak[view.handle] = 0
            note = f"{verb} {view.handle} ({observed} -> {desired})"

        await self.audit.aaudit(f"fleet.{verb}", note, handle=view.handle, service=service,
                         desired=desired, observed=observed, simulated=outcome.simulated)
        return note

    async def _notify(self, text: str, *, urgent: bool = False) -> None:
        """Send to the digest channel. Never raises: a page that fails must not
        break the tick that detected the problem."""
        sender = self.digest if self.digest.configured else self.approvals
        if not sender.configured:
            log.warning("no telegram channel configured, page dropped: %s", text[:160])
            return
        result = await sender.send(text)
        if not result.ok:
            log.warning("page delivery failed: %s", result.error)
            await self.audit.aerror(
                "notify.failed", f"could not deliver page: {result.error}", urgent=urgent)

    # -------------------------------------------------------------------- run
    async def run_forever(self) -> None:
        self.boot_banner()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

        tick_seconds = self.cfg.supervisor_tick_seconds
        # One immediate tick so `docker compose up` reconciles the fleet at once
        # rather than after a blind wait.
        first = True
        while not self._stopping.is_set():
            try:
                result = await self.tick()
                if result.errors and not first:
                    log.warning("tick reported %d error(s)", len(result.errors))
            except Exception as exc:
                # Last line of defence: a tick must never end the loop.
                log.exception("tick crashed: %s", exc)
                try:
                    await self.audit.aerror("supervisor.tick_crash", f"tick crashed: {exc}")
                except Exception as audit_exc:
                    # Both the tick and its audit write failed: the audit database
                    # is the likely common cause, which is worth its own line.
                    log.error("audit write failed during crash handling: %s", audit_exc)
            first = False

            interval = int(self.db.setting("supervisor.tick_seconds", tick_seconds))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=max(5, interval))
            except TimeoutError:
                continue


def main() -> None:
    from .logging_setup import setup_logging

    setup_logging("supervisor")
    supervisor = Supervisor()
    supervisor.migrate()
    try:
        asyncio.run(supervisor.run_forever())
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.run(supervisor.shutdown())


if __name__ == "__main__":
    main()
