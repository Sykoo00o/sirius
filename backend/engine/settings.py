"""Typed access to the engine.settings table.

Anything an operator might want to change without a redeploy lives here, and
every consumer goes through a typed getter rather than reading the raw JSON. That
keeps two properties:

* A bad value in the database degrades to the default plus a log line, instead
  of raising inside a financial path at 03:00.
* Type coercion happens in one place, so `int`/`bool`/`float` parsing cannot
  disagree between the supervisor and a worker.
"""

from __future__ import annotations

import logging
from typing import Any

from .db import Database

log = logging.getLogger("engine.settings")


class SettingsAccessor:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        try:
            value = self.db.setting(key, default)
        except Exception as exc:
            log.warning("could not read setting %s: %s", key, exc)
            return default
        return default if value is None else value

    def int(self, key: str, default: int) -> int:
        raw = self.get(key, default)
        try:
            return int(raw)
        except (TypeError, ValueError):
            log.error("setting %s=%r is not an int; using %s", key, raw, default)
            return default

    def float(self, key: str, default: float) -> float:
        raw = self.get(key, default)
        try:
            return float(raw)
        except (TypeError, ValueError):
            log.error("setting %s=%r is not a number; using %s", key, raw, default)
            return default

    def bool(self, key: str, default: bool) -> bool:
        raw = self.get(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return default

    def list(self, key: str, default: list[Any] | None = None) -> list[Any]:
        raw = self.get(key, None)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str) and raw:
            return [p.strip() for p in raw.split(",") if p.strip()]
        return list(default or [])

    def set(self, key: str, value: Any, actor: str = "api", description: str = "") -> None:
        self.db.set_setting(key, value, actor=actor, description=description)

    # --------------------------------------------------------- named getters
    @property
    def dry_run(self) -> bool:
        return self.bool("engine.dry_run", True)

    @property
    def heartbeat_stale_seconds(self) -> int:
        return self.int("heartbeat.stale_seconds", 180)

    @property
    def supervisor_tick_seconds(self) -> int:
        return self.int("supervisor.tick_seconds", 20)

    @property
    def llm_daily_cap_usd(self) -> float:
        return self.float("llm.daily_cap_usd", 8.0)

    @property
    def tier1_max_minor(self) -> int:
        return self.int("approval.tier1_max_minor", 5000)

    @property
    def tier2_max_minor(self) -> int:
        return self.int("approval.tier2_max_minor", 25000)

    @property
    def accept_unsigned_webhooks(self) -> bool:
        """Default True: a dev box has no signing secret on the first webhook.

        The API warns on every unsigned delivery so the convenience cannot quietly
        reach production. Flip this to false before arming live money.
        """
        return self.bool("webhook.accept_unsigned", True)

    def all_for_display(self) -> list[dict[str, Any]]:
        return self.db.read(
            "select key, value, description, updated_at, updated_by "
            "from engine.settings order by key"
        )
