-- ============================================================================
-- Ghost X2 - Module 1: engine core schema
--
-- Namespace: everything lives in the `engine` schema so this shares the
-- Supabase project with the old ops tables without touching them.
--
-- Money convention: every amount is a BIGINT in MINOR UNITS (cents) plus an
-- ISO 4217 `currency` column. No floats touch money anywhere in this system.
--
-- Ledger convention: `engine.ledger` is append-only. Balance is a fold over
-- it, never a mutable column someone forgets to update. Corrections are
-- reversing entries, so history is auditable after a mistake.
--
-- Cashflow direction is carried by entry_type, never by the sign of a column:
--   inflow  = revenue, refund, grant, transfer_in, dispute_reversal
--   outflow = charge, fee, expense, dispute, chargeback, transfer_out
-- ('charge' is a card charge: money leaving us to a vendor or ad platform.)
-- ============================================================================

create schema if not exists engine;
set search_path to engine;

-- Ensure gen_random_uuid() is reachable under the search_path above.
--
-- On PG13+ it is core (pg_catalog) and always resolves. On some Supabase setups
-- an override lives in schema `extensions`, which our bare `set search_path to
-- engine` would drop, so a column DEFAULT could fail to resolve at INSERT time.
-- We do not blindly `create extension pgcrypto`: on Supabase it is already
-- installed into `extensions` and owned by a role we are not, and
-- `create extension if not exists` then raises "must be owned by postgres",
-- failing every deploy on a cluster that is already fine.
do $$
begin
  if to_regprocedure('pg_catalog.gen_random_uuid()') is null then
    begin
      create extension if not exists pgcrypto with schema extensions;
    exception
      -- PL/pgSQL has no comma-separated WHEN list; one handler per condition.
      when insufficient_privilege then null;  -- preinstalled, owned by a role we are not
      when duplicate_object then null;        -- raced another installer
      when others then
        -- Any other failure (unsupported schema, etc.) is worth surfacing, but
        -- must not abort the whole migration if the core function already works.
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
-- Enums
-- ----------------------------------------------------------------------------
create type agent_status as enum ('running', 'paused', 'error', 'disabled');

-- lifecycle: discovered -> validating -> approved -> executing -> completed
--            any state  -> rejected / expired / failed
create type target_status as enum (
  'discovered', 'validating', 'approved', 'executing', 'completed',
  'rejected', 'expired', 'failed'
);

-- Autonomy tiers from architecture doc section 3.
-- tier1 = under $50, autonomous. tier2 = $50-$250, needs a human tap.
-- tier3 = over $250, hard block, manual execution only.
create type approval_tier as enum ('tier1', 'tier2', 'tier3');
create type approval_state as enum (
  'pending', 'approved', 'rejected', 'expired', 'auto_approved', 'blocked'
);

-- Direction of cash movement. Anything not listed goes to `log`, which is
-- non-monetary (e.g. "stripe webhook received") and excluded from balances.
create type ledger_entry_type as enum (
  'charge', 'refund', 'dispute', 'dispute_reversal', 'chargeback', 'fee',
  'grant', 'revenue', 'transfer_in', 'transfer_out', 'expense'
);

create type log_level as enum ('debug', 'info', 'warning', 'error', 'critical');

create type hustle_kind as enum ('funnel', 'arbitrage', 'whitelabel');

-- ----------------------------------------------------------------------------
-- Settings: key/value runtime switches, read live (no redeploy to flip)
-- ----------------------------------------------------------------------------
-- IF NOT EXISTS because the migrator's ensure_table() pre-creates this exact
-- table (so seeds and settings writes can run before migration 0002 installs
-- RLS). Plain CREATE would abort the very first migration on every fresh boot.
create table if not exists settings (
  key         text primary key,
  value       jsonb   not null,
  description text,
  updated_at  timestamptz not null default now(),
  updated_by  text
);

alter table settings enable row level security;
comment on table settings is 'Runtime switches. Read live via get_setting().';

-- ----------------------------------------------------------------------------
-- Agent registry and health
-- ----------------------------------------------------------------------------
create table agents (
  id                uuid primary key default gen_random_uuid(),
  handle            text not null unique,          -- 'supervisor', 'finance', 'worker:funnel'
  display_name      text not null,
  role              text not null,                 -- supervisor | finance | marketing | worker
  hustle            hustle_kind,                   -- null for non-worker agents
  status            agent_status not null default 'disabled',
  -- Desired state, set by a human. The supervisor converges reality to this.
  desired_state     agent_status not null default 'disabled',
  container         text,                          -- docker service name
  tick_seconds      integer not null default 60 check (tick_seconds between 5 and 3600),
  -- Cost accounting per agent, so a runaway loop is attributable.
  llm_budget_usd_day numeric(10,2) not null default 0,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

alter table agents enable row level security;

-- Heartbeats are append-only: the fleet grid reads the newest per agent, and
-- the supervisor keeps the full series for incident forensics.
create table agent_heartbeats (
  id              bigint generated always as identity primary key,
  agent_id        uuid not null references agents(id) on delete cascade,
  seq             bigint not null,                 -- monotonic per agent, catches gaps
  status          agent_status not null,
  message         text,                            -- short 'why', e.g. 'poll 404s from ticketmaster'
  latency_ms      integer check (latency_ms is null or latency_ms >= 0),
  -- Cheap cost probe: what this agent spent since the last beat.
  spend_delta_minor bigint not null default 0 check (spend_delta_minor >= 0),
  tokens_delta    bigint not null default 0 check (tokens_delta >= 0),
  details         jsonb not null default '{}'::jsonb,
  reported_at     timestamptz not null default now()
);

create index heartbeats_agent_time_idx on agent_heartbeats (agent_id, reported_at desc);
alter table agent_heartbeats enable row level security;

-- Per-agent spend window rollup, maintained incrementally by trigger. The
-- cap check reads one row instead of aggregating the log stream on every tick.
create table agent_spend_daily (
  agent_id     uuid not null references agents(id) on delete cascade,
  day          date not null,
  cost_usd     numeric(12,4) not null default 0,
  tokens_total bigint not null default 0,
  calls        integer not null default 0,
  cap_usd      numeric(10,2) not null default 0,
  breach_at    timestamptz,                         -- when the cap was first hit
  primary key (agent_id, day)
);

alter table agent_spend_daily enable row level security;

-- ----------------------------------------------------------------------------
-- HITL approval queue
-- ----------------------------------------------------------------------------
create table approval_requests (
  id                uuid primary key default gen_random_uuid(),
  requested_by      uuid not null references agents(id),
  title             text not null,
  rationale         text not null,                 -- why the agent wants this
  expected_roi_note text,                          -- shown on the approval card
  source_url        text,
  hustle            hustle_kind,
  vendor            text,
  cost_minor        bigint not null check (cost_minor >= 0),
  currency          char(3) not null default 'USD',
  tier            approval_tier not null,
  state           approval_state not null default 'pending',
  idempotency_key text unique,                     -- replay guard: worker crash + retry
  decided_by      text,                            -- 'telegram:1489...' | 'webui:<email>'
  decided_at      timestamptz,
  decision_note   text,
  expires_at      timestamptz,                     -- null = never auto-expires
  -- Snapshot of what the gate evaluated at request time, for the audit trail.
  gate_context    jsonb not null default '{}'::jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index approval_pending_idx on approval_requests (state, created_at)
  where state = 'pending';
create index approval_agent_idx on approval_requests (requested_by, created_at desc);
alter table approval_requests enable row level security;

-- Every transition, appended. Who moved what, from which state to which.
create table approval_events (
  id          bigint generated always as identity primary key,
  approval_id uuid not null references approval_requests(id) on delete cascade,
  from_state  approval_state,
  to_state    approval_state not null,
  actor       text not null,                       -- 'system' | 'telegram' | 'webui'
  note        text,
  occurred_at timestamptz not null default now()
);

create index approval_events_idx on approval_events (approval_id, id);
alter table approval_events enable row level security;

-- ----------------------------------------------------------------------------
-- Financial ledger (append-only)
-- ----------------------------------------------------------------------------
create table ledger_entries (
  id            bigint generated always as identity primary key,
  occurred_at   timestamptz not null default now(),
  posted_at     timestamptz not null default now(),
  entry_type    ledger_entry_type not null,
  -- Signed: positive is money in, negative is money out. The sign is derived
  -- from entry_type by the writer helper, not typed by hand.
  amount_minor  bigint not null,
  currency      char(3) not null default 'USD',
  account       text not null default 'operating', -- operating | reserve | per-hustle
  hustle        hustle_kind,
  agent_id      uuid references agents(id),
  approval_id   uuid references approval_requests(id),
  -- External references: Stripe event/payment/card charge, Lithic txn, etc.
  external_provider text,
  external_id     text,
  description   text not null,
  metadata      jsonb not null default '{}'::jsonb,
  -- One entry per external event, so a redelivered webhook cannot double-post.
  dedupe_key    text unique,
  reversed_by   bigint references ledger_entries(id),
  reverses      bigint references ledger_entries(id)
);

create index ledger_time_idx on ledger_entries (occurred_at desc);
create index ledger_account_idx on ledger_entries (account, currency, occurred_at desc);
create index ledger_hustle_idx on ledger_entries (hustle, occurred_at desc)
  where hustle is not null;
alter table ledger_entries enable row level security;

-- Append-only enforcement. UPDATE and DELETE are refused outright; a mistake
-- is corrected with a reversing entry, which is what an auditor wants to see.
create or replace function ledger_reject_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'engine.ledger_entries is append-only (% blocked)', tg_op;
end $$;

create trigger ledger_no_update before update on ledger_entries
  for each row execute function ledger_reject_mutation();
create trigger ledger_no_delete before delete on ledger_entries
  for each row execute function ledger_reject_mutation();

-- ----------------------------------------------------------------------------
-- Sourced opportunities (Hustle A/B/C share one table, differ in payload)
-- ----------------------------------------------------------------------------
create table arbitrage_targets (
  id                 uuid primary key default gen_random_uuid(),
  hustle             hustle_kind not null,
  agent_id           uuid references agents(id),
  source             text not null,           -- 'ticketmaster' | 'appsumo' | 'expired-domains' | 'directory'
  external_ref       text,                    -- event/product/domain id at the source
  title              text not null,
  url                text,
  status             target_status not null default 'discovered',
  acquisition_cost_minor bigint check (acquisition_cost_minor is null or acquisition_cost_minor >= 0),
  resale_floor_minor     bigint check (resale_floor_minor is null or resale_floor_minor >= 0),
  currency           char(3) not null default 'USD',
  -- Computed: (floor - cost) / cost, refreshed by the validation gate.
  expected_margin_pct numeric(8,2),
  liquidity_score    numeric(5,2),            -- 0-100, resale velocity estimate
  evidence_count     integer not null default 0 check (evidence_count >= 0),
  scoring_rationale  text,
  -- Everything source-specific, so Hustle C's client data needs no new columns.
  payload            jsonb not null default '{}'::jsonb,
  approval_id        uuid references approval_requests(id),
  -- Executed purchase, once it happens.
  purchase_ref       text,
  relisted_at        timestamptz,
  sold_at            timestamptz,
  sale_proceeds_minor bigint,
  discovered_at      timestamptz not null default now(),
  decided_at         timestamptz,
  updated_at         timestamptz not null default now(),
  unique (hustle, source, external_ref)
);

create index targets_open_idx on arbitrage_targets (hustle, status, discovered_at desc);
create index targets_margin_idx on arbitrage_targets (expected_margin_pct desc nulls last)
  where status in ('discovered', 'validating');
alter table arbitrage_targets enable row level security;

-- ----------------------------------------------------------------------------
-- Funnels and landing pages (Hustle A)
-- ----------------------------------------------------------------------------
create table funnels (
  id             uuid primary key default gen_random_uuid(),
  agent_id       uuid references agents(id),
  slug           text not null unique,
  offer          text not null,
  url            text,                          -- Cloudflare Pages deployment URL
  trial_days     integer not null default 7 check (trial_days between 0 and 365),
  price_minor    bigint not null check (price_minor >= 0),
  currency       char(3) not null default 'USD',
  merchant_account text not null default 'funnel',
  -- One-click cancellation endpoint is mandatory (section 2 Hustle A.2).
  cancel_url     text,
  status         text not null default 'draft', -- draft | live | paused | retired
  metrics        jsonb not null default '{}'::jsonb, -- visits, trials, conv %, refund %
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);

alter table funnels enable row level security;

-- Subscriptions and cancellations, mirrored from Stripe so the dashboard has
-- them without a Stripe call, and disputes can be traced to a cancel path.
create table funnel_subscriptions (
  id                   uuid primary key default gen_random_uuid(),
  funnel_id            uuid not null references funnels(id) on delete cascade,
  stripe_customer_id   text,
  stripe_subscription_id text unique,
  status               text not null,           -- trialing | active | canceled | past_due
  trial_ends_at        timestamptz,
  canceled_at          timestamptz,
  cancel_reason        text,
  disputed             boolean not null default false,
  mrr_minor            bigint not null default 0,
  currency             char(3) not null default 'USD',
  raw                  jsonb not null default '{}'::jsonb,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

create index funnel_subs_idx on funnel_subscriptions (funnel_id, status);
alter table funnel_subscriptions enable row level security;

-- ----------------------------------------------------------------------------
-- Raw webhook ingress: always land the event first, process after
-- ----------------------------------------------------------------------------
create table webhook_events (
  id            bigint generated always as identity primary key,
  provider      text not null,                 -- 'stripe' | 'telegram' | 'github'
  event_type    text,
  external_id   text,
  payload       jsonb not null,
  received_at   timestamptz not null default now(),
  processed_at  timestamptz,
  status        text not null default 'received', -- received | processed | failed | ignored
  error         text,
  -- Unique only when the provider supplies an id, so unsigned probes can retry.
  received_key  text generated always as (
    case when external_id is null then null
         else provider || ':' || external_id end
  ) stored unique
);

create index webhook_unprocessed_idx on webhook_events (received_at)
  where processed_at is null;
alter table webhook_events enable row level security;

-- ----------------------------------------------------------------------------
-- Agent logs. Container stdout stays in Docker; this is the structured stream
-- the ledger/audit view reads, capped by retention below.
-- ----------------------------------------------------------------------------
create table agent_logs (
  id          bigint generated always as identity primary key,
  agent_id    uuid references agents(id) on delete set null,
  level       log_level not null default 'info',
  event       text,                            -- dotted name: 'approval.requested'
  message     text not null,
  context     jsonb not null default '{}'::jsonb,
  -- Set when the row came from a human or an approval decision, so the audit
  -- stream can be filtered without a join.
  is_audit    boolean not null default false,
  created_at  timestamptz not null default now()
);

create index logs_agent_time_idx on agent_logs (agent_id, created_at desc);
create index logs_audit_idx on agent_logs (created_at desc) where is_audit;
create index logs_level_idx on agent_logs (level, created_at desc)
  where level in ('error', 'critical');
alter table agent_logs enable row level security;

-- ----------------------------------------------------------------------------
-- Derived metrics, folded nightly for the executive header
-- ----------------------------------------------------------------------------
create table daily_metrics (
  day                 date not null,
  hustle              hustle_kind not null,
  revenue_minor       bigint not null default 0,
  cost_minor          bigint not null default 0,
  llm_cost_usd        numeric(12,4) not null default 0,
  mrr_minor           bigint not null default 0,
  gross_volume_minor  bigint not null default 0,
  dispute_count       integer not null default 0,
  chargeback_count    integer not null default 0,
  net_profit_minor    bigint generated always as (revenue_minor - cost_minor) stored,
  primary key (day, hustle)
);

alter table daily_metrics enable row level security;

-- ============================================================================
-- Functions
-- ============================================================================

-- Current value of a setting, with a caller-supplied fallback.
create or replace function get_setting(p_key text, p_default jsonb default null)
returns jsonb language sql stable as $$
  select coalesce((select value from settings where key = p_key), p_default);
$$;

-- Sign a request expects for a cost: which autonomy tier does it fall into?
create or replace function classify_approval_tier(p_cost_minor bigint)
returns approval_tier language sql stable as $$
  -- CASE yields text; cast to the declared enum return type, which Postgres
  -- validates for SQL functions at definition time.
  select case
    when p_cost_minor <= coalesce(
           (get_setting('approval.tier1_max_minor')->>'value')::bigint, 5000)
      then 'tier1'
    when p_cost_minor <= coalesce(
           (get_setting('approval.tier2_max_minor')->>'value')::bigint, 25000)
      then 'tier2'
    else 'tier3'
  end::approval_tier;
$$;

-- The dry-run master switch, read live by every external action.
create or replace function is_dry_run() returns boolean language sql stable as $$
  -- ::boolean, because jsonb ->> extracts TEXT. Casts 'true'/'false' and JSON
  -- booleans correctly; the sandbox exercises this, so a regression cannot slip.
  select coalesce((get_setting('engine.dry_run')->>'value')::boolean, true);
$$;

-- Log an LLM call and roll it into the agent's daily spend, atomically.
-- Returns true if the agent is still under its cap AFTER this call, so the
-- caller can stop before spending one more dollar it is not allowed to.
create or replace function log_llm_call(
  p_agent_handle text,
  p_model text,
  p_input_tokens bigint,
  p_output_tokens bigint,
  p_cost_usd numeric,
  p_meta jsonb default '{}'::jsonb
) returns boolean language plpgsql as $$
declare
  v_agent agents;
  v_day date := (now() at time zone coalesce(
            (get_setting('owner.timezone')->>'value'), 'UTC'))::date;
  v_row agent_spend_daily;
  v_cap numeric;
begin
  select * into v_agent from agents where handle = p_agent_handle;
  if not found then
    raise exception 'unknown agent %', p_agent_handle;
  end if;

  insert into agent_logs (agent_id, level, event, message, context)
  values (v_agent.id, 'debug', 'llm.call',
          'llm ' || p_model || ' $' || p_cost_usd::text,
          jsonb_build_object('model', p_model,
                             'in_tokens', p_input_tokens,
                             'out_tokens', p_output_tokens,
                             'cost_usd', p_cost_usd) || p_meta);

  insert into agent_spend_daily (agent_id, day, cost_usd, tokens_total, calls, cap_usd)
  values (v_agent.id, v_day, p_cost_usd, p_input_tokens + p_output_tokens, 1,
          v_agent.llm_budget_usd_day)
  on conflict (agent_id, day) do update
    set cost_usd     = agent_spend_daily.cost_usd + excluded.cost_usd,
        tokens_total = agent_spend_daily.tokens_total + excluded.tokens_total,
        calls        = agent_spend_daily.calls + 1
  returning * into v_row;

  -- Global kill switch beats the per-agent cap.
  v_cap := least(
    nullif(v_agent.llm_budget_usd_day, 0),
    coalesce((get_setting('llm.daily_cap_usd')->>'value')::numeric, 8)
  );
  if v_cap is null then
    v_cap := coalesce((get_setting('llm.daily_cap_usd')->>'value')::numeric, 8);
  end if;

  if v_row.breach_at is null and v_row.cost_usd > v_cap then
    update agent_spend_daily
       set breach_at = now()
     where agent_id = v_agent.id and day = v_day;
    insert into agent_logs (agent_id, level, event, message, is_audit)
    values (v_agent.id, 'critical', 'llm.cap_breached',
            format('LLM spend $%.4f exceeded cap $%.2f; agent halted',
                   v_row.cost_usd, v_cap), true);
  end if;

  return v_row.cost_usd <= v_cap;
end $$;

-- Request spend. Applies tiering, writes the approval row, and decides whether
-- execution may proceed without a human.
create or replace function request_approval(
  p_agent_handle text,
  p_title text,
  p_cost_minor bigint,
  p_currency char(3) default 'USD',
  p_rationale text default '',
  p_source_url text default null,
  p_expected_roi text default null,
  p_hustle hustle_kind default null,
  p_vendor text default null,
  p_idempotency_key text default null,
  p_context jsonb default '{}'::jsonb
) returns table (approval_id uuid, state approval_state, tier approval_tier)
language plpgsql as $$
declare
  v_agent agents;
  v_tier approval_tier;
  v_auto approval_state;
  v_existing uuid;
  v_expiry interval;
begin
  select * into v_agent from agents where handle = p_agent_handle;
  if not found then
    raise exception 'unknown agent %', p_agent_handle;
  end if;

  -- Replay guard: a worker that crashed and restarted must not queue twice.
  if p_idempotency_key is not null then
    select a.id, a.state, a.tier into approval_id, state, tier
      from approval_requests a
     where a.idempotency_key = p_idempotency_key;
    if found then
      return next;
      return;
    end if;
  end if;

  v_tier := classify_approval_tier(p_cost_minor);
  v_expiry := make_interval(hours => coalesce(
      (get_setting('approval.expiry_hours')->>'value')::int, 24));

  if v_tier = 'tier1' then
    v_auto := 'auto_approved';
  elsif v_tier = 'tier3' then
    v_auto := 'blocked';                      -- hard block, manual execution
  else
    v_auto := 'pending';                      -- needs the human tap
  end if;

  insert into approval_requests (
    requested_by, title, rationale, expected_roi_note, source_url, hustle, vendor,
    cost_minor, currency, tier, state, idempotency_key, expires_at, gate_context, decided_by, decided_at)
  values (
    v_agent.id, p_title, p_rationale, p_expected_roi, p_source_url, p_hustle, p_vendor,
    p_cost_minor, p_currency, v_tier, v_auto, p_idempotency_key, now() + v_expiry,
    p_context || jsonb_build_object('dry_run', is_dry_run()),
    case when v_auto <> 'pending' then 'gatekeeper' end,
    case when v_auto <> 'pending' then now() end)
  returning approval_requests.id INTO v_existing;

  insert into approval_events (approval_id, from_state, to_state, actor, note)
  values (v_existing, null, v_auto, 'gatekeeper',
          'classified ' || v_tier::text || ' at ' || p_cost_minor::text || ' minor');

  insert into agent_logs (agent_id, level, event, message, is_audit, context)
  values (v_agent.id, 'info', 'approval.requested',
          format('%s: %s (%s, %s minor)', v_auto::text, p_title, v_tier::text, p_cost_minor),
          true, jsonb_build_object('approval_id', v_existing, 'cost_minor', p_cost_minor));

  approval_id := v_existing;
  state := v_auto;
  tier := v_tier;
  return next;
end $$;

-- Decide a pending approval. Single entry point for Telegram and Web UI, so
-- both interfaces enforce identical rules.
create or replace function decide_approval(
  p_approval_id uuid,
  p_decision text,            -- 'approve' | 'reject'
  p_actor text,               -- 'telegram:1489...' | 'webui:owner@...'
  p_note text default null
) returns approval_state language plpgsql as $$
declare
  v approval_requests;
  v_next approval_state;
begin
  select * into v from approval_requests where id = p_approval_id for update;
  if not found then
    raise exception 'approval % not found', p_approval_id;
  end if;

  if v.state <> 'pending' then
    -- Idempotent: double-tapping Approve on Telegram returns the settled state.
    return v.state;
  end if;

  if v.expires_at is not null and v.expires_at < now() then
    update approval_requests
       set state = 'expired', updated_at = now(), decided_at = now(), decided_by = p_actor
     where id = p_approval_id;
    insert into approval_events (approval_id, from_state, to_state, actor, note)
    values (p_approval_id, 'pending', 'expired', 'system', 'expired before decision');
    return 'expired';
  end if;

  -- Tier 3 is a hard block: a tap cannot unlock it, only a manual run can.
  if v.tier = 'tier3' and p_decision = 'approve' then
    raise exception 'approval % is tier3 (> cap): manual execution required', p_approval_id;
  end if;

  v_next := case when p_decision = 'approve' then 'approved' else 'rejected' end;

  update approval_requests
     set state = v_next, decided_by = p_actor, decided_at = now(),
         decision_note = p_note, updated_at = now()
   where id = p_approval_id;

  insert into approval_events (approval_id, from_state, to_state, actor, note)
  values (p_approval_id, 'pending', v_next, p_actor, p_note);

  insert into agent_logs (agent_id, level, event, message, is_audit, context)
  values (v.requested_by, 'info', 'approval.decided',
          format('%s %s by %s', v_next::text, v.title, p_actor), true,
          jsonb_build_object('approval_id', p_approval_id, 'cost_minor', v.cost_minor));

  return v_next;
end $$;

-- Post to the ledger. Signs the amount from entry_type, refuses a duplicate
-- via dedupe_key, and returns the row id.
create or replace function post_ledger_entry(
  p_entry_type ledger_entry_type,
  p_amount_minor bigint,
  p_description text,
  p_currency char(3) default 'USD',
  p_account text default 'operating',
  p_hustle hustle_kind default null,
  p_agent_handle text default null,
  p_approval_id uuid default null,
  p_external_provider text default null,
  p_external_id text default null,
  p_dedupe_key text default null,
  p_metadata jsonb default '{}'::jsonb,
  p_occurred_at timestamptz default null
) returns bigint language plpgsql as $$
declare
  v_signed bigint;
  v_agent uuid;
  v_id bigint;
begin
  if p_amount_minor < 0 then
    raise exception 'amount_minor must be a magnitude; sign is derived from entry_type';
  end if;

  -- Inflow types store positive, outflow types store negative. 'log'-ish types
  -- are not in the enum, so every row here moves money.
  v_signed := case when p_entry_type in ('charge', 'fee', 'expense', 'dispute', 'transfer_out')
                   then -p_amount_minor else p_amount_minor end;

  if p_agent_handle is not null then
    select id into v_agent from agents where handle = p_agent_handle;
  end if;

  begin
    insert into ledger_entries (
      occurred_at, entry_type, amount_minor, currency, account, hustle, agent_id,
      approval_id, external_provider, external_id, description, metadata, dedupe_key)
    values (
      coalesce(p_occurred_at, now()), p_entry_type, v_signed, p_currency, p_account,
      p_hustle, v_agent, p_approval_id, p_external_provider, p_external_id,
      p_description, p_metadata, p_dedupe_key)
    returning id into v_id;
  exception when unique_violation then
    -- Redelivered webhook: return the original posting untouched.
    select id into v_id from ledger_entries where dedupe_key = p_dedupe_key;
    return v_id;
  end;

  return v_id;
end $$;

-- Reverse a posting with a compensating entry (ledger is append-only).
create or replace function reverse_ledger_entry(p_entry_id bigint, p_reason text, p_actor text default 'system')
returns bigint language plpgsql as $$
declare
  v ledger_entries;
  v_id bigint;
begin
  select * into v from ledger_entries where id = p_entry_id for update;
  if not found then
    raise exception 'ledger entry % not found', p_entry_id;
  end if;
  if v.reversed_by is not null then
    raise exception 'ledger entry % already reversed', p_entry_id;
  end if;

  insert into ledger_entries (
    entry_type, amount_minor, currency, account, hustle, agent_id, approval_id,
    external_provider, external_id, description, metadata, reverses)
  values (
    v.entry_type, -v.amount_minor, v.currency, v.account, v.hustle, v.agent_id, v.approval_id,
    v.external_provider, v.external_id, 'reversal: ' || p_reason,
    jsonb_build_object('reverses', p_entry_id, 'actor', p_actor), p_entry_id)
  returning id into v_id;

  update ledger_entries set reversed_by = v_id where id = p_entry_id;

  insert into agent_logs (level, event, message, is_audit, context)
  values ('warning', 'ledger.reversed',
          format('reversed ledger entry %s: %s', p_entry_id, p_reason), true,
          jsonb_build_object('entry_id', p_entry_id, 'reversal_id', v_id, 'actor', p_actor));

  return v_id;
end $$;

-- Agent heartbeat, also publishes desired_state so the agent learns about a
-- pause/disable the moment it reports in (no extra round trip, no polling gap).
create or replace function report_heartbeat(
  p_agent_handle text,
  p_status agent_status,
  p_seq bigint,
  p_message text default null,
  p_latency_ms integer default null,
  p_spend_delta_minor bigint default 0,
  p_tokens_delta bigint default 0,
  p_details jsonb default '{}'::jsonb
) returns jsonb language plpgsql as $$
declare
  v agents;
begin
  select * into v from agents where handle = p_agent_handle;
  if not found then
    raise exception 'unknown agent %', p_agent_handle;
  end if;

  insert into agent_heartbeats
    (agent_id, seq, status, message, latency_ms, spend_delta_minor, tokens_delta, details)
  values (v.id, p_seq, p_status, p_message, p_latency_ms,
          p_spend_delta_minor, p_tokens_delta, p_details);

  -- Reflect live status on the registry row so the fleet grid is one cheap read.
  update agents set status = p_status, updated_at = now()
   where id = v.id and status is distinct from p_status;

  return jsonb_build_object(
    'desired_state', v.desired_state,
    'status', p_status,
    'stale_after_seconds', coalesce(
      (get_setting('heartbeat.stale_seconds')->>'value')::int, 180)
  );
end $$;

-- Fold one day of ledger + LLM spend into daily_metrics. Called by the digest
-- job; safe to re-run for a day (it recomputes from source rather than adding).
-- Declared before the views so they bind to already-existing relations.
create or replace function rollup_daily_metrics(p_day date) returns void language plpgsql as $$
declare
  h hustle_kind;
  v_rev bigint; v_cost bigint; v_llm numeric; v_disp int; v_cb int; v_mrr bigint;
begin
  -- MRR is a company-level snapshot; each hustle row carries the same figure so
  -- the header can read any single row without summing across hustles.
  select coalesce(sum(mrr_minor), 0) into v_mrr
    from funnel_subscriptions
   where status in ('active', 'trialing');

  -- Plain `for ... execute`, not `foreach ... execute`: the FOREACH form only
  -- accepts an ARRAY expression until PostgreSQL 18, so on this server (17) a
  -- `foreach h in execute 'select ...'` fails to PARSE, which aborts the whole
  -- create function and takes 0002's grant on rollup_daily_metrics down with it.
  -- Iterating a result set is exactly what FOR ... EXECUTE means anyway.
  --
  -- The enum is schema-qualified on purpose. The string handed to EXECUTE is
  -- parsed at RUNTIME using the calling session's search_path (a Supabase client
  -- gets "$user", public, not engine), so a bare `hustle_kind` can resolve to
  -- nothing and raise `type "hustle_kind" does not exist` even though the
  -- enclosing function compiled fine. Table names inside function bodies need no
  -- qualification: plpgsql resolves those at compile time against the function's
  -- own search_path and caches the plan. Only names inside dynamic SQL need it.
  for h in execute 'select unnest(enum_range(null::engine.hustle_kind))' loop
    select
      coalesce(sum(amount_minor) filter (where amount_minor > 0), 0),
      coalesce(-sum(amount_minor) filter (where amount_minor < 0), 0),
      coalesce(count(*) filter (where entry_type = 'dispute'), 0)::int,
      coalesce(count(*) filter (where entry_type = 'chargeback'), 0)::int
    into v_rev, v_cost, v_disp, v_cb
      from ledger_entries
     where hustle = h
       and reversed_by is null
       and occurred_at >= p_day
       and occurred_at < p_day + 1;

    select coalesce(sum(d.cost_usd), 0) into v_llm
      from agent_spend_daily d
      join agents a on a.id = d.agent_id
     where d.day = p_day and a.hustle = h;

    insert into daily_metrics (day, hustle, revenue_minor, cost_minor, llm_cost_usd,
                               dispute_count, chargeback_count, mrr_minor)
    values (p_day, h, v_rev, v_cost, v_llm, v_disp, v_cb, coalesce(v_mrr, 0))
    on conflict (day, hustle) do update
      set revenue_minor = excluded.revenue_minor,
          cost_minor    = excluded.cost_minor,
          llm_cost_usd  = excluded.llm_cost_usd,
          dispute_count = excluded.dispute_count,
          chargeback_count = excluded.chargeback_count,
          mrr_minor     = excluded.mrr_minor;
  end loop;
end $$;

-- Newest heartbeat per agent: the fleet-health read.
create or replace view vw_agent_fleet_health as
select a.id, a.handle, a.display_name, a.role, a.hustle,
       a.status, a.desired_state, a.container,
       h.seq, h.status as last_status, h.message as last_message,
       h.reported_at as last_heartbeat_at,
       extract(epoch from (now() - h.reported_at))::bigint as seconds_since_beat,
       h.latency_ms,
       s.cost_usd as today_llm_cost_usd, s.cap_usd as llm_cap_usd,
       s.breach_at is not null as llm_cap_breached
  from agents a
  left join lateral (
      select * from agent_heartbeats hb
       where hb.agent_id = a.id order by hb.reported_at desc, hb.id desc limit 1
  ) h on true
  left join agent_spend_daily s
    on s.agent_id = a.id
   and s.day = (now() at time zone 'UTC')::date;

-- Executive header: live aggregates without a nightly job.
create or replace view vw_executive_metrics as
with today as (
  select coalesce(sum(amount_minor) filter (where amount_minor > 0), 0) as revenue_minor,
         coalesce(-sum(amount_minor) filter (where amount_minor < 0), 0) as cost_minor,
         coalesce(count(*) filter (where entry_type = 'dispute'), 0)::int as disputes
    from ledger_entries
   where occurred_at > now() - interval '24 hours'
     and reversed_by is null
),
-- Denominator for the dispute ratio is payments taken, i.e. inflow volume.
-- Card 'charge' rows are money OUT to vendors, so counting them here would
-- dilute the ratio and hide a merchant-account problem.
gross as (
  select coalesce(sum(amount_minor), 0) as volume_minor
    from ledger_entries
   where entry_type in ('revenue', 'refund')
     and amount_minor > 0
     and occurred_at > now() - interval '30 days'
),
mrr as (
  select coalesce(sum(mrr_minor), 0) as mrr_minor
    from funnel_subscriptions
   where status in ('active', 'trialing')
),
pend as (
  select count(*) as pending_approvals,
         coalesce(sum(cost_minor), 0) as pending_spend_minor
    from approval_requests where state = 'pending'
)
select today.revenue_minor, today.cost_minor,
       (today.revenue_minor - today.cost_minor) as net_24h_minor,
       today.disputes, gross.volume_minor,
       -- Ratio against 30 days of payment volume, because card networks measure
       -- disputes on a rolling multi-day window, not a single day.
       case when gross.volume_minor > 0
            then round(today.disputes::numeric * 100 / gross.volume_minor, 3)
            else 0 end as dispute_ratio_pct,
       mrr.mrr_minor, pend.pending_approvals, pend.pending_spend_minor
  from today, gross, mrr, pend;

-- Open approvals for the dashboard queue feed.
create or replace view vw_approval_queue as
select ar.*, a.handle as agent_handle, a.display_name as agent_name,
       coalesce(extract(epoch from (ar.expires_at - now()))::bigint, 0) as seconds_to_expiry
  from approval_requests ar
  join agents a on a.id = ar.requested_by
 where ar.state = 'pending'
 order by ar.cost_minor desc, ar.created_at asc;
