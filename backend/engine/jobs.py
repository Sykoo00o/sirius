"""Scheduled jobs: morning digest, daily rollup, per-hustle scans.

Scheduler choice: APScheduler's BlockingScheduler in the supervisor process,
because the jobs need the full engine (Postgres, Telegram, the gatekeeper) and
cron would mean shipping a second, separately-configured way in. One process,
one config, one audit stream.

Idempotency contract, since `docker compose up` can start the supervisor twice
during a deploy and a restart replays missed jobs:
* rollup: recomputes a day from source and upserts, never increments.
* digest: a `jobs.last_sent.<name>.<date>` marker is written *before* sending.
  A crash after the marker means one digest is skipped on that day, which is the
  correct trade against mailing the owner twice.
* scans: each worker loop already does its own scanning, so these jobs are the
  scheduled *trigger* for work that has no loop, and they are no-ops when the
  relevant worker is running.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .audit import AuditLog
from .config import Config, get_config
from .db import Database
from .finance_gatekeeper import FinanceGatekeeper, fmt_minor
from .settings import SettingsAccessor
from .telegram import TelegramSender

log = logging.getLogger("engine.jobs")


class JobRunner:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self.db = Database(self.cfg)
        self.settings = SettingsAccessor(self.db)
        self.audit = AuditLog(self.db, "supervisor")
        self.finance = FinanceGatekeeper(self.db, self.cfg, self.audit)
        self.digest = TelegramSender(self.cfg.digest_bot, self.cfg, "digest", self.db)
        self.approvals = TelegramSender(self.cfg.approval_bot, self.cfg, "approval", self.db)
        self._scheduler: BlockingScheduler | None = None

    # ------------------------------------------------------------- utilities
    def _already_ran(self, name: str, bucket: str) -> bool:
        key = f"jobs.last_run.{name}.{bucket}"
        return bool(self.settings.get(key, None))

    def _mark_ran(self, name: str, bucket: str) -> None:
        self.settings.set(f"jobs.last_run.{name}.{bucket}",
                          datetime.now(tz=UTC).isoformat(), actor="jobs")

    def _sender(self, prefer: str = "digest") -> TelegramSender:
        """Digest channel when configured, otherwise whatever bot we have.

        Falling back rather than dropping the message matters on a fresh install
        where exactly one bot has been created so far.
        """
        primary = self.digest if prefer == "digest" else self.approvals
        if primary.configured:
            return primary
        other = self.approvals if prefer == "digest" else self.digest
        if other.configured:
            log.info("preferred telegram channel %s not configured; using the other one", prefer)
            return other
        return primary

    # ------------------------------------------------------------------ jobs
    def job_daily_rollup(self) -> None:
        """Fold yesterday's ledger into daily_metrics for the dashboard charts."""
        yesterday = datetime.now(tz=UTC).date() - 1
        try:
            self.db.call("rollup_daily_metrics", yesterday)
            self.audit.info("jobs.rollup", f"rolled up {yesterday} into daily_metrics")
        except Exception as exc:
            self.audit.error("jobs.rollup_failed", f"rollup for {yesterday} failed: {exc}")

    def job_expire_approvals(self) -> None:
        expired = self.finance.expire_stale()
        if expired:
            self.audit.info("jobs.approvals_expired",
                            f"{len(expired)} approval(s) expired unanswered",
                            ids=expired[:20])

    def job_morning_digest(self) -> None:
        """The 08:00 card: PnL, fleet, approvals, disputes."""
        today = datetime.now(tz=self.cfg.timezone).date().isoformat()
        if self._already_ran("morning_digest", today):
            log.info("morning digest already sent for %s", today)
            return
        # Marker first: at-most-once beats at-least-twice for a digest.
        self._mark_ran("morning_digest", today)

        pnl = self.finance.pnl_summary(hours=24)
        fleet = self.db.read("select * from engine.vw_agent_fleet_health order by handle")
        queue = self.finance.pending(limit=10)
        stale_after = self.settings.heartbeat_stale_seconds

        lines = [
            f"<b>Ghost X2 daily digest</b>  {today}",
            f"<i>{'DRY RUN, no live money' if pnl['dry_run'] else 'LIVE'}</i>",
            "",
            f"In 24h:  {fmt_minor(pnl['in_minor'])}",
            f"Out 24h:  {fmt_minor(pnl['out_minor'])}",
            f"Net 24h:  {fmt_minor(pnl['net_minor'])}"
            + ("  ⚠️ negative" if pnl["net_minor"] < 0 else ""),
            f"LLM spend: ${pnl['llm_cost_usd']:.2f} of "
            f"${self.settings.llm_daily_cap_usd:.2f} cap",
            f"Disputes:  {pnl['disputes']}",
            "",
            "<b>Fleet</b>",
        ]
        for a in fleet:
            seconds = a.get("seconds_since_beat")
            if a["desired_state"] == "disabled":
                icon, note = "⚪", "disabled"
            elif seconds is None or int(seconds) > stale_after:
                icon, note = "🔴", f"no heartbeat for {_human_age(seconds)}"
            elif a.get("llm_cap_breached"):
                icon, note = "🟡", "paused by budget"
            else:
                icon, note = "🟢", a["status"]
            lines.append(f"{icon} {a['handle']}: {note}")

        if queue:
            lines += ["", f"<b>{len(queue)} approval(s) waiting</b>"]
            for q in queue[:5]:
                lines.append(
                    f"• {q['title']}  {fmt_minor(int(q['cost_minor']), q['currency'])}  "
                    f"[{q['tier']}]")
            if len(queue) > 5:
                lines.append(f"…and {len(queue) - 5} more")
        else:
            lines += ["", "No approvals waiting."]

        sender = self._sender("digest")
        result = sender_cfg = None
        try:
            import asyncio

            result = asyncio.run(sender.send("\n".join(lines), disable_preview=True))
        except Exception as exc:
            self.audit.error("jobs.digest_failed", f"could not send digest: {exc}")
        finally:
            sender_cfg = sender.configured
        if result is not None and not result.ok and sender_cfg:
            self.audit.error("jobs.digest_delivery", f"digest delivery failed: {result.error}")

    def job_weekly_sourcing(self) -> None:
        """Trigger a sourcing sweep for any hustle whose worker is not running.

        No-op when the loop is alive: the loop is the real engine, and running
        both would source the same target twice within a tick.
        """
        rows = self.db.read(
            """
            select handle, desired_state from engine.agents
             where role = 'worker' and desired_state = 'running'
            """)
        running = {r["handle"] for r in rows}
        sweeps = {
            "worker:funnel": "funnel_offer_sweep",
            "worker:arbitrage": "arbitrage_source_sweep",
            "worker:whitelabel": "whitelabel_prospect_sweep",
        }
        skipped = [h for h, _job in sweeps.items() if h in running]
        triggered = [name for h, name in sweeps.items() if h not in running]
        for name in triggered:
            self.audit.info("jobs.sweep_queued",
                            f"{name} queued by schedule; its worker loop is not running",
                            sweep=name)
        log.info("weekly sourcing: %d sweep(s) queued, %d worker loop(s) already running",
                 len(triggered), len(skipped))

    def job_budget_reset_notice(self) -> None:
        """Tell the owner when yesterday ended over budget, before today's spend."""
        rows = self.db.read(
            """
            select a.handle, d.day, d.cost_usd, d.cap_usd
              from engine.agent_spend_daily d
              join engine.agents a on a.id = d.agent_id
             where d.breach_at is not null
               and d.day >= (now() at time zone 'UTC')::date - 1
             order by d.day desc
             limit 20
            """)
        if not rows:
            return
        lines = ["<b>Yesterday's LLM budget breaches</b>"]
        for r in rows:
            lines.append(f"• {r['handle']}: ${float(r['cost_usd']):.2f} against a "
                         f"${float(r['cap_usd'] or 0):.2f} cap on {r['day']}")
        sender = self._sender("digest")
        if sender.configured:
            import asyncio

            asyncio.run(sender.send("\n".join(lines)))

    # -------------------------------------------------------------- schedule
    def build_scheduler(self) -> BlockingScheduler:
        sched = BlockingScheduler(timezone=str(self.cfg.timezone))
        digest_cron = self._clean_cron("DIGEST_CRON", "0 8 * * *")

        # Five-field cron from env, with a guarded fallback: a typo'd expression
        # must not stop the whole scheduler, since that would silently stop the
        # morning digest too.
        sched.add_job(self.job_daily_rollup, CronTrigger(hour="0", minute="15"),
                      id="rollup", misfire_grace_time=3600, coalesce=True)
        sched.add_job(self.job_expire_approvals, "interval", minutes=10,
                      id="expire", misfire_grace_time=300, coalesce=True)
        sched.add_job(self.job_morning_digest, CronTrigger.from_crontab(digest_cron),
                      id="digest", misfire_grace_time=3600, coalesce=True)
        sched.add_job(self.job_weekly_sourcing,
                      CronTrigger.from_crontab(self._clean_cron("SOURCING_CRON", "0 6 * * 1")),
                      id="sourcing", misfire_grace_time=3600, coalesce=True)
        sched.add_job(self.job_budget_reset_notice, CronTrigger(hour="7", minute="55"),
                      id="budget_notice", misfire_grace_time=3600, coalesce=True)

        log.info("scheduler armed: rollup 00:15, expiry every 10m, digest '%s', "
                 "sourcing '%s'", digest_cron, self._clean_cron("SOURCING_CRON", "0 6 * * 1"))
        return sched

    def _clean_cron(self, name: str, default: str) -> str:
        import os

        raw = (os.getenv(name) or "").strip()
        if raw.lower() in {"", "null", "none", '""', "undefined"}:
            return default
        try:
            CronTrigger.from_crontab(raw)
        except (ValueError, KeyError):
            log.error("%s=%r is not a valid crontab; using %s", name, raw, default)
            return default
        return raw

    def run_forever(self) -> None:
        import os

        # The API process owns the scheduler so there is exactly one of each job.
        # A supervisor container with ENABLE_SCHEDULER=false just runs the tick.
        if os.getenv("ENABLE_SCHEDULER", "true").strip().lower() in {"0", "false", "no"}:
            log.info("scheduler disabled by ENABLE_SCHEDULER; running ticks only")
            return
        self._scheduler = self.build_scheduler()
        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass

    def run_once(self, name: str) -> dict[str, Any]:
        """Run one job by name: for `engine jobs run digest` and for testing."""
        jobs = {
            "digest": self.job_morning_digest,
            "rollup": self.job_daily_rollup,
            "expire": self.job_expire_approvals,
            "sourcing": self.job_weekly_sourcing,
            "budget": self.job_budget_reset_notice,
        }
        if name not in jobs:
            raise SystemExit(f"unknown job {name!r}; choose from {', '.join(sorted(jobs))}")
        # Force-send the digest when asked explicitly, ignoring the marker.
        if name == "digest":
            today = datetime.now(tz=self.cfg.timezone).date().isoformat()
            self.db.execute("delete from engine.settings where key = %s",
                            (f"jobs.last_run.morning_digest.{today}",))
        jobs[name]()
        return {"job": name, "ran": True}

    def close(self) -> None:
        import asyncio

        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        for sender in (self.digest, self.approvals):
            try:
                asyncio.run(sender.aclose())
            except RuntimeError:
                pass
        self.db.close()


def _human_age(seconds: int | None) -> str:
    if seconds is None:
        return "ever"
    if seconds < 120:
        return f"{int(seconds)}s"
    if seconds < 7200:
        return f"{int(seconds) // 60}m"
    return f"{int(seconds) // 3600}h"
