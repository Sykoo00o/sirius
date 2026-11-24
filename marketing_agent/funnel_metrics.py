"""Funnel health, read through the operator's own metrics script.

The agent does not re-implement a single ratio. It runs `scripts/export_lane_c_metrics.py` with
`--json --offline` against the target app (`FUNNEL_APP_DIR`) and a **private** `--csv` under the
agent's state dir, so:

* the counts (`lp_view` … `trial_paid`), `bin_prepaid_share_pct`, refund, dispute and fraud ratios
  are byte-for-byte the numbers on the operator's sheet;
* the operator's CSV is never written by the agent;
* no network call is made (`--offline`), and the funnel databases are opened read-only by the script.

Halting rule: a ceiling breach reported by the script, or a prepaid share above the configured
threshold, sets `halt_scaling`. That overrides every per-ad "winner" recommendation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, MarketingSettings

EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_lane_c_metrics.py"


@dataclass(frozen=True)
class FunnelHealth:
    day: str
    available: bool
    halt_scaling: bool
    reasons: tuple[str, ...] = ()
    counts: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "available": self.available, "halt_scaling": self.halt_scaling,
                "reasons": list(self.reasons), "warnings": list(self.warnings),
                "counts": self.counts, "metrics": self.metrics}


def _pct(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(day: str, detail: dict[str, Any], settings: MarketingSettings) -> FunnelHealth:
    metrics = detail.get("metrics") or {}
    reasons = [f"ceiling: {b}" for b in detail.get("breaches") or []]
    prepaid = _pct(metrics.get("bin_prepaid_share_pct"))
    if prepaid is not None and prepaid > settings.prepaid_share_halt_pct:
        reasons.append(f"prepaid share {prepaid}% > {settings.prepaid_share_halt_pct}%")
    return FunnelHealth(day=day, available=True, halt_scaling=bool(reasons),
                        reasons=tuple(reasons), counts=detail.get("counts") or {},
                        metrics=metrics, warnings=tuple(detail.get("warnings") or ()))


def snapshot(settings: MarketingSettings, day: str = "yesterday", *,
             env: dict[str, str] | None = None, timeout: int = 120) -> FunnelHealth:
    """Run the export for one day. `available=False` (never a halt) when the funnel has no data."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    private_csv = settings.state_dir / f"funnel_snapshot_{settings.app_dir.name}.csv"
    run_env = {**os.environ, **(env or {}), "FUNNEL_APP_DIR": str(settings.app_dir)}
    completed = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--day", day, "--json", "--offline",
         "--csv", str(private_csv)],
        capture_output=True, text=True, timeout=timeout, env=run_env, check=False)
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if completed.returncode == 1 or not lines:
        reason = (completed.stderr.strip() or "no rows for the day")[:300]
        return FunnelHealth(day=day, available=False, halt_scaling=False, reasons=(reason,))
    payload = json.loads(lines[-1])
    return evaluate(payload.get("day", day), payload, settings)
