-- ============================================================================
-- Ghost X2 - Module 1 part 2: row level security
--
-- Model: the `engine` schema is private infrastructure. Only the two service
-- roles reach it, and they do so through SECURITY DEFINER functions or narrow
-- table grants. Every human-facing path (anon key / authenticated JWT) is
-- denied by these policies, including the dashboard views, until a role is
-- explicitly named in an `actor` allowlist below.
--
-- Why bother with RLS on top of grants: the anon and authenticated Postgres
-- roles exist on Supabase and share the database. Without these, a leaked
-- publishable key plus a guessed table name reads the whole ledger.
-- ============================================================================

set search_path to engine;

-- Precondition: FORCE ROW LEVEL SECURITY, ALTER VIEW ... security_invoker, and
-- ALTER ROLE ... SET search_path all require owning the object or being
-- superuser. We connect as the Supabase `postgres` role, which qualifies; if a
-- deploy runs as some lesser role, fail here with a sentence that explains the
-- situation, not on a dozen ALTER VIEW permission errors.
do $$
begin
  if not (
    exists (select 1 from pg_roles where rolname = current_user and rolsuper)
    or (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'engine' and c.relkind = 'r'
          and pg_get_userbyid(c.relowner) = current_user) > 0
  ) then
    raise exception
      'migration 0002 must run as the owner of the engine schema or a superuser '
      '(current role: %). On Supabase this means connecting as the postgres role, '
      'not the anon/authenticated/service_role keys.', current_user;
  end if;
end $$;

-- The two service identities must exist before anything references them: the
-- `alter role ... set search_path` below and every `create policy ... to
-- engine_runtime` in this file resolve role names immediately and raise
-- `role "..." does not exist` otherwise. They are created here rather than in
-- 0003 because this migration is where they are first depended on; 0003 keeps its
-- own creation block so that applying it alone (or on a database where these
-- statements were skipped) still works. Passwords are placeholders by design: a
-- committed migration must not carry a production secret, and provisioning sets
-- the real value with `alter role ... password`.
do $$
begin
  if not exists (select from pg_roles where rolname = 'engine_runtime') then
    create role engine_runtime nosuperuser nocreatedb noinherit login
      password 'CHANGE_ME_ENGINE_RUNTIME';
  end if;
  if not exists (select from pg_roles where rolname = 'engine_dashboard') then
    create role engine_dashboard nosuperuser nocreatedb noinherit login
      password 'CHANGE_ME_ENGINE_DASHBOARD';
  end if;
end $$;

-- Service roles bypass RLS as owners of their own objects; pinning their
-- search_path means a settings read resolves engine.* without schema-qualifying.
alter role engine_runtime set search_path to engine, public;
alter role engine_dashboard set search_path to engine, public;

-- Helper: is the current session one of the trusted engine roles?
-- SECURITY DEFINER, and the policy below references it unqualified, so it
-- executes as its (postgres) owner and reads pg_roles / engine.settings without
-- granting the caller SELECT on either. Without DEFINER an anon connection
-- evaluating this predicate would need its own read access and the deny-by-
-- default stance would leak through the helper.
create or replace function engine.is_service_role()
returns boolean language sql stable security definer set search_path = pg_catalog, engine as $$
  select current_user in ('engine_runtime', 'engine_dashboard', 'postgres', 'supabase_admin');
$$;

-- Helper: named humans allowed through, by JWT role/email. Empty by default,
-- which means anon and authenticated see nothing at all.
-- SECURITY DEFINER for the same reason: it reads engine.settings on the
-- caller's behalf. search_path is pinned so an attacker who can create objects
-- in a schema ahead of ours cannot shadow auth.jwt() or jsonb_* here.
create or replace function engine.is_allowed_dashboard_user()
returns boolean language sql stable security definer set search_path = auth, engine, pg_catalog as $$
  select exists (
    select 1 from engine.settings
     where key = 'dashboard.allowed_emails'
       and jsonb_typeof(value) = 'array'
       and exists (
         select 1 from jsonb_array_elements_text(value) e
          where lower(e) = lower(coalesce(auth.jwt() ->> 'email', ''))
       )
  )
  and coalesce(auth.jwt() ->> 'role', '') not in ('anon', 'service_role')
  and coalesce(auth.jwt() ->> 'email', '') <> '';
$$;

-- Every engine table: full rights for service roles, nothing for anyone else.
do $$
declare
  t text;
begin
  foreach t in array array[
    'settings', 'schema_migrations', 'agents', 'agent_heartbeats', 'agent_spend_daily',
    'approval_requests', 'approval_events', 'ledger_entries', 'arbitrage_targets',
    'funnels', 'funnel_subscriptions', 'webhook_events', 'agent_logs', 'daily_metrics'
  ] loop
    execute format('alter table engine.%I force row level security', t);

    -- Unqualified: the policy predicate resolves at the table's own search_path
    -- (the table is in engine), and the function is SECURITY DEFINER so it runs
    -- as postgres regardless of which role is connecting.
    execute format($f$
      create policy engine_service_all on %I.%I
        for all to engine_runtime, engine_dashboard
        using (is_service_role())
        with check (is_service_role());$f$, 'engine', t);

    -- Humans get read-only, and only on the tables worth showing them. Write
    -- access goes through decide_approval()/request_approval(), never raw SQL,
    -- so the tier rules cannot be stepped around from the dashboard.
    -- The view is covered by its underlying tables' policies (security_invoker
    -- views check the invoker against every base table), so it is not listed here.
    if t in ('agents', 'agent_heartbeats', 'approval_requests', 'approval_events',
             'ledger_entries', 'agent_logs', 'daily_metrics') then
      execute format($f$
        create policy engine_dashboard_read on %I.%I
          for select to anon, authenticated
          using (is_allowed_dashboard_user());$f$, 'engine', t);
    end if;
  end loop;
end $$;

-- Views run with the privileges of their owner, which silently walks past RLS.
-- Pin them to invoker rights so a dashboard query is subject to the policies.
--
-- `set ( ... )` with parentheses, verified against the target server. Neither
-- `alter view v security_invoker = true` nor `alter view v set security_invoker =
-- true` parses here: PostgreSQL's grammar takes the bare assignment form only for
-- a *partitioned* view, and `security_invoker` is not one of the four column
-- attributes (comment / compression / nulls distinct / statistics) that the
-- unparenthesised form covers. Without the parens the statement dies with
-- `syntax error at or near "security_invoker"` and 0002 never completes, which
-- would have surfaced as a half-installed schema on the very first deploy.
alter view engine.vw_agent_fleet_health set (security_invoker = true);
alter view engine.vw_executive_metrics   set (security_invoker = true);
alter view engine.vw_approval_queue      set (security_invoker = true);

-- The functions humans call directly are definer-owned, so revoke the casual
-- grant from the general public role and hand it to the two roles that need it.
revoke all on function engine.request_approval(text, text, bigint, char(3), text, text, text, hustle_kind, text, text, jsonb) from public;
revoke all on function engine.decide_approval(uuid, text, text, text) from public;
revoke all on function engine.post_ledger_entry(ledger_entry_type, bigint, text, char(3), text, hustle_kind, text, uuid, text, text, text, jsonb, timestamptz) from public;
revoke all on function engine.reverse_ledger_entry(bigint, text, text) from public;

grant execute on function engine.request_approval(text, text, bigint, char(3), text, text, text, hustle_kind, text, text, jsonb) to engine_runtime;
grant execute on function engine.decide_approval(uuid, text, text, text) to engine_runtime, engine_dashboard;
grant execute on function engine.post_ledger_entry(ledger_entry_type, bigint, text, char(3), text, hustle_kind, text, uuid, text, text, text, jsonb, timestamptz) to engine_runtime;
grant execute on function engine.reverse_ledger_entry(bigint, text, text) to engine_runtime;

-- Read helpers anyone in-engine may call.
grant execute on function engine.get_setting(text, jsonb) to engine_runtime, engine_dashboard;
grant execute on function engine.is_dry_run() to engine_runtime, engine_dashboard;
grant execute on function engine.classify_approval_tier(bigint) to engine_runtime, engine_dashboard;
grant execute on function engine.report_heartbeat(text, agent_status, bigint, text, integer, bigint, bigint, jsonb) to engine_runtime;
grant execute on function engine.log_llm_call(text, text, bigint, bigint, numeric, jsonb) to engine_runtime;
grant execute on function engine.rollup_daily_metrics(date) to engine_runtime;
