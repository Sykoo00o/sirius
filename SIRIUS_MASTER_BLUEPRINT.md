# Sirius Autonomous Operations — Master Blueprint & Next-Phase Roadmap

This document captures the complete current state of the Sirius multi-agent engine, infrastructure architecture, verification steps, and the architectural specification for upgrading the Telegram integration from simple outbound approval alerts into a fully interactive conversational command agent.

---

## 1. System Architecture & Current State

### Infrastructure & Stack
* **Backend**: FastAPI app running on Oracle Cloud Ubuntu 24.04 ARM64 VPS (`132.145.27.197`), managed via systemd (`sirius-finance`) on port `8000`.
* **Database**: Supabase PostgreSQL featuring the `engine` schema (`engine.financial_ledger`, `engine.transactions`, `engine.agent_logs`, `engine.settings`).
* **Frontend**: Tailwind-styled operations dashboard hosted on Cloudflare Pages (`https://sirius-v68.pages.dev/`), polling live metrics every 5 seconds.
* **Networking**: Cloudflare Tunnel bridging backend routes (`/api/status`, `/api/actions/*`, `/webhooks/stripe`) securely.

### Verified Working Components
1. **Live Ledger & Spend Tracking**: Supabase aggregates net financial position and monthly spend, feeding real-time metrics (`-$45.00` ledger balance, `$45.00` monthly spend) directly to the dashboard.
2. **Command Deck**: Dashboard buttons execute live backend actions (`/api/actions/supervisor-tick`, `/api/actions/telegram-test`).
3. **Outbound Telegram Alerts**: Successfully transmits real-time approval requests and test pings to chat ID `1489331008` using `httpx` and the Telegram Bot API.

---

## 2. Roadmap: Making Claude Code the Autonomous Driver

Moving forward, manual file edits and server SSH commands should be handled entirely via **Claude Code** running in your local terminal workspace linked to your Git repository and SSH tunnel.

### Standard Operating Protocol for Claude Code:
1. **Code Changes**: Claude Code edits files locally (`frontend/index.html`, `backend/finance_agent.py`, `backend/supervisor.py`).
2. **Deployment Sync**: Claude commits and pushes changes (`git push origin main`), or syncs directly to the VPS via rsync/SSH.
3. **Service Management**: Claude executes remote commands (`sudo systemctl restart sirius-finance`, `journalctl -u sirius-finance -n 50`) to verify error-free execution.

---

## 3. Architecture Spec: Interactive Telegram Conversational Agent

To turn your Telegram bot into an active conversational assistant (capable of answering queries about spend, triggering agent loops on demand, and querying Supabase data), we need to transition from an outbound-only webhook/alert sender to a **Telegram Long-Polling or Webhook Event Loop** equipped with natural language intent routing.

### Architecture Options
* **Option A: Telegram Webhook (Recommended for FastAPI)**: Point Telegram's webhook to a new FastAPI route (`POST /webhooks/telegram`), allowing FastAPI to handle incoming messages instantly without maintaining an active background polling thread.
* **Option B: Background Polling Worker (`telegram_bot.py`)**: A standalone Python worker using `python-telegram-bot` or raw `httpx` polling `getUpdates` asynchronously, mirroring `supervisor.py`.

### Required Telegram Bot Capabilities
1. **`/status` or Text Queries**: Ask *"What is our monthly spend?"* or *"Are our agents healthy?"* $\rightarrow$ Bot queries Supabase ledger via `FinanceStore` and replies with live metrics.
2. **Command Triggers**: Type `/tick` $\rightarrow$ Bot executes a manual supervisor cycle.
3. **Interactive Buttons**: Inline keyboards for Tier 2 approval requests (`✅ Approve $45` / `❌ Reject`), capturing callback queries (`callback_query`) and updating transaction status in Supabase.

---

## 4. Implementation Plan for the Interactive Telegram Agent

### Step 1: Add Telegram Webhook / Listener Route in FastAPI (`backend/finance_agent.py`)

Add a new endpoint to handle incoming Telegram updates:

```python
@app.post("/webhooks/telegram")
async def telegram_webhook(update: dict[str, Any] = Body(...), store: FinanceStore = Depends(get_store)):
    """Receives incoming messages and inline button callbacks from Telegram."""
    message = update.get("message")
    callback = update.get("callback_query")
    
    bot_token = env("TELEGRAM_BOT_TOKEN") or env("BOT_TOKEN")
    if not bot_token:
        raise HTTPException(400, "Bot token unconfigured")

    async def send_reply(chat_id: int, text: str):
        url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){bot_token}/sendMessage"
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

    if message:
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        
        if text.startswith("/status") or "spend" in text.lower():
            today, month = store.spend_totals(AGENT_HANDLE, "USD")
            await send_reply(chat_id, f"📊 *Sirius Status Report*\n• Monthly Spend: `$${month / 100:.2f}`\n• Active Agents: `3 / 4 online`")
        elif text.startswith("/tick"):
            store.log("INFO", "TICK", "Manual trigger via Telegram chat command.", is_audit=True)
            await send_reply(chat_id, "⚡ Supervisor tick executed successfully.")
        else:
            await send_reply(chat_id, "🤖 Sirius Agent online. Commands: `/status`, `/tick`")

    elif callback:
        chat_id = callback["message"]["chat"]["id"]
        data = callback.get("data", "")
        # Handle approval callbacks here (e.g., approve_tx_<id>)
        await send_reply(chat_id, f"✅ Action processed: `{data}`")

    return {"status": "ok"}
```

---

## Execution Log & Progress

<!-- Progress entries appended below. Format per CLAUDE.md Rule 4:
### YYYY-MM-DD HH:MM — <short title>
- **Git commit:** <hash> (or `pending` until committed)
- **Implemented:** <what changed, file(s)>
- **Verified:** <syntax check, systemd restart, API response, etc.>
-->
### 2026-11-18 19:40 UTC — Interactive Telegram webhook deployed (Option A)
- **Git commits:** `430c87b` (webhook feature + agent_logs fix), `db15473` (legacy Bot API URL default), `92adc39` (unsigned-updates warning) — pushed to `origin/main` (Sykoo00o/sirius)
- **Implemented:** `POST /webhooks/telegram` in `backend/finance_agent.py`: shape validation, `X-Telegram-Bot-Api-Secret-Token` check, owner-chat gate (`TELEGRAM_APPROVAL_CHAT_ID`, plus optional `TELEGRAM_ALLOWED_IPS`), `/status` (live net position, today/month spend, pending queue via `FinanceStore`), `/tick`, `/pending`, `/approve|/reject <uuid>`, inline `appr:`/`rej:` `callback_query` approvals with the same race-safe still-pending semantics as `supervisor.py`, HTML replies over httpx, background processing so Telegram never retries on slow I/O. Fixed the blueprint's mangled sendMessage URL. Two latent bugs found by testing: `FinanceStore.log()` cast `::log_level` onto `agent_handle` (every `agent_logs` write from this module failed silently), and V3 header-auth defaulted on, sending `/botsendMessage` (token stripped from URL) which Telegram 404s for this token — legacy URL is now the default, V3 opt-in.
- **Verified:** `py_compile` clean; offline mock suite (routing, auth gates, callbacks, escaping); VPS `git pull` + `systemctl restart sirius-finance` → `active`, `/health` 200; route present in live `openapi.json`; inbound update from Telegram's egress answered `200`; outbound `sendMessage` to `api.telegram.org/bot<token>/sendMessage` → **`200 OK`** (was 404). Root-caused the reported 404 as (a) historical: webhook pointed at a dead trycloudflare quick tunnel before the route existed, and (b) live: outbound V3 URL. Stale webhook deleted via `deleteWebhook`; `getWebhookInfo` url now empty — **next step: register webhook on the permanent Cloudflare Tunnel hostname with `secret_token=$TELEGRAM_WEBHOOK_SECRET`** (currently unset on the VPS, so unsigned mode is active with a loud warning).

### 2026-11-18 20:10 UTC — Interactive inline approval cards (approve_/deny_) + live gate fix
- **Git commits:** `42073c4` (cards + editMessageText + /approvals/{id}/notify), `050bafd` (psycopg3 `%s1` placeholder fix) — pushed to `origin/main`
- **Implemented:** pending tier-2 intents now push an outbound card (httpx sendMessage) with inline `Approve ✅` / `Deny ❌` (`callback_data approve_<uuid>` / `deny_<uuid>`); amounts over $50 (`TELEGRAM_APPROVAL_NOTIFY_FLOOR_MINOR` env / `telegram.approval_floor_minor` DB setting) carry a ⚠️ high-value marker. Tap flow: regex-validate callback → race-safe decision UPDATE (`approved`+`authorized` / `rejected`+`cancelled`) → `editMessageText` rewrites the original card to final ledger state + UTC timestamp and clears buttons; `answerCallbackQuery` toast; agent_logs audit (`approval.alert_sent` w/ message_id, `approval.decided`). Callback parser accepts BOTH the new `approve_/deny_` and supervisor's live `appr:/rej:` vocabularies so in-flight cards stayed tappable. New `POST /approvals/{tx_id}/notify` resends a card for any still-pending row. All new I/O async (to_thread for psycopg).
- **Verified:** offline suite (grammar both vocabularies incl. malformed rejects; builders; tap→answer→edit sequence with button-clearing assert; notify incl. >$50 marker and 404-on-decided); VPS pull+restart active; LIVE E2E: `/spend/gate` $42 tier2 → pending row + `approval.alert_sent` card delivered; simulated owner tap → row flipped `approved/authorized/telegram:1489331008` + approved_at; re-tap idempotent (no state change); audit rows correct. Two live-only truths: `/spend/gate` had NEVER successfully written a row in prod before this (psycopg2-ism `%s1` placeholders → `IndeterminateDatatype`, fixed and reproduced against live PG16), and the VPS daily card cap is $50 so E2E spend must stay inside the envelope. editMessageText's own 400 "chat not found" on synthetic message_ids is expected (fabricated card), and the failure path proved clean: decision recorded, error logged, no crash.

### 2026-11-18 22:00 UTC — Autonomous supervisor loop (in-process, opt-in)
- **Git commit:** `f610c9e` — pushed to `origin/main`
- **Implemented:** `run_supervisor_loop()` in `backend/finance_agent.py`, started from the FastAPI lifespan (clean start/cancel on shutdown) when `SUPERVISOR_ENABLED=true` — **default OFF** on the VPS so the standalone `supervisor.py` PTB tick stays the sole auto-deciding writer (two loops would double-decide). `supervisor_tick()`: scans pending spend + agent health each pass; auto-approves micro intents (≤ `SUPERVISOR_MICRO_CAP_MINOR`, default $5) when the agent is healthy and inside the daily card envelope; flags anomalies for HITL (amount ≥ `SUPERVISOR_ANOMALY_FACTOR`× micro cap, agent error in window, age > 24h, envelope breach) with hourly re-card dedupe, 12h escalation, and expiry past `approval_expiry_minutes`; per-tick error digest excluding its own rows. Notifications (Telegram, best-effort): anomaly re-cards ≤3 per tx, ⏳ escalation cards, error-digest alert throttled to digest-change-or-15min — all deduped through append-only `agent_logs` marker rows (snapshot taken before writes so autocommit can't self-compete). Resilience: per-tick `wait_for` timeout, exponential backoff floored at the configured interval with 600s ceiling, interval re-read per tick (live-tunable via `supervisor.interval_seconds` DB setting), structured `supervisor.tick` audit row; `CancelledError` propagates — the loop can never crash the API process.
- **Verified:** `py_compile` clean; offline rule/dedupe suite (2 micro-approves, 5 anomaly flags, escalation + expiry counting, hourly re-card dedupe incl. stale-marker re-alert proof); resilience suite (3 injected failures → 5/10/20s ladder → recovery notify); lifespan suite (`started → cancelled-cleanly`, task cleared); legacy webhook regression re-run green (tap → `editMessageText`, buttons cleared, duplicate-safe). VPS: `git pull --ff-only` (050bafd→f610c9e), `systemctl restart sirius-finance` → `active`, `/health` 200, journal shows `supervisor=False` (dark, as intended until `SUPERVISOR_ENABLED=true` is set in `~/Sirius/.env`, which also retires the standalone supervisor's sole-writer role). Note: the regression fixture initially faked `missing` outcomes because the route consumes `Depends(get_store)` — a second `FinanceStore` built during lifespan — so class-level patching is required for offline tests (test-only lesson, no product change).

### 2026-11-18 23:15 UTC — Multi-provider LLM router + plain-English Telegram agent
- **Git commits:** `21e4b71` (router + agent + tests), `cd436d4` (`backend/tests/conftest.py`) — local (`origin/main` push pending)
- **Implemented:** (1) `backend/llm_router.py` (new): async wrapper over OpenAI-compatible chat completions for **OpenRouter** (`https://openrouter.ai/api/v1`) and **NVIDIA NIM** (`https://integrate.api.nvidia.com/v1`), `tier="fast"` (cheap: llama-3.3-70b / claude-haiku) vs `tier="reasoning"` (frontier: claude-sonnet), `tools` + `tool_choice="auto"` by default, `json_mode`, per-provider base-URL overrides. Four design decisions: model chains keyed by **(provider, tier)** not tier alone (NIM lists no Anthropic slugs, so a tier-only table would 404 this repo's own `LLM_MODEL_STRONG=anthropic/claude-sonnet-5.5` on every reasoning call — those legacy env names now fold into the OpenRouter chain only, letting one `.env` drive both routers); a provider with no key is dropped, never probed, and placeholder values (`changeme`, `TODO`, `your-…`) read as unset; typed errors so timeouts/429/404/HTML-gateway bodies rotate with capped exponential backoff while a **401 aborts immediately** (`credential_failure`) instead of burning the chain; cost falls back to a documented rate when a provider reports none. `get_llm_completion()` exported for the spec's call signature; `describe()` / `GET /llm/routing` expose the live routing table with no secrets. Standalone module (stdlib + httpx) so it cannot entangle with `engine/llm.py`'s per-agent budget gate. (2) Conversational Telegram path in `backend/finance_agent.py`: unmatched `message.text` now reaches the model instead of returning help text (`handle_telegram_message` signals "unmatched" with `""`), system prompt defines Sirius as the autonomous financial supervisor and is **generated to match the tools actually handed over**, tools `get_pending_transactions` / `approve_transaction` / `reject_transaction` executed in-loop against `FinanceStore` with `agent_logs` audit (`llm.tool_executed` / `llm.tool_refused`), and id-resolution refusing (no write) on invented, ambiguous, too-short or absent ids, with wording that says "was already approved" rather than "not found". Read-only tools by default; writers are opt-in per caller. `TG_LLM_ENABLED` kill switch; `/health` gained an `llm` block distinguishing `configured` from `enabled`; lifespan closes the shared client. (3) Bounded turn: `MAX_CHAT_TOOL_STEPS` round-trips plus `MAX_CHAT_CONTEXT_CHARS` bytes, both recomputed from the message list before each request rather than accumulated (assistant/tool scaffolding has size too), and hitting the ceiling **withdraws the tools** — a model still able to call one busts the reserve anyway. Per-tool-result budget takes a fraction of what is left and shrinks the largest nested list from the tail, so a wide payload can neither blow the turn nor arrive as sliced-invalid JSON.
- **Verified:** `py_compile` + `ruff` at the committed baseline (4 pre-existing findings, no new ones); **57 offline tests** (`backend/tests/test_llm_router.py`, `backend/tests/test_finance_chat.py`) incl. wire-format asserts for both providers (URL, `Bearer`, `X-Title`, `usage.include`, message order), error taxonomy, tier→provider resolution, no-Anthropic-on-NIM, hallucinated/garbled/duplicate-id refusals asserting **zero writes**, and reply shape asserted on the `sendMessage` payload. **LIVE OpenRouter** (NIM key still absent from `~/Sirius`/local `.env`, contrary to the task brief): three owner turns answered with real ledger context (`$4.20 of $500.00`), 5 calls / 5 attempts / 0 failures; degraded turn when the ledger tool raised (`AttributeError` on a stubbed store) reported the failure honestly rather than inventing rows. `GET /health` 200 with `llm.configured=true, enabled=true`; `GET /llm/routing` 200 showing OpenRouter chains alone with the key absent from the body, and with `NVIDIA_API_KEY` set it lists both catalogues separately. **Counter-proof of the parse-mode fix against Telegram's own entity rule:** `/approve <transaction id>` sent with `parse_mode=HTML` is refused — `400 can't parse entities at byte offset 22` — so model prose now ships with parsing off, carried on a `ChatReply` so the keyword router's markup keeps rendering; the previous behaviour also emitted the literal `/approve &lt;id&gt;`, equally broken when pasted back. Also fixed: a lone `<` in inbound text corrupted the error reply about that failure (raw text escaped into a malformed entity — a reply failing to send about a reply failing to send). `.env` / `.env.example` gained the router + `TG_LLM_ENABLED` blocks (`NVIDIA_API_KEY=` left empty, reads as unset). Suite reproducibility fixed in `cd436d4`: `backend/tests/` had never been tracked, `pytest backend/tests` died at import (bare-name imports resolve only from `backend/`), and `engine.config` loading a live `.env` meant a routing test could pass on a keyless laptop and fail on one with `NVIDIA_API_KEY` set — now keys/endpoints/chains are blanked per test with `llm_router.configure()` re-run afterwards, since the router caches its provider list. Verified green from repo root and from `backend/`, in both file orders and isolated.

### 2026-11-18 22:50 UTC — VPS: "I don't understand" root-caused and plain-English chat went live
- **Git commit:** `deefaca` (pushed `5774b07..deefaca` to `origin/main`); VPS `~/Sirius` pulled `5774b07 → deefaca`
- **Root cause:** the VPS ran `5774b07`, which predates the feature entirely: `backend/llm_router.py` did not exist on the box, and `grep -c "handle_telegram_plain_english|llm_router" backend/finance_agent.py` returned **0**. Unmatched text hit the single static line `🤖 I don't know <code>…</code> yet.` That string was the *only* reachable reply for free text, so no env flag could have changed it. The two diagnoses proposed in the brief were both disproven first: (1) `TG_LLM_ENABLED` was indeed absent from `/home/ubuntu/Sirius/.env`, but adding it to the old build changes nothing (nothing reads it); (2) the `python3 -c "import os, dotenv…"` probe failed with `ModuleNotFoundError: No module named 'dotenv'`, which is **not** a key-loading failure — `sirius-finance.service` sets `EnvironmentFile=/home/ubuntu/Sirius/.env`, so systemd injects the file straight into the process and `load_dotenv()` is never in the runtime path at all. The system `python3` has no `python-dotenv`; the app's `.venv` does, and with it the key loads: `OpenRouter Key Present: True`.
- **Implemented (ops, no code change):** pushed the 4 unpushed commits; on the VPS backed up `.env` (`.env.bak.20261118-224624`), `git pull --ff-only`, appended the router block with `TG_LLM_ENABLED=true`, `sudo systemctl restart sirius-finance`.
- **Verified:** `/proc/$MainPID/environ` proves the running process sees `TG_LLM_ENABLED=true` and `OPENROUTER_API_KEY` (the honest first read of the *pre-restart* PID showed it absent, which is what justified the restart); `/health` → `llm = {configured: true, enabled: true, providers: [openrouter], fast_model: anthropic/claude-haiku-5.5, reasoning_model: anthropic/claude-sonnet-5.5}`; `/llm/routing` → 200 with both tier chains; journal for a posted plain-English update shows `POST /webhooks/telegram 200` → `POST https://openrouter.ai/api/v1/chat/completions 200 OK` → `POST api.telegram.org/…/sendMessage 200 OK`, i.e. the model answered and Telegram delivered it. Remaining path to the "I don't know" string is `llm_available() == False`, reproducible only with the flag off.
- **Side effect worth noting (unfixed, deliberate):** `sirius-supervisor.service` (`supervisor.py`, PTB) still calls `updater.start_polling()`, which cannot work while the webhook is registered — Telegram returns `409 Conflict: can't use getUpdates method while webhook is active` and the supervisor logs a traceback in its reconnect backoff (visible at 22:47:52, after our restart). It costs nothing and steals no updates (webhook wins, `pending_update_count: 0`), so it was left alone rather than silenced by disabling the supervisor's bot, which still owns its own approval-command path. The tidy fix is for `supervisor.py` to detect an active webhook and skip polling, or move to `TELEGRAM_MODE=webhook`.
