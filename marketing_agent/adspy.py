"""Competitor *patterns*, never competitor copy. No scraping.

Two sources, one pipeline (`ingest_items`):

* **Meta Ad Library API** (`AdLibrarySource`, primary): `GET /ads_archive` through `graph.py`, with
  `META_AD_LIBRARY_ACCESS_TOKEN` (falls back to `META_ACCESS_TOKEN`). Used only when the CLI is
  called with `--source api`. Meta's API returns commercial (non-political) ads only for ads
  delivered in the EU/UK, so `ad_reached_countries` must name EU/UK countries for this niche;
  an empty result for `US` alone is expected, not a bug.
* **JSON file** (`ingest_file`, fallback and test fixture): a list of already-normalised items:

    [{"source_ref": "adlib:123", "niche": "routine", "days_active": 41,
      "pattern": {"hook_type": "question", "cta_style": "quiz_entry", ...},
      "reference_text": "optional transcript, kept only so the gate can reject copied text"}]

What is stored is an abstraction: a hook *type*, a CTA *style*, a copy-length bucket, platforms.
`normalize_archive_ad` derives those from the ad text and stores no phrase from it in the pattern.
The ad's own words go only into `reference_text`, which `policy.copied_text` reads to *reject* a
draft that echoes them. Ads younger than `min_days_active` are skipped: longevity is the
blueprint's proxy for an ad that pays, and it is a proxy, not a measurement.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .graph import GraphReader

PATTERN_KEYS = ("hook_type", "visual_metaphor", "emotional_driver", "quiz_entry_angle", "cta_style",
                "copy_length", "platforms")

ARCHIVE_FIELDS = ("id", "ad_creation_time", "ad_delivery_start_time", "ad_delivery_stop_time",
                  "ad_creative_bodies", "ad_creative_link_titles", "ad_creative_link_captions",
                  "publisher_platforms")

#: Only these values may appear in a derived pattern, so nothing free-text from an ad leaks in.
HOOK_TYPES = ("question", "number_list", "first_person_story", "direct_address", "statement")
CTA_STYLES = ("quiz_entry", "free_offer", "learn_more", "shop", "sign_up", "other")
COPY_LENGTHS = ("short", "medium", "long")
PLATFORM_NAMES = ("facebook", "instagram", "messenger", "audience_network", "threads", "whatsapp")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def hook_type(first_line: str) -> str:
    line = first_line.strip()
    if not line:
        return "statement"
    if line.endswith("?"):
        return "question"
    if re.match(r"^\d", line):
        return "number_list"
    if re.match(r"^(i|my|i'm|i've|when i)\b", line, re.I):
        return "first_person_story"
    if re.match(r"^(you|your|if you)\b", line, re.I):
        return "direct_address"
    return "statement"


def cta_style(text: str) -> str:
    low = text.lower()
    for style, words in (("quiz_entry", ("quiz", "take the test", "answer")),
                         ("free_offer", ("free",)),
                         ("sign_up", ("sign up", "join", "start")),
                         ("shop", ("shop", "buy", "order")),
                         ("learn_more", ("learn", "discover", "find out"))):
        if any(w in low for w in words):
            return style
    return "other"


def normalize_archive_ad(ad: dict[str, Any], *, niche: str,
                         now: datetime | None = None) -> dict[str, Any] | None:
    """One `ads_archive` record -> one ingest item, or None when it has no id or start time."""
    ad_id = str(ad.get("id") or "").strip()
    start = _parse_time(ad.get("ad_delivery_start_time") or ad.get("ad_creation_time"))
    if not ad_id or start is None:
        return None
    stop = _parse_time(ad.get("ad_delivery_stop_time")) or now or datetime.now(timezone.utc)
    days = max(0, (stop - start).days)
    bodies = _strings(ad.get("ad_creative_bodies"))
    titles = _strings(ad.get("ad_creative_link_titles")) + _strings(ad.get("ad_creative_link_captions"))
    first_body = bodies[0] if bodies else ""
    first_line = re.split(r"(?<=[.!?])\s|\n", first_body.strip(), maxsplit=1)[0] if first_body else ""
    length = len(first_body)
    platforms = sorted({p.lower() for p in _strings(ad.get("publisher_platforms"))}
                       & set(PLATFORM_NAMES))
    pattern = {
        "hook_type": hook_type(first_line),
        "cta_style": cta_style(" ".join(titles + bodies[:1])),
        "quiz_entry_angle": "quiz_first" if re.search(r"\bquiz\b", first_body, re.I) else "",
        "copy_length": "short" if length < 90 else "medium" if length < 280 else "long",
        "platforms": ",".join(platforms),
    }
    return {"source_ref": f"adlib:{ad_id}", "niche": niche, "days_active": days,
            "pattern": {k: v for k, v in pattern.items() if v},
            "reference_text": "\n".join(bodies + titles)}


def _clean_pattern(raw: dict[str, Any]) -> dict[str, str]:
    allowed = {"hook_type": HOOK_TYPES, "cta_style": CTA_STYLES, "copy_length": COPY_LENGTHS}
    out: dict[str, str] = {}
    for key in PATTERN_KEYS:
        value = raw.get(key)
        if not value:
            continue
        value = str(value)[:200]
        if key in allowed and value not in allowed[key]:
            continue
        out[key] = value
    return out


def ingest_items(items: Iterable[Any], store, *, min_days_active: int = 30) -> dict[str, int]:
    stats = {"stored": 0, "duplicate": 0, "too_young": 0, "invalid": 0}
    for item in items:
        if not isinstance(item, dict) or not item.get("source_ref") or not item.get("niche"):
            stats["invalid"] += 1
            continue
        try:
            days = int(item.get("days_active") or 0)
        except (TypeError, ValueError):
            stats["invalid"] += 1
            continue
        if days < min_days_active:
            stats["too_young"] += 1
            continue
        pattern = _clean_pattern(item.get("pattern") or {})
        created = store.save_pattern(source_ref=str(item["source_ref"]), niche=str(item["niche"]),
                                     days_active=days, pattern=pattern,
                                     reference_text=str(item.get("reference_text", ""))[:5000])
        stats["stored" if created else "duplicate"] += 1
    return stats


def ingest_file(path: Path, store, *, min_days_active: int = 30) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("patterns file must be a JSON list")
    return ingest_items(data, store, min_days_active=min_days_active)


class AdLibrarySource:
    """Read-only `ads_archive` search. Builds no pattern itself; `normalize_archive_ad` does."""

    def __init__(self, reader: GraphReader) -> None:
        self.reader = reader

    def search(self, *, search_terms: str, countries: list[str], limit: int = 50,
               active_status: str = "ALL") -> list[dict[str, Any]]:
        if not search_terms.strip():
            raise ValueError("search_terms is required")
        codes = [c.strip().upper() for c in countries if c.strip()]
        if not codes or not all(re.fullmatch(r"[A-Z]{2}", c) for c in codes):
            raise ValueError("countries must be ISO-3166 alpha-2 codes, e.g. DE,FR,GB")
        if active_status not in ("ALL", "ACTIVE", "INACTIVE"):
            raise ValueError("active_status must be ALL, ACTIVE or INACTIVE")
        params = {"search_terms": search_terms.strip()[:100],
                  "ad_reached_countries": json.dumps(codes),
                  "ad_type": "ALL", "ad_active_status": active_status,
                  "fields": ",".join(ARCHIVE_FIELDS), "limit": str(max(1, min(limit, 100)))}
        return list(self.reader.paged("ads_archive", params))


def ingest_ad_library(source: AdLibrarySource, store, *, niche: str, search_terms: str,
                      countries: list[str], min_days_active: int = 30, limit: int = 50,
                      now: datetime | None = None) -> dict[str, int]:
    raw = source.search(search_terms=search_terms, countries=countries, limit=limit)
    items: list[Any] = [normalize_archive_ad(ad, niche=niche, now=now) for ad in raw]
    stats = ingest_items(items, store, min_days_active=min_days_active)
    stats["fetched"] = len(raw)
    return stats
