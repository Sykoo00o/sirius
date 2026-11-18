-- ============================================================================
-- Ghost X2 - Module 1 part 3: least-privilege grants.
--
-- Separate migration on purpose: GRANT ... ON ALL FUNCTIONS / ON ALL TABLES and
-- ALTER DEFAULT PRIVILEGES act only on objects that already exist and are owned
-- by the granting role. By the time 0002 has finished installing RLS, every
-- engine function and table is present, so this is the correct moment to hand
-- them to engine_runtime / engine_dashboard. Keeping it apart also avoids the
-- trap of granting SELECT on tables before ledger_entries exists.
-- ============================================================================

set search_path to engine;

-- ----------------------------------------------------------------------------
-- Roles: the two service identities. Passwords are placeholders; override them
-- with ALTER ROLE ... PASSWORD during provisioning (a committed migration must
-- not carry a production secret, and the engine connects as `postgres` here).
-- ----------------------------------------------------------------------------
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

-- Take ownership of the whole schema before granting. ensure_table() creates
-- engine, engine.settings and engine.schema_migrations on a *separate*
-- connection from the one running these statements, so those tables can be owned
-- by whichever role the deploy user is. GRANT ... ON ALL TABLES only acts on
-- tables the current role owns, so without this the bootstrap tables would get no
-- privileges and every settings read would fail. We run as the schema owner or
-- superuser (checked in 0002); ownership transfer succeeds for both.
alter schema engine owner to postgres;
alter table if exists engine.settings owner to postgres;
alter table if exists engine.schema_migrations owner to postgres;

grant usage on schema engine to engine_runtime, engine_dashboard;
grant select on all tables    in schema engine to engine_dashboard;
grant execute on all functions in schema engine to engine_dashboard;
grant select, insert, update, delete on all tables in schema engine to engine_runtime;
grant usage, select on all sequences   in schema engine to engine_runtime;
grant execute on all functions         in schema engine to engine_runtime;

-- Belt over suspenders: even with table-wide INSERT/UPDATE granted, direct
-- mutation of the ledger trips the append-only trigger installed in 0001.
revoke update, delete on engine.ledger_entries from engine_dashboard;
revoke update, delete on engine.ledger_entries from engine_runtime;

-- Default privileges, installed FOR ROLE postgres: every engine object is
-- owned by postgres (above), so a later migration's new table inherits the right
-- grants without each migration re-granting by hand.
alter default privileges for role postgres in schema engine grant all on tables to engine_runtime;
alter default privileges for role postgres in schema engine grant select on tables to engine_dashboard;
alter default privileges for role postgres in schema engine grant all on sequences to engine_runtime;

-- ----------------------------------------------------------------------------
-- SECURITY DEFINER tightening. The gatekeepers run as their owner (postgres) so
-- a worker holding only table grants can still execute a ledger post or a
-- decision. Definer functions must pin search_path (else a caller-controlled
-- schema ahead of ours could shadow a name the body resolves unqualified) and be
-- revoked from PUBLIC (else the execute path is open to any role).
-- ----------------------------------------------------------------------------
alter function engine.request_approval(text, text, bigint, char(3), text, text, text,
                                       hustle_kind, text, text, jsonb)
  set search_path = engine, pg_catalog;
alter function engine.post_ledger_entry(ledger_entry_type, bigint, text, char(3), text,
                                        hustle_kind, text, uuid, text, text, text, jsonb,
                                        timestamptz)
  set search_path = engine, pg_catalog;
alter function engine.reverse_ledger_entry(bigint, text, text)
  set search_path = engine, pg_catalog;
alter function engine.log_llm_call(text, text, bigint, bigint, numeric, jsonb)
  set search_path = engine, pg_catalog;
alter function engine.report_heartbeat(text, agent_status, bigint, text, integer,
                                       bigint, bigint, jsonb)
  set search_path = engine, pg_catalog;

revoke execute on function engine.request_approval(text, text, bigint, char(3), text, text,
  text, hustle_kind, text, text, jsonb) from public;
revoke execute on function engine.post_ledger_entry(ledger_entry_type, bigint, text, char(3),
  text, hustle_kind, text, uuid, text, text, text, jsonb, timestamptz) from public;
revoke execute on function engine.reverse_ledger_entry(bigint, text, text) from public;
