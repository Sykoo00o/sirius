# Sirius / Ghost X2: Execution Plan and Operating Workflow

Companion to `multi_agent_engine_master_architecture.md` and `SIRIUS_MASTER_BLUEPRINT.md`.
Derived from the shipped code, not from the design sketch. Read-only analysis; no product code changed.

## 0. Ground Truth (what exists today)

Two stacks live side by side in one `engine` schema. The plan does not assume a merge that has not happened.

| Concern | Live Sirius stack | Module 1 (Ghost X2) stack |
|---|---|---|
| Money state | `engine.transactions` + `engine.financial_ledger` | `engine.approval_requests` + `engine.ledger_entries` |
| Gate | `route_spend()` in `backend/finance_agent.py`, tiers 30/250 USD | `request_approval()` RPC via `backend/engine/finance_gatekeeper.py`, tiers 50/250 USD |
| HITL | `POST /webhooks/telegram` (deployed) + `backend/supervisor.py` PTB loop | REST approvals in `engine/main.py` + `engine/telegram_bot.py` |
| Runtime | systemd `sirius-finance`, VPS port 8000, Cloudflare Tunnel | `docker-compose.yml` on port 3100 behind Caddy |

A third runtime now sits beside those two, deliberately not merged into either: systemd
`funnel-app`, VPS `127.0.0.1:8090`, its own Cloudflare Tunnel hostname, its own venv, and two
SQLite files under `/var/lib/funnel-app`. Deploy artifacts and the runbook are in
`deploy/funnel_app/`. It shares no process, port, or database with the rows above, so a restart
of one cannot blink the others.
| Workers | `backend/workers/{funnel,digital_arbitrage,agency}_worker.py` | `backend/engine/workers/{funnel,arbitrage,whitelabel}_worker.py` |
| LLM | `backend/llm_router.py`, OpenRouter + NIM, fast/reasoning tiers, live | `backend/engine/llm.py` |

Confirmed from source, not assumed:

- `ENGINE_DRY_RUN=true`, and `is_dry_run()` fails closed to `true` on any error (backend/finance_agent.py:352).
- `SUPERVISOR_ENABLED` defaults OFF because two auto-approver loops double-decide (backend/finance_agent.py:255).
- `FINANCE_AGENT_URL` / `FINANCE_AGENT_TOKEN` are read at backend/workers/common.py:964 and are absent from `.env` and `.env.example`. Ungated workers raise rather than spend.
- The arbitrage worker never calls the gate: discovery only (backend/workers/digital_arbitrage_worker.py:10). Checkout is unbuilt.
- Telegram webhook is registered while `supervisor.py` still polls, producing a benign `409 Conflict` traceback.
- No CI. Offline suite at `backend/tests/` (chat + router).
- `hustle_kind` is `(funnel, arbitrage, whitelabel)`. No venture (TrailLock, Cohold, UnitTrail, academy, iOS) has a first-class identity.

## 1. Agent Task Distribution

### 1.1 Ownership map

| Actor | Handle | Owns | Must never touch |
|---|---|---|---|
| Supervisor loop | `supervisor` | Heartbeat reconciliation, desired-state convergence, expiry sweep, error digest | Card charges, vendor APIs, ledger posts |
| Finance gatekeeper | `finance-agent` | Tier classification, card envelope, Stripe intake, ledger posts, Telegram decision writes | Scraping, funnel copy, provisioning |
| Funnel worker (Hustle A) | `worker:funnel` | Trigger sequences, one-click cancellation, sub mirror, dispute auto-refund, MRR | Card issuance, uncapped ad spend |
| Arbitrage scanner (Hustle B) | `worker:arbitrage` | Scraping, floor validation, `arbitrage_targets` rows | Checkout (by design, no gate call) |
| Buyer process (to build) | `worker:buyer` | Claim `approved` targets, card checkout, relist | Discovery, tier policy |
| White-label worker (Hustle C) | `worker:whitelabel` | Prospect intake, provisioning, support routing, renewals | Funnel merchant billing entity |
| Marketing worker | `marketing` | Copy, funnel experiments, ad-spend intents | Executing its own charge |
| Telegram interface | `telegram` | Read/report, owner-only decision relay | Initiating spend |

### 1.2 Collision control (four primitives already shipped)

1. Unique handles. `agents.handle` is `UNIQUE` (supabase/migrations/0001_engine_core.sql:111);
   `request_approval` raises on an unknown handle, so an unregistered agent cannot spend at all.
2. Leases per (handle, job). `WorkerStore.claim()` in backend/workers/common.py:496 is a two-statement
   transaction with `for update skip locked`. A second replica logs `worker.lease.contended` and does
   nothing, which is what makes horizontal scaling safe.
3. Two dedupe nets. `seen()` / `mark_seen()` on an identity hash plus a DB unique constraint
   (`arbitrage_targets (hustle, source, external_ref)`, `transactions_idempotency_uq`).
4. Per-agent envelopes. `check_card_limit()` (backend/finance_agent.py:606) computes today/month from
   the ledger for one `agent_handle` only, so one venture exhausting its cap cannot starve another.

### 1.3 Venture identity (the missing piece)

`hustle_kind` is a three-value enum. Multiple B2B SaaS products cannot be separated in PnL today.
Recommended: keep `hustle_kind` as the model family and add `engine.ventures (slug, display_name,
stripe_account, card_profile, llm_budget_usd_day, status)` plus a nullable `venture_id` FK on
`transactions`, `ledger_entries`, `arbitrage_targets`, `hustle_metrics`, `agent_logs`.
Alternative with zero migration cost: `metadata.venture` plus a expression-index view. Choose the FK
before scaling past two products per family; retrofitting PnL attribution after six ventures is expensive.

### 1.4 Mapping portfolio to lanes

| Venture | Lane (hustle_kind) | Owning worker | Merchant account |
|---|---|---|---|
| Automated AI academy funnels | funnel | `worker:funnel` | Stripe funnel (high-risk) |
| iOS app subscriptions | funnel (App Store, not Stripe) | new `storekit` poller | Apple |
| TrailLock | whitelabel or funnel | `worker:whitelabel` | Stripe B2B |
| Cohold | whitelabel | `worker:whitelabel` | Stripe B2B |
| UnitTrail | parked (see .env:5, dropped) | none | none |
| Ticket/domain bundles | arbitrage | `worker:arbitrage` then `worker:buyer` | card-issued |

Two ventures sharing a lane share a tick cadence, so give each its own `job` name in
`engine.worker_state` (for example `whitelabel:traillock:scan`) rather than a shared one.
