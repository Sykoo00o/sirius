# Multi-Agent Grey-Hat Business Engine: Master Architecture & Build Spec

## Overview
A hub-and-spoke multi-agent pipeline designed to automate three distinct zero-inventory online business models (**Negative-Option/Free-Trial Funnels**, **Digital & Ticket Arbitrage**, and **White-Label Agency Arbitrage**). The system operates under strict programmatic financial safety rails with dual control interfaces: a **Telegram Bot** for instant mobile approvals and a **Web UI Dashboard** for visual monitoring and command.

---

## 1. System Architecture: The "Hub-and-Spoke" Pipeline

Instead of a single monolithic agent, the system operates as a **Supervisor Agent** coordinating specialized downstream worker agents.

```text
               ┌────────────────────────┐
               │    Supervisor Agent    │ (Orchestrator / FastAPI / Claude API)
               └───────────┬────────────┘
                           │
       ┌───────────────────┼───────────────────┐
       ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│Finance Agent │    │Marketing     │    │Execution &   │
│(Stripe/Cards)│    │Agent (X/Ads) │    │Hustle Workers│
└──────┬───────┘    └──────────────┘    └──────┬───────┘
       │                                       │
       └───────────────┬───────────────────────┘
                       ▼
       ┌───────────────────────────────┐
       │   Dual Control Interface      │
       │  • Telegram Bot (Mobile HITL) │
       │  • Web UI Dashboard (Next.js) │
       └───────────────────────────────┘
```

### Core Roles
* **The Orchestrator (Supervisor):** Runs on an Oracle Cloud Ubuntu 24.04 VPS. Scheduled via cron jobs and event triggers, it evaluates daily state, reviews metrics, and dispatches tasks to worker containers.
* **Finance Agent (The Gatekeeper):** Manages capital allocation, revenue tracking, Stripe webhooks, and virtual card purchase approvals. *Rule: Never give an AI unconstrained card access.*
* **Marketing Agent:** Generates landing page copy, manages social accounts, schedules ad spend, and tracks conversion funnels.
* **Hustle Workers:** Three isolated Docker worker routines tailored to each selected business model.

---

## 2. Hustle Worker Specifications

### Hustle A: Negative-Option / Free-Trial Funnel
* **Agent Role:** Landing page generator, funnel traffic optimizer, and customer support/cancellation handler.
* **Automation Workflow:**
  1. **Acquisition:** Generates landing pages (deployed via Cloudflare Pages) testing micro-offers or digital info products with low-cost 7-day trials.
  2. **Retention & Compliance Engine:** **Mandatory one-click cancellation webhook** to protect merchant accounts from mass chargebacks and card-network fraud flags.
  3. **Payment & Webhook Listener:** Listens to Stripe webhooks (`invoice.payment_succeeded`, `customer.subscription.deleted`, `charge.dispute.created`) via Cloudflare Workers to log MRR and auto-refund high-risk dispute triggers.

### Hustle B: Digital Arbitrage & Scalping (Zero Inventory)
*Replaces physical retail clearance with high-velocity digital assets requiring zero physical storage.*
* **Agent Role:** Automated digital asset monitor, ticket-drop sniffer, and instant checkout bot.
* **Automation Workflow:**
  1. **Sourcing Scraper:** Headless browser scripts (Playwright/Puppeteer) monitor event drop APIs (Ticketmaster/AXS), software bundle deals (AppSumo tier-stacking), or high-intent expired domains where acquisition cost is low and secondary market floor price yields a verified margin.
  2. **Validation Gate:** Evaluates secondary market liquidity, platform listing restrictions, and historical resale velocity.
  3. **Purchase & Relisting Automation:** Triggers virtual card checkouts up to pre-approved limits within seconds of release, then pushes listings to secondary marketplaces with dynamic floor-tracking pricing.

### Hustle C: White-Label / Agency Arbitrage
* **Agent Role:** Outreach, client onboarding, and support dispatcher.
* **Automation Workflow:**
  1. **Prospecting:** Scrapes directory listings or social mentions for businesses needing specific SaaS tools.
  2. **Onboarding:** Automatically provisions client sub-accounts via the white-label vendor’s API upon Stripe invoice settlement.
  3. **Support Routing:** Intercepts initial support tickets; routes platform bugs cleanly without exposing the underlying vendor, preventing the "anonymous scapegoat" failure mode.

---

## 3. Financial Safety Rails & Human-in-the-Loop (HITL)

To prevent runaway API loops, unauthorized charges, or processor bans, all capital outflow passes through the **Finance Agent**:

1. **Virtual Cards with Strict Spending Caps:**
   * Use an API-first card issuer (Stripe Issuing, Lithic, etc.) to issue dedicated virtual cards per worker agent with hard daily/monthly caps (e.g., `$50/day` max for digital asset tests).
2. **Tiered Approval Checkpoints:**
   * **Tier 1 (`< $50`):** Fully autonomous (domain renewals, minor ad spend, pre-validated micro-buys).
   * **Tier 2 (`$50 – $250`):** **Telegram & Web UI Approval Gate.** The agent pauses execution, logs the pending transaction in Supabase, and sends an interactive alert (`Item`, `Cost`, `Expected ROI`, `Source URL`) with `[Approve]` and `[Reject]` triggers.
   * **Tier 3 (`> $250`):** Hard block requiring manual execution.
3. **Merchant Account Isolation:**
   * Separate Stripe accounts/legal entities between high-risk trial funnels and standard B2B white-label SaaS billing.

---

## 4. Recommended Tech Stack & Dual Control Interfaces

* **Hosting & Orchestration:** Oracle Cloud Ubuntu 24.04 VPS running Docker Compose (`supervisor`, `finance-gatekeeper`, `worker-funnel`, `worker-arbitrage`, `worker-whitelabel`).
* **Edge & Webhooks:** Cloudflare Workers & Pages (for ultra-fast Stripe webhooks, one-click cancellation endpoints, and funnel hosting).
* **Database & State:** Supabase (PostgreSQL) storing agent logs, scraped arbitrage targets, approval queues, and financial ledger entries.
* **Control Interface A (Mobile):** Telegram Bot API (`python-telegram-bot` or `grammy`) for real-time alerts, daily PnL digests, and one-tap HITL purchase approvals.
* **Control Interface B (Web UI Dashboard):** Next.js + Tailwind CSS frontend connected to Supabase Realtime and a FastAPI backend.

### Web UI Dashboard Modules to Build
1. **Executive Metrics Header:** Live aggregate MRR, 24h net profit, active virtual card spend, and dispute/chargeback ratio monitor.
2. **HITL Approval Queue:** Interactive card feed of pending purchases (`$50–$250`) with projected ROI badges and instant `Approve` / `Reject` actions synced with Telegram.
3. **Agent Fleet Health Grid:** Real-time status indicators (`Running`, `Paused`, `Error`), last heartbeat timestamp, and toggle switches for each of the 3 hustle workers.
4. **Financial Ledger & Audit Stream:** Filterable table of all virtual card charges, Stripe webhook events, and live stdout logs from the Docker containers.

---

## 5. Suggested Repository Structure for Claude Code

```text
multi-agent-engine/
├── docker-compose.yml
├── .env.example
├── supabase/
│   └── schema.sql                 # Tables: agents, ledger, approval_queue, arbitrage_targets, logs
├── backend/                       # Python FastAPI + Supervisor + Telegram Bot
│   ├── main.py
│   ├── supervisor.py
│   ├── finance_gatekeeper.py
│   ├── telegram_bot.py
│   └── workers/
│       ├── funnel_worker.py
│       ├── arbitrage_worker.py
│       └── whitelabel_worker.py
├── edge-workers/                  # Cloudflare Workers (Stripe webhooks + 1-click cancel)
│   └── stripe-webhook.ts
└── dashboard/                     # Next.js + Tailwind CSS Web UI
    ├── src/app/page.tsx
    └── src/components/
        ├── ApprovalQueue.tsx
        ├── AgentStatusGrid.tsx
        └── FinancialLedger.tsx
```

## 6. Build Order for Claude Code
1. **Step 1 (Database & Core State):** Generate `supabase/schema.sql` with tables for the financial ledger, HITL approval queue, agent heartbeats, and arbitrage targets.
2. **Step 2 (Finance Gatekeeper & Telegram HITL):** Build `finance_gatekeeper.py` and `telegram_bot.py` enforcing the `< $50 / $50–$250 / > $250` spending rules.
3. **Step 3 (Web UI Dashboard):** Scaffold the Next.js + Tailwind dashboard in `dashboard/` wired to Supabase Realtime so approvals and logs update live in the browser.
4. **Step 4 (Hustle Workers & Edge Webhooks):** Implement the Cloudflare Stripe webhook handler (with mandatory one-click cancellation) and the three isolated worker scripts.