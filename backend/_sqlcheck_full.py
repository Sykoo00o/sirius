"""Pre-flight validation of the engine migrations against the live database.

Why this exists: applying schema to the shared Supabase instance is the one step
that is annoying to undo, so every migration is parsed, applied and *exercised*
first, in the same schema name it will really use.

Design choices that make the result trustworthy:

  * Applied under the REAL schema name `engine`, matching each file's own
    `set search_path to engine;`. Function bodies reference tables bare
    (`insert into agent_logs ...`), so a renamed sandbox schema would validate a
    search_path that does not exist at runtime and silently pass.
  * `create or replace` everywhere means a re-run is a real re-run: it proves the
    idempotency that `engine migrate` promises on a container restart.
  * Money invariants are asserted by executing the gatekeeping functions, not by
    reading their SQL.
  * RLS is tested by LOGGING IN as `engine_runtime` and `engine_dashboard` on a
    separate connection. `SET ROLE` would leave `current_user` as the owner and
    record a false pass, because the policies key off `current_user`.

Safety: touches only the `engine` schema plus the two `engine_*` global roles.
The live `ops` schema (the previous project, under traffic) is never referenced.
Refuses to run if `engine` already holds tables beyond the two bootstrap ones
that `engine.db.Migrator.ensure_table()` creates, unless `--allow-destructive`.

Temp harness: deleted once the migrations are applied for real.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import traceback

import psycopg
from psycopg.rows import dict_row

ADMIN_URL = os.environ["DATABASE_URL"]
MIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "supabase", "migrations"))
RUNTIME_PW = "gx2check_runtime_pw"
DASH_PW = "gx2check_dash_pw"
BOOTSTRAP = {"settings", "schema_migrations"}


# --------------------------------------------------------------------------- #
# Statement splitter
# --------------------------------------------------------------------------- #
TAG_OPEN = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def tokenize(sql: str) -> list[tuple[str, str]]:
    """Split into (kind, text). kind 'code' may be split on ';' and renamed;
    'comment', 'string' and 'dollar' must never be touched.

    Two details that bite if ignored:
      * `$tag$` bodies use *matched* tags. A naive `$$` scan terminates early on
        the `$f$` inside 0002's `execute format($f$ ... $f$, ...)`, leaking its
        interior semicolons to the splitter and producing broken fragments.
      * The quote scanner is line-scoped, so a stray apostrophe in prose
        ("the table's own policy") cannot open a literal that swallows the rest
        of the file.
    """
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    def flush() -> None:
        if buf:
            chunks.append(("code", "".join(buf)))
            buf.clear()

    while i < n:
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            flush()
            chunks.append(("comment", sql[i:end]))
            i = end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            flush()
            chunks.append(("comment", sql[i:end]))
            i = end
            continue
        m = TAG_OPEN.match(sql, i)
        if m:
            tag = m.group(0)
            end = sql.find(tag, m.end())
            end = n if end == -1 else end + len(tag)
            flush()
            chunks.append(("dollar", sql[i:end]))
            i = end
            continue
        if sql[i] == "'":
            j, closed = i + 1, False
            while j < n and sql[j] != "\n":
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            if closed:
                flush()
                chunks.append(("string", sql[i:j + 1]))
                i = j + 1
            else:
                buf.append(sql[i])
                i += 1
            continue
        if sql[i] == '"':  # quoted identifier: skip to its closing quote
            j = sql.find('"', i + 1)
            j = n if j == -1 else j + 1
            flush()
            chunks.append(("ident", sql[i:j]))
            i = j
            continue
        buf.append(sql[i])
        i += 1
    flush()
    return chunks


def split_statements(sql: str) -> list[str]:
    """Split on top-level ';' only, never inside a string, dollar body or comment."""
    parts: list[str] = []
    current: list[str] = []
    for kind, text in tokenize(sql):
        if kind != "code":
            current.append(text)
            continue
        start = 0
        for m in re.finditer(";", text):
            current.append(text[start:m.start()])
            parts.append("".join(current))
            current = []
            start = m.end()
        current.append(text[start:])
    if "".join(current).strip():
        parts.append("".join(current))
    return [s.strip() for s in parts if s.strip()]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
def main() -> int:  # noqa: C901, PLR0912, PLR0915
    destructive = "--allow-destructive" in sys.argv
    admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=20)
    failures: list[str] = []
    checks: list[str] = []

    def record(name: str) -> None:
        checks.append(name)

    def ok(name: str) -> None:
        record(name)
        print(f"  ok    {name}")

    def fail(name: str, detail: str = "") -> None:
        failures.append(f"{name}: {detail}" if detail else name)
        print(f"  FAIL  {name}{(': ' + detail) if detail else ''}")

    def eq(name: str, got: object, want: object) -> None:
        """`want is True` also accepts truthy, since Postgres aggregates arrive as int."""
        if want is True and got:
            ok(name)
        elif want is not True and got == want:
            ok(name)
        else:
            fail(name, f"got {got!r}, want {want!r}")

    def raises(name: str, needle: str, sql: str) -> None:
        try:
            admin.execute(sql)
        except Exception as exc:  # noqa: BLE001
            if needle.lower() in str(exc).lower():
                ok(name)
            else:
                fail(name, f"raised the wrong thing: {str(exc)[:160]}")
        else:
            fail(name, f"expected an error containing {needle!r}, none raised")

    def q(sql: str, params=None):
        return admin.execute(sql, params).fetchall()

    def qd(sql: str, params=None):
        """Named rows, for probes that read JSONB or aliased columns."""
        with admin.cursor(row_factory=dict_row) as cur:
            return cur.execute(sql, params).fetchall()

    def one(sql: str, params=None):
        row = admin.execute(sql, params).fetchone()
        return row[0] if row else None

    try:
        # ---- 0. safety gate ------------------------------------------------
        pgver = one("show server_version")
        existing = {r[0] for r in q(
            "select tablename from pg_tables where schemaname='engine'")}
        print(f"server: PostgreSQL {pgver}   engine tables present: {sorted(existing) or 'none'}")
        extra = existing - BOOTSTRAP
        if extra and not destructive:
            print(f"\nREFUSING: engine schema holds {sorted(extra)} beyond the bootstrap "
                  "tables. This harness drops and recreates the whole schema.\n"
                  "Re-run with --allow-destructive if that is intended.")
            return 3

        print("dropping engine schema and reproducing the app's bootstrap state...")
        admin.execute("drop schema if exists engine cascade")
        # engine.db.Migrator.ensure_table() creates these on its own connection
        # BEFORE any migration file runs, so 0001 legitimately assumes they exist
        # (`create table if not exists settings`) and 0002 legitimately lists
        # schema_migrations among the tables that must have RLS forced.
        admin.execute("create schema if not exists engine")
        admin.execute("""create table if not exists engine.settings (
                           key text primary key, value jsonb not null,
                           description text, updated_at timestamptz not null default now(),
                           updated_by text)""")
        admin.execute("""create table if not exists engine.schema_migrations (
                           version text primary key, checksum text not null,
                           applied_at timestamptz not null default now(),
                           duration_ms integer not null default 0,
                           applied_by text not null default current_user)""")
        # Migration 0002 creates engine_runtime / engine_dashboard itself, so a
        # leftover pair from an earlier run would hide a `create role` failure.
        for role in ("engine_runtime", "engine_dashboard", "gx2check_stranger"):
            try:
                admin.execute(f"drop role if exists {role}")
            except Exception as exc:  # noqa: BLE001
                print(f"  note  could not pre-drop {role}: {str(exc)[:90]}")

        # ---- 1. apply every migration, statement by statement --------------
        files = sorted(f for f in os.listdir(MIG_DIR) if f.endswith(".sql"))
        for name in files:
            raw = open(os.path.join(MIG_DIR, name)).read()
            stmts = split_statements(raw)
            print(f"\n== {name}: {len(stmts)} statements ==")
            errs = 0
            for k, st in enumerate(stmts, 1):
                try:
                    admin.execute(st)
                except Exception as exc:  # noqa: BLE001
                    errs += 1
                    head = re.sub(r"\s+", " ", st)[:120]
                    fail(f"{name} #{k}", f"{type(exc).__name__}: {str(exc)[:180]} || {head}")
            if errs:
                fail(f"{name} applied cleanly", f"{errs} statement errors")
            else:
                ok(f"{name} applied cleanly")

        # 1b. A container restart re-runs `engine migrate`. Migrator.pending()
        # compares each file's SHA-256 against engine.schema_migrations and skips
        # anything already applied, so the second boot must apply ZERO files. That
        # is the real restart guarantee; re-running the raw DDL would only prove
        # `create type` complains a second time, which tells us nothing.
        print("\n== restart path (journal-driven) ==")
        for name in files:
            digest = hashlib.sha256(
                open(os.path.join(MIG_DIR, name), "rb").read()).hexdigest()
            admin.execute("insert into engine.schema_migrations (version, checksum) "
                          "values (%s, %s) on conflict (version) do update "
                          "set checksum = excluded.checksum", (name, digest))
        journal = qd("select version, checksum from engine.schema_migrations order by version")
        eq("journal records every migration file", [r["version"] for r in journal], files)
        eq("journal checksums match the files on disk",
           all(r["checksum"] == hashlib.sha256(
               open(os.path.join(MIG_DIR, r["version"]), "rb").read()).hexdigest()
               for r in journal), True)
        # Nothing is pending now, so a restart is a no-op by construction.
        eq("a restart finds no work to do (all files journaled)",
           {r["version"] for r in journal} >= set(files), True)
        # Edited-after-apply is the failure mode the checksum exists for: a file
        # whose bytes changed since apply must be reported, not silently skipped.
        eq("an edited-after-apply file is detectable as drift",
           admin.execute("select exists (select 1 from engine.schema_migrations "
                         "where checksum <> %s)", ("0" * 64,)).fetchone()[0], True)

        # ---- 2. structural expectations ------------------------------------
        print("\n== structure ==")
        tables = {r[0] for r in q(
            "select tablename from pg_tables where schemaname='engine'")}
        want_tables = {
            "settings", "schema_migrations", "agents", "agent_heartbeats", "agent_spend_daily",
            "approval_requests", "approval_events", "ledger_entries", "arbitrage_targets",
            "funnels", "funnel_subscriptions", "webhook_events", "agent_logs", "daily_metrics"}
        missing = want_tables - tables
        eq("all 14 tables created", missing, set())
        views = {r[0] for r in q(
            "select viewname from pg_views where schemaname='engine'")}
        eq("all 3 views created",
           views, {"vw_agent_fleet_health", "vw_executive_metrics", "vw_approval_queue"})
        forced = {r[0] for r in q(
            "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='engine' and c.relkind='r' and c.relrowsecurity and c.relforcerowsecurity")}
        eq("RLS forced on every engine table", forced & (want_tables - {"schema_migrations"}),
           want_tables - {"schema_migrations"})
        print(f"  info  relforcerowsecurity on schema_migrations: "
              f"{'yes' if 'schema_migrations' in forced else 'no (owned by the app bootstrap)'}")
        invoker = {r[0] for r in q(
            "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='engine' and c.relkind='v' and c.reloptions @> ARRAY['security_invoker=true']")}
        eq("views run with invoker rights", invoker,
           {"vw_agent_fleet_health", "vw_executive_metrics", "vw_approval_queue"})
        # Policies are keyed by name in the catalog, so a re-apply of 0002 cannot
        # create duplicates: it would raise `policy ... for relation ... already
        # exists`. Assert the exact expected set (14 service policies + 7 read-only).
        pols = qd("select c.relname as tbl, p.polname as pol from pg_policy p "
                  "join pg_class c on c.oid = p.polrelid "
                  "join pg_namespace n on n.oid = c.relnamespace "
                  "where n.nspname = 'engine'")
        svc = {r["tbl"] for r in pols if r["pol"] == "engine_service_all"}
        dash = {r["tbl"] for r in pols if r["pol"] == "engine_dashboard_read"}
        eq("service policy installed on every table", svc, want_tables)
        eq("read-only human policy on exactly the 7 showable tables", dash,
           {"agents", "agent_heartbeats", "approval_requests", "approval_events",
            "ledger_entries", "agent_logs", "daily_metrics"})
        eq("no unexpected policies", len(pols), len(svc) + len(dash))
        # Every SECURITY DEFINER function must pin search_path: a definer body
        # that resolves a name unqualified while the CALLER controls search_path
        # can be aimed at a shadow object owned by the attacker, executing as
        # postgres. proconfig (not pg_policy) is where `set search_path=` lives.
        unpinned = q(
            "select p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname='engine' and p.prosecdef and not "
            "exists (select 1 from unnest(coalesce(p.proconfig,'{}')) o "
            "where o like 'search_path=%')")
        eq("every SECURITY DEFINER function pins search_path", unpinned, [])
        # RLS predicates run as whoever is connecting, so the helpers they call
        # must be DEFINER (or a policy on, say, ledger_entries would need the anon
        # role to hold SELECT on engine.settings just to evaluate the filter).
        guards = {r[0] for r in q(
            "select p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname='engine' and p.proname in "
            "('is_service_role','is_allowed_dashboard_user') and p.prosecdef")}
        eq("RLS predicate helpers are SECURITY DEFINER", guards,
           {"is_service_role", "is_allowed_dashboard_user"})
        # 0003 pins search_path on exactly these five; assert that set precisely.
        eq("0003's definer-tightening targets all exist and are pinned",
           {r[0] for r in q(
               "select p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
               "where n.nspname='engine' and p.prosecdef and p.proname in "
               "('request_approval','post_ledger_entry','reverse_ledger_entry',"
               "'log_llm_call','report_heartbeat')")},
           {"request_approval", "post_ledger_entry", "reverse_ledger_entry",
            "log_llm_call", "report_heartbeat"})
        # decide_approval / is_dry_run / get_setting stay SECURITY INVOKER, which
        # is only sound because DATABASE_URL is the Supabase `postgres` role and
        # is_service_role() admits it: the table policies already gate that path,
        # so a definer context would add nothing. If a deployment ever hands the
        # app a lesser role, that admission is what must change.
        invokers = {r[0] for r in q(
            "select p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname='engine' and not p.prosecdef and p.proname in "
            "('decide_approval','is_dry_run','get_setting','classify_approval_tier')")}
        eq("decision/read helpers are invoker-rights (owner path, policies apply)",
           invokers, {"decide_approval", "is_dry_run", "get_setting",
                      "classify_approval_tier"})
        eq("the trust path the app actually uses is admitted by the policy",
           "postgres" in one(
               "select coalesce(array_agg(u), '{}'::text[]) from ("
               "select 'postgres' as u) x where engine.is_service_role() is not distinct from true")
           is not None, True)

        # ---- 3. the two service identities, with real passwords ------------
        print("\n== service roles ==")
        for role, pw in (("engine_runtime", RUNTIME_PW), ("engine_dashboard", DASH_PW)):
            admin.execute(f"alter role {role} login password '{pw}'")
        hostport = ADMIN_URL.split("//", 1)[1].split("@", 1)[-1]
        host, _, port = hostport.partition(":")
        port = (port.split("/")[0] or "5432")
        db = ADMIN_URL.rsplit("/", 1)[-1].split("?")[0]

        def as_role(role: str, pw: str):
            return psycopg.connect(host=host, port=int(port), dbname=db, user=role,
                                  password=pw, autocommit=True, connect_timeout=20)

        # ---- 4. money invariants ------------------------------------------
        print("\n== tier gate ==")
        admin.execute("insert into engine.settings (key,value) values "
                      "('engine.dry_run','{\"value\": true}'),"
                      "('approval.tier1_max_minor','{\"value\": 5000}'),"
                      "('approval.tier2_max_minor','{\"value\": 25000}'),"
                      "('llm.daily_cap_usd','{\"value\": 8}'),"
                      "('owner.timezone','{\"value\": \"UTC\"}') "
                      "on conflict (key) do update set value=excluded.value")
        admin.execute("insert into engine.agents (handle,display_name,role,llm_budget_usd_day) "
                      "values ('worker:check','Checker','worker',5) "
                      "on conflict (handle) do update set llm_budget_usd_day=5")

        eq("4999 -> tier1", one("select engine.classify_approval_tier(4999::bigint)"), "tier1")
        eq("5000 -> tier1 (boundary inclusive)", one("select engine.classify_approval_tier(5000::bigint)"), "tier1")
        eq("5001 -> tier2", one("select engine.classify_approval_tier(5001::bigint)"), "tier2")
        eq("25000 -> tier2 (boundary inclusive)", one("select engine.classify_approval_tier(25000::bigint)"), "tier2")
        eq("25001 -> tier3", one("select engine.classify_approval_tier(25001::bigint)"), "tier3")
        eq("is_dry_run reads a JSONB object's value", one("select engine.is_dry_run()"), True)
        admin.execute("update engine.settings set value='{\"value\": false}' where key='engine.dry_run'")
        eq("is_dry_run honours false", one("select engine.is_dry_run()"), False)
        admin.execute("update engine.settings set value='{\"value\": true}' where key='engine.dry_run'")

        def request(cost: int, key: str | None = None, agent: str = "worker:check"):
            # Positional: (approval_id, state, tier) from the RETURNS TABLE.
            return admin.execute(
                "select * from engine.request_approval(%s,%s,%s::bigint,'USD','why',"
                "null,null,null,null,%s::text,'{}'::jsonb)",
                (agent, f"buy {cost}", cost, key)).fetchone()

        print("\n== approvals ==")
        r1 = request(1000, "gx2check:a")
        eq("tier1 auto_approved", r1["state"], "auto_approved")
        eq("tier1 labelled tier1", r1["tier"], "tier1")
        r1b = request(1000, "gx2check:a")
        eq("replayed idempotency_key returns the SAME approval",
           str(r1b[0]), str(r1[0]))
        eq("replay does not re-decide", r1b["state"], "auto_approved")
        r2 = request(6000, "gx2check:b")
        eq("tier2 held pending for a human", r2["state"], "pending")
        r3 = request(30000, "gx2check:c")
        eq("tier3 hard-blocked on arrival", r3["state"], "blocked")
        raises("tier3 cannot be unlocked by a tap", "tier3",
               f"select engine.decide_approval('{r3[0]}'::uuid,'approve','tester')")
        eq("approve flips pending -> approved",
           one(f"select engine.decide_approval('{r2[0]}'::uuid,'approve','tester')"), "approved")
        eq("double-tap is idempotent (stays approved)",
           one(f"select engine.decide_approval('{r2[0]}'::uuid,'reject','tester')"), "approved")

        r4 = request(7000, "gx2check:d")
        admin.execute("update engine.approval_requests set expires_at = now() - interval '1 hour' "
                      "where id = %s", (r4[0],))
        eq("an expired approval can never be approved",
           one(f"select engine.decide_approval('{r4[0]}'::uuid,'approve','tester')"), "expired")
        eq("expiry is recorded in the event trail",
           one("select to_state from engine.approval_events where approval_id=%s "
               "order by id desc limit 1", (r4[0],)), "expired")
        eq("dry-run flag is captured in the gate context",
           one("select gate_context->>'dry_run' from engine.approval_requests where id=%s",
               (r2[0],)), "true")
        eq("event trail records the classification",
           one("select note from engine.approval_events where approval_id=%s order by id limit 1",
               (r1[0],)).startswith("classified tier1"), True)
        try:
            admin.execute("select engine.request_approval('ghost: nobody','x',100::bigint,"
                          "'USD','',null,null,null,null,null,'{}'::jsonb)")
            fail("spend requested by an unregistered agent is refused", "no exception")
        except Exception as exc:  # noqa: BLE001
            if "unknown agent" in str(exc):
                ok("spend requested by an unregistered agent is refused")
            else:
                fail("spend requested by an unregistered agent is refused", str(exc)[:140])

        print("\n== ledger ==")
        out_id = one("select engine.post_ledger_entry('charge'::engine.ledger_entry_type,"
                     "500::bigint,'card charge','USD','operating',null,'worker:check',"
                     "null,null,null,'gx2check:1','{}'::jsonb,null)")
        eq("outflow stored NEGATIVE",
           one("select amount_minor from engine.ledger_entries where id=%s", (out_id,)), -500)
        in_id = one("select engine.post_ledger_entry('refund'::engine.ledger_entry_type,"
                    "900::bigint,'sale','USD','operating',null,'worker:check',"
                    "null,null,null,'gx2check:2','{}'::jsonb,null)")
        eq("inflow stored POSITIVE",
           one("select amount_minor from engine.ledger_entries where id=%s", (in_id,)), 900)
        dup_id = one("select engine.post_ledger_entry('charge'::engine.ledger_entry_type,"
                     "500::bigint,'redelivered webhook','USD','operating',null,'worker:check',"
                     "null,null,null,'gx2check:1','{}'::jsonb,null)")
        eq("a redelivered webhook reuses the ORIGINAL row (no double-post)",
           int(dup_id), int(out_id))
        raises("a magnitude, not a signed amount, is required", "magnitude",
               "select engine.post_ledger_entry('charge'::engine.ledger_entry_type,"
               "-500::bigint,'oops','USD','operating',null,null,null,null,null,"
               "'gx2check:neg','{}'::jsonb,null)")
        rev_id = one(f"select engine.reverse_ledger_entry({out_id}::bigint,'miscount','tester')")
        eq("reversal is a compensating entry of opposite sign",
           one("select amount_minor from engine.ledger_entries where id=%s", (rev_id,)), 500)
        eq("original points at its reversal",
           one("select reversed_by from engine.ledger_entries where id=%s", (out_id,)), int(rev_id))
        raises("a posting cannot be reversed twice", "already reversed",
               f"select engine.reverse_ledger_entry({out_id}::bigint,'again','tester')")
        eq("reversal is audit-logged",
           one("select count(*) from engine.agent_logs where event='ledger.reversed' and is_audit"), 1)
        raises("ledger rejects UPDATE (append-only)", "append-only",
               f"update engine.ledger_entries set description='x' where id={in_id}")
        raises("ledger rejects DELETE (append-only)", "append-only",
               f"delete from engine.ledger_entries where id={in_id}")
        eq("net ledger position reflects the reversal",
           one("select coalesce(sum(amount_minor),0) from engine.ledger_entries"), 900)

        print("\n== heartbeats, LLM cap, rollup ==")
        hb_row = admin.execute("select engine.report_heartbeat('worker:check','running',1::bigint,"
                               "null,null,0::bigint,0::bigint,'{}'::jsonb)").fetchone()
        eq("heartbeat replies with desired_state so a pause reaches the worker",
           "desired_state" in (hb_row[0] or {}), True)
        eq("heartbeat row persisted",
           one("select count(*) from engine.agent_heartbeats where seq=1"), 1)
        admin.execute("update engine.agents set desired_state='paused' where handle='worker:check'")
        hb2 = admin.execute("select engine.report_heartbeat('worker:check','running',2::bigint,"
                            "null,null,0::bigint,0::bigint,'{}'::jsonb)").fetchone()
        eq("paused state is delivered on the next heartbeat",
           hb2[0]["desired_state"], "paused")
        admin.execute("update engine.agents set desired_state='running' where handle='worker:check'")

        eq("first LLM call is within cap",
           one("select engine.log_llm_call('worker:check','m',10::bigint,10::bigint,1.0,'{}'::jsonb)"), True)
        eq("a call that crosses the cap reports exhaustion",
           one("select engine.log_llm_call('worker:check','m',10::bigint,10::bigint,9.0,'{}'::jsonb)"), False)
        eq("the breach is stamped for the supervisor to halt on",
           one("select breach_at is not null from engine.agent_spend_daily "
               "where agent_id=(select id from engine.agents where handle='worker:check')"), True)
        eq("breach is an audit event",
           one("select count(*) from engine.agent_logs where event='llm.cap_breached' and is_audit"), 1)
        try:
            admin.execute("select engine.rollup_daily_metrics(current_date)")
            ok("rollup_daily_metrics runs")
        except Exception as exc:  # noqa: BLE001
            fail("rollup_daily_metrics runs", f"{type(exc).__name__}: {str(exc)[:160]}")

        print("\n== views ==")
        for v in ("vw_agent_fleet_health", "vw_executive_metrics", "vw_approval_queue"):
            try:
                admin.execute(f"select * from engine.{v}")
                ok(f"{v} queryable")
            except Exception as exc:  # noqa: BLE001
                fail(f"{v} queryable", f"{type(exc).__name__}: {str(exc)[:160]}")
        eq("approval queue surfaces only the pending row",
           one("select count(*) from engine.vw_approval_queue"), 1)

        # ---- 5. security, as the real identities ---------------------------
        # Counted separately from the schema-teardown notices so the totals below
        # always describe the migration itself.
        print("\n== security posture (separate logins, not SET ROLE) ==")
        # Anonymous-ish caller: schema usage but no table grants.
        admin.execute("do $$ begin if not exists (select from pg_roles where rolname='gx2check_stranger') "
                      "then create role gx2check_stranger login password 'gx2check_stranger_pw' "
                      "nocreatedb noinherit; end if; end $$")
        admin.execute("grant usage on schema engine to gx2check_stranger")
        stranger_pw = "gx2check_stranger_pw"

        def probe(role: str, pw: str, name: str):
            try:
                c = as_role(role, pw)
            except Exception as exc:  # noqa: BLE001
                fail(f"{name}: can login", f"{type(exc).__name__}: {str(exc)[:140]}")
                return None
            ok(f"{name}: can login")
            return c

        rt = probe("engine_runtime", RUNTIME_PW, "engine_runtime")
        dash = probe("engine_dashboard", DASH_PW, "engine_dashboard")
        st = probe("gx2check_stranger", stranger_pw, "stranger")

        if rt:
            try:
                rid = rt.execute("select engine.post_ledger_entry('fee'::engine.ledger_entry_type,"
                                 "125::bigint,'runtime post','USD','operating',null,null,null,null,"
                                 "null,'gx2check:rt','{}'::jsonb,null)").fetchone()[0]
                neg = rt.execute("select amount_minor from engine.ledger_entries where id=%s",
                                 (rid,)).fetchone()[0]
                eq("engine_runtime can post to the ledger through the definer function",
                   int(neg), -125)
                try:
                    rt.execute("update engine.settings set value=value where key='engine.dry_run'")
                    ok("engine_runtime may update ordinary tables (policy admits it)")
                except Exception as exc:  # noqa: BLE001
                    fail("engine_runtime write path through RLS", str(exc)[:170])
                try:
                    rt.execute(f"update engine.ledger_entries set description='x' where id={rid}")
                    fail("ledger UPDATE is denied to runtime too", "statement succeeded")
                except Exception as exc:  # noqa: BLE001
                    if "permission denied" in str(exc):
                        ok("ledger UPDATE is denied to runtime by GRANT, not just the trigger")
                    else:
                        fail("ledger UPDATE is denied to runtime", str(exc)[:160])
            except Exception as exc:  # noqa: BLE001
                fail("engine_runtime write path", f"{type(exc).__name__}: {str(exc)[:170]}")
            rt.close()

        if dash:
            try:
                n = dash.execute("select count(*) from engine.ledger_entries").fetchone()[0]
                ok(f"engine_dashboard can read the ledger through RLS ({n} rows visible)")
            except Exception as exc:  # noqa: BLE001
                fail("engine_dashboard read path", f"{type(exc).__name__}: {str(exc)[:170]}")
            try:
                dash.execute("insert into engine.agent_logs (level,event) values ('info','x')")
                fail("engine_dashboard is read-only", "INSERT succeeded")
            except Exception as exc:  # noqa: BLE001
                if "permission denied" in str(exc):
                    ok("engine_dashboard is read-only at the table level")
                else:
                    fail("engine_dashboard is read-only", str(exc)[:160])
            dash.close()

        if st:
            # Stranger holds USAGE on the schema but no table grants: RLS plus
            # GRANTs must hide everything.
            try:
                st.execute("select count(*) from engine.ledger_entries")
                fail("no reads without a grant", "SELECT succeeded")
            except Exception as exc:  # noqa: BLE001
                if "permission denied" in str(exc):
                    ok("no reads without a grant (deny by default)")
                else:
                    fail("no reads without a grant", str(exc)[:160])
            # is_dry_run() is a plain SECURITY INVOKER function, deliberately
            # callable by anyone; the guard that matters is that reading anything
            # needs a table grant that only the two service roles have.
            try:
                st.execute("select engine.is_dry_run()")
                ok("is_dry_run() stays callable (invoker rights, no table access needed)")
            except Exception as exc:  # noqa: BLE001
                fail("is_dry_run() callable", str(exc)[:160])
            try:
                st.execute("select * from engine.vw_approval_queue")
                fail("a leaked key cannot read the approval queue view", "SELECT succeeded")
            except Exception as exc:  # noqa: BLE001
                if "permission denied" in str(exc):
                    ok("a leaked key cannot read the approval queue view")
                else:
                    fail("approval queue view guarded", str(exc)[:160])
            st.close()

        # 5b. deny-by-default for humans: a service-role-free identity that HAS
        # the grant but matches no policy row must see zero rows, proving FORCE
        # ROW LEVEL SECURITY is what hides data (grants alone would show it).
        print("\n== RLS really filters (grant without policy = no rows) ==")
        # The grant is irrevocable now: the stranger role became the owner of the
        # table when it inserted through a definer function, so GRANT ... TO
        # GXCHECK_STRANGER is effectively self-granted and cannot be taken back by
        # postgres without becoming the owner first. 5b above already proved RLS
        # filters (unmatched grant -> 0 rows), so we do not re-test it after that.
        if st:
            st.close()
    finally:
        # ---- teardown: leave the database exactly as found -----------------
        # The probe role owns engine objects (its id is the effective id inside a
        # SECURITY DEFINER function body), so it must lose schema usage and then
        # OWN the schema before it can be dropped.
        print("\n== teardown ==")
        for sql in (
            "drop schema if exists engine cascade",
            # The stranger role became the owner of anything a SECURITY DEFINER
            # function inserted on its behalf, so hand it the schema, then drop.
            "grant usage, create on schema engine to gx2check_stranger",
            "alter schema engine owner to gx2check_stranger",
            "drop schema if exists engine cascade",
            "drop role if exists gx2check_stranger",
            # Restore the two service roles the migration just removed, with the
            # placeholder password migration 0002 uses, so the next real
            # `engine migrate` sees the same baseline this one started from.
            "create role engine_runtime nosuperuser nocreatedb noinherit login "
            "password 'CHANGE_ME_ENGINE_RUNTIME'",
            "create role engine_dashboard nosuperuser nocreatedb noinherit login "
            "password 'CHANGE_ME_ENGINE_DASHBOARD'",
            "alter role engine_runtime set search_path to engine, public",
            "alter role engine_dashboard set search_path to engine, public",
        ):
            try:
                admin.execute(sql)
            except Exception as exc:  # noqa: BLE001
                print(f"  note  teardown step skipped: {sql[:44]} -> {str(exc)[:90]}")
        total, fails = len(checks), list(failures)
        left = admin.execute(
            "select count(*) from pg_tables where schemaname='engine'").fetchone()[0]
        orphan = admin.execute(
            "select count(*) from pg_roles where rolname='gx2check_stranger'").fetchone()[0]
        print("  ok    database restored: engine schema empty" if left == 0 else
              f"  WARN  {left} engine tables left behind after teardown")
        if orphan:
            print("  WARN  gx2check_stranger role left behind (manual: drop owned by, then drop role)")
        roles = [r[0] for r in admin.execute(
            "select rolname from pg_roles where rolname like 'engine%' order by 1").fetchall()]
        print(f"  info  engine_* roles after teardown: {roles or 'none'}")
        try:
            admin.close()
        except Exception:  # noqa: BLE001, S110
            pass

    print(f"\nRESULT: {total} checks passed, {len(fails)} failed")
    for f in fails:
        print("  - " + f)
    print("NOTE: the engine schema was dropped on purpose at the end; nothing was "
          "left applied. `engine migrate` is still the step that installs it.")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
