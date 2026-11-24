"""The agent's own SQLite state. Same conventions as `funnel_app/telemetry.py`: WAL, one connection
per call, a process lock, append-only logs, and partial unique `idempotency_key` indexes.

`agent_logs` has exactly the funnel's columns so a later lift into `engine.agent_logs` is a copy.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HANDLE = "marketing"

DRAFT = "DRAFT"
APPROVED = "APPROVED"
STATUSES = (DRAFT, "FLAGGED", "PASSED_CHECKS", APPROVED)

_SCHEMA = """
create table if not exists creative_briefs (
  brief_id      text primary key,
  app           text not null,
  niche         text not null,
  concept_name  text not null,
  payload_json  text not null,
  status        text not null default 'DRAFT',
  created_at    text not null,
  updated_at    text not null
);
create table if not exists compliance_audits (
  id            integer primary key autoincrement,
  brief_id      text not null,
  status        text not null,
  layers        text not null default '',
  findings_json text not null default '[]',
  created_at    text not null
);
create index if not exists compliance_audits_brief_idx on compliance_audits (brief_id, id);
create table if not exists asset_variants (
  variant_code  text primary key,
  brief_id      text not null,
  asset_type    text not null,
  status        text not null default 'PLANNED',
  storage_url   text not null default '',
  meta_ad_id    text not null default '',
  created_at    text not null
);
create table if not exists competitor_patterns (
  id             integer primary key autoincrement,
  source_ref     text not null unique,
  niche          text not null,
  days_active    integer not null default 0,
  pattern_json   text not null default '{}',
  reference_text text not null default '',
  created_at     text not null
);
create table if not exists ad_telemetry (
  id                 integer primary key autoincrement,
  meta_ad_id         text not null,
  window_end         text not null,
  variant_code       text not null default '',
  spend_minor        integer not null default 0,
  impressions        integer not null default 0,
  video_3s_views     integer not null default 0,
  video_p50_views    integer not null default 0,
  outbound_clicks    integer not null default 0,
  purchases          integer not null default 0,
  revenue_minor      integer not null default 0,
  hours_live         integer not null default 0,
  created_at         text not null,
  unique (meta_ad_id, window_end)
);
create table if not exists recommendations (
  id          integer primary key autoincrement,
  meta_ad_id  text not null,
  window_end  text not null,
  action      text not null,
  reason      text not null,
  executed    integer not null default 0,
  created_at  text not null,
  unique (meta_ad_id, window_end)
);
create table if not exists agent_logs (
  id              integer primary key autoincrement,
  created_at      text    not null,
  agent_handle    text    not null default 'marketing',
  level           text    not null default 'info',
  event           text    not null,
  message         text    not null default '',
  context_json    text    not null default '{}',
  is_audit        integer not null default 0,
  idempotency_key text
);
create index if not exists agent_logs_event_idx on agent_logs (event, created_at);
create unique index if not exists agent_logs_dedupe_uq
  on agent_logs (idempotency_key) where idempotency_key is not null;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class MarketingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma journal_mode = wal")
        return conn

    # ---- logs --------------------------------------------------------------

    def log(self, *, event: str, message: str = "", level: str = "info",
            context: dict[str, Any] | None = None, is_audit: bool = False,
            idempotency_key: str = "") -> bool:
        if level not in ("debug", "info", "warning", "error", "critical"):
            raise ValueError(f"unknown log level {level!r}")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """insert or ignore into agent_logs (created_at, agent_handle, level, event,
                   message, context_json, is_audit, idempotency_key)
                   values (?, ?, ?, ?, ?, ?, ?, ?)""",
                (utc_now(), HANDLE, level, event, message[:500],
                 json.dumps(context or {}, ensure_ascii=False, default=str),
                 1 if is_audit else 0, idempotency_key or None))
            return cur.rowcount > 0

    def log_rows(self, event: str = "") -> list[dict[str, Any]]:
        sql, args = "select * from agent_logs", ()
        if event:
            sql, args = sql + " where event = ?", (event,)
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql + " order by id", args)]

    # ---- briefs ------------------------------------------------------------

    def save_brief(self, bid: str, payload: dict[str, Any]) -> bool:
        """True when this call created the brief; False when the same content already exists."""
        now = utc_now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """insert or ignore into creative_briefs
                   (brief_id, app, niche, concept_name, payload_json, status, created_at, updated_at)
                   values (?, ?, ?, ?, ?, 'DRAFT', ?, ?)""",
                (bid, payload["app"], payload["niche"], payload["concept_name"],
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), now, now))
            return cur.rowcount > 0

    def get_brief(self, bid: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("select * from creative_briefs where brief_id = ?", (bid,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["payload"] = json.loads(out.pop("payload_json"))
        return out

    def record_audit(self, bid: str, result: dict[str, Any]) -> None:
        """Append the audit and move the brief to the gate's verdict. An approved brief that is
        re-audited and flagged drops back to FLAGGED: approval does not outlive a failing gate."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """insert into compliance_audits (brief_id, status, layers, findings_json, created_at)
                   values (?, ?, ?, ?, ?)""",
                (bid, result["status"], ",".join(result["layers"]),
                 json.dumps(result["findings"], ensure_ascii=False), utc_now()))
            current = conn.execute("select status from creative_briefs where brief_id = ?",
                                   (bid,)).fetchone()
            keep_approved = current and current["status"] == APPROVED and \
                result["status"] == "PASSED_CHECKS"
            if not keep_approved:
                conn.execute("update creative_briefs set status = ?, updated_at = ? where brief_id = ?",
                             (result["status"], utc_now(), bid))

    def approve(self, bid: str, *, actor: str) -> tuple[bool, str]:
        """Human approval. Only a brief whose latest audit passed can be approved."""
        actor = actor.strip()
        if not actor:
            return False, "approval needs a named actor"
        brief = self.get_brief(bid)
        if brief is None:
            return False, "no such brief"
        if brief["status"] == APPROVED:
            return True, "already approved"
        if brief["status"] != "PASSED_CHECKS":
            return False, f"brief is {brief['status']}; only PASSED_CHECKS can be approved"
        with self._lock, self._connect() as conn:
            conn.execute("update creative_briefs set status = ?, updated_at = ? where brief_id = ?",
                         (APPROVED, utc_now(), bid))
        self.log(event="marketing.brief_approved", message=f"brief {bid} approved by {actor}",
                 context={"brief_id": bid, "actor": actor}, is_audit=True,
                 idempotency_key=f"approve:{bid}")
        return True, "approved"

    def plan_variant(self, variant_code: str, bid: str, asset_type: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """insert or ignore into asset_variants (variant_code, brief_id, asset_type, created_at)
                   values (?, ?, ?, ?)""", (variant_code, bid, asset_type, utc_now()))
            return cur.rowcount > 0

    def variant(self, variant_code: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("select * from asset_variants where variant_code = ?",
                               (variant_code,)).fetchone()
            return dict(row) if row else None

    def variants(self, bid: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "select * from asset_variants where brief_id = ? order by variant_code", (bid,))]

    # ---- patterns, telemetry, recommendations ------------------------------

    def save_pattern(self, *, source_ref: str, niche: str, days_active: int,
                     pattern: dict[str, Any], reference_text: str = "") -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """insert or ignore into competitor_patterns
                   (source_ref, niche, days_active, pattern_json, reference_text, created_at)
                   values (?, ?, ?, ?, ?, ?)""",
                (source_ref, niche, int(days_active), json.dumps(pattern, sort_keys=True),
                 reference_text, utc_now()))
            return cur.rowcount > 0

    def patterns(self, niche: str = "") -> list[dict[str, Any]]:
        """Abstract patterns only (`pattern_json`); never the reference text."""
        sql, args = "select pattern_json from competitor_patterns", ()
        if niche:
            sql, args = sql + " where niche = ?", (niche,)
        with self._lock, self._connect() as conn:
            return [json.loads(r["pattern_json"]) for r in conn.execute(sql + " order by id", args)]

    def set_variant_status(self, variant_code: str, status: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("update asset_variants set status = ? where variant_code = ?",
                         (status, variant_code))

    def reference_texts(self, niche: str = "") -> list[str]:
        sql, args = "select reference_text from competitor_patterns where reference_text != ''", ()
        if niche:
            sql, args = sql + " and niche = ?", (niche,)
        with self._lock, self._connect() as conn:
            return [r[0] for r in conn.execute(sql, args)]

    def save_telemetry(self, row: dict[str, Any]) -> bool:
        cols = ("meta_ad_id", "window_end", "variant_code", "spend_minor", "impressions",
                "video_3s_views", "video_p50_views", "outbound_clicks", "purchases",
                "revenue_minor", "hours_live")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"insert or ignore into ad_telemetry ({', '.join(cols)}, created_at) "
                f"values ({', '.join('?' for _ in cols)}, ?)",
                tuple(row.get(c, "" if c in ("variant_code",) else 0) for c in cols) + (utc_now(),))
            return cur.rowcount > 0

    def save_recommendation(self, *, meta_ad_id: str, window_end: str, action: str,
                            reason: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """insert or ignore into recommendations
                   (meta_ad_id, window_end, action, reason, executed, created_at)
                   values (?, ?, ?, ?, 0, ?)""",
                (meta_ad_id, window_end, action, reason, utc_now()))
            return cur.rowcount > 0

    def recommendations(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute("select * from recommendations order by id")]
