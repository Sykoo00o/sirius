"""Supabase Postgres access: pooled connections, transaction safety, migrations.

Connection shape matters here. `ENGINE_RUNTIME` decides whether we can trust
this process at all:

* `container` (what docker-compose sets): no sudo needed to reach the Docker
  socket, so a supervisor tick may restart a worker.
* `local` (a laptop): `container_exec` and `restart_container` refuse to run
  unless ALLOW_LOCAL_SUDO=true AND ENGINE_DRY_RUN=false. A laptop reaching a
  production Docker daemon with a `docker restart` is how an afternoon dies.

Transactions are explicit. `Database.read` runs in autocommit with a statement
timeout; `Database.tx` is a real BEGIN/COMMIT that rolls back on any exception,
including a rollback failure that would otherwise drop the connection silently.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Config, get_config

log = logging.getLogger("engine.db")

# Statements the supervisor may send to a worker container. Whitelisted rather
# than interpolated: the alternative is passing an arbitrary shell string built
# by an LLM to `docker exec sh -c`, which is a command injection with a badge on.
ALLOWED_CONTAINER_CMDS = frozenset(
    {"ps aux", "df -h", "free -m", "uptime", "cat /proc/1/status", "true"}
)


class SudoRequiredError(RuntimeError):
    """Raised when a local process tries a container operation it is not armed for."""


class MigrationError(RuntimeError):
    pass


class Database:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or get_config()
        if not self.cfg.database_url:
            raise RuntimeError("DATABASE_URL is unset; cannot open the engine database")
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ pool
    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = ConnectionPool(
                        conninfo=self.cfg.database_url,
                        min_size=self.cfg.db_pool_min,
                        max_size=self.cfg.db_pool_max,
                        kwargs={
                            "row_factory": dict_row,
                            "autocommit": True,
                            # Session-level statement timeout, applied per
                            # connection at connect time. Measured, not assumed:
                            # a bare `set local` under autocommit leaves
                            # statement_timeout at Postgres' own default (Supabase
                            # ships 2min), so per-query SET LOCAL guarded nothing.
                            # A hung migration otherwise stalls a container through
                            # the full stop_grace_period and then looks like a crash.
                            "options": f"-c statement_timeout={int(self.cfg.db_statement_timeout_ms)}",
                        },
                        # Supabase's pooler drops idle conns; a stale conn in the
                        # pool surfaces as a confusing 'server closed connection'.
                        max_idle=120,
                        max_lifetime=1800,
                        check=ConnectionPool.check_connection,
                        timeout=15,
                    )
                    # Explicit, non-blocking open. Two reasons this is not left
                    # implicit: psycopg warns that implicit-open is going away,
                    # and open(wait=False) returns immediately, so a slow or dead
                    # database costs a query its timeout rather than stalling the
                    # process at construction (which is what `wait=True` would do).
                    self._pool.open(wait=False)
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ reads
    def read(self, query: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with self.pool.connection(timeout=15) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall())

    def read_one(self, query: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = self.read(query, params)
        return rows[0] if rows else None

    def scalar(self, query: str, params: Sequence[Any] | None = None, default: Any = None) -> Any:
        row = self.read_one(query, params)
        if not row:
            return default
        return next(iter(row.values()))

    # ----------------------------------------------------------------- writes
    def execute(self, query: str, params: Sequence[Any] | None = None) -> int:
        """Single statement, autocommit. Returns rowcount.

        Statement timeout applies via the pool's per-connection options: a write
        blocked on a lock is the common case, and an unwedged CLI matters as much
        as an unwedged read.
        """
        with self.pool.connection(timeout=15) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """BEGIN/COMMIT with guaranteed rollback and connection disposal.

        Yields the connection so callers can chain statements and read back
        LAST_INSERT_ID-equivalents inside the same transaction. If COMMIT
        raises, the transaction is definitely not applied, and psycopg discards
        the connection when it is returned to the pool.
        """
        with self.pool.connection(timeout=15) as conn:
            autocommit = conn.autocommit
            conn.autocommit = False
            try:
                yield conn
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception as rb_exc:
                    # Rollback itself failed: the connection is unusable and will
                    # be dumped, but say so loudly, since this is the case where a
                    # charge may or may not have been committed.
                    log.error("rollback failed after error; connection discarded: %s", rb_exc)
                raise
            finally:
                conn.autocommit = autocommit

    def execute_many(self, statement: str, seq: Sequence[Sequence[Any]]) -> int:
        if not seq:
            return 0
        with self.tx() as conn, conn.cursor() as cur:
            cur.executemany(statement, seq)
            return cur.rowcount

    # ------------------------------------------------------- engine primitives
    def call(self, fn: str, *args: Any) -> Any:
        """Call an engine.* function and return its value.

        The identifier is validated, not parameterised: Postgres takes no
        placeholder for a function name. The argument list uses real placeholders.
        """
        query = f"select * from engine.{pg_identifier(fn)}({placeholders_text(args)})"
        row = self.read_one(query, args)
        if row is None:
            return None
        # A table-returning function (request_approval) comes back as several
        # columns; a scalar function comes back as one. Callers expect either.
        return next(iter(row.values())) if len(row) == 1 else row

    def setting(self, key: str, default: Any = None) -> Any:
        raw = self.read_one("select engine.get_setting(%s, %s::jsonb) as v", (key, json.dumps(None)))
        value = raw["v"] if raw else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        return default if value is None else value

    def is_dry_run(self) -> bool:
        """Live dry-run switch. Defaults True: unknown means do not spend."""
        return bool(self.setting("engine.dry_run", True))

    def set_setting(self, key: str, value: Any, actor: str = "system", description: str = "") -> None:
        self.execute(
            """
            insert into engine.settings (key, value, description, updated_at, updated_by)
            values (%s, %s::jsonb, nullif(%s, ''), now(), %s)
            on conflict (key) do update
              set value = excluded.value,
                  description = coalesce(nullif(excluded.description, ''), engine.settings.description),
                  updated_at = now(),
                  updated_by = excluded.updated_by
            """,
            (key, json.dumps(value), description, actor),
        )


def pg_identifier(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def placeholders_text(args: Sequence[Any]) -> str:
    return ", ".join(["%s"] * len(args))


# ------------------------------------------------------------------------ ops
def assert_container_allowed(cfg: Config, action: str) -> None:
    """Gate Docker control by runtime, per the safety rule in the module docstring."""
    if cfg.is_container:
        return
    if cfg.allow_local_sudo and not cfg.engine_dry_run:
        log.warning("running %s from a LOCAL runtime via ALLOW_LOCAL_SUDO", action)
        return
    raise SudoRequiredError(
        f"refusing to {action} from runtime=local. Set ENGINE_RUNTIME=container when this "
        "process runs in Docker, or ALLOW_LOCAL_SUDO=true together with ENGINE_DRY_RUN=false "
        "if you really mean to control Docker from a laptop."
    )


# ------------------------------------------------------------------- migration
class Migrator:
    """File-based migrations with checksums, applied under an advisory lock.

    Concurrency is the whole design here: `docker compose up` starts five
    containers, each of which wants the schema present. Without the advisory
    lock they race, and two of them apply the same migration.
    """

    LOCK_KEY = 0x6E676E72  # 'ngnr'

    def __init__(self, db: Database, migrations_dir: Path) -> None:
        self.db = db
        self.dir = Path(migrations_dir)

    def ensure_table(self) -> None:
        # engine.settings and engine.schema_migrations must exist before
        # migration 0002 runs: 0002 forces RLS onto the migration journal, and
        # FORCE row level security fails on a table that does not exist yet.
        self.db.execute(
            """
            create schema if not exists engine
            """
        )
        self.db.execute(
            """
            create table if not exists engine.settings (
              key         text primary key,
              value       jsonb   not null,
              description text,
              updated_at  timestamptz not null default now(),
              updated_by  text
            )
            """
        )
        self.db.execute(
            """
            create table if not exists engine.schema_migrations (
              version     text primary key,
              checksum    text not null,
              applied_at  timestamptz not null default now(),
              duration_ms integer not null default 0,
              applied_by  text not null default current_user
            )
            """
        )

    def _files(self) -> list[Path]:
        if not self.dir.is_dir():
            raise MigrationError(f"migrations directory not found: {self.dir}")
        return sorted(self.dir.glob("*.sql"))

    def pending(self) -> list[Path]:
        self.ensure_table()
        applied = {r["version"]: r["checksum"] for r in self.db.read(
            "select version, checksum from engine.schema_migrations"
        )}
        out: list[Path] = []
        for path in self._files():
            checksum = _checksum(path)
            if path.name in applied:
                if applied[path.name] != checksum:
                    raise MigrationError(
                        f"{path.name} was edited after it was applied "
                        f"(checksum {applied[path.name][:12]} -> {checksum[:12]}). "
                        "Applied migrations are immutable: add a new migration."
                    )
                continue
            out.append(path)
        return out

    def apply_all(self, *, dry_run: bool = False) -> list[str]:
        from .seeds import apply_seeds

        pending = self.pending()
        if not pending:
            log.info("schema up to date; nothing to migrate")
            if not dry_run:
                apply_seeds(self.db)
            return []

        with self.db.tx() as conn, conn.cursor() as cur:
            # Session-level advisory lock: held until COMMIT/ROLLBACK, so it
            # covers the whole apply, and releases itself if the process dies.
            cur.execute("select pg_advisory_xact_lock(%s)", (self.LOCK_KEY,))
            cur.execute("select version, checksum from engine.schema_migrations")
            applied_now = {r["version"]: r["checksum"] for r in cur.fetchall()}

            done: list[str] = []
            for path in pending:
                if path.name in applied_now:
                    continue  # another container beat us to it
                import hashlib
                import time

                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                script = path.read_text(encoding="utf-8")
                log.info("applying migration %s", path.name)
                started = time.monotonic()
                # One script per implicit transaction: the whole file is
                # already inside the outer tx, so a failure rolls back all of it.
                cur.execute(script)
                elapsed = int((time.monotonic() - started) * 1000)
                cur.execute(
                    "insert into engine.schema_migrations (version, checksum, duration_ms) "
                    "values (%s, %s, %s)",
                    (path.name, checksum, elapsed),
                )
                done.append(path.name)
                log.info("applied %s in %dms", path.name, elapsed)

        if not dry_run:
            apply_seeds(self.db)
        return done

    def status(self) -> list[dict[str, Any]]:
        self.ensure_table()
        applied = {r["version"]: r for r in self.db.read("select * from engine.schema_migrations")}
        out = []
        for path in self._files():
            row = applied.get(path.name)
            out.append(
                {
                    "version": path.name,
                    "applied": row is not None,
                    "applied_at": row["applied_at"] if row else None,
                    "checksum_matches": (
                        row["checksum"] == _checksum(path) if row else None
                    ),
                }
            )
        return out


def _checksum(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrations_dir() -> Path:
    """Locate supabase/migrations, allowing an explicit override.

    Source-tree layout is repo/supabase/migrations, which is parents[2] from this
    file. A container image may stage the SQL elsewhere, so ENGINE_MIGRATIONS_DIR
    wins when set: the alternative is baking the same assumption into every image.
    """
    import os

    override = (os.getenv("ENGINE_MIGRATIONS_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def make_migrator(cfg: Config | None = None) -> Migrator:
    cfg = cfg or get_config()
    return Migrator(Database(cfg), migrations_dir())


def row_json(row: dict[str, Any]) -> dict[str, Any]:
    """Render a DB row for JSON transport: Decimal and datetime are not JSON-native."""
    return json.loads(json.dumps(row, default=str))
