"""Environment-only configuration for the Lane C Rank-4 funnel.

Rule 2 of CLAUDE.md: no hardcoded secrets. Everything sensitive comes from the
environment, and the defaults below are chosen so that a missing variable means
"do less", never "assume permission": charges default to off, and no LLM key means
the deterministic plan builder serves the preview instead of erroring.

Phase 1 posture (Phase 0 rules still hold):
  * no live ad spend,
  * `SUPERVISOR_ENABLED` untouched,
  * no automated charges: `FUNNEL_CHARGES_ENABLED` defaults false, so even the
    checkout button records intent rather than asking a card.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATHS = (ROOT.parent / ".env", ROOT / ".env")


def load_dotenv() -> None:
    """Load the nearest .env without clobbering real environment variables.

    Process env wins over the file, which is what a container expects: a compose
    `environment:` block must be able to override whatever is parked in .env.
    """
    for path in ENV_PATHS:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)
        break  # first .env found wins; we do not merge two files


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FunnelSettings:
    """Everything the funnel needs to know, all of it from the environment."""

    brand_name: str = "Routine Lab"
    #: Public site origin. Feeds canonical URLs and the confirmation page copy, so
    #: a staging deploy cannot accidentally advertise the production domain.
    public_url: str = "http://localhost:8090"
    host: str = "127.0.0.1"
    port: int = 8090

    #: Preview generation. `llm` tries the `fast` tier first and falls back to the
    #: deterministic builder on any failure; `local` never calls a model.
    preview_mode: str = "llm"
    llm_timeout_seconds: int = 25

    #: Phase 1: false. No card is touched, and the checkout screen says so.
    charges_enabled: bool = False

    #: Offer terms, mirroring the Phase 0 spec for this lane. Kept here rather than
    #: in templates so a price change is one edit and stays consistent across every
    #: disclosure slot (badge, checkout card, receipt, policy pages).
    trial_amount_minor: int = 299
    trial_days: int = 7
    rebill_amount_minor: int = 2499
    currency: str = "usd"
    refund_window_days: int = 14

    #: SQLite file for quiz responses. Relative paths resolve under `funnel_app/`,
    #: so a default deploy never writes into the repo root by accident.
    database_path: str = "data/responses.sqlite3"

    #: Secret for the signed quiz cookie. Empty means "generate one at boot":
    #: responses still save, but a restart loses a half-finished quiz. It is a
    #: convenience, not a record, so a missing secret is tolerated and logged.
    session_secret: str = ""

    support_email: str = ""

    @property
    def database_abspath(self) -> Path:
        path = Path(self.database_path).expanduser()
        return path if path.is_absolute() else ROOT / path

    @classmethod
    def from_env(cls) -> "FunnelSettings":
        load_dotenv()
        return cls(
            brand_name=os.getenv("FUNNEL_BRAND_NAME", cls.brand_name).strip() or cls.brand_name,
            public_url=os.getenv("FUNNEL_PUBLIC_URL", cls.public_url).rstrip("/"),
            host=os.getenv("FUNNEL_HOST", cls.host),
            port=int(os.getenv("FUNNEL_PORT", str(cls.port))),
            preview_mode=os.getenv("FUNNEL_PREVIEW_MODE", cls.preview_mode).strip().lower(),
            llm_timeout_seconds=int(os.getenv("FUNNEL_LLM_TIMEOUT_SECONDS", "25")),
            charges_enabled=_flag("FUNNEL_CHARGES_ENABLED"),
            trial_amount_minor=int(os.getenv("FUNNEL_TRIAL_AMOUNT_MINOR", "299")),
            trial_days=int(os.getenv("FUNNEL_TRIAL_DAYS", "7")),
            rebill_amount_minor=int(os.getenv("FUNNEL_REBILL_AMOUNT_MINOR", "2499")),
            currency=os.getenv("FUNNEL_CURRENCY", "usd").lower(),
            refund_window_days=int(os.getenv("FUNNEL_REFUND_WINDOW_DAYS", "14")),
            database_path=os.getenv("FUNNEL_DB_PATH", cls.database_path),
            session_secret=os.getenv("FUNNEL_SESSION_SECRET", ""),
            support_email=os.getenv("FUNNEL_SUPPORT_EMAIL", ""),
        )
