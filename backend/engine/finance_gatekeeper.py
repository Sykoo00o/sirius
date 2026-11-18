"""Finance gatekeeper: the only door through which money leaves.

Architecture doc section 3 says "never give an AI unconstrained card access".
This module is where that sentence becomes code:

  tier1  < $50       auto-approved, executed, and posted to the ledger
  tier2  $50-$250    queued as `pending`; nothing executes until a human taps
  tier3  > $250      `blocked`; a tap cannot unlock it, only a manual run

The tier decision is made once, inside Postgres (`request_approval`), from the
`settings` table, so the Python side and any future Node side cannot disagree
about where $50 falls. Callers get a decision, never a choice.

Dry-run semantics: the gate is evaluated identically when dry-running. The
difference is only whether `execute` is True in the returned intent. That way a
dry run exercises the real branch and proves the rails would have fired.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .config import Config, get_config
from .db import Database

log = logging.getLogger("engine.finance")

CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "NGN": "₦"}


def fmt_minor(amount: int, currency: str = "USD") -> str:
    """Render cents as '$12.34'. Every money string in this system goes through here."""
    symbol = CURRENCY_SYMBOL.get(currency, currency + " ")
    sign = "-" if amount < 0 else ""
    return f"{sign}{symbol}{abs(int(amount)) / 100:,.2f}"


@dataclass
class SpendIntent:
    """What the gatekeeper hands back: whether to proceed, and what to execute."""

    approval_id: str
    tier: str
    state: str
    execute: bool
    reason: str
    cost_minor: int
    currency: str
    dry_run: bool

    @property
    def needs_human(self) -> bool:
        return self.state == "pending"

    @property
    def blocked(self) -> bool:
        return self.state in ("blocked", "rejected", "expired")

    def describe(self) -> str:
        mode = "dry-run" if self.dry_run else "LIVE"
        return (f"{self.tier}/{self.state} {fmt_minor(self.cost_minor, self.currency)} "
                f"execute={self.execute} [{mode}] ({self.reason})")


class FinanceGatekeeper:
    """Money gatekeeping. Sync core, async facade.

    Every method here hits Postgres, and this deployment talks to Supabase across
    regions: measured 127 ms per round-trip (median of 12) and ~940 ms to open a
    connection. That is far too long to occupy an event loop, so anything running
    under asyncio must call the `a`-prefixed wrappers below, which hand the same
    blocking code to a worker thread.

    Why a facade instead of making the class async: the CLI and the scheduled jobs
    run synchronously and gain nothing from awaiting, and the FastAPI endpoints are
    plain `def`, which Starlette already dispatches to its threadpool (capacity 40,
    so 20 concurrent round-trips finish in one 127 ms wave instead of 2.5 s). The
    only callers that truly need this are the supervisor loop and the workers,
    whose `async def` bodies share a loop with everything else in the process.
    """

    def __init__(self, db: Database, cfg: Config | None = None, audit: Any = None) -> None:
        self.db = db
        self.cfg = cfg or get_config()
        self.audit = audit

    # ------------------------------------------------------------ async facade
    async def apreflight(self, agent_handle: str, title: str, cost_minor: int,
                         **kw: Any) -> SpendIntent:
        return await asyncio.to_thread(self.preflight, agent_handle, title, cost_minor, **kw)

    async def aexpire_stale(self) -> list[str]:
        """One SELECT plus one UPDATE per stale approval: N+1 round-trips, which is
        exactly why this path especially must not run on the loop."""
        return await asyncio.to_thread(self.expire_stale)

    async def adecide(self, approval_id: str, decision: str, actor: str,
                      note: str | None = None) -> str:
        return await asyncio.to_thread(self.decide, approval_id, decision, actor, note)

    async def arecord_result(self, intent: SpendIntent, **kw: Any) -> int | None:
        return await asyncio.to_thread(self.record_result, intent, **kw)

    async def apending(self, limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.pending, limit)

    async def acard_envelope(self, agent_handle: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.card_envelope, agent_handle)

    async def apnl_summary(self, hours: int = 24) -> dict[str, Any]:
        return await asyncio.to_thread(self.pnl_summary, hours)

    # --------------------------------------------------------------- settings
    def tier_limits(self) -> tuple[int, int]:
        tier1 = int(self.db.setting("approval.tier1_max_minor", self.cfg.tier1_max_minor))
        tier2 = int(self.db.setting("approval.tier2_max_minor", self.cfg.tier2_max_minor))
        if tier1 >= tier2:
            # A bad DB edit could invert the gates and auto-approve everything.
            log.error("tier limits inverted (%s >= %s); using env defaults", tier1, tier2)
            return self.cfg.tier1_max_minor, self.cfg.tier2_max_minor
        return tier1, tier2

    def classify(self, cost_minor: int) -> str:
        tier1, tier2 = self.tier_limits()
        if cost_minor <= tier1:
            return "tier1"
        if cost_minor <= tier2:
            return "tier2"
        return "tier3"

    # ------------------------------------------------------------ the request
    def request_spend(
        self,
        agent_handle: str,
        title: str,
        cost_minor: int,
        *,
        currency: str | None = None,
        rationale: str = "",
        source_url: str | None = None,
        expected_roi: str | None = None,
        hustle: str | None = None,
        vendor: str | None = None,
        idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> SpendIntent:
        """Ask to spend. Returns a decision; never raises on 'not allowed'.

        A refusal is a normal outcome, so it is data, not an exception. Only
        real faults (unknown agent, DB down) propagate.
        """
        if cost_minor < 0:
            raise ValueError("cost_minor is a magnitude; pass a positive number")
        currency = (currency or self.cfg.default_currency).upper()

        # Refuse before touching Postgres if this agent is not registered: an
        # unregistered agent has no budget and no audit trail.
        known = self.db.scalar(
            "select handle from engine.agents where handle = %s", (agent_handle,))
        if not known:
            raise LookupError(
                f"agent {agent_handle!r} is not registered; refusing spend from an unknown agent"
            )

        row = self.db.call(
            "request_approval",
            agent_handle, title, cost_minor, currency, rationale, source_url,
            expected_roi, hustle, vendor, idempotency_key,
            json.dumps(context or {}, default=str),
        )
        if isinstance(row, dict):
            approval_id = str(row["approval_id"])
            state = row["state"]
            tier = row["tier"]
        else:
            raise RuntimeError(f"request_approval returned unexpected shape: {row!r}")

        dry_run = self.db.is_dry_run()
        execute = state in ("approved", "auto_approved") and not dry_run
        reason = {
            "auto_approved": "under tier1 cap",
            "pending": "tier2: waiting for a human tap",
            "blocked": "tier3: over the cap, manual execution required",
            "approved": "already approved by a human",
            "rejected": "a human rejected this",
            "expired": "the approval window closed",
        }.get(state, state)

        intent = SpendIntent(
            approval_id=approval_id, tier=tier, state=state, execute=execute,
            reason=reason, cost_minor=cost_minor, currency=currency, dry_run=dry_run,
        )
        log.info("gate %s: %s", agent_handle, intent.describe())
        if self.audit:
            self.audit.spend(
                f"{agent_handle} wants {fmt_minor(cost_minor, currency)} for {title}",
                approval_id=approval_id, tier=tier, state=state,
                execute=execute, vendor=vendor, idempotency_key=idempotency_key,
            )
        return intent

    # ------------------------------------------------------------- execution
    def record_result(
        self,
        intent: SpendIntent,
        *,
        ok: bool,
        description: str | None = None,
        external_provider: str = "manual",
        external_id: str | None = None,
        entry_type: str = "charge",
        metadata: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> int | None:
        """Post the outcome of an executed intent to the ledger.

        Dry-run spends are logged as an audit event, NOT as ledger entries: a
        simulation must not pollute a balance that an accountant will later read.
        """
        if intent.dry_run:
            if self.audit:
                self.audit.spend(
                    f"DRY RUN: would have posted {entry_type} "
                    f"{fmt_minor(intent.cost_minor, intent.currency)} ({description or ''})",
                    approval_id=intent.approval_id, ok=ok,
                )
            return None

        if not ok:
            if self.audit:
                self.audit.error("spend.failed",
                                 f"execution failed for {fmt_minor(intent.cost_minor, intent.currency)}",
                                 approval_id=intent.approval_id)
            return None

        entry_id = self.db.call(
            "post_ledger_entry",
            entry_type, intent.cost_minor,
            description or f"agent spend via approval {intent.approval_id}",
            intent.currency, "operating", None, None, intent.approval_id,
            external_provider, external_id,
            dedupe_key or f"approval:{intent.approval_id}",
            json.dumps(metadata or {}, default=str),
        )
        if self.audit and entry_id:
            self.audit.spend(
                f"posted {entry_type} {fmt_minor(intent.cost_minor, intent.currency)}",
                ledger_id=entry_id, approval_id=intent.approval_id,
                external_id=external_id,
            )
        return int(entry_id) if entry_id else None

    # ---------------------------------------------------------- daily envelope
    def card_envelope(self, agent_handle: str) -> dict[str, Any]:
        """Remaining spend room today, from the ledger, not from a card API.

        The card issuer is authoritative on what a card actually allows; this is
        the engine's own budget, which is stricter by design. Checking locally
        first means a runaway loop stops without spending anything, rather than
        discovering the cap when the issuer declines.
        """
        daily_cap = int(self.db.setting("card.daily_cap_minor", self.cfg.card_daily_cap_minor))
        monthly_cap = int(self.db.setting(
            "card.monthly_cap_minor", self.cfg.card_monthly_cap_minor))
        rows = self.db.read(
            """
            select
              coalesce(-sum(amount_minor) filter (
                where occurred_at >= date_trunc('day', now())), 0)::bigint as today_minor,
              coalesce(-sum(amount_minor) filter (
                where occurred_at >= date_trunc('month', now())), 0)::bigint as month_minor
              from engine.ledger_entries
             where amount_minor < 0
               and reversed_by is null
               and agent_id = (select id from engine.agents where handle = %s)
            """,
            (agent_handle,),
        )
        row = rows[0] if rows else {"today_minor": 0, "month_minor": 0}
        today = int(row["today_minor"] or 0)
        month = int(row["month_minor"] or 0)
        remaining = max(0, min(daily_cap - today, monthly_cap - month))
        return {
            "agent": agent_handle,
            "today_minor": today,
            "month_minor": month,
            "daily_cap_minor": daily_cap,
            "monthly_cap_minor": monthly_cap,
            "remaining_today_minor": max(0, daily_cap - today),
            "remaining_this_request_minor": remaining,
            "exhausted": remaining <= 0,
        }

    def can_afford(self, agent_handle: str, cost_minor: int) -> tuple[bool, str]:
        env = self.card_envelope(agent_handle)
        if env["exhausted"]:
            return False, (
                f"{agent_handle} has spent {fmt_minor(env['today_minor'])} today against a "
                f"{fmt_minor(env['daily_cap_minor'])} cap"
            )
        if cost_minor > env["remaining_this_request_minor"]:
            return False, (
                f"{fmt_minor(cost_minor)} exceeds the remaining "
                f"{fmt_minor(env['remaining_this_request_minor'])} envelope"
            )
        return True, ""

    def preflight(self, agent_handle: str, title: str, cost_minor: int, **kw: Any) -> SpendIntent:
        """Envelope check, then the tier gate. The envelope runs first so an
        over-budget request is refused without consuming an approval slot."""
        affordable, why = self.can_afford(agent_handle, cost_minor)
        if not affordable:
            tier = self.classify(cost_minor)
            if self.audit:
                self.audit.danger("envelope.exceeded", f"{agent_handle}: {why}")
            return SpendIntent(
                approval_id="", tier=tier, state="blocked", execute=False, reason=why,
                cost_minor=cost_minor, currency=self.cfg.default_currency,
                dry_run=self.db.is_dry_run(),
            )
        return self.request_spend(agent_handle, title, cost_minor, **kw)

    # ------------------------------------------------------------- pending set
    def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.read(
            "select * from engine.vw_approval_queue limit %s", (int(limit),))

    def decide(self, approval_id: str, decision: str, actor: str,
               note: str | None = None) -> str:
        """Approve or reject. Thin wrapper so Telegram and the Web UI share one
        code path, including the tier3 hard block that Postgres enforces."""
        state = self.db.call("decide_approval", approval_id, decision, actor, note)
        if self.audit:
            self.audit.audit(f"approval.{state}",
                             f"{actor} {decision}d approval {approval_id}",
                             approval_id=approval_id, decision=decision)
        return str(state)

    def expire_stale(self) -> list[str]:
        """Auto-reject approvals nobody answered. Called by the supervisor."""
        rows = self.db.read(
            """
            select id::text as id, title, cost_minor from engine.approval_requests
             where state = 'pending' and expires_at is not null and expires_at < now()
            """)
        expired = []
        for r in rows:
            try:
                # Route through decide_approval so the transition is audited the
                # same way a human decision is; the function returns 'expired'.
                self.db.call("decide_approval", r["id"], "reject", "system:expiry",
                             "no decision before the window closed")
                expired.append(str(r["id"]))
            except Exception as exc:
                log.error("could not expire approval %s: %s", r["id"], exc)
        return expired

    # ------------------------------------------------------------------ digest
    def pnl_summary(self, hours: int = 24) -> dict[str, Any]:
        """Money in/out for the digest card. Aggregated in SQL, formatted here."""
        row = self.db.read_one(
            """
            select
              coalesce(sum(amount_minor) filter (where amount_minor > 0), 0)::bigint as in_minor,
              coalesce(-sum(amount_minor) filter (where amount_minor < 0), 0)::bigint as out_minor,
              coalesce(count(*) filter (where entry_type in ('dispute','chargeback')), 0)::int
                as disputes
              from engine.ledger_entries
             where occurred_at > now() - (%s || ' hours')::interval
               and reversed_by is null
            """,
            (str(int(hours)),),
        ) or {}
        in_minor = int(row.get("in_minor") or 0)
        out_minor = int(row.get("out_minor") or 0)
        llm = Decimal(str(self.db.scalar(
            "select coalesce(sum(cost_usd),0) from engine.agent_spend_daily "
            "where day >= (now() at time zone 'UTC')::date - %s",
            (0,), default=0) or 0))
        return {
            "hours": hours,
            "in_minor": in_minor,
            "out_minor": out_minor,
            "net_minor": in_minor - out_minor,
            "disputes": int(row.get("disputes") or 0),
            "llm_cost_usd": float(llm),
            "dry_run": self.db.is_dry_run(),
        }
