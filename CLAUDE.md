# Ghost X2 Project Guidelines

## Project Overview
Building a 100% automated multi-agent hub-and-spoke system for grey-hat digital/ticket arbitrage, negative-option funnels, and white-label agency automation. Managed via Telegram and a Web UI dashboard.

## Tech Stack
- Backend: Python (FastAPI, APScheduler, Playwright)
- Database: Supabase (PostgreSQL)
- Infrastructure: Docker, Oracle Cloud VPS, Cloudflare Workers
- Control: Telegram Bot API + Custom Web UI

## Strict Rules for Claude Code
1. **Incremental Building:** Build one specific file or module at a time. Do not attempt to scaffold the entire project in a single response.
2. **Clean Secrets:** Never hardcode API keys, Stripe secrets, or Telegram bot tokens. Always read them from environment variables (`.env`).
3. **Keep Context Light:** Refer to `multi_agent_engine_master_architecture.md` for specs when needed, but output concise, production-ready code blocks.
4. **Execution Log:** After every successfully implemented feature, bug fix, or VPS deployment, automatically append or update a progress entry in `SIRIUS_MASTER_BLUEPRINT.md` under the "## Execution Log & Progress" section. Each entry must include: (a) timestamp and git commit hash, (b) what was implemented, (c) verification steps taken (e.g., syntax check, systemd restart, Telegram API response).
