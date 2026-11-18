-- ============================================================================
-- Ghost X2 - supabase/schema.sql : core state for the hub-and-spoke engine
--
-- Scope: the four tables the supervisor, finance gatekeeper and dashboard all
-- read. Tables: agent_logs, financial_ledger, transactions (HITL approval
-- state), hustle_metrics.
--
-- Apply with `supabase db push`, or psql -v ON_ERROR_STOP=1 -1 -f against the
-- project. Idempotent: safe to re-run on a database that already has some or all
-- of it. The file sets `search_path to engine` for its own duration, so DDL
-- lands in `engine`. Names inside the SQL functions and views are additionally
-- qualified: PostgreSQL binds a view or SQL-function body to the objects it named
-- at creation time (recorded in pg_depend), so it keeps resolving to them even
-- when the caller's search_path omits `engine`, and the qualifier mostly makes
-- that binding readable at the source rather than being strictly required.
--
-- ---------------------------------------------------------------------------
-- Relationship to supabase/migrations/0001_engine_core.sql
-- ---------------------------------------------------------------------------
-- That migration is the richer build: its ledger_entries, approval_requests and
-- daily_metrics cover the same ground as three tables here, and it already has
-- a table literally named agent_logs. Both files declare the `engine` schema,
-- so they share one namespace and a name collision aborts the migration run.
-- Decide which is the source of truth before pushing both. Two workable options:
--   1. Treat 0001 as truth and use this file as the minimal bootstrap for a
--      fresh database (drop the overlap: keep only the four tables below, no
--      views, no writers).
--   2. Adopt the names here as canonical, and have 0001 alias the old names
--      (vw_* views over the new tables) so deployed containers keep working.
-- Names are deliberately identical in shape to 0001 - minor-unit bigint money,
-- append-only ledger, enum-driven state - so either choice is a rename, not a
-- rewrite.
--
-- ---------------------------------------------------------------------------
-- Money convention
-- ---------------------------------------------------------------------------
-- Every amount is a BIGINT in MINOR UNITS (cents) paired with an ISO 4217
-- `currency` column. No float ever touches money in this system. Derived
-- ratios use NUMERIC, which is exact for division, never DOUBLE PRECISION.
--
-- Cashflow direction lives in entry_type, never in the sign a caller typed:
--   inflow  = revenue, refund, transfer_in, dispute_reversal, grant
--   outflow = charge, fee, expense, dispute, chargeback, transfer_out
-- `amount_minor` is stored unsigned. The sign is derived, by
-- ledger_sign(entry_type), so two writers cannot disagree about a number by
-- disagreeing about a minus sign.
--
-- ---------------------------------------------------------------------------
-- Audit convention
-- ---------------------------------------------------------------------------
-- financial_ledger is append-only, enforced by trigger. UPDATE and DELETE are
-- refused outright. A posted mistake is corrected with a reversing entry that
-- points back at the original (`reverses`), so the history still explains the
-- mistake to whoever audits it six months later.
-- ============================================================================

create schema if not exists engine;
set search_path to engine;

-- gen_random_uuid() reachability.
--
-- On PG13+ it is core (pg_catalog) and always resolves. Some Supabase setups
-- install an override into schema `extensions`, which the bare `set search_path`
-- above drops, so a column DEFAULT could fail to resolve at INSERT time. We do
-- not blindly `create extension if not exists pgcrypto`: on Supabase it is
-- already installed and owned by a role we are not, and the statement then
-- raises "must be owned by postgres", failing every deploy on a healthy cluster.
do $$
begin
  if to_regprocedure('pg_catalog.gen_random_uuid()') is null then
    begin
      create extension if not exists pgcrypto with schema extensions;
    exception
      -- PL/pgSQL has no comma-separated WHEN list: one handler per condition.
      when insufficient_privilege then null;  -- preinstalled, owned by another role
      when duplicate_object then null;        -- raced a concurrent installer
      when others then
        -- Surface anything else, but never abort over a function that works.
        if to_regprocedure('pg_catalog.gen_random_uuid()') is null then
          raise;
        end if;
    end;
  end if;

  if to_regprocedure('engine.gen_random_uuid()') is null
     and to_regprocedure('pg_catalog.gen_random_uuid()') is null
     and to_regprocedure('extensions.gen_random_uuid()') is null then
    raise exception 'gen_random_uuid() is unreachable under search_path engine; '
                    'grant usage on schema extensions or install pgcrypto into public';
  end if;
end $$;

-- ----------------------------------------------------------------------------
-- updated_at bookkeeping, shared by the mutable tables below
-- ----------------------------------------------------------------------------
create or replace function touch_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- ----------------------------------------------------------------------------
-- Enum types
-- ----------------------------------------------------------------------------
-- PostgreSQL has no `create type ... if not exists`, so a bare CREATE TYPE
-- aborts the whole re-run with "type already exists" even though every label is
-- already present. Each type is therefore guarded. Labels are added to an
-- existing enum with `alter type ... add value`, which cannot run inside a
-- transaction block in older majors and is deliberately not attempted here: if
-- a type exists with a different label set than declared below, that is a schema
-- drift worth failing loudly on, not something to paper over silently.
do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'engine' and t.typname = 'log_level') then
    -- Severity of a structured log line.
    create type log_level as enum ('debug', 'info', 'warning', 'error', 'critical');
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'engine' and t.typname = 'hustle_kind') then
    -- The three business models from the architecture doc. Every metric, ledger
    -- row and log line is attributable to exactly one, or to null when the actor
    -- is shared infrastructure (supervisor, finance gatekeeper).
    create type hustle_kind as enum ('funnel', 'arbitrage', 'whitelabel');
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'engine' and t.typname = 'approval_tier') then
    -- Autonomy tiers from architecture doc section 3, restated in minor units to
    -- match this file's money convention. A bare dollar sign is kept out of
    -- comments anyway: quote state resolves before comment stripping, so the
    -- ordering between the two varies across client-side syntax tools even where
    -- PostgreSQL itself is immune. Cheaper to avoid the character than to audit
    -- which tokenizer needs the exception.
    --   tier1 = under 5000 minor units:         fully autonomous
    --   tier2 = 5000 to 25000 minor units:      needs a human tap, Telegram or Web UI
    --   tier3 = over 25000 minor units:         hard block, manual execution only
    create type approval_tier as enum ('tier1', 'tier2', 'tier3');
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'engine' and t.typname = 'approval_state') then
    -- HITL lifecycle. tier1 requests enter at auto_approved and never occupy the
    -- queue. blocked is the tier3 terminal state: recorded, never executable.
    -- expired is set by the sweep on requests older than their expires_at, so a
    -- stale intent cannot be approved days after the deal behind it vanished.
    create type approval_state as enum (
      'pending', 'approved', 'rejected', 'expired', 'auto_approved', 'blocked'
    );
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'engine' and t.typname = 'ledger_entry_type') then
    -- Direction of cash movement. 'log' is the non-monetary catch-all (e.g. a
    -- stripe webhook received): it carries amount 0 and is excluded from balances.
    create type ledger_entry_type as enum (
      'charge', 'refund', 'dispute', 'dispute_reversal', 'chargeback', 'fee',
      'grant', 'revenue', 'transfer_in', 'transfer_out', 'expense', 'log'
    );
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'engine' and t.typname = 'transaction_source') then
    -- How a transaction entered the system.
    create type transaction_source as enum (
      'stripe_webhook', 'card_issuer', 'agent', 'manual'
    );
    -- stripe_webhook: revenue side, funnel charge / subscription / refund
    -- card_issuer:    spend side, Stripe Issuing or Lithic authorisation
    -- agent:          booked by a worker through the finance gatekeeper
    -- manual:         a human corrected or added it in the dashboard
  end if;
end $$;

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'engine' and t.typname = 'risk_flag') then
    -- Card-network and processor outcomes, tracked because the executive header
    -- monitors the dispute/chargeback ratio as a merchant-account-health signal.
    -- 'none' is the ordinary case, so a ratio query needs no null handling.
    create type risk_flag as enum (
      'none', 'disputed', 'chargeback', 'fraud_flag', 'refund_requested'
    );
  end if;
end $$;

-- ----------------------------------------------------------------------------
-- agent_logs
-- ----------------------------------------------------------------------------
-- The structured event stream the dashboard's "Financial Ledger & Audit Stream"
-- module reads, not a replacement for container stdout (which stays in Docker).
-- `event` is a dotted, stable name ('approval.approved', 'target.validated') so
-- queries filter on a name that survives message rewording; `message` is the
-- human-readable detail. Log volume is why this is the one high-write table:
-- keep it narrow, and let retention prune it.
create table if not exists agent_logs (
  id          bigint generated always as identity primary key,
  -- Actor identity, kept as a plain handle rather than a foreign key: a log
  -- line must land even when the agent registry is down or the writer is a
  -- one-off script, and losing a diagnostic to a constraint is the worst
  -- possible failure for a diagnostics table.
  agent_handle text not null,                    -- 'supervisor' | 'finance' | 'worker:funnel'
  hustle      hustle_kind,
  level       log_level  not null default 'info',
  event       text,                              -- dotted stable name
  message     text       not null,
  -- Correlation: the approval, transaction, target or Stripe event this line
  -- is about, so one incident is one query instead of a fuzzy time-window scan.
  context     jsonb      not null default '{}'::jsonb,
  -- Flagged at write time when the row records a human action or an approval
  -- decision, so the audit stream filters without a join to transactions.
  is_audit    boolean    not null default false,
  created_at  timestamptz not null default now()
);

-- Fleet health grid and per-agent drill-down: newest lines for one agent.
create index if not exists agent_logs_agent_time_idx
  on agent_logs (agent_handle, created_at desc);
-- Errors only. Partial so the routine info-volume does not bloat the index that
-- incident triage depends on.
create index if not exists agent_logs_error_idx
  on agent_logs (created_at desc)
  where level in ('error', 'critical');
-- The audit stream, likewise partial: audit rows are a small minority.
create index if not exists agent_logs_audit_idx
  on agent_logs (created_at desc) where is_audit;
-- Event-name lookups ("show me every approval.approved in June").
create index if not exists agent_logs_event_idx
  on agent_logs (event, created_at desc) where event is not null;

comment on table agent_logs is
  'Structured agent event stream for the dashboard audit feed. Not container stdout.';
comment on column agent_logs.event is
  'Stable dotted name, safe to filter on; message is free-form.';

-- ----------------------------------------------------------------------------
-- financial_ledger
-- ----------------------------------------------------------------------------
-- Append-only double-entry-ish book of record. Balance is a fold over this
-- table, never a mutable "balance" column someone forgets to update. Every real
-- money movement gets exactly one row, keyed by `dedupe_key` so a redelivered
-- Stripe webhook cannot post revenue twice.
create table if not exists financial_ledger (
  id              bigint generated always as identity primary key,
  -- Business time vs. insert time. A webhook replayed hours late must still
  -- land in the fiscal day it belongs to, so reports read occurred_at and
  -- never posted_at.
  occurred_at     timestamptz not null default now(),
  posted_at       timestamptz not null default now(),
  entry_type      ledger_entry_type not null,
  -- Stored UNSIGNED. The sign is derived from entry_type by ledger_sign();
  -- callers never type a minus, so two writers cannot disagree by sign.
  amount_minor    bigint not null check (amount_minor >= 0),
  currency        char(3) not null default 'USD' check (currency ~ '^[A-Z]{3}$'),
  -- Segregation of accounts mirrors the doc's merchant-account isolation: the
  -- high-risk trial funnels never commingle with white-label B2B billing.
  account         text not null default 'operating',
  hustle          hustle_kind,
  -- Weak reference to transactions.transaction_id on purpose (no foreign key).
  -- A provider webhook can post a ledger row before its transaction row is
  -- reconciled, and an immutable ledger must never be rejected by a mutable
  -- table arriving out of order.
  transaction_id  uuid,
  agent_handle    text,
  source          transaction_source not null default 'manual',
  external_provider text,                         -- 'stripe' | 'lithic' | ...
  external_id       text,                         -- provider's event / charge id
  description     text not null,
  metadata        jsonb not null default '{}'::jsonb,
  -- Replay guard, one row per upstream event. Nullable for entries with no
  -- provider event to key on (internal transfers); those rely on the writer.
  dedupe_key      text,
  -- Reversal linkage, both directions, so a correction is self-documenting:
  -- the original shows what undid it, the reversal shows what it undid.
  reversed_by     bigint references financial_ledger(id),
  reverses        bigint references financial_ledger(id),
  created_at      timestamptz not null default now(),
  -- Non-monetary rows are always zero; monetary rows are never zero. Stops a
  -- bug that silently books a no-op, and a genuine zero-amount "revenue" row that
  -- would quietly distort a fold.
  constraint financial_ledger_amount_shape_check check (
    (entry_type = 'log') = (amount_minor = 0)
  )
);

-- Time-series reports and the PnL digest: newest entries in a window.
create index if not exists financial_ledger_time_idx
  on financial_ledger (occurred_at desc);
-- Per-account, per-currency balance folds. Currency is a leading key column
-- because balances never sum across currencies.
create index if not exists financial_ledger_account_idx
  on financial_ledger (account, currency, occurred_at desc);
-- Per-hustle contribution margin. Partial: shared infrastructure has null
-- hustle and does not need to sit in the index.
create index if not exists financial_ledger_hustle_idx
  on financial_ledger (hustle, occurred_at desc) where hustle is not null;
-- Reconciliation joins on the provider id; unique, since one provider event
-- posts one row.
create unique index if not exists financial_ledger_external_uq
  on financial_ledger (external_provider, external_id)
  where external_id is not null;
-- Drift detection: transaction rows and their ledger rows must agree.
create index if not exists financial_ledger_txn_idx
  on financial_ledger (transaction_id) where transaction_id is not null;

comment on table financial_ledger is
  'Append-only book of record. Balances are folds over this table, never stored columns.';
comment on column financial_ledger.amount_minor is
  'Unsigned minor units. Sign is derived from entry_type by ledger_sign().';

-- Append-only enforcement. A mistake is corrected with a reversing entry, which
-- is what an auditor wants to see, rather than an edit that erases the mistake.
create or replace function ledger_reject_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'financial_ledger is append-only (% blocked)', tg_op;
end $$;

drop trigger if exists financial_ledger_no_update on financial_ledger;
create trigger financial_ledger_no_update before update on financial_ledger
  for each row execute function ledger_reject_mutation();
drop trigger if exists financial_ledger_no_delete on financial_ledger;
create trigger financial_ledger_no_delete before delete on financial_ledger
  for each row execute function ledger_reject_mutation();

-- ----------------------------------------------------------------------------
-- transactions
-- ----------------------------------------------------------------------------
-- The HITL gate and the money trail, in one table. One row per intended or
-- observed money movement, carrying the approval decision beside the amount it
-- approved, so "who authorised this vendor charge" is a single row read and not
-- a join across three tables. Status is a superset of the card flow (a Stripe
-- revenue event has no approval to await and sits at recorded).
create table if not exists transactions (
  transaction_id  uuid primary key default gen_random_uuid(),
  source          transaction_source not null,
  -- Spend (an agent wants to buy) or revenue (money arrived from a customer).
  -- Revenue skips the approval gate entirely, hence approval_tier nullable.
  direction       text not null check (direction in ('spend', 'revenue')),
  hustle          hustle_kind,
  agent_handle    text,
  amount_minor    bigint not null check (amount_minor >= 0),
  currency        char(3) not null default 'USD' check (currency ~ '^[A-Z]{3}$'),
  vendor          text,
  title           text not null,
  -- Why the agent wants this, and the projected return shown on the approval
  -- card. Both are rendered in Telegram and the queue, so the human decides
  -- with the same reasoning the agent had.
  rationale       text,
  expected_roi_note text,
  source_url      text,
  -- --- HITL approval state -------------------------------------------------
  tier            approval_tier,
  approval_status approval_state not null default 'pending',
  approved_by     text,                           -- 'telegram:1489...' | 'webui:<email>'
  approved_at     timestamptz,
  approval_note   text,
  expires_at      timestamptz,                    -- null = never auto-expires
  -- Snapshot of what the gate evaluated at request time (caps remaining today,
  -- card limit, live balance). Immutable once decided: the audit trail records
  -- the state of the world the human actually saw, not a recomputed guess.
  gate_context    jsonb not null default '{}'::jsonb,
  -- --- Fulfilment and risk -------------------------------------------------
  fulfillment_status text not null default 'initiated'
    check (fulfillment_status in
      ('initiated','authorized','captured','settled','failed','cancelled')),
  risk_flag       risk_flag not null default 'none',
  external_provider text,
  external_id       text,
  idempotency_key text,                           -- crash-and-retry replay guard
  metadata        jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  -- Only spend needs a tier, and spend must always have one: a spend row with no
  -- tier is a spend that escaped the gate. Revenue rows legitimately have none.
  constraint transactions_tier_direction_check check (
    (direction = 'spend') = (tier is not null)
  ),
  -- An approval decision is meaningless without its actor and timestamp, and
  -- neither may exist without the other.
  constraint transactions_decision_pairing_check check (
    (approved_by is null) = (approved_at is null)
  )
);

-- The queue itself: pending tier-2 intents, oldest first, tiny and hot.
create index if not exists transactions_pending_idx
  on transactions (created_at)
  where approval_status = 'pending';
-- Expiry sweep, which runs on a timer over the pending set only.
create index if not exists transactions_expiry_idx
  on transactions (expires_at)
  where approval_status = 'pending' and expires_at is not null;
-- Per-agent audit and per-agent spend attribution.
create index if not exists transactions_agent_idx
  on transactions (agent_handle, created_at desc) where agent_handle is not null;
-- Per-hustle PnL and the risk monitor (disputes and chargebacks are rare, so
-- filtering them out of the index keeps it small).
create index if not exists transactions_hustle_idx
  on transactions (hustle, created_at desc) where hustle is not null;
create index if not exists transactions_risk_idx
  on transactions (risk_flag, created_at desc) where risk_flag <> 'none';
-- Webhook reconciliation: one provider event maps to one transaction.
create unique index if not exists transactions_external_uq
  on transactions (external_provider, external_id)
  where external_id is not null;
-- Idempotency for agent-initiated intents, where there is no provider id yet.
create unique index if not exists transactions_idempotency_uq
  on transactions (idempotency_key) where idempotency_key is not null;

drop trigger if exists transactions_touch_updated_at on transactions;
create trigger transactions_touch_updated_at before update on transactions
  for each row execute function touch_updated_at();

comment on table transactions is
  'HITL approval gate plus the money trail: one row per intended or observed movement.';
comment on column transactions.gate_context is
  'Immutable snapshot of the gate evaluation the human saw when deciding.';
comment on column transactions.approval_status is
  'HITL state. tier1 auto-approves, tier3 blocks; only tier2 occupies the queue.';

-- ----------------------------------------------------------------------------
-- hustle_metrics
-- ----------------------------------------------------------------------------
-- Daily rollups per hustle, feeding the Executive Metrics Header. The dashboard
-- aggregates live from financial_ledger for "today" and reads this table for
-- history, which keeps the header fast without a full-table fold per page load.
-- Day granularity with no hour: the digest is daily, and a finer grain doubles
-- the row count for a view nobody reads.
create table if not exists hustle_metrics (
  day                  date not null,
  hustle               hustle_kind not null,
  -- Money, minor units, unsigned; direction is already split by column name.
  revenue_minor        bigint not null default 0 check (revenue_minor >= 0),
  cost_minor           bigint not null default 0 check (cost_minor >= 0),
  -- Generated so net profit can never drift from its inputs, no matter which
  -- writer touched the row.
  net_profit_minor     bigint generated always as (revenue_minor - cost_minor) stored,
  -- Subscription signals for the negative-option funnel.
  mrr_minor            bigint not null default 0 check (mrr_minor >= 0),
  active_subscriptions integer not null default 0 check (active_subscriptions >= 0),
  new_subscriptions    integer not null default 0 check (new_subscriptions >= 0),
  cancellations        integer not null default 0 check (cancellations >= 0),
  -- Merchant-account health. Counts and ratio both stored: the counts sum
  -- correctly across days, the ratio is stored per day because averaging daily
  -- ratios is not the same as the ratio of sums.
  gross_volume_minor   bigint not null default 0 check (gross_volume_minor >= 0),
  dispute_count        integer not null default 0 check (dispute_count >= 0),
  chargeback_count     integer not null default 0 check (chargeback_count >= 0),
  -- Exactly four decimals: the merchant-account warning threshold sits near
  -- 1.0%, and rounding to two would hide the approach to it.
  dispute_ratio_pct    numeric(7,4) not null default 0
    check (dispute_ratio_pct >= 0),
  -- Arbitrage funnel throughput: how many sourced deals converted, and what the
  -- realized margin was against what validation predicted. The gap between
  -- realized and expected margin is the signal that the scoring model is wrong.
  targets_discovered   integer not null default 0 check (targets_discovered >= 0),
  targets_executed     integer not null default 0 check (targets_executed >= 0),
  realized_margin_pct  numeric(8,2) check (realized_margin_pct is null or realized_margin_pct >= 0),
  expected_margin_pct  numeric(8,2) check (expected_margin_pct is null or expected_margin_pct >= 0),
  -- Agency-side counts.
  clients_onboarded    integer not null default 0 check (clients_onboarded >= 0),
  support_tickets      integer not null default 0 check (support_tickets >= 0),
  -- Runaway-loop guard: the cost of the model calls that did the work. Kept in
  -- USD decimal, not minor units, because fractional-cent token costs lose
  -- meaning when truncated to cents.
  llm_cost_usd         numeric(12,4) not null default 0 check (llm_cost_usd >= 0),
  currency             char(3) not null default 'USD' check (currency ~ '^[A-Z]{3}$'),
  -- Denormalised for the header, and nullable: a hustle with zero activity has
  -- no ROI to report, and 0 would read as "returns nothing" rather than
  -- "nothing happened yet".
  roi_pct              numeric(8,2),
  -- Which fold produced this row, so a backfill and a nightly rollup are
  -- distinguishable when they disagree.
  computed_at          timestamptz not null default now(),
  primary key (day, hustle)
);

-- The common read is "the last N days, all hustles", which the primary key
-- (day-leading) does not serve efficiently for one hustle's trend line.
create index if not exists hustle_metrics_hustle_day_idx
  on hustle_metrics (hustle, day desc);
-- "Which hustle is bleeding" scans the most recent day across hustles.
create index if not exists hustle_metrics_net_idx
  on hustle_metrics (day desc, net_profit_minor);

comment on table hustle_metrics is
  'Daily per-hustle rollups for the executive header; live figures fold from financial_ledger.';
comment on column hustle_metrics.roi_pct is
  'Null when cost is zero, not 0: no activity is not the same as no return.';
comment on column hustle_metrics.llm_cost_usd is
  'Decimal USD, deliberately not minor units: fractional-cent token costs.';

-- ============================================================================
-- Read helpers
-- ============================================================================
-- The dashboard and the Telegram digest should not each re-implement the sign
-- rule, the dispute ratio, or which entry_types count as inflow. One definition
-- here, and every consumer agrees by construction.

-- Cash direction as a multiplier: 1 inflow, -1 outflow, 0 non-monetary.
create or replace function ledger_sign(p_entry_type ledger_entry_type)
returns integer language sql immutable parallel safe as $$
  select case p_entry_type
    when 'revenue'          then 1
    when 'refund'           then 1   -- money returning to us from a vendor
    when 'transfer_in'      then 1
    when 'dispute_reversal' then 1   -- we won the dispute
    when 'grant'            then 1
    when 'charge'           then -1  -- we paid a vendor or an ad platform
    when 'fee'              then -1
    when 'expense'          then -1
    when 'dispute'          then -1
    when 'chargeback'       then -1
    when 'transfer_out'     then -1
    else 0                           -- 'log': non-monetary
  end;
$$;

-- Qualification here is about making the binding explicit at the source, not
-- about rescuing an unqualified name at call time. This was tested against a
-- real PostgreSQL 16, and the intuition it corrects is worth recording:
--   - `security invoker` governs whose TABLE PERMISSIONS are checked. It does
--     NOT make a stored object re-resolve its names against each caller's
--     search_path. (search_path matters for name resolution in a plpgsql body
--     only when combined with SECURITY DEFINER, which is why 0002 pins
--     `set search_path` there: that guards against a search_path attack on a
--     definer's permissions, a different concern from resolution.)
--   - Both views and SQL-language function bodies are parsed once at creation,
--     and each referenced object is recorded in pg_depend with an AUTO
--     dependency. Execution uses those bound OIDs. Verified empirically: a view
--     and a function defined with an unqualified `financial_ledger` both ran
--     correctly in a session whose search_path was the default "$user", public,
--     and `drop table` refused with "dependency ... view depends on".
-- So an unqualified reference here would work. It stays qualified because the
-- source should say which schema it means, and because `create or replace
-- function` re-parses under whatever search_path the replacing session has,
-- where being explicit removes the one window where that could bind elsewhere.
-- It is cheap insurance and legible, not a correctness requirement.
--
-- Net cash for one account/currency up to a point in time. `p_to` makes an
-- as-of balance queryable for yesterday's digest without a separate rollup.
create or replace function balance_minor(
  p_account text default 'operating', p_currency char(3) default 'USD',
  p_to timestamptz default now()
) returns bigint language sql stable as $$
  select coalesce(sum(engine.ledger_sign(entry_type) * amount_minor), 0)::bigint
  from engine.financial_ledger
  where account = p_account and currency = p_currency and occurred_at <= p_to;
$$;

-- Chargeback + dispute rate over gross volume, the merchant-account health
-- signal. Percentage; returns 0 on a volume-free window rather than null, so a
-- caller comparing against the 1% threshold needs no null branch.
create or replace function dispute_ratio_pct(
  p_from timestamptz, p_to timestamptz, p_hustle hustle_kind default null
) returns numeric language sql stable as $$
  select case when coalesce(sum(amount_minor), 0) = 0 then 0
    else round(
      100.0 * sum(case when entry_type in ('dispute', 'chargeback') then 1 else 0 end)
          / nullif(sum(amount_minor), 0), 4)
  end
  from engine.financial_ledger
  where occurred_at >= p_from and occurred_at < p_to
    and entry_type <> 'log'
    and (p_hustle is null or hustle = p_hustle);
$$;

-- Current spend attributed to an agent, for the gate's daily-card-cap check.
-- Reads transactions (intents that were actually executed), not the ledger, so
-- a rejected approval never consumes cap.
create or replace function agent_spend_today_minor(
  p_agent_handle text, p_currency char(3) default 'USD'
) returns bigint language sql stable as $$
  select coalesce(sum(amount_minor), 0)::bigint
  from engine.transactions
  where agent_handle = p_agent_handle
    and currency = p_currency
    and direction = 'spend'
    and approval_status in ('approved', 'auto_approved')
    and fulfillment_status in ('authorized', 'captured', 'settled')
    and created_at >= date_trunc('day', now());
$$;

-- ----------------------------------------------------------------------------
-- Views for the four dashboard modules in the architecture doc section 4
-- ----------------------------------------------------------------------------

-- Executive Metrics Header: live, whole-book, current instant.
-- Deliberately aggregates the whole book in one pass with no GROUP BY, rather
-- than grouping by currency and cross joining an MRR rollup. An aggregate over
-- zero rows with no GROUP BY still returns exactly one row (the COALESCE turns
-- empty sums into zeros), whereas a grouped CTE over an empty ledger returns no
-- rows at all. Joined together, the earlier grouped-and-cross-joined version
-- vanished entirely on a day with no ledger activity or no active subscriptions,
-- blanking the dashboard header instead of showing zeros. A single flat USD row
-- is also what this header displays; if per-currency breakdowns are ever needed,
-- add them to the conditional sums rather than reintroducing a GROUP BY here.
create or replace view vw_executive_metrics as
select 'USD'::char(3) as currency,
       coalesce(sum(case when occurred_at >= now() - interval '24 hours'
                          and engine.ledger_sign(entry_type) > 0
                         then amount_minor end), 0)::bigint   as revenue_24h_minor,
       coalesce(sum(case when occurred_at >= now() - interval '24 hours'
                          and engine.ledger_sign(entry_type) < 0
                         then amount_minor end), 0)::bigint   as cost_24h_minor,
       coalesce(sum(case when occurred_at >= now() - interval '24 hours'
                         then engine.ledger_sign(entry_type) * amount_minor end), 0)::bigint
                                                              as net_profit_24h_minor,
       coalesce((select sum(mrr_minor) from engine.hustle_metrics
                 where day = (select max(day) from engine.hustle_metrics)), 0)::bigint
                                                              as mrr_minor,
       engine.dispute_ratio_pct(now() - interval '30 days', now())
                                                              as dispute_ratio_pct_30d
from engine.financial_ledger;

-- HITL Approval Queue: pending tier-2 intents, oldest first, with the cap
-- context the approver needs. The gate's own tier and expiry logic is not
-- re-derived in SQL here: it lives in the finance gatekeeper, and this view
-- reflects whichever state that module wrote.
create or replace view vw_approval_queue as
select transaction_id, title, rationale, expected_roi_note, source_url,
       hustle, vendor, agent_handle, tier, amount_minor, currency,
       created_at, expires_at,
       (expires_at is not null and expires_at < now()) as is_expired,
       gate_context
from transactions
where approval_status = 'pending'
order by created_at;

-- Financial Ledger & Audit Stream: ledger joined to the approval that
-- authorised it, newest first. Left join, because most ledger rows (Stripe
-- revenue) have no approval behind them.
create or replace view vw_ledger_audit_stream as
select l.id, l.occurred_at, l.entry_type,
       ledger_sign(l.entry_type) * l.amount_minor as signed_amount_minor,
       l.amount_minor, l.currency, l.account, l.hustle,
       l.description, l.external_provider, l.external_id, l.metadata,
       t.transaction_id, t.approval_status, t.approved_by, t.approved_at, t.tier
from financial_ledger l
left join transactions t on t.transaction_id = l.transaction_id
order by l.occurred_at desc;

-- Per-hustle contribution, latest day first, combining history and the ratio
-- that decides whether a hustle earns its keep.
create or replace view vw_hustle_leaderboard as
select day, hustle, currency, revenue_minor, cost_minor, net_profit_minor,
       mrr_minor, active_subscriptions, cancellations,
       dispute_count, chargeback_count, dispute_ratio_pct,
       llm_cost_usd, roi_pct,
       case when targets_discovered > 0
         then round(100.0 * targets_executed / targets_discovered, 2)
       end as execution_rate_pct
from hustle_metrics
order by day desc, net_profit_minor desc;

-- ============================================================================
-- Row level security
-- ============================================================================
-- The anon and authenticated Postgres roles reach the same tables the Python
-- service does, over PostgREST, with only an API key in front of them. RLS is
-- the backstop that makes a leaked publishable key a read-only problem.
-- Two roles express the split:
--   engine_runtime   the containers: full write, via the finance gatekeeper
--   engine_dashboard the Next.js UI: reads only; approval writes go through the
--                    FastAPI backend, never from the browser
-- Both are NOLOGIN by default; wire them to PostgREST in Supabase's settings.
-- Mirrors the role model in migrations/0002_engine_rls.sql.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'engine_runtime') then
    create role engine_runtime nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'engine_dashboard') then
    create role engine_dashboard nologin;
  end if;
end $$;

-- `force` rather than `enable`: RLS stays on even when a query runs as the table
-- owner, which is the case during a superuser-driven backfill.
alter table agent_logs       force row level security;
alter table financial_ledger force row level security;
alter table transactions     force row level security;
alter table hustle_metrics   force row level security;

drop policy if exists engine_service_all on agent_logs;
create policy engine_service_all on agent_logs
  to engine_runtime using (true) with check (true);
drop policy if exists engine_service_all on financial_ledger;
create policy engine_service_all on financial_ledger
  to engine_runtime using (true) with check (true);
drop policy if exists engine_service_all on transactions;
create policy engine_service_all on transactions
  to engine_runtime using (true) with check (true);
drop policy if exists engine_service_all on hustle_metrics;
create policy engine_service_all on hustle_metrics
  to engine_runtime using (true) with check (true);

drop policy if exists engine_dashboard_read on agent_logs;
create policy engine_dashboard_read on agent_logs
  for select to engine_dashboard using (true);
drop policy if exists engine_dashboard_read on financial_ledger;
create policy engine_dashboard_read on financial_ledger
  for select to engine_dashboard using (true);
drop policy if exists engine_dashboard_read on transactions;
create policy engine_dashboard_read on transactions
  for select to engine_dashboard using (true);
drop policy if exists engine_dashboard_read on hustle_metrics;
create policy engine_dashboard_read on hustle_metrics
  for select to engine_dashboard using (true);

-- ============================================================================
-- Grants
-- ============================================================================
grant usage on schema engine to engine_runtime, engine_dashboard;
grant select, insert, update, delete on all tables   in schema engine to engine_runtime;
grant usage, select                  on all sequences in schema engine to engine_runtime;
grant execute on all functions       in schema engine to engine_runtime;
grant select on all tables           in schema engine to engine_dashboard;
grant execute on all functions       in schema engine to engine_dashboard;

-- Belt over suspenders, on top of the table grants above: the append-only
-- guarantee must not depend on a trigger firing, so UPDATE and DELETE are also
-- unreachable by grant. Both directions, because the ledger's own reversals
-- arrive as fresh INSERTs.
revoke update, delete on financial_ledger from engine_runtime;
revoke update, delete on financial_ledger from engine_dashboard;

-- PostgREST reaches these as `anon` / `authenticated`, not as the roles above.
-- Grant the two API roles the read side plus the pure functions, nothing more:
-- writes belong to the FastAPI backend holding engine_runtime credentials.
grant usage on schema engine to anon, authenticated;
grant select on agent_logs, financial_ledger, transactions, hustle_metrics
  to anon, authenticated;
grant execute on function
  ledger_sign(ledger_entry_type),
  balance_minor(text, char(3), timestamptz),
  dispute_ratio_pct(timestamptz, timestamptz, hustle_kind),
  agent_spend_today_minor(text, char(3))
  to anon, authenticated;
