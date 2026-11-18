"""Idempotent seeds: agent registry, runtime settings, demo fixtures.

Runs after every migration and on every container start, so the engine can
never boot into a database with no agents registered (which would make
`request_spend` refuse everything with 'unknown agent').

Seeding rules:
* `settings` uses ON CONFLICT DO NOTHING. A value someone changed in production
  is never reverted by a deploy.
* `agents` upserts only its structural columns (display name, role, tick cadence,
  container) and never touches `desired_state`. Deploying must not silently
  restart a worker an operator paused on purpose.
"""

from __future__ import annotations

import logging
from typing import Any

from .db import Database

log = logging.getLogger("engine.seeds")

# handle, display_name, role, hustle, container, tick_seconds, llm_budget_usd_day
AGENTS: list[tuple[str, str, str, str | None, str, int, float]] = [
    ("supervisor", "Supervisor", "supervisor", None, "supervisor", 20, 0.50),
    ("finance", "Finance Gatekeeper", "finance", None, "finance-gatekeeper", 30, 0.50),
    ("telegram", "Telegram Interface", "interface", None, "telegram-bot", 15, 0.25),
    ("marketing", "Marketing Agent", "marketing", None, "worker-funnel", 300, 2.00),
    ("worker:funnel", "Funnel Worker (Hustle A)", "worker", "funnel", "worker-funnel", 60, 1.50),
    ("worker:arbitrage", "Arbitrage Worker (Hustle B)", "worker", "arbitrage",
     "worker-arbitrage", 60, 1.50),
    ("worker:whitelabel", "White-Label Worker (Hustle C)", "worker", "whitelabel",
     "worker-whitelabel", 120, 1.50),
]

# key, value, description
SETTINGS: list[tuple[str, Any, str]] = [
    ("engine.dry_run", True,
     "Master kill switch for every external effect. Read live by every gate. "
     "Set false only after the approval path has been proven."),
    ("engine.runtime", "container", "Runtime this engine believes it runs in: container | local."),
    ("owner.timezone", "Africa/Lagos", "Timezone for digests and daily boundaries."),
    ("llm.daily_cap_usd", 8.0,
     "Engine-wide LLM spend ceiling per day in USD. Enforced fail-closed."),
    ("approval.tier1_max_minor", 5000,
     "Costs at or below this (cents) execute autonomously. $50."),
    ("approval.tier2_max_minor", 25000,
     "Costs at or below this (cents) need a Telegram/Web UI tap. Above it: tier3 hard block."),
    ("approval.expiry_hours", 24,
     "Pending approvals older than this auto-reject, so a stale yes cannot fund "
     "yesterday's opportunity."),
    ("heartbeat.stale_seconds", 180,
     "No heartbeat for this long means the agent is treated as dead."),
    ("supervisor.tick_seconds", 20, "Supervisor loop cadence."),
    ("card.daily_cap_minor", 5000, "Engine's own per-agent daily spend envelope (cents)."),
    ("card.monthly_cap_minor", 50000, "Engine's own per-agent monthly envelope (cents)."),
    ("alert.consecutive_error_ticks", 3, "Consecutive failing ticks before a page."),
    ("dashboard.allowed_emails", [],
     "Emails allowed to read engine tables through a Supabase JWT. Empty means "
     "only the service roles can read anything."),
    ("webhook.accept_unsigned", True,
     "Dev convenience: accept unsigned incoming webhooks. Set false before live money."),
    ("funnel.auto_refund_dispute_minor", 10000,
     "Auto-refund a dispute at or below this amount (cents) to protect the "
     "merchant account from a chargeback ratio penalty."),
]

# Demo fixtures: three candidates at different stages, so the dashboard renders
# real numbers on first boot instead of a blank grid. Skipped once any real
# target exists, so they cannot masquerade as live pipeline data.
DEMO_TARGETS = [
    ("arbitrage", "appsumo", "demo-appsumo-stack",
     "Demo: AppSumo tier stack, lifetime deal", "discovered", 4900, 8900, 3),
    ("arbitrage", "ticketmaster", "demo-tm-resale",
     "Demo: resale seat with verified floor", "validating", 12000, 17500, 4),
    ("funnel", "internal", "demo-trial-offer",
     "Demo: 7-day trial micro-offer", "approved", 1500, None, 2),
]


def apply_seeds(db: Database) -> None:
    seed_settings(db)
    seed_agents(db)
    seed_demo_targets(db)


def seed_settings(db: Database) -> None:
    for key, value, description in SETTINGS:
        db.execute(
            """
            insert into engine.settings (key, value, description, updated_by)
            values (%s, to_jsonb(%s::jsonb), %s, 'seed')
            on conflict (key) do nothing
            """,
            (key, _json_literal(value), description),
        )


def seed_agents(db: Database) -> None:
    for handle, display_name, role, hustle, container, tick, budget in AGENTS:
        db.execute(
            """
            insert into engine.agents
              (handle, display_name, role, hustle, container, tick_seconds, llm_budget_usd_day)
            values (%s, %s, %s, %s::engine.hustle_kind, %s, %s, %s)
            on conflict (handle) do update
              set display_name = excluded.display_name,
                  role         = excluded.role,
                  hustle       = excluded.hustle,
                  container    = excluded.container,
                  tick_seconds = excluded.tick_seconds,
                  updated_at   = now()
            """,
            (handle, display_name, role, hustle, container, tick, budget),
        )


def seed_demo_targets(db: Database) -> None:
    """Insert demo pipeline rows only if the pipeline is completely empty."""
    count = db.scalar(
        "select count(*) from engine.arbitrage_targets", default=0)
    if int(count or 0) > 0:
        return
    for hustle, source, ref, title, status, cost, floor, evidence in DEMO_TARGETS:
        margin = (
            round((floor - cost) / cost * 100, 2)
            if (cost and floor and cost > 0) else None
        )
        db.execute(
            """
            insert into engine.arbitrage_targets
              (hustle, source, external_ref, title, status,
               acquisition_cost_minor, resale_floor_minor, expected_margin_pct,
               evidence_count, scoring_rationale)
            values (%s::engine.hustle_kind, %s, %s, %s, %s::engine.target_status,
                    %s, %s, %s, %s, %s)
            on conflict (hustle, source, external_ref) do nothing
            """,
            (hustle, source, ref, title, status, cost, floor, margin, evidence,
             "demo fixture seeded on first boot; delete once real sourcing runs"),
        )
    log.info("seeded %d demo arbitrage targets", len(DEMO_TARGETS))


def _json_literal(value: Any) -> str:
    import json

    return json.dumps(value)
