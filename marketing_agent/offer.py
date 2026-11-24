"""The funnel's price terms, read from the funnel's own sources so an ad cannot quote a stale price.

Order, lowest to highest precedence: the `FunnelSettings` class defaults in `<app>/config.py`
(parsed as text, not imported), then the app's `.env`, then this process's environment. Only the
price and identity keys below are read from `.env`; nothing else in that file (Stripe keys
included) is loaded into this process.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_KEYS = {
    "FUNNEL_TRIAL_AMOUNT_MINOR": "trial_amount_minor",
    "FUNNEL_TRIAL_DAYS": "trial_days",
    "FUNNEL_REBILL_AMOUNT_MINOR": "rebill_amount_minor",
    "FUNNEL_REFUND_WINDOW_DAYS": "refund_window_days",
    "FUNNEL_CURRENCY": "currency",
    "FUNNEL_BRAND_NAME": "brand_name",
    "FUNNEL_PUBLIC_URL": "public_url",
}
_INT_FIELDS = {"trial_amount_minor", "trial_days", "rebill_amount_minor", "refund_window_days"}


@dataclass(frozen=True)
class OfferTerms:
    brand_name: str
    public_url: str
    trial_amount_minor: int
    trial_days: int
    rebill_amount_minor: int
    refund_window_days: int
    currency: str = "usd"

    def money(self, minor: int) -> str:
        """Same display rule as `funnel_copy.money`, so ad and page print the same string."""
        symbol = "$" if self.currency == "usd" else f"{self.currency.upper()} "
        return f"{symbol}{minor / 100:.2f}".rstrip("0").rstrip(".")

    @property
    def trial_amount(self) -> str:
        return self.money(self.trial_amount_minor)

    @property
    def rebill_per_month(self) -> str:
        return f"{self.money(self.rebill_amount_minor)}/month"

    def price_line(self, claims) -> str:
        """The funnel's own disclosure sentence (`claims.price_line`)."""
        return claims.price_line(claims.Slots(
            trial_amount=self.trial_amount, rebill_per_month=self.rebill_per_month,
            trial_days=self.trial_days, refund_days=self.refund_window_days, renewal_date=""))


def _class_defaults(config_text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for field in _KEYS.values():
        match = re.search(rf"^\s+{field}:\s*\w+\s*=\s*(.+?)\s*(#.*)?$", config_text, re.M)
        if match:
            found[field] = match.group(1).strip().strip("\"'")
    return found


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        if key in _KEYS:
            values[_KEYS[key]] = value.strip().strip("\"'")
    return values


def load_offer(app_dir: Path) -> OfferTerms:
    config_path = app_dir / "config.py"
    merged = _class_defaults(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    merged.update(_dotenv(app_dir / ".env"))
    for env_key, field in _KEYS.items():
        if os.getenv(env_key, "").strip():
            merged[field] = os.environ[env_key].strip()
    missing = [f for f in ("trial_amount_minor", "trial_days", "rebill_amount_minor") if f not in merged]
    if missing:
        raise ValueError(f"cannot read {missing} from {config_path} or its .env")
    kwargs: dict[str, object] = {
        "brand_name": merged.get("brand_name", app_dir.name),
        "public_url": merged.get("public_url", "").rstrip("/"),
        "currency": merged.get("currency", "usd").lower(),
        "refund_window_days": 14,
    }
    for field in _INT_FIELDS:
        if field in merged:
            kwargs[field] = int(merged[field])
    return OfferTerms(**kwargs)  # type: ignore[arg-type]
