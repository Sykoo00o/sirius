"""Competitor pattern ingestion from a file. No scraping.

Input is a JSON list, e.g. an export from Meta's official Ad Library API or notes typed by hand:

    [{"source_ref": "adlib:123", "niche": "routine", "days_active": 41,
      "pattern": {"hook_type": "question", "visual_metaphor": "...", "emotional_driver": "...",
                  "quiz_entry_angle": "..."},
      "reference_text": "optional transcript, kept only so the gate can reject copied text"}]

Only the whitelisted pattern keys are stored. Ads younger than `min_days_active` are skipped:
longevity is the blueprint's proxy for an ad that pays, and it is a proxy, not a measurement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PATTERN_KEYS = ("hook_type", "visual_metaphor", "emotional_driver", "quiz_entry_angle", "cta_style")


def ingest_file(path: Path, store, *, min_days_active: int = 30) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("patterns file must be a JSON list")
    stats = {"stored": 0, "duplicate": 0, "too_young": 0, "invalid": 0}
    for item in data:
        if not isinstance(item, dict) or not item.get("source_ref") or not item.get("niche"):
            stats["invalid"] += 1
            continue
        days = int(item.get("days_active") or 0)
        if days < min_days_active:
            stats["too_young"] += 1
            continue
        raw: dict[str, Any] = item.get("pattern") or {}
        pattern = {k: str(raw[k])[:200] for k in PATTERN_KEYS if raw.get(k)}
        created = store.save_pattern(source_ref=str(item["source_ref"]), niche=str(item["niche"]),
                                     days_active=days, pattern=pattern,
                                     reference_text=str(item.get("reference_text", ""))[:5000])
        stats["stored" if created else "duplicate"] += 1
    return stats
