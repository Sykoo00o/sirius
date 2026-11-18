"""Telegram: outbound sender plus an inbound long-poller, as separate identities.

A bot token is bound to one credential pair, so the approval bot (which receives
taps) and the digest bot (which posts a morning summary) are separate objects.
They never share state.

Delivery failures are classified because the fix differs:
  429 -> back off using retry_after, then retry
  403 -> the human blocked the bot or left the chat: disable the channel and
         stop hammering it, otherwise every tick wastes a request forever
  400 -> our payload is malformed: do not retry, it will fail identically
  5xx / network -> retry, then report
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config, TelegramSettings, get_config

log = logging.getLogger("engine.telegram")

TG_API = "https://api.telegram.org"

# Inline keyboard for the approval gate. callback_data carries the approval id
# and the decision; Telegram caps it at 64 bytes, so we send the id's short form
# and look the rest up server-side.
APPROVE_LABEL = "✅ Approve"
REJECT_LABEL = "❌ Reject"


class TelegramDisabled(RuntimeError):
    pass


class ChannelBlocked(RuntimeError):
    """The human unsubscribed or blocked the bot: stop trying, not just this send."""


@dataclass
class SendResult:
    ok: bool
    message_id: int | None = None
    error: str = ""
    retryable: bool = False
    retry_after: float = 0.0


class TelegramSender:
    def __init__(self, settings: TelegramSettings | None, cfg: Config | None = None,
                 role: str = "approval", db: Any = None) -> None:
        self.settings = settings
        self.cfg = cfg or get_config()
        self.role = role
        self.db = db
        self._client: httpx.AsyncClient | None = None
        self._disabled_until = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.settings and self.settings.configured)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _chat(self) -> str:
        assert self.settings is not None
        return self.settings.chat_id or str(self.settings.owner_id)

    async def send(
        self, text: str, *, markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML", disable_preview: bool = True, max_attempts: int = 3,
    ) -> SendResult:
        if not self.configured:
            return SendResult(False, error=f"{self.role} telegram bot not configured")

        import time

        if time.monotonic() < self._disabled_until:
            return SendResult(False, error=f"{self.role} telegram channel cooling down")

        assert self.settings is not None
        url = f"{TG_API}/bot{self.settings.bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._chat(),
            "text": text[:4000],  # Telegram's hard limit per message
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if markup:
            payload["reply_markup"] = json.dumps(markup)

        last = SendResult(False, error="not attempted")
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await (await self._http()).post(url, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last = SendResult(False, error=f"network: {exc}", retryable=True, retry_after=1)
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("ok"):
                        return SendResult(True, message_id=body["result"]["message_id"])
                    last = SendResult(False, error=f"telegram ok=false: {body}")
                elif resp.status_code == 429:
                    detail = _safe_json(resp.text)
                    retry_after = float(((detail.get("parameters") or {}).get("retry_after")) or 2)
                    last = SendResult(False, error="rate limited", retryable=True,
                                      retry_after=retry_after)
                elif resp.status_code in (401, 403):
                    # Blocked, kicked, or a bad token. Back off a long time:
                    # retrying a blocked channel every tick is pure waste.
                    self._disabled_until = time.monotonic() + 1800
                    last = SendResult(False, error=f"channel refused ({resp.status_code}): "
                                                 f"{resp.text[:200]}")
                elif resp.status_code == 400:
                    last = SendResult(False, error=f"bad request: {resp.text[:300]}")
                else:
                    last = SendResult(False, error=f"http {resp.status_code}: {resp.text[:200]}",
                                      retryable=True, retry_after=2)

            if not last.retryable or attempt == max_attempts:
                break
            # Jitter only: spreads retries so six workers do not retry in lockstep.
            await asyncio.sleep(last.retry_after + random.uniform(0, 0.5))  # noqa: S311

        log.warning("telegram %s send failed: %s", self.role, last.error)
        if self.db is not None:
            try:
                self.db.execute(
                    "insert into engine.agent_logs (level, event, message, context) "
                    "values (%s, %s, %s, %s::jsonb)",
                    ("warning" if last.retryable else "error",
                     "telegram.send_failed",
                     f"{self.role}: {last.error}"[:2000],
                     json.dumps({"role": self.role, "attempts": max_attempts})),
                )
            except Exception as log_exc:
                # We could not even record the failure. Loud on stdout is the
                # last honest option, since the DB is possibly what broke.
                log.warning("could not persist telegram send failure: %s", log_exc)
        return last

    async def edit_keyboard(
        self, message_id: int, text: str, markup: dict[str, Any] | None = None
    ) -> SendResult:
        """Replace an approval card in place once it has been decided."""
        if not self.configured or message_id is None:
            return SendResult(False, error="cannot edit")
        assert self.settings is not None
        url = f"{TG_API}/bot{self.settings.bot_token}/editMessageText"
        payload = {
            "chat_id": self._chat(), "message_id": message_id, "text": text[:4000],
            "parse_mode": "HTML",
        }
        if markup is not None:
            payload["reply_markup"] = json.dumps(markup)
        try:
            resp = await (await self._http()).post(url, json=payload)
            return SendResult(resp.status_code == 200, error=resp.text[:200])
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return SendResult(False, error=str(exc))


class TelegramPoller:
    """Long-polls one bot for the owner's commands and approval taps.

    Long-polling rather than a webhook because this bot's token is currently
    bound to a legacy project: registering a webhook would steal its traffic.
    Polling is additive, and offset persistence means a restart replays at most
    one update instead of the whole backlog.
    """

    def __init__(self, settings: TelegramSettings, db: Any,
                 on_update: Any, cfg: Config | None = None) -> None:
        self.settings = settings
        self.db = db
        self.on_update = on_update  # async def(update: dict) -> None
        self.cfg = cfg or get_config()
        self._stop = asyncio.Event()
        self._sender = TelegramSender(settings, cfg, "approval", db)

    async def stop(self) -> None:
        self._stop.set()
        await self._sender.aclose()

    def _offset_key(self) -> str:
        # Keyed by token hash so two bots sharing a database never read each
        # other's offset and skip updates.
        import hashlib

        return f"telegram.offset.{hashlib.sha256(self.settings.bot_token.encode()).hexdigest()[:12]}"

    def _load_offset(self) -> int:
        try:
            return int(self.db.setting(self._offset_key(), 0) or 0)
        except Exception:
            return 0

    def _save_offset(self, update_id: int) -> None:
        try:
            self.db.set_setting(self._offset_key(), update_id, actor="telegram")
        except Exception as exc:
            log.warning("could not persist telegram offset: %s", exc)

    async def run(self) -> None:
        """Poll until stopped. Survives its own errors: a dropped poll restarts
        with backoff instead of ending the process, since this is the only path
        by which approvals arrive."""
        offset = self._load_offset()
        errors = 0
        url = f"{TG_API}/bot{self.settings.bot_token}/getUpdates"
        log.info("telegram poller starting, offset=%s", offset)

        while not self._stop.is_set():
            timeout = 0.0
            try:
                resp = await (await self._sender._http()).get(
                    url,
                    params={"offset": offset + 1, "timeout": 30,
                            "allowed_updates": '["message","callback_query","edited_message"]'},
                    timeout=httpx.Timeout(45, connect=10),
                )
                if resp.status_code == 409:
                    # Someone registered a webhook on this token; getUpdates is
                    # disabled until it is deleted. Loud, then keep retrying.
                    log.error("telegram 409: a webhook is registered on the %s bot token. "
                              "Call deleteWebhook or move this bot to its own token.", self.role)
                    timeout = 60
                elif resp.status_code == 401:
                    log.critical("telegram 401: the %s bot token is invalid", self.role)
                    timeout = 300
                elif resp.status_code == 429:
                    detail = _safe_json(resp.text)
                    timeout = float(((detail.get("parameters") or {}).get("retry_after")) or 5)
                elif resp.status_code != 200:
                    log.warning("telegram poll http %d: %s", resp.status_code, resp.text[:200])
                    timeout = 5
                else:
                    errors = 0
                    for update in resp.json().get("result", []):
                        offset = max(offset, int(update.get("update_id", 0)))
                        self._save_offset(offset)
                        try:
                            await self.on_update(update)
                        except Exception as exc:
                            # One bad update must not kill the loop, and must not
                            # be silently eaten either.
                            log.exception("telegram update handler failed: %s", exc)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                errors += 1
                # Jitter only, not a token: prevents a thundering retry herd.
                timeout = min(2 ** min(errors, 6), 120) + random.uniform(0, 1)  # noqa: S311
                log.warning("telegram poll error #%d: %s (retry in %.0fs)", errors, exc, timeout)
            except Exception as exc:
                errors += 1
                timeout = min(2 ** min(errors, 6), 120)
                log.exception("unexpected telegram poll failure: %s (retry in %.0fs)", exc, timeout)

            if timeout <= 0:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except TimeoutError:
                pass


def approval_keyboard(approval_id: str) -> dict[str, Any]:
    """Approve/Reject buttons. callback_data is `decide:<id>:approve|reject`."""
    short = approval_id.replace("-", "")[:16]
    return {
        "inline_keyboard": [[
            {"text": APPROVE_LABEL, "callback_data": f"decide:{short}:approve"},
            {"text": REJECT_LABEL, "callback_data": f"decide:{short}:reject"},
        ]]
    }


def decided_keyboard() -> dict[str, Any]:
    """Empty keyboard: removes the buttons so a stale tap cannot hit a decided row."""
    return {"inline_keyboard": []}


def _safe_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}
