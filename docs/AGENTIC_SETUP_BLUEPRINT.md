# Agentic & Operational Setup Blueprint

A reusable description of how Sirius / Ghost X2 runs its agents and services today, written so a
new autonomous or human-in-the-loop (HITL) agent can be added the same way.

**How to read this document**

* Every claim points at a file in this repository. Where a path has a line number, it was
  read from the source at the time of writing; line numbers drift, the function names do not.
* Secret **values** are never written here. Only variable names, defaults and file locations.
* Where the code does **not** do something you might expect, this document says so under a
  "Gap" or "Not in the code" label instead of describing a design that does not exist.
* Scope: `backend/` (engine + finance agent + workers), `funnel_app/` (Routine Lab),
  `kegeldoc_app/` (a fork of it), `deploy/`, `scripts/`, `supabase/`, and the planning docs
  `SIRIUS_MASTER_BLUEPRINT.md`, `EXECUTION_PLAN.md`, `VALIDATION_AND_AGENT_ASSIST_PLAN.md`,
  `funnel_app/PHASE3_MANUAL_TEST.md`.

---

## 1. System Topology & Service Isolation

### 1.1 Runtimes on the host

Three runtimes exist side by side; they are deliberately not merged (`EXECUTION_PLAN.md` §0).

| Runtime | Process / unit | Bind | Code | State | Public path |
|---|---|---|---|---|---|
| Sirius engine (finance agent) | systemd `sirius-finance` | port `8000` | `backend/finance_agent.py` (FastAPI) | Supabase Postgres, `engine` schema | Cloudflare Tunnel `sirius-tunnel`, engine rule (`http://localhost:8000`) |
| Sirius supervisor (PTB) | systemd `sirius-supervisor` | — | `backend/supervisor.py` | Supabase | none |
| Routine Lab funnel | systemd `funnel-app` | `127.0.0.1:8090` | `funnel_app/main.py` | SQLite under `/var/lib/funnel-app` | Tunnel hostname `routinelab.vegastudios.site` |
| kegeldoc funnel | systemd `kegeldoc-app` | `127.0.0.1:8091` | `kegeldoc_app/main.py` | SQLite under `/var/lib/kegeldoc-app` | Tunnel hostname `kegeldoc.vegastudios.site` (temporary) |
| Module 1 engine (compose) | `docker-compose.yml` service `api` | `${API_BIND_IP:-127.0.0.1}:${BACKEND_PORT:-3100}` | `backend/engine/` (`engine api`) | Supabase, migrations `0001`–`0003` | Caddy (`deploy/Caddyfile`, profile `webhook`) |

Facts and caveats, each from source:

* **The `sirius-finance` and `sirius-supervisor` unit files are not in this repository.** Their
  names, port 8000 and the restart commands come from `SIRIUS_MASTER_BLUEPRINT.md` §1–2 and its
  Execution Log. `backend/finance_agent.py:3060` `main()` defaults to
  `--host ${FINANCE_AGENT_HOST:-0.0.0.0} --port ${FINANCE_AGENT_PORT:-3200}`, so 8000 is set by the
  (unversioned) unit, not by the code.
* **Gap — the engine is not loopback-only.** The kegeldoc apply entry in
  `SIRIUS_MASTER_BLUEPRINT.md` (2026-11-22 13:05 UTC) records the live listeners as
  `127.0.0.1:8090`, `127.0.0.1:8091` and **`0.0.0.0:8000`**. Only the two funnels are loopback-bound.
  If you want "`127.0.0.1:8000` engine", set `FINANCE_AGENT_HOST=127.0.0.1` (or `--host`) in the
  `sirius-finance` unit; that is an ops change and has not been made.
* The compose stack's `api` container (port 3100) is described by the funnel installer as
  "not running on the current host" and treated as informational only
  (`deploy/funnel_app/install-funnel-app.sh`, section 5). Compose also defines `supervisor`,
  `bot`, `worker-funnel`, `worker-arbitrage`, `worker-whitelabel`, a `oneshot` `jobs expire`
  service and `caddy` (`docker-compose.yml`).
* The funnel units state their three isolation facts at the top of the file
  (`deploy/funnel_app/funnel-app.service`, `deploy/kegeldoc_app/kegeldoc-app.service`):
  one port only, bind `127.0.0.1`, own venv and own `StateDirectory`, "so restarting this unit
  cannot blink the finance API".

### 1.2 Code isolation (no shared imports, separate venvs)

* `funnel_app/` imports nothing from `backend/engine` (`deploy/funnel_app/README.md` "Its own
  venv"). Each funnel has its own venv at `<app>/.venv` with exactly
  `fastapi uvicorn anyio jinja2 httpx python-multipart` (README Step 2; the installer import-probes
  the same six at `install-funnel-app.sh` section 2).
* The installer falls back to `backend/.venv/bin/python` only if the app venv is missing
  (`install-funnel-app.sh`, `for cand in "$VENV/bin/python" "$REPO_DIR/backend/.venv/bin/python"`).
* The one shared piece of code, `backend/llm_router.py`, is **loaded by file path**, not imported
  as a package (see §2.4), so the funnel runs with the engine stopped.
* `backend/finance_agent.py` and `backend/supervisor.py` are themselves standalone
  ("stdlib + FastAPI + httpx + psycopg only", `finance_agent.py` docstring) and do not import the
  `backend/engine` package.

### 1.3 Dedicated hostnames via Cloudflare Tunnel ingress

Template: `deploy/funnel_app/cloudflared-ingress.yml` and `deploy/kegeldoc_app/cloudflared-ingress.yml`.
These are the *shape* of a change merged into the live `/etc/cloudflared/config.yml`, not files to
drop in.

```yaml
ingress:
  # existing engine rule stays first and untouched
  - hostname: <app-hostname>
    service: http://127.0.0.1:<port>
    originRequest:
      keepAliveTimeout: 90s      # singular, under originRequest (validated with cloudflared 2026.11.1)
  - service: http_status:404     # catch-all, must stay last
```

Why a hostname per app rather than a path prefix (from the template comments):

1. `/webhooks/stripe` must be unique per origin; a path-rewrite would be the only thing separating
   the engine's webhook from the funnel's.
2. Absolute URLs come from `FUNNEL_PUBLIC_URL`, which is only correct on its own origin.
3. Processor origin checks / Radar signals key off the hostname, so each funnel keeps its own
   reputation.

Rollout as recorded in the Execution Log (kegeldoc apply): validated candidate config, timestamped
backup of `config.yml`, `cloudflared tunnel route dns` for the CNAME, cloudflared restart, then a
check that the *other* hostnames still answer `/health` 200 and that `http://<vps-ip>:8091/health`
gets **no** answer (loopback proof).

### 1.4 Separate databases

| Store | Used by | Location |
|---|---|---|
| Supabase Postgres, schema `engine` | finance agent, supervisor, workers, compose engine | `DATABASE_URL` (engine env); schema in `supabase/schema.sql` and `supabase/migrations/0001_engine_core.sql`–`0003_engine_grants.sql` |
| `responses.sqlite3` | funnel quiz answers (`funnel_app/storage.py`) | `FUNNEL_DB_PATH`, default `data/responses.sqlite3` (repo-relative) |
| `billing.sqlite3` | subscriptions, notification markers, save-offer events, **and** `trial_events` + `agent_logs` telemetry (`funnel_app/billing_store.py`, `funnel_app/telemetry.py`) | `FUNNEL_BILLING_DB_PATH`, default `data/billing.sqlite3` |
| outbox `.eml` files | `funnel_app/mailer.py` | `FUNNEL_EMAIL_OUTBOX`, default `data/outbox` |
| metrics CSV | `scripts/export_lane_c_metrics.py` | `FUNNEL_METRICS_CSV`, default `<app>/data/metrics/lane_c_daily.csv` |

Production values (from `deploy/funnel_app/funnel-app.env.template`) put every one of those under
`/var/lib/funnel-app/…` (kegeldoc: `/var/lib/kegeldoc-app/…`). Telemetry lives in the *billing*
file on purpose, so the export reads subscriptions and events through one connection
(`telemetry.py`, `TelemetryStore` docstring). The funnels have no connection to Supabase; the
column names are kept in parity with `engine.trial_events` so a later lift is a migration
(`telemetry.py` module docstring).

### 1.5 How systemd keeps state safe across `git pull` / `git clean`

Directives used in `deploy/funnel_app/*.service|*.timer` (kegeldoc identical with its own names):

| Directive | Where | Effect |
|---|---|---|
| `StateDirectory=funnel-app` + `StateDirectoryMode=0750` | `funnel-app.service` | systemd creates and owns `/var/lib/funnel-app` as the service user before `ExecStart`; a `git pull` / `git clean -fdx` on the checkout cannot touch billing state, WAL files or the outbox. |
| `ProtectSystem=strict` | all units | The checkout is read-only to the process; the app writes nothing under it. |
| `ReadWritePaths=/var/lib/funnel-app` | `funnel-metrics.service` only | The metrics oneshot has no `StateDirectory=`, so it needs the explicit allowance to write the CSV. Change the state path and you must change it in both units. |
| `EnvironmentFile=<repo>/funnel_app/.env` (**no `-` prefix**) | app + metrics units | A missing `.env` is a start failure, not a silent boot on defaults (charges off, outbox, repo-relative paths). |
| `Persistent=true` | both timers | A run missed during a reboot fires at next boot. Safe because both jobs are idempotent (§3, §4). |
| `OnCalendar=*-*-* 09:05:00 UTC` / `09:15:00 UTC`, `RandomizedDelaySec=120/180` | metrics / notices timers | Metrics first, notices second, so they never interleave in the journal. |
| `Restart=always`, `RestartSec=3`, `TimeoutStopSec=30`, `KillSignal=SIGINT`, `KillMode=mixed` | app unit | Restart on crash; graceful close for in-flight checkout redirects. |
| `PrivateTmp`, `NoNewPrivileges`, `ProtectKernel*`, `ProtectControlGroups`, `RestrictSUIDSGID`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | app unit | Standard hardening. |
| `StandardOutput=journal`, `SyslogIdentifier=funnel-app` | all | The journal is the log; it survives redeploys. |

Config precedence: `funnel_app/config.py` `load_dotenv()` loads `funnel_app/.env` **then** the repo
root `.env` (`ENV_PATHS`), and process env always wins (`setdefault` with a pristine-env snapshot).
To make something beat the file — e.g. arming charges — use a drop-in (`systemctl edit funnel-app`
→ `Environment=…`), per the unit comment. Settings are read at import time
(`main.py: settings = FunnelSettings.from_env()`); there is no reload endpoint, so any `.env` change
needs `systemctl restart`.

The installer (`deploy/funnel_app/install-funnel-app.sh`) is plan-only by default and `--apply`
as root. It: checks the checkout and git HEAD (pinned with `--git-dir`), the interpreter and deps,
`.env` presence and mode (600/400), charge arming vs keys, every state path is absolute and under
`/var/lib/<app>`, port ownership by **unit** (via `/proc/<pid>/cgroup`, so a redeploy is not
mistaken for a collision), renders tokens `@RUN_USER@ @VENV_PYTHON@ @REPO_DIR@` and fails on any
leftover token, runs `systemd-analyze verify` and classifies on its *output*, installs, verifies the
installed copies, refuses to start on any blocker, **restarts** on redeploy and checks the MainPID
changed, and `start`s the timers (an `enable` alone left them dormant on the first kegeldoc apply).
Exit 0 = only advisories, 1 = blockers. Note the defaults `REPO_DIR=/opt/ghost-x2`,
`RUN_USER=sirius`; the live host uses `/home/ubuntu/Sirius` (Execution Log), so pass both explicitly.

---

## 2. Agent Control Plane, Supervisor & Safety Guardrails

### 2.1 Actors and ownership

From `EXECUTION_PLAN.md` §1.1 (handles are the identity written to `agent_logs.agent_handle`):

| Actor | Handle | Owns | Must never touch |
|---|---|---|---|
| Supervisor loop | `supervisor` | heartbeat reconciliation, expiry sweep, error digest | card charges, vendor APIs, ledger posts |
| Finance gatekeeper | `finance-agent` | tier classification, card envelope, Stripe intake, ledger, Telegram decision writes | scraping, funnel copy, provisioning |
| Funnel worker | `worker:funnel` | sequences, cancellation, subscription mirror | card issuance, uncapped ad spend |
| Arbitrage scanner | `worker:arbitrage` | discovery rows only | checkout (no gate call by design) |
| White-label worker | `worker:whitelabel` | intake, provisioning, renewals | funnel billing entity |
| Telegram interface | `telegram` | read/report, owner-only decision relay | initiating spend |

Collision primitives that already exist (`EXECUTION_PLAN.md` §1.2): `agents.handle UNIQUE`
(`supabase/migrations/0001_engine_core.sql`); per-(handle, job) leases with
`for update skip locked` in `WorkerStore.claim()` (`backend/workers/common.py`), logging
`worker.lease.contended`; `seen()/mark_seen()` plus DB unique constraints; per-`agent_handle` card
envelopes (`check_card_limit()` in `backend/finance_agent.py`).

### 2.2 Kill-switch / dry-run hierarchy

Ordered from "most authoritative" to "least". Every default means **do less**.

| Switch | Read by | Default | Behaviour |
|---|---|---|---|
| DB row `engine.settings['engine.dry_run']` | `FinanceStore.is_dry_run()` (`backend/finance_agent.py:352`), `Database.is_dry_run()` (`backend/engine/db.py:194`), `backend/engine/settings.py:75` | seeded `true` (`backend/engine/seeds.py:39`, "Master kill switch for every external effect") | **The live switch.** Fails closed: DB error, missing row or unparseable JSON → `True`. Writers: `engine` CLI (`backend/engine/cli.py:373`), REST (`backend/engine/main.py:544`, logs `engine.dry_run_changed`), Telegram toggle (`backend/engine/telegram_bot.py:143`). |
| Env `ENGINE_DRY_RUN` | `backend/engine/config.py:196`, `backend/finance_agent.py:250` (`dry_run_default`), `backend/supervisor.py:992,1128` | `true` (`.env.example:114`) | An environment *default*, not the spend switch. `engine/config.py:249` warns at startup when it is `false`: "The live switch is the DB row … and it wins over this." `VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.4: setting it false "does nothing on the spend path". |
| Env `SUPERVISOR_ENABLED` | `backend/finance_agent.py:256` | **absent = off** (no default; only `1/true/yes/on` enable) | Gates the in-API auto-approve loop. Off because the standalone `supervisor.py` may also run and two loops double-decide. **Gap:** single-writer is convention only; nothing in code or SQL enforces it (`supervisor_tick()` docstring, `finance_agent.py`). |
| Env `FUNNEL_CHARGES_ENABLED` | `funnel_app/config.py:237` (`_flag`, default `"false"`) | `false` (`funnel_app/.env.example:18`, env template "THE MONEY SWITCH") | `stripe_billing.client_for()` returns `None` unless **both** a funnel secret key is present **and** this flag is true; with `None`, checkout records intent and touches no card. |
| Spend tiers | `route_spend()` / `classify_tier()` in `backend/finance_agent.py` | `FINANCE_TIER1_MAX_MINOR=3000`, `FINANCE_TIER2_MAX_MINOR=25000` | ≤$30 auto, $30–$250 queued for Telegram HITL, >$250 blocked. (Module 1 stack uses $50/$250 via `approval.tier*` settings, `backend/engine/seeds.py`.) |
| Card envelope | `check_card_limit()` | `CARD_DAILY_CAP_MINOR=5000`, `CARD_MONTHLY_CAP_MINOR=50000` per `agent_handle` | Local cap is the real cap: the issuer check fails open (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.4). |
| Supervisor micro-cap | `FinanceSettings` | `SUPERVISOR_MICRO_CAP_MINOR=500`, `SUPERVISOR_ANOMALY_FACTOR=5`, `SUPERVISOR_INTERVAL_SECONDS=45` (clamped 5–3600) | Only micro, healthy-agent, inside-envelope intents are auto-approved; in dry-run the intent is approved without executing. |

Funnel isolation from engine switches: the funnel reads none of `SUPERVISOR_ENABLED`,
`ENGINE_DRY_RUN` or `engine.dry_run`; the env template lists them under "intentionally NOT set
here", and the installer emits a WARN if `SUPERVISOR_ENABLED` appears armed in a funnel `.env`.
`funnel_app/tests/test_telemetry.py` asserts these postures against source text
(`funnel_app/PHASE3_MANUAL_TEST.md` Part 1).

Known behaviour, not a bug: `TxStore.expire_due()` in `backend/supervisor.py` expires pending
intents regardless of `ENGINE_DRY_RUN` (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §3.4).

### 2.3 Cross-account and mode-mismatch guards (funnels)

All in `funnel_app/stripe_billing.py` and `funnel_app/billing.py` (kegeldoc identical).

1. **Key-prefix vs declared mode.** `FunnelKeys.load()` decodes `key_type` from the secret key's
   prefix (test vs live) and raises `StripeMisconfigured` when `FUNNEL_STRIPE_MODE` is set to the
   other mode, when `FUNNEL_STRIPE_MODE` is neither `test` nor `live`, or when the publishable key's
   mode segment disagrees with the secret key's. An absent secret key is *not* an error (rehearsal).
   *Precision note:* this runs when a client is requested (`billing_client()` in `main.py`, called
   by checkout, the webhook, `/ops/send-notices` and `/health`), **not at process import**: at
   import `BillingService` is built with `client=None`. `/health` catches the exception and renders
   it as a finding instead of a 500 (`main.py` `health()`), so a mismatch is visible on the first
   health check after a restart.
2. **Webhook signature first.** `POST /webhooks/stripe` verifies the raw body against
   `STRIPE_WEBHOOK_SECRET_FUNNEL` before parsing (`main.py`, `stripe_billing.verify_signature`).
   Without a secret it accepts unsigned posts with a warning in rehearsal, and logs an **error**
   when charges are enabled; `billing_readiness()` lists the missing secret on `/health`.
3. **`livemode` vs held keys.** `event_from_webhook()` reads Stripe's envelope key `livemode`
   (`stripe_billing.py`, "one word, on the event, not the object"). `BillingService.holds_test_keys()`
   returns **True when there is no client at all**, so a rehearsal app treats every live-mode
   event as another account's money. On any live event while holding test keys, `handle_event()`
   returns `ignored:test-mode-account-mismatch:<type>` and `account_signal()` writes one
   `agent_logs` row `event="mode_mismatch_event"`, `level=warning`, keyed
   `mode_mismatch:<stripe_event_id>`, and keeps it out of every ratio. The guard sits inside
   `account_signal()` itself so no caller can walk past it.
4. **Account type.** `billing_readiness()` flags a secret key whose decoded account type is not
   `standard`/`express` ("Confirm it is not the B2B account's key").
5. **Ignored event types return 200**, not 5xx, so Stripe does not retry forever; only failures
   the app causes return 502.

**Not in the code:** there is no check of the Stripe `account` field on incoming events, and no
per-app tagging of checkouts. The Execution Log (kegeldoc apply, "Open before arming charges on
the shared account") records that if Routine Lab and kegeldoc share one Stripe account, each app
will record the other's disputes/refunds/Radar/expiry events. Solve that before arming charges.

Engine side: `backend/finance_agent.py` verifies Stripe's `t=,v1=` HMAC itself with a 300-second
timestamp tolerance and books exactly one event type (`invoice.payment_succeeded`); it has **no**
`livemode` check. Its idempotency is `financial_ledger_external_uq` (§3.3).

### 2.4 LLM routing and deterministic output guardrails

* **Router:** `backend/llm_router.py` — OpenAI-compatible providers OpenRouter and NVIDIA NIM,
  callers ask for a tier (`fast` / `reasoning`), chains keyed by (provider, tier), providers with
  no key are dropped from the chain, typed errors (`AllProvidersFailed` transient vs
  `LLMRequestRejected` permanent), per-call usage with a fallback price
  (`FALLBACK_COST_PER_MTOKEN = 3.0`) so unreported cost is never free. Env knobs:
  `LLM_ROUTER_FAST_MODELS`, `LLM_ROUTER_REASONING_MODELS`, `LLM_ROUTER_OPENROUTER_MODELS`,
  `LLM_ROUTER_NVIDIA_MODELS`, plus `LLM_MODEL_CHEAP`, `LLM_MODEL_STRONG`, `LLM_FALLBACK_CHAIN`;
  provider keys `OPENROUTER_API_KEY`, `NVIDIA_API_KEY`. **Not enforced by this router:** a spend cap
  ("Cost is not enforced by the router", `VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.4); the
  per-agent budget gate lives in `backend/engine/llm.py` (`llm.daily_cap_usd` setting, seeded 8.0).
* **Loaded by file path:** `funnel_app/llm_flavour.py` `load_router()` uses
  `importlib.util.spec_from_file_location("funnel_llm_router", backend/llm_router.py)`, caches it
  in `sys.modules`, and returns `None` (builder-only mode) if the file or `httpx` is missing.
  `finance_agent.py` imports it as a sibling module.
* **The LLM adds voice, never structure.** `plan_builder.py` decides the plan deterministically;
  the LLM writes an optional coach note (`fast` tier, `max_tokens=220`, 25 s timeout,
  `MAX_COACH_CHARS = 420`, rules in `SYSTEM_RULES`).
* **Reject, never scrub.** The note is passed to `claims.violations()`; any hit logs
  "coach note rejected by copy guardrail" and returns `None`, and the caller renders the
  human-written `builder_coach_note()`. Any error, timeout or empty reply does the same. Nothing
  edits model output into compliance.
* **The scanner:** `funnel_app/claims.py` — `FORBIDDEN_TERMS` (whole-word/prefix, case-insensitive,
  HTML stripped, per line) and `DISCLAIMER_EXEMPTIONS` (complete sentences exempted by exact
  equality, so a claim cannot hide inside a disclaimer). `funnel_copy.guardrail_scan()` scans every
  string the copy module can emit; it runs in `tests/test_copy_guardrails.py` against every template
  and live on `/health`. `FUNNEL_PREVIEW_MODE=local` runs with no LLM at all.

---

## 3. State, Idempotency & Audit Logging (`trial_events` + `agent_logs`)

### 3.1 Funnel tables (SQLite, `funnel_app/telemetry.py` `_SCHEMA`)

```sql
create table if not exists trial_events (
  id                 integer primary key autoincrement,
  occurred_at        text    not null,                 -- second-resolution UTC ISO-8601
  visitor_key        text    not null default '',
  funnel_type        text    not null default 'trial',
  offer_id           text    not null default '',
  test_variable      text    not null default 'baseline',
  event              text    not null,                 -- must be in telemetry.EVENTS
  amount_minor       integer not null default 0,       -- captured money
  gross_volume_minor integer not null default 0,       -- authorised volume (dispute denominator)
  refunded_minor     integer not null default 0,
  card_funding       text    not null default '',      -- credit|debit|prepaid|unknown|''
  card_country       text    not null default '',
  currency           text    not null default 'usd',
  reference_id       text    not null default '',      -- charge / dispute / session id
  detail_json        text    not null default '{}',
  idempotency_key    text
);
create unique index if not exists trial_event_dedupe_uq
  on trial_events (idempotency_key) where idempotency_key is not null;
create index if not exists trial_events_window_idx  on trial_events (occurred_at, event);
create index if not exists trial_events_visitor_idx on trial_events (visitor_key, occurred_at);

create table if not exists agent_logs (
  id              integer primary key autoincrement,
  created_at      text    not null,
  agent_handle    text    not null default 'worker:funnel',
  level           text    not null default 'info',     -- debug|info|warning|error|critical
  event           text    not null,
  message         text    not null default '',         -- truncated to 500 chars on write
  context_json    text    not null default '{}',
  is_audit        integer not null default 0,
  idempotency_key text
);
create index if not exists agent_logs_event_idx on agent_logs (event, created_at);
create unique index if not exists agent_logs_dedupe_uq
  on agent_logs (idempotency_key) where idempotency_key is not null;
```

Write semantics (`TelemetryStore.record()` / `.log()`): `insert or ignore`; returns `True` if this
call wrote the row and `False` for a duplicate key (not an error). An unknown event name or funding
type **raises** (programming error, otherwise a metric silently reads 0). Any other
`IntegrityError` is re-raised so a real bug never masquerades as a redelivery. Rows without a key
append without limit (events that legitimately repeat). Nothing is ever updated: a resolution is a
new row. WAL mode, one connection per call, a process lock.

Other funnel tables in the same file (`funnel_app/billing_store.py`): `funnel_subscriptions`
(PK `visitor_key`), `funnel_notifications` (`unique (visitor_key, kind)` — the send-once marker),
`funnel_save_offer_events`.

### 3.2 Idempotency key catalogue (funnel)

| Key shape | Dedupe meaning | Source |
|---|---|---|
| `{event}:{visitor_key}:{YYYY-MM-DD}` | page step once per visitor per UTC day (`lp_view`, `quiz_started`, …) | `main.py` `count_page_view()` |
| `trial_started:{visitor_key}:{day}`, `cancel_clicked:…:{day}`, `save_offer_selected:…:{day}` | once per visitor per day | `main.py`, `billing.py` |
| `trial_paid:{session_id or visitor_key}` and backfill `trial_paid:{subscription_id}` | one conversion per checkout / subscription | `billing.py` |
| `{event}:{stripe_event_id}` for `dispute_created`, `radar_early_warning`, `refund_issued` | **processor event ID**: a redelivered dispute counts once against the 1% ceiling | `billing.py` `account_signal()` |
| `rebill_declined:{event_id}`, `rebill_succeeded:…`, `rebill_terminal:{visitor}:…`, `checkout_expired:{charge or event id}` | one row per processor object | `billing.py` |
| `mode_mismatch:{stripe_event_id}` | one anomaly row per foreign event, even under retry floods | `billing.py` `account_signal()` |
| `log:{trial_events key}` | the `agent_logs` mirror of a notable event, deduped with it | `billing.py` `track()` |
| `funnel_notifications (visitor_key, kind)` | a renewal notice is recorded **before** it is sent, so a rerun cannot double-send | `billing.py` `send_due_notices()`, `billing_store.mark_notification_sent()` |

### 3.3 Engine tables (Supabase, `supabase/schema.sql`)

`engine.agent_logs`: `id bigint identity PK`, `agent_handle text not null` (deliberately **not** a
foreign key, so a log lands when the registry is down), `hustle hustle_kind`,
`level log_level default 'info'`, `event text` (stable dotted name), `message text not null`,
`context jsonb default '{}'` (the approval/transaction/Stripe-event this line is about),
`is_audit boolean default false`, `created_at timestamptz default now()`. Indexes: by
`(agent_handle, created_at desc)`; partial on errors (`level in ('error','critical')`); partial on
`is_audit`; partial on `(event, created_at desc) where event is not null`. **No idempotency key
column** in the engine version: callers that need "once" dedupe by querying markers first
(e.g. `finance_agent.py` Telegram fan-out, `workers/common.py`).

`engine.trial_events` (created by `backend/workers/funnel_worker.py`, not in `schema.sql`):
`visitor_key`, `funnel_type`, `offer_id`, `status check in ('started','paid','cancelled')`,
`occurred_at`, `amount_minor bigint`, `currency` (3 upper-case letters), `sequence_id`,
`idempotency_key`, with the same partial unique index `trial_event_dedupe_uq`.

Money tables: `financial_ledger` is append-only, enforced by triggers
(`ledger_reject_mutation`) **and** by `revoke update, delete` from `engine_runtime` /
`engine_dashboard`; `financial_ledger_external_uq (external_provider, external_id) where
external_id is not null` is the processor-event-ID net used by
`finance_agent.py` (`on conflict … do nothing` → "already posted"). `transactions` has the same
external unique index plus `transactions_idempotency_uq` (crash-and-retry replay guard).

### 3.4 What `agent_logs` records vs the business fact tables

* **Fact tables** (`trial_events`, `funnel_subscriptions`, `financial_ledger`, `transactions`)
  are what ratios, money and state are computed from.
* **`agent_logs`** records what an agent or human must *see as a named event*: in the funnel,
  only `AGENT_NOTABLE_EVENTS = (refund_issued, dispute_created, radar_early_warning)` plus
  `mode_mismatch_event`; in the engine, approvals, `engine.dry_run_changed`, supervisor heartbeats,
  Telegram inbound messages and alert-dedupe markers, worker errors (`agent_logs` writes in
  `finance_agent.py`, `supervisor.py`, `workers/common.py`). Audit rows (human actions, approval
  decisions) set `is_audit`.
* `backend/engine/audit.py` writes are best-effort by design (a log write must never break the
  caller) and scrub secrets on the way in, because agent-authored context "regularly contains a URL
  with a token in it, and agent_logs is readable by a browser".

### 3.5 Telemetry is recording-only

* `funnel_app/telemetry.py` makes **no network call**; it writes two SQLite tables and returns
  (`PHASE3_MANUAL_TEST.md` Part 1).
* `BillingService.track()` swallows every telemetry exception, logs a warning and returns `False`:
  "telemetry must never break a purchase". Every call site sits **after** the state change it
  describes.
* No reader of telemetry feeds a charge decision: "The database can be emptied and the funnel
  behaves identically" (`PHASE3_MANUAL_TEST.md`). The renewal-notice dedupe uses
  `funnel_notifications`, not telemetry.
* The page-view middleware runs after the response, only for status < 400, inside `try/except`.
* The **only** outbound call near telemetry is in the export script (§4.4): an optional read-only
  Stripe cross-check, skipped with `--offline` and for any day other than today.

---

## 4. Operator Interface & Reusable Scaffolding Checklist

### 4.1 `/health` self-diagnostics

* **Funnels** — `GET /health` (`funnel_app/main.py` `health()`) renders one HTML page with: copy
  guardrail scan result, disclosure coverage, an offline self-check render of the paid artefact
  ("All 11 artefact checks present"), plan/meal audit, LLM router availability and routing table
  (`router.describe()`, no keys), billing state (charges flag, key present/type/account type,
  webhook secret present, mail transport, coupon configured), `billing_readiness()` sentences
  (each a fix an operator can act on), DB path and counts, and whether the session secret is
  ephemeral. A misconfigured key is a finding on the page, never a 500.
* **Finance agent** — `GET /health` (`backend/finance_agent.py:1088`) returns JSON
  `{ok, db, dry_run, issuer_configured, llm}`; `GET /llm/routing` shows the model chain.
* **Compose engine** — Caddy proxies `/health` to `api:3100` without auth; healthcheck
  `curl -fsS http://127.0.0.1:3100/health` (`docker-compose.yml`).

### 4.2 `/ops/...` idempotent sweeps

`POST /ops/send-notices` (`funnel_app/main.py`) runs `BillingService.send_due_notices()` in a
thread and returns `{sent, failed, transport, detail}`. It is called by
`funnel-notices.service` with `curl --fail --max-time 120 -X POST http://127.0.0.1:8090/ops/send-notices`,
so a 4xx/5xx becomes a failed unit. A timer, a human `curl` and a retry all hit the same function,
which records each send before attempting it.

**Gap — no auth on operator routes.** `/ops/send-notices`, `/health`, `/dev/responses` and
`/dev/docs` have no authentication or loopback check in the app, and the tunnel forwards the whole
hostname. The sweep is idempotent (worst case: an early notice for an already-due period), but
`/dev/responses` and `/health` expose operational data. When replicating, restrict `/ops/*` and
`/dev/*` (loopback check, Cloudflare Access, or a shared-secret header). The compose stack shows
the pattern: Caddy puts `basic_auth` on everything except `/health` and `/webhooks/*`
(`deploy/Caddyfile`).

### 4.3 Outbox `.eml` previews before SMTP

`funnel_app/mailer.py`: transport chosen once at construction — `FUNNEL_SMTP_URL` set → SMTP
(`smtp://` STARTTLS / `smtps://` implicit TLS, stdlib `smtplib`); unset → write the full RFC-822
message to `FUNNEL_EMAIL_OUTBOX/<stamp>-<kind>-<recipient>.eml`. "What was reviewed is what would
have been delivered." `send_all()` reports per-message failure instead of raising, so a mail
outage never fails a cancellation. Receipt/notice text reuses `claims.price_line`, the same sentence
checkout renders.

### 4.4 Stdlib-only CLI metric scripts

`scripts/export_lane_c_metrics.py` (imports: stdlib only — `argparse csv json os re sqlite3
subprocess …`):

* Opens the app's SQLite files **read-only** (`mode=ro` URI) and writes one CSV; never writes the DB,
  never spends, never sends.
* `--day YYYY-MM-DD|today|yesterday|Ndaysago` (word forms avoid `%F` / shell in units), `--since/--until`,
  `--csv`, `--stdout`, `--json`, `--offline`, `--fail-on-ceiling`.
* Ceilings: `dispute_cb_pct` ≤ 1.0, `fraud_pct` ≤ 0.5; `LOW_VOLUME_BELOW = 250` is reported but
  never suppresses a breach. Zero denominators produce blanks, never 0 or 100.
* Exit codes: **0** exported, **1** nothing exported (no rows / no telemetry table), **2** a ceiling
  tripped with `--fail-on-ceiling` (prints `CEILING BREACH: …` to stderr). The systemd unit turns 2
  into a failed unit.
* Append is idempotent per `(day, offer_id, test_variable)`; hand-typed columns on other rows are
  preserved.
* Optional Stripe cross-check: `GET` only via `curl`, key from `STRIPE_READ_KEY_FUNNEL` or
  `STRIPE_SECRET_KEY_FUNNEL`, sent as a Basic auth header (never in the URL), max 10 pages / 1000
  objects, only for today, skipped by `--offline`.
* Multi-app: `FUNNEL_APP_DIR=<repo>/kegeldoc_app` points the shared script at another app
  (`deploy/kegeldoc_app/kegeldoc-metrics.service`).

### 4.5 Other operator surfaces (engine)

* Telegram: `POST /webhooks/telegram` in `finance_agent.py` (inline approve/reject; writers limited
  to `approve_transaction` / `reject_transaction` behind `can_write`, per
  `VALIDATION_AND_AGENT_ASSIST_PLAN.md` §2.4). `TELEGRAM_WEBHOOK_SECRET` ships empty — set it before
  approvals become a money path. **Known conflict:** `sirius-supervisor` still long-polls while the
  webhook is registered, producing a benign `409 Conflict` (`EXECUTION_PLAN.md` §0).
* `POST /spend/gate`, `GET /spend/envelope`, `POST /webhooks/stripe/dryrun` (`finance_agent.py`).
* `engine` CLI (`backend/engine/cli.py`) for settings such as `engine.dry_run`.

### 4.6 Starter checklist for a new isolated agent / worker

Copy this list into the new agent's README and tick it before the first deploy.

**Identity & ownership**
- [ ] Unique handle (e.g. `worker:<lane>` / `<app>-agent`); if it touches the engine, register it in
      `engine.agents` **before** deploy (`VALIDATION_AND_AGENT_ASSIST_PLAN.md` §3.4 item 3).
- [ ] One-line "owns / must never touch" row added to `EXECUTION_PLAN.md` §1.1.
- [ ] Unique `job` name per lane-venture pair if it uses worker leases.

**Isolation**
- [ ] Own directory `<app>/`, own `.venv`, no import of `backend/engine`; shared code loaded by file path only.
- [ ] Own port, bound to `127.0.0.1`; confirm it is not 8000, 8090, 8091, 3100 (or any other live listener).
- [ ] Own Tunnel hostname (not a path prefix); merged into `/etc/cloudflared/config.yml` before the catch-all; `ingress validate` clean; backup of the old config.
- [ ] Own state under `/var/lib/<app>` via `StateDirectory=`; every DB/outbox/CSV path in `.env` is absolute and under it.

**Configuration & secrets**
- [ ] `.env.example` documents every knob with safe defaults ("missing means do less").
- [ ] Production `<app>.env.template` carries only what differs from defaults; secrets commented out; `.env` mode 600, owned by the service user, git-ignored.
- [ ] `EnvironmentFile=` with **no** `-` prefix; arming overrides go in a `systemctl edit` drop-in.
- [ ] No secret in code, unit files, logs, URLs or this repo (CLAUDE.md rule 2).

**Safety switches**
- [ ] A money/effect switch that defaults off (pattern: `FUNNEL_CHARGES_ENABLED`) and a client factory that returns `None` unless the switch **and** the key are both present.
- [ ] If it acts through the engine: respect `engine.settings['engine.dry_run']` (fail closed), go through `route_spend` tiers and the per-handle card envelope; never set `SUPERVISOR_ENABLED`.
- [ ] Key-mode validation (declared mode vs key prefix vs publishable key) and a `livemode` vs held-keys guard on every webhook path, with foreign events logged as a named anomaly and excluded from metrics.
- [ ] Webhook: verify raw-body signature first; 200 for ignored types, 5xx only for self-caused failures.
- [ ] LLM output: deterministic builder first, LLM decorates only, guardrail scan **rejects** to a human-written fallback; tests scan every template.
- [ ] Tests that assert the posture against source text (pattern: `funnel_app/tests/test_telemetry.py`).

**State & audit**
- [ ] Fact table(s) with a nullable `idempotency_key` + partial unique index; keys built from processor event IDs or `{event}:{visitor}:{utc_day}`.
- [ ] An `agent_logs` mirror only for events a human must see; `is_audit` for human/approval actions; append-only.
- [ ] Telemetry writes wrapped so they can never fail the business action, placed after it, with no outbound calls and no reader in a money path.
- [ ] Send-once markers recorded **before** the side effect.

**Operator surface**
- [ ] `GET /health` that renders guardrail state and readiness sentences, never 500s on misconfig.
- [ ] `POST /ops/<sweep>` idempotent routes, called by a systemd timer via loopback `curl --fail`.
- [ ] `/ops/*` and `/dev/*` protected (the current funnels do **not** do this; see §4.2).
- [ ] Outbox transport by default; SMTP only when `*_SMTP_URL` is set.
- [ ] Stdlib-only metrics CLI, read-only DB, exit codes 0/1/2, `--fail-on-ceiling`, `--offline`.

**Deploy & record**
- [ ] Installer that is plan-only by default, renders and token-checks units, verifies with `systemd-analyze`, refuses to start on blockers, restarts on redeploy and checks the PID changed, and starts timers.
- [ ] Timers with `Persistent=true` and staggered `OnCalendar` slots.
- [ ] Smoke: `systemctl status`, `curl 127.0.0.1:<port>/health`, external hostname 200, direct `http://<vps-ip>:<port>` gets no answer, neighbour hostnames still 200.
- [ ] Execution Log entry in `SIRIUS_MASTER_BLUEPRINT.md` with timestamp, commit hash, what, and verification (CLAUDE.md rule 4).

### 4.7 Directory template

```text
<repo>/
├── <app>/                          # e.g. funnel_app/, kegeldoc_app/
│   ├── .env.example                # every knob, safe defaults (tracked)
│   ├── .env                        # real values, mode 600 (git-ignored)
│   ├── .venv/                      # own interpreter (git-ignored)
│   ├── config.py                   # env-only settings; load_dotenv(app .env, then root .env), process env wins
│   ├── main.py                     # FastAPI app: routes, /health, /ops/*, /webhooks/*
│   ├── <processor>_billing.py      # key loading + mode validation, client_for() -> None unless armed
│   ├── billing.py                  # business logic; webhook handling; mode-mismatch guard; track()
│   ├── billing_store.py            # SQLite facts + send-once markers
│   ├── telemetry.py                # trial_events + agent_logs, idempotent, no network
│   ├── claims.py                   # forbidden terms + exact-sentence exemptions
│   ├── llm_flavour.py              # loads backend/llm_router.py by path; reject-to-fallback
│   ├── mailer.py                   # outbox .eml by default, SMTP when configured
│   ├── templates/  static/
│   └── tests/                      # posture, guardrail, billing, telemetry tests
├── deploy/<app>/
│   ├── README.md                   # runbook, step by step
│   ├── install-<app>.sh            # plan-only by default, --apply as root
│   ├── <app>.env.template          # production deltas only, secrets commented
│   ├── <app>.service               # 127.0.0.1:<port>, StateDirectory=<app>, EnvironmentFile= (no '-')
│   ├── <app>-notices.service/.timer   # curl --fail POST /ops/<sweep>, Persistent=true
│   ├── <app>-metrics.service/.timer   # scripts/<metrics>.py --day yesterday --fail-on-ceiling
│   └── cloudflared-ingress.yml     # the rule to merge, not a whole config
└── scripts/<metrics>.py            # stdlib-only, read-only DB, exit 0/1/2
```

Runtime layout on the VPS:

```text
/var/lib/<app>/            (StateDirectory, 0750, service user)
├── responses.sqlite3
├── billing.sqlite3        (+ -wal / -shm)   # facts, markers, trial_events, agent_logs
├── outbox/*.eml
└── metrics/<lane>_daily.csv
/etc/systemd/system/<app>*.service|*.timer
/etc/cloudflared/config.yml                 # one hostname rule per app, catch-all last
```

---

## Appendix: environment variable index (names only)

Funnel (`funnel_app/config.py`, `stripe_billing.py`, kegeldoc adds `FUNNEL_INTRO_DISCOUNT_BPS`,
`FUNNEL_INTRO_DISCOUNT_MINUTES` and `FUNNEL_PLAN_*`): `FUNNEL_BRAND_NAME`, `FUNNEL_PUBLIC_URL`,
`FUNNEL_HOST`, `FUNNEL_PORT`, `FUNNEL_PREVIEW_MODE`, `FUNNEL_LLM_TIMEOUT_SECONDS`,
`FUNNEL_CHARGES_ENABLED`, `FUNNEL_TRIAL_AMOUNT_MINOR`, `FUNNEL_TRIAL_DAYS`,
`FUNNEL_REBILL_AMOUNT_MINOR`, `FUNNEL_CURRENCY`, `FUNNEL_REFUND_WINDOW_DAYS`,
`FUNNEL_LITE_AMOUNT_MINOR`, `FUNNEL_PAUSE_DAYS`, `FUNNEL_DISCOUNT_COUPON_ID`,
`FUNNEL_SAVE_OFFER_ENABLED`, `FUNNEL_SMTP_URL`, `FUNNEL_EMAIL_FROM`, `FUNNEL_EMAIL_OUTBOX`,
`FUNNEL_PREBILLING_NOTICE_DAYS`, `FUNNEL_DECLINE_RETRY_DAYS`, `FUNNEL_DB_PATH`,
`FUNNEL_BILLING_DB_PATH`, `FUNNEL_SESSION_SECRET`, `FUNNEL_SUPPORT_EMAIL`, `FUNNEL_METRICS_CSV`,
`FUNNEL_APP_DIR`, `FUNNEL_STRIPE_MODE`, `STRIPE_SECRET_KEY_FUNNEL`, `STRIPE_PUBLISHABLE_KEY_FUNNEL`,
`STRIPE_WEBHOOK_SECRET_FUNNEL`, `STRIPE_READ_KEY_FUNNEL`.

Engine / finance agent (`backend/finance_agent.py`, `backend/engine/config.py`, `.env.example`):
`DATABASE_URL`, `ENGINE_DRY_RUN`, `SUPERVISOR_ENABLED`, `SUPERVISOR_INTERVAL_SECONDS`,
`SUPERVISOR_MICRO_CAP_MINOR`, `SUPERVISOR_ANOMALY_FACTOR`, `FINANCE_TIER1_MAX_MINOR`,
`FINANCE_TIER2_MAX_MINOR`, `CARD_DAILY_CAP_MINOR`, `CARD_MONTHLY_CAP_MINOR`,
`FINANCE_APPROVAL_EXPIRY_MINUTES`, `FINANCE_LEDGER_ACCOUNT`, `DEFAULT_CURRENCY`,
`TELEGRAM_APPROVAL_NOTIFY_FLOOR_MINOR`, `FINANCE_AGENT_HOST`, `FINANCE_AGENT_PORT`,
`FINANCE_AGENT_URL`, `FINANCE_AGENT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `WEBHOOK_SIGNING_SECRET`,
`OPENROUTER_API_KEY`, `NVIDIA_API_KEY`, `ALLOW_LOCAL_SUDO`, `API_BIND_IP`, `BACKEND_PORT`,
`CADDY_ADMIN_EMAIL`, `CADDY_OWNER_HASH`, `ENGINE_DOMAIN`, and the `LLM_*` router names in §2.4.
