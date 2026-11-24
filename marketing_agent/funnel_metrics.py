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


def window_snapshot(settings: MarketingSettings, since: str, until: str, *,
                    env: dict[str, str] | None = None, timeout: int = 300) -> FunnelHealth:
    """Funnel health over a window: halts if *any* day in it tripped a ceiling or the prepaid bar.

    Ceilings are daily in the operator sheet, so a breach on Tuesday is not averaged away by a
    quiet week. `available=False` when the export could not run; callers treat that as "not
    verified", which withholds winners (see `meta.evaluate`).
    """
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    private_csv = settings.state_dir / f"funnel_snapshot_{settings.app_dir.name}.csv"
    run_env = {**os.environ, **(env or {}), "FUNNEL_APP_DIR": str(settings.app_dir)}
    completed = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--since", since, "--until", until, "--json",
         "--offline", "--csv", str(private_csv)],
        capture_output=True, text=True, timeout=timeout, env=run_env, check=False)
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    label = f"{since}..{until}"
    if completed.returncode == 1 or not lines:
        reason = (completed.stderr.strip() or "no rows for the window")[:300]
        return FunnelHealth(day=label, available=False, halt_scaling=False, reasons=(reason,))
    days = [evaluate(d.get("day", ""), d, settings) for d in map(json.loads, lines)]
    reasons = tuple(f"{h.day}: {r}" for h in days for r in h.reasons)
    warnings = tuple(f"{h.day}: {w}" for h in days for w in h.warnings)
    return FunnelHealth(day=label, available=True, halt_scaling=any(h.halt_scaling for h in days),
                        reasons=reasons, counts={h.day: h.counts for h in days},
                        metrics={h.day: h.metrics for h in days}, warnings=warnings)


def by_ad(settings: MarketingSettings, since: str = "7daysago", until: str = "today", *,
          env: dict[str, str] | None = None, timeout: int = 120) -> dict[str, Any]:
    """Per-`utm_content` funnel counts from the export script's read-only `--by-ad` mode.

    Same reuse rule as `snapshot`: the attribution logic (first touch inside the window, joined on
    `visitor_key`) lives in the script, so the agent and the operator read the same numbers. The
    script writes nothing in this mode. Returns `{"available": False, "reason": ...}` when the
    funnel has no database rather than raising.
    """
    run_env = {**os.environ, **(env or {}), "FUNNEL_APP_DIR": str(settings.app_dir)}
    completed = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--since", since, "--until", until, "--by-ad",
         "--json", "--offline"],
        capture_output=True, text=True, timeout=timeout, env=run_env, check=False)
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if completed.returncode != 0 or not lines:
        return {"available": False,
                "reason": (completed.stderr.strip() or "no output from the export")[:300]}
    return {"available": True, **json.loads(lines[-1])}
