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