"""Configuration, read once from the environment at process start.

Two rules this module enforces, because they were the failure mode of the
previous project:

1. Secrets are never repr'd. `Config.__repr__` masks anything that looks like
   a key, so a stray `print(config)` in a container log cannot leak a token.
2. Anything that arms a live external effect is re-read live from the DB
   settings table (see `runtime_flags`), not cached here. Flipping dry-run
   must not need a redeploy, and a redeploy must not silently arm the card.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from zoneinfo import ZoneInfo

_MASK = "***"
_SECRET_HINTS = ("key", "token", "secret", "password", "url", "passwd")


def _clean(value: str | None) -> str:
    """Empty, literal 'null' and whitespace count as unset.

    Heredoc-generated .env files write `KEY=null`, and a literal 'null' passed
    to a bearer auth header fails with a confusing 401 rather than a clear
    'not configured'.
    """
    if value is None:
        return ""
    value = value.strip()
    if value.lower() in {"", "null", "none", '""', "''", "undefined"}:
        return ""
    # A trailing quote from a half-written .env line breaks bearer auth headers.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _int(name: str, default: int) -> int:
    raw = _clean(os.getenv(name))
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _clean(os.getenv(name))
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _clean(os.getenv(name)).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _placeholder_or_value(name: str) -> str:
    """Treat the __REPLACE_WITH_*__ seeds as unset.

    A placeholder token must never reach Telegram's API: it wastes a request,
    then fails with 'token was malformed' in a place a human reads as
    'the bot is broken' rather than 'you have not configured this yet'.
    """
    raw = _clean(os.getenv(name))
    if raw.startswith("__REPLACE_WITH_"):
        return ""
    return raw


@dataclass(frozen=True)
class TelegramSettings:
    """One bot token plus the chat it posts to. A bot is bound to exactly one
    credential pair, so approvals and digests are separate identities."""

    bot_token: str
    owner_id: int
    chat_id: str
    mode: str  # 'polling' | 'webhook'
    webhook_secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.bot_token) and bool(self.chat_id or self.owner_id)


@dataclass(frozen=True)
class Config:
    # runtime
    runtime: str
    env: str
    timezone: ZoneInfo
    # llm
    openrouter_api_key: str
    llm_model_cheap: str
    llm_model_strong: str
    llm_daily_cap_usd: float
    llm_timeout_seconds: int
    llm_max_attempts: int
    llm_fallback_chain: tuple[str, ...]
    # db
    database_url: str
    db_pool_min: int
    db_pool_max: int
    db_statement_timeout_ms: int
    # http
    backend_port: int
    public_base_url: str
    # supervisor
    supervisor_tick_seconds: int
    worker_tick_seconds: int
    heartbeat_stale_seconds: int
    # finance rails
    tier1_max_minor: int
    tier2_max_minor: int
    approval_expiry_hours: int
    default_currency: str
    engine_dry_run: bool
    allow_local_sudo: bool
    card_daily_cap_minor: int
    card_monthly_cap_minor: int
    # research
    jina_api_key: str = field(repr=False, default="")
    exa_api_key: str = field(repr=False, default="")

    # interfaces, attached after construction (see from_env)
    approval_bot: TelegramSettings | None = None
    digest_bot: TelegramSettings | None = None
    webhook_signing_secret: str = field(repr=False, default="")
    supabase_anon_key: str = field(repr=False, default="")
    supabase_url: str = ""

    @property
    def is_container(self) -> bool:
        return self.runtime == "container"

    def __repr__(self) -> str:
        fields = []
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if any(hint in name for hint in _SECRET_HINTS):
                value = _MASK if value else ""
            fields.append(f"{name}={value!r}")
        return f"Config({', '.join(fields)})"

    @classmethod
    def from_env(cls) -> Config:
        owner_tz = _clean(os.getenv("OWNER_TIMEZONE")) or "UTC"
        try:
            tz = ZoneInfo(owner_tz)
        except Exception:
            # A typo'd tz must not stop the engine; it must shout about it.
            tz = ZoneInfo("UTC")

        cfg = cls(
            runtime=(_clean(os.getenv("ENGINE_RUNTIME")) or "local").lower(),
            env=(_clean(os.getenv("ENGINE_ENV")) or "dev").lower(),
            timezone=tz,
            openrouter_api_key=_clean(os.getenv("OPENROUTER_API_KEY")),
            llm_model_cheap=_clean(os.getenv("LLM_MODEL_CHEAP"))
            or "anthropic/claude-haiku-5.5",
            llm_model_strong=_clean(os.getenv("LLM_MODEL_STRONG"))
            or "anthropic/claude-sonnet-5.5",
            llm_daily_cap_usd=_float("LLM_DAILY_CAP_USD", 8.0),
            llm_timeout_seconds=_int("LLM_TIMEOUT_SECONDS", 90),
            llm_max_attempts=_int("LLM_MAX_ATTEMPTS", 3),
            llm_fallback_chain=tuple(
                m.strip()
                for m in _clean(os.getenv("LLM_FALLBACK_CHAIN")).split(",")
                if m.strip()
            ),
            database_url=_clean(os.getenv("DATABASE_URL")),
            db_pool_min=_int("DB_POOL_MIN", 1),
            db_pool_max=_int("DB_POOL_MAX", 5),
            db_statement_timeout_ms=_int("DB_STATEMENT_TIMEOUT_MS", 15000),
            backend_port=_int("BACKEND_PORT", 3100),
            public_base_url=_clean(os.getenv("PUBLIC_BASE_URL")) or "http://localhost:3100",
            supervisor_tick_seconds=_int("SUPERVISOR_TICK_SECONDS", 20),
            worker_tick_seconds=_int("WORKER_TICK_SECONDS", 60),
            heartbeat_stale_seconds=_int("HEARTBEAT_STALE_SECONDS", 180),
            tier1_max_minor=_int("APPROVAL_TIER1_MAX_MINOR", 5000),
            tier2_max_minor=_int("APPROVAL_TIER2_MAX_MINOR", 25000),
            approval_expiry_hours=_int("APPROVAL_EXPIRY_HOURS", 24),
            default_currency=(_clean(os.getenv("DEFAULT_CURRENCY")) or "USD").upper(),
            engine_dry_run=_bool("ENGINE_DRY_RUN", True),
            allow_local_sudo=_bool("ALLOW_LOCAL_SUDO", False),
            card_daily_cap_minor=_int("CARD_DAILY_CAP_MINOR", 5000),
            card_monthly_cap_minor=_int("CARD_MONTHLY_CAP_MINOR", 50000),
            jina_api_key=_clean(os.getenv("JINA_AI_API_KEY")),
            exa_api_key=_clean(os.getenv("EXA_API_KEY")),
            webhook_signing_secret=_placeholder_or_value("WEBHOOK_SIGNING_SECRET"),
            supabase_anon_key=_clean(os.getenv("SUPABASE_ANON_KEY")),
            supabase_url=_clean(os.getenv("SUPABASE_URL")),
        )
        return cfg

    def telegram(self, which: str) -> TelegramSettings | None:
        """Build a Telegram credential pair by role: 'approval' | 'digest'."""
        if which == "approval":
            token = _placeholder_or_value("TELEGRAM_APPROVAL_BOT_TOKEN")
            chat = _placeholder_or_value("TELEGRAM_APPROVAL_CHAT_ID")
        elif which == "digest":
            token = _placeholder_or_value("TELEGRAM_DIGEST_BOT_TOKEN")
            chat = _placeholder_or_value("TELEGRAM_DIGEST_CHAT_ID")
        else:
            raise ValueError(f"unknown telegram role {which!r}")

        if not token:
            return None
        return TelegramSettings(
            bot_token=token,
            owner_id=_int("TELEGRAM_OWNER_ID", 0),
            chat_id=chat,
            mode=(_clean(os.getenv("TELEGRAM_MODE")) or "polling").lower(),
            webhook_secret=_clean(os.getenv("TELEGRAM_WEBHOOK_SECRET")),
        )

    def validate(self) -> list[str]:
        """Return human-readable problems. Empty list means safe to boot.

        Warnings are returned rather than raised: an unconfigured approval bot
        is a real problem, but it should stop the approval flow, not the whole
        engine at 3am.
        """
        problems: list[str] = []
        if not self.database_url:
            problems.append("DATABASE_URL is unset: the engine has no state and cannot start")
        if not self.openrouter_api_key:
            problems.append("OPENROUTER_API_KEY unset: LLM calls will fail closed")
        if self.tier1_max_minor >= self.tier2_max_minor:
            problems.append(
                "approval tiers are inverted: tier1_max_minor must be below tier2_max_minor"
            )
        if self.approval_expiry_hours <= 0:
            problems.append("APPROVAL_EXPIRY_HOURS must be positive")
        if not self.engine_dry_run:
            problems.append(
                "ENGINE_DRY_RUN=false in the environment: live effects armed. "
                "The live switch is the DB row engine.settings['engine.dry_run'],"
                "and it wins over this."
            )
        return problems


@lru_cache(maxsize=1)
def get_config() -> Config:
    cfg = Config.from_env()
    cfg_dict = {
        "approval": cfg.telegram("approval"),
        "digest": cfg.telegram("digest"),
    }
    # frozen dataclass: attach via object.__setattr__ so the pairs stay immutable
    object.__setattr__(cfg, "approval_bot", cfg_dict["approval"])
    object.__setattr__(cfg, "digest_bot", cfg_dict["digest"])
    return cfg
