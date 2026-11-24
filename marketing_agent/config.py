"""Environment-only settings. Every default means "do less" (CLAUDE.md rule 2: no secrets in code).

Five switches, all "do less" by default, each read independently:

  MARKETING_DRY_RUN          true   master switch; unparseable values also mean true
  MARKETING_AUTO_PAUSE       false  may a pause recommendation become a pause call
  MARKETING_PUBLISH_ENABLED  false  may an approved asset be uploaded or launched
  MARKETING_RENDER_ENABLED   false  may a manifest be sent to an external (paid) media renderer
  MARKETING_AI_ENABLED       false  may a model draft hooks or review a brief (add-only, fail-closed);
                                    `MARKETING_LLM_ENABLED` is read as a legacy alias

Arming an action needs dry-run off AND its own flag (AND the Meta credentials for ad actions). Even
then there is no mutation, upload or render client (`meta.UnarmedMetaClient` / `assets.stage`
raise `NotBuilt`), so arming is also a code change.

Meta *reads* (Ad Library `ads_archive`, Marketing API insights) are GET-only, go through
`graph.py`'s host-allowlisted transport, and run only when a CLI call asks for `--source api`.
Tokens are read from the environment at the moment a reader is built and never stored here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def _dry_run() -> bool:
    """Only an explicit false turns dry-run off. Anything else, typo included, stays dry."""
    return os.getenv("MARKETING_DRY_RUN", "").strip().lower() not in _FALSE


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def ad_library_token() -> str:
    """`META_AD_LIBRARY_ACCESS_TOKEN`, falling back to `META_ACCESS_TOKEN`. Read on use, never kept."""
    return (os.getenv("META_AD_LIBRARY_ACCESS_TOKEN", "").strip()
            or os.getenv("META_ACCESS_TOKEN", "").strip())


@dataclass(frozen=True)
class AdRules:
    """Recommendation thresholds, from the web2app blueprint's decision table. Minor units for
    money, percent for rates. They produce recommendations only."""

    thumbstop_min_pct: float = 25.0
    thumbstop_min_spend_minor: int = 2500
    thumbstop_min_impressions: int = 1500
    ctr_min_pct: float = 1.0
    ctr_min_spend_minor: int = 3500
    ctr_min_impressions: int = 2000
    roas_floor: float = 0.50
    roas_min_spend_minor: int = 5000
    roas_min_hours: int = 48
    winner_roas: float = 0.90
    winner_min_purchases: int = 3

    @classmethod
    def from_env(cls) -> "AdRules":
        d = cls()
        return cls(
            thumbstop_min_pct=_float("MARKETING_THUMBSTOP_MIN_PCT", d.thumbstop_min_pct),
            thumbstop_min_spend_minor=_int("MARKETING_THUMBSTOP_MIN_SPEND_MINOR",
                                           d.thumbstop_min_spend_minor),
            thumbstop_min_impressions=_int("MARKETING_THUMBSTOP_MIN_IMPRESSIONS",
                                           d.thumbstop_min_impressions),
            ctr_min_pct=_float("MARKETING_CTR_MIN_PCT", d.ctr_min_pct),
            ctr_min_spend_minor=_int("MARKETING_CTR_MIN_SPEND_MINOR", d.ctr_min_spend_minor),
            ctr_min_impressions=_int("MARKETING_CTR_MIN_IMPRESSIONS", d.ctr_min_impressions),
            roas_floor=_float("MARKETING_ROAS_FLOOR", d.roas_floor),
            roas_min_spend_minor=_int("MARKETING_ROAS_MIN_SPEND_MINOR", d.roas_min_spend_minor),
            roas_min_hours=_int("MARKETING_ROAS_MIN_HOURS", d.roas_min_hours),
            winner_roas=_float("MARKETING_WINNER_ROAS", d.winner_roas),
            winner_min_purchases=_int("MARKETING_WINNER_MIN_PURCHASES", d.winner_min_purchases),
        )


@dataclass(frozen=True)
class MarketingSettings:
    dry_run: bool = True
    auto_pause: bool = False
    publish_enabled: bool = False
    llm_enabled: bool = False
    render_enabled: bool = False
    app_dir: Path = REPO_ROOT / "funnel_app"
    db_path: Path = PACKAGE_DIR / "data" / "marketing.sqlite3"
    state_dir: Path = PACKAGE_DIR / "data"
    meta_token_present: bool = False
    meta_account_present: bool = False
    ad_library_token_present: bool = False
    graph_api_version: str = "v21.0"
    #: Prepaid share above this halts scaling, matching the export's own note threshold.
    prepaid_share_halt_pct: float = 20.0
    rules: AdRules = AdRules()

    @classmethod
    def from_env(cls) -> "MarketingSettings":
        db_path = _path("MARKETING_DB_PATH", cls.db_path)
        return cls(
            dry_run=_dry_run(),
            auto_pause=_flag("MARKETING_AUTO_PAUSE", False),
            publish_enabled=_flag("MARKETING_PUBLISH_ENABLED", False),
            llm_enabled=_flag("MARKETING_AI_ENABLED", False) or _flag("MARKETING_LLM_ENABLED", False),
            render_enabled=_flag("MARKETING_RENDER_ENABLED", False),
            app_dir=_path("MARKETING_FUNNEL_APP_DIR", cls.app_dir),
            db_path=db_path,
            state_dir=_path("MARKETING_STATE_DIR", db_path.parent),
            # Presence only. The values are never stored on this object, so it can be printed.
            meta_token_present=bool(os.getenv("META_ACCESS_TOKEN", "").strip()),
            meta_account_present=bool(os.getenv("META_AD_ACCOUNT_ID", "").strip()),
            ad_library_token_present=bool(ad_library_token()),
            graph_api_version=(os.getenv("META_GRAPH_API_VERSION", "").strip() or "v21.0"),
            prepaid_share_halt_pct=_float("MARKETING_PREPAID_SHARE_HALT_PCT", 20.0),
            rules=AdRules.from_env(),
        )

    @property
    def meta_configured(self) -> bool:
        return self.meta_token_present and self.meta_account_present

    def can_pause_ads(self) -> bool:
        return (not self.dry_run) and self.auto_pause and self.meta_configured

    def can_publish(self) -> bool:
        return (not self.dry_run) and self.publish_enabled and self.meta_configured

    def can_render(self) -> bool:
        return (not self.dry_run) and self.render_enabled

    @property
    def ai_enabled(self) -> bool:
        return self.llm_enabled

    def describe(self) -> dict[str, object]:
        """Safe to print: flags and paths, never credentials."""
        return {
            "handle": "marketing",
            "dry_run": self.dry_run,
            "auto_pause": self.auto_pause,
            "publish_enabled": self.publish_enabled,
            "ai_enabled": self.llm_enabled,
            "render_enabled": self.render_enabled,
            "meta_configured": self.meta_configured,
            "ad_library_configured": self.ad_library_token_present,
            "graph_api_version": self.graph_api_version,
            "can_pause_ads": self.can_pause_ads(),
            "can_publish": self.can_publish(),
            "can_render": self.can_render(),
            "app_dir": str(self.app_dir),
            "db_path": str(self.db_path),
            "mutation_client": "not built (draft-only; render, publish and pause raise NotBuilt)",
            "meta_reads": "GET only, on request (--source api)",
        }
