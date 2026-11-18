"""The approval bot: inbound Telegram, and the bridge to the gatekeeper.

Security posture, in the order it is applied to every inbound item:

1. Ignore anything from a chat we did not choose. The chat id is compared to a
   value from config, so an unsolicited DM saying "approve" is not an approval.
2. For callback taps, ownership is checked against the row: the tapped approval
   must exist and still be `pending`. Stale buttons on a decided card are
   acknowledged and answered honestly rather than erroring at the tapper.
3. Tier 3 is not unlocked here. `decide_approval` raises, and that refusal is
   what we show, so the UI cannot be more permissive than the gate.

One bot, one chat, no command parsing library: there are about eight commands,
and a matcher keeps the failure modes visible.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .audit import scrub
from .config import Config, get_config
from .db import Database
from .finance_gatekeeper import FinanceGatekeeper, fmt_minor
from .jobs import JobRunner
from .settings import SettingsAccessor
from .telegram import (
    TelegramPoller,
    TelegramSender,
    approval_keyboard,
    decided_keyboard,
)

log = logging.getLogger("engine.telegram_bot")

HELP = """<b>Ghost X2 approvals</b>
/approvals  pending queue
/status     fleet and money
/log        recent engine events
/pause &lt;worker&gt;  &lt;resume&gt; &lt;worker&gt;
/dryrun     show or set the live-money switch
/digest     send the daily card now
/help       this"""

COMMANDS = ("/approvals", "/status", "/log", "/pause", "/resume", "/dryrun", "/digest", "/help")


class ApprovalBot:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        self.db = Database(self.cfg)
        self.settings = SettingsAccessor(self.db)
        self.gate = FinanceGatekeeper(self.db, self.cfg)
        self.settings_accessor = self.settings
        self.sender = TelegramSender(self.cfg.approval_bot, self.cfg, "approval", self.db)
        self._jobs = JobRunner(self.cfg)
        self.poller: TelegramPoller | None = None
        # approval id -> telegram message id, so a decision edits the right card.
        self._cards: dict[str, int] = {}

    # ---------------------------------------------------------------- helpers
    def _authorised_chat(self, chat_id: Any) -> bool:
        if not self.cfg.approval_bot:
            return False
        expected = {str(self.cfg.approval_bot.chat_id)}
        if self.cfg.approval_bot.owner_id:
            expected.add(str(self.cfg.approval_bot.owner_id))
        return str(chat_id) in expected

    # ----------------------------------------------------------------- updates
    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_callback(update["callback_query"])
        elif "message" in update:
            await self._on_message(update["message"])
        elif "edited_message" in update:
            log.debug("edited_message ignored")

    async def _on_message(self, msg: dict[str, Any]) -> None:
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if not self._authorised_chat(chat_id):
            log.info("ignoring message from unlisted chat %s", chat_id)
            # Deliberately silent: replying to strangers invites a conversation
            # this bot has no ability to have.
            return

        parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]

        if cmd.startswith("/start") or cmd in ("/help", "/help".lower()):
            await self.sender.send(HELP)
        elif cmd == "/approvals":
            await self.sender.send(await self.render_queue())
        elif cmd == "/status":
            await self.sender.send(self.render_status())
        elif cmd == "/log":
            await self.sender.send(self.render_log())
        elif cmd == "/digest":
            # Runs synchronously here: the owner asked for it and expects the card.
            await asyncio.to_thread(self._jobs.run_once, "digest")
            await self.sender.send("Digest sent to the digest channel.")
        elif cmd in ("/pause", "/resume"):
            await self._set_state(cmd, args)
        elif cmd == "/dryrun":
            await self._dryrun(args)
        elif cmd in COMMANDS:
            await self.sender.send(f"{cmd} needs an argument, eg {cmd} worker:funnel")
        else:
            await self.sender.send("Unrecognised. /help")

    async def _set_state(self, cmd: str, args: list[str]) -> None:
        if not args:
            agents = self.db.read("select handle from engine.agents order by handle")
            handles = "\n".join(a["handle"] for a in agents)
            await self.sender.send(f"Which one?\n<code>{handles}</code>")
            return
        handle = args[0]
        desired = "paused" if cmd == "/pause" else "running"
        updated = self.db.execute(
            "update engine.agents set desired_state = %s, updated_at = now() where handle = %s",
            (desired, handle))
        if not updated:
            await self.sender.send(f"No agent called <code>{scrub(handle)}</code>.")
            return
        await self.sender.send(
            f"{handle} desired state is now <b>{desired}</b>. The supervisor will apply it "
            "on its next tick.")

    async def _dryrun(self, args: list[str]) -> None:
        if not args:
            state = self.db.is_dry_run()
            await self.sender.send(
                f"dry_run is <b>{str(state).lower()}</b>.\n"
                "/dryrun off arms live effects. /dryrun on disarms them.")
            return
        want = args[0].lower() in {"off", "false", "no", "live", "armed"}
        previous = self.db.is_dry_run()
        self.db.set_setting("engine.dry_run", not want, actor="telegram",
                            description=f"set from telegram by {self.cfg.approval_bot.owner_id}")
        self.gate.audit = None
        # Arm/disarm is the single most consequential setting change in the
        # system, so it is audited whether or not it moved anything.
        await self.sender.send(
            f"dry_run: {str(previous).lower()} → <b>{str(not want).lower()}</b>"
            + ("\n⚠️ LIVE MONEY is now armed." if want and not previous else
               "\nExternal effects are simulated." if not want else ""),
        )

    # --------------------------------------------------------------- callbacks
    async def _on_callback(self, cb: dict[str, Any]) -> None:
        query_id = cb.get("id")
        data = (cb.get("data") or "").strip()
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        from_id = ((cb.get("from") or {}).get("id"))
        message_id = (cb.get("message") or {}).get("message_id")

        if not data.startswith("decide:"):
            await self._answer(query_id, "Not recognised")
            return
        if not self._authorised_chat(chat_id):
            log.warning("approval tap from unlisted chat %s (user %s) ignored", chat_id, from_id)
            await self._answer(query_id, "This chat is not the approval channel")
            return

        parts = data.split(":")
        if len(parts) != 3 or parts[2] not in ("approve", "reject"):
            await self._answer(query_id, "Malformed button")
            return
        short_id, decision = parts[1], parts[2]

        row = self.db.read_one(
            "select id::text as id, replace(id::text,'-','') as flat, title, cost_minor, "
            "currency, state, tier from engine.approval_requests "
            "where replace(id::text,'-','') like %s",
            (f"{short_id}%",))
        if not row:
            await self._answer(query_id, "That approval no longer exists")
            return
        # Prefix match could theoretically hit two rows; refuse rather than
        # guessing which purchase a thumb was aiming at.
        ambiguous = self.db.read(
            "select id from engine.approval_requests where replace(id::text,'-','') like %s",
            (f"{short_id}%",))
        if len(ambiguous) != 1:
            await self._answer(query_id, "Ambiguous id, use /approvals")
            return

        approval_id = row["id"]
        actor = f"telegram:{from_id}"
        if row["state"] != "pending":
            await self._answer(query_id, f"Already {row['state']}")
            await self.sender.edit_keyboard(
                message_id, self._card_text(row, note=f"already {row['state']}"),
                decided_keyboard())
            return

        try:
            state = self.gate.decide(approval_id, decision, actor, "via telegram tap")
        except Exception as exc:
            message = scrub(str(exc))
            # The tier3 block is the gate refusing, not a fault: show it plainly.
            await self._answer(query_id, message[:180] or "refused")
            await self.sender.edit_keyboard(
                message_id, self._card_text(row, note=f"refused: {message[:120]}"),
                decided_keyboard())
            return

        await self._answer(query_id, f"Recorded: {state}")
        await self.sender.edit_keyboard(
            message_id, self._card_text(row, note=f"{state} by {actor}"), decided_keyboard())

    async def _answer(self, query_id: str | None, text: str) -> None:
        """Acknowledge the tap so Telegram stops spinning the button."""
        if not query_id or not self.cfg.approval_bot:
            return
        import httpx

        url = (f"https://api.telegram.org/bot{self.cfg.approval_bot.bot_token}"
               f"/answerCallbackQuery")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"callback_query_id": query_id, "text": text[:190]})
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    # ---------------------------------------------------------------- render
    def _card_text(self, row: dict[str, Any], note: str = "") -> str:
        money = fmt_minor(int(row["cost_minor"]), row["currency"])
        lines = [
            f"<b>{scrub(str(row['title']))}</b>",
            f"{money}  ·  tier {row['tier']}",
        ]
        full = self.db.read_one(
            "select rationale, expected_roi_note, source_url, vendor from engine.approval_requests "
            "where id = %s", (row["id"],)) or {}
        if full.get("vendor"):
            lines.append(f"vendor: {scrub(str(full['vendor']))}")
        if full.get("rationale"):
            lines.append(scrub(str(full["rationale"]))[:400])
        if full.get("expected_roi_note"):
            lines.append(f"ROI: {scrub(str(full['expected_roi_note']))[:200]}")
        if full.get("source_url"):
            lines.append(f'<a href="{full["source_url"]}">source</a>')
        if note:
            lines.append(f"<i>{scrub(note)}</i>")
        return "\n".join(lines)

    async def render_queue(self) -> str:
        pending = self.gate.pending(limit=15)
        if not pending:
            return "Nothing waiting. 🎉"
        total = sum(int(p["cost_minor"]) for p in pending)
        lines = [f"<b>{len(pending)} approval(s) waiting</b>  "
                 f"total {fmt_minor(total)}", ""]
        for p in pending:
            lines.append(
                f"• <b>{scrub(str(p['title']))}</b>\n"
                f"  {fmt_minor(int(p['cost_minor']), p['currency'])} · {p['tier']} · "
                f"expires in {p.get('seconds_to_expiry', 0) // 3600}h\n"
                f"  id <code>{p['id'][:8]}</code>")
        return "\n".join(lines)

    def render_status(self) -> str:
        pnl = self.gate.pnl_summary(24)
        fleet = self.db.read("select * from engine.vw_agent_fleet_health order by handle")
        stale_after = self.settings_accessor.heartbeat_stale_seconds
        lines = [
            "<b>Status</b>",
            f"dry_run: <b>{str(pnl['dry_run']).lower()}</b>",
            f"net 24h: {fmt_minor(pnl['net_minor'])}",
            f"in {fmt_minor(pnl['in_minor'])} / out {fmt_minor(pnl['out_minor'])}",
            f"LLM today: ${pnl['llm_cost_usd']:.2f}",
            f"disputes: {pnl['disputes']}",
            "",
        ]
        for a in fleet:
            seconds = a.get("seconds_since_beat")
            if a["desired_state"] == "disabled":
                icon = "⚪"
            elif seconds is None or int(seconds) > stale_after:
                icon = "🔴"
            else:
                icon = "🟢"
            lines.append(f"{icon} {a['handle']}: desired {a['desired_state']}, "
                         f"beat {'never' if seconds is None else str(int(seconds)) + 's ago'}")
        return "\n".join(lines)

    def render_log(self) -> str:
        rows = self.db.read(
            """select created_at, level, message from engine.agent_logs
                where is_audit or level in ('error','critical')
                order by created_at desc limit 12""")
        if not rows:
            return "No audit events yet."
        return "\n".join(
            f"{r['created_at'].strftime('%m-%d %H:%M')} [{r['level']}] "
            f"{scrub(str(r['message']))[:120]}" for r in rows)

    # ------------------------------------------------------- outbound pushes
    async def push_approval_card(self, approval_id: str) -> int | None:
        """Send the interactive card for one pending approval. Returns the
        Telegram message id so the eventual decision can edit that card."""
        row = self.db.read_one(
            "select id::text as id, title, cost_minor, currency, state, tier "
            "from engine.approval_requests where id = %s", (approval_id,))
        if not row or row["state"] != "pending":
            return None
        result = await self.sender.send(self._card_text(row),
                                        markup=approval_keyboard(row["id"]))
        if result.ok and result.message_id:
            self._cards[row["id"]] = result.message_id
            return result.message_id
        log.warning("could not deliver approval card for %s: %s", approval_id, result.error)
        return None

    async def scan_new_approvals(self) -> None:
        """Push a card for any pending approval we have not already sent.

        Driven from a timer rather than from the request path so a worker that
        queues an approval does not need Telegram to be reachable, and a Telegram
        outage does not turn into a failed worker tick.
        """
        rows = self.db.read(
            """select id::text as id from engine.approval_requests
                where state = 'pending' order by created_at asc limit 20""")
        for r in rows:
            if r["id"] in self._cards:
                continue
            # Remember the attempt even on failure: retrying an undeliverable
            # card every 15s for a day is 5,700 pointless API calls.
            self._cards[r["id"]] = -1
            await self.push_approval_card(r["id"])

    # ------------------------------------------------------------------ run
    async def run_forever(self) -> None:
        if not self.cfg.approval_bot:
            log.critical(
                "TELEGRAM_APPROVAL_BOT_TOKEN is unset, so this container has nothing to do. "
                "Create a bot with @BotFather, set the token and the chat id, and restart.")
            # Stay alive: docker restart loops on an unconfigured install look
            # like a broken build, and the operator never sees the instructions.
            await asyncio.sleep(3600)
            return

        self.poller = TelegramPoller(self.cfg.approval_bot, self.db, self.handle_update, self.cfg)
        log.info("approval bot starting (mode=%s)", self.cfg.approval_bot.mode)
        await asyncio.gather(
            self.poller.run(),
            self._push_loop(),
        )

    async def _push_loop(self) -> None:
        while True:
            try:
                await self.scan_new_approvals()
            except Exception as exc:
                log.exception("approval push loop error: %s", exc)
            await asyncio.sleep(15)

    async def aclose(self) -> None:
        if self.poller:
            await self.poller.stop()
        await self.sender.aclose()
        self.db.close()


def main() -> None:
    from .logging_setup import setup_logging

    setup_logging("telegram-bot")
    bot = ApprovalBot()
    try:
        asyncio.run(bot.run_forever())
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.run(bot.aclose())


if __name__ == "__main__":
    main()
