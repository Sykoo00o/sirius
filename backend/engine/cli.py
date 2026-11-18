"""`engine` CLI: migrations, status, approvals, ledger queries, worker runners.

Built with Typer so `--help` is real. Every command exits non-zero on failure
so a healthcheck or a deploy script can rely on it, and every amount is entered
and printed in dollars while stored in minors: the CLI is where a human types
money, so it is where a decimal error would be born.

No command here mutates the ledger except `ledger revert`, and that one writes a
compensating entry rather than deleting history.
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from .config import get_config
from .db import Database, make_migrator
from .finance_gatekeeper import FinanceGatekeeper, fmt_minor
from .logging_setup import setup_logging
from .settings import SettingsAccessor

app = typer.Typer(add_completion=False, help="Ghost X2 engine controls.")
console = Console()
err_console = Console(stderr=True)


def _db() -> Database:
    setup_logging("cli")
    return Database(get_config())


def _dollars_to_minor(text: str) -> int:
    """Parse '$1,234.56' into 123456. Rejects anything ambiguous."""
    cleaned = text.strip().replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned:
        raise typer.BadParameter("empty amount")
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise typer.BadParameter(f"'{text}' is not an amount") from exc
    minor = round(value * 100)
    if abs(minor - value * 100) > 0.5:
        raise typer.BadParameter(f"'{text}' has more precision than cents")
    return int(minor)


@app.callback()
def _root() -> None:
    """Ghost X2 engine controls."""


# --------------------------------------------------------------------- migrate
@app.command()
def migrate(
    dry: bool = typer.Option(False, "--dry", help="List what would run, change nothing."),
) -> None:
    """Apply pending schema migrations under an advisory lock."""
    migrator = make_migrator()
    pending = migrator.pending()
    if not pending:
        console.print("[green]schema up to date[/]")
        if not dry:
            from .seeds import apply_seeds

            with _db() as db:
                apply_seeds(db)
        return
    for path in pending:
        console.print(f"  {'would apply' if dry else 'applying'} {path.name}")
    if dry:
        return
    applied = migrator.apply_all()
    console.print(f"[green]applied {len(applied)} migration(s)[/]")


@app.command()
def status() -> None:
    """Fleet health, money today, and pending approvals in one screen."""
    db = _db()
    try:
        cfg = get_config()
        settings = SettingsAccessor(db)
        console.print(
            f"runtime=[bold]{cfg.runtime}[/] env={cfg.env} "
            f"dry_run=[bold]{'on' if settings.dry_run else 'OFF (live money)'}[/]"
        )

        table = Table(title="Fleet")
        for col in ("agent", "desired", "actual", "last beat", "llm today", "cap"):
            table.add_column(col)
        stale_after = settings.heartbeat_stale_seconds
        for a in db.read("select * from engine.vw_agent_fleet_health order by handle"):
            seconds = a.get("seconds_since_beat")
            if seconds is None:
                beat, style = "never", "red"
            elif int(seconds) > stale_after:
                beat, style = f"{int(seconds)}s ago", "red"
            else:
                beat, style = f"{int(seconds)}s ago", "green"
            cost = float(a.get("today_llm_cost_usd") or 0)
            table.add_row(
                a["handle"], a["desired_state"], a["status"],
                f"[{style}]{beat}[/]", f"${cost:.3f}",
                f"${float(a.get('llm_cap_usd') or 0):.2f}",
            )
        console.print(table)

        row = db.read_one("select * from engine.vw_executive_metrics") or {}
        console.print(
            f"in 24h {fmt_minor(int(row.get('revenue_minor') or 0))}  "
            f"out {fmt_minor(int(row.get('cost_minor') or 0))}  "
            f"net [bold]{fmt_minor(int(row.get('net_24h_minor') or 0))}[/]  "
            f"mrr {fmt_minor(int(row.get('mrr_minor') or 0))}  "
            f"pending {row.get('pending_approvals') or 0} "
            f"({fmt_minor(int(row.get('pending_spend_minor') or 0))})"
        )
    finally:
        db.close()


# ------------------------------------------------------------------ approvals
@app.command("approvals")
def list_approvals(
    state: str = typer.Option("pending", "--state",
                              help="pending|approved|rejected|expired|blocked|all"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List approvals."""
    db = _db()
    try:
        sql = "select id, title, cost_minor, currency, tier, state, created_at, decided_by " \
              "from engine.approval_requests"
        params: list = []
        if state != "all":
            sql += " where state = %s"
            params.append(state)
        sql += " order by created_at desc limit %s"
        params.append(limit)
        table = Table(title=f"approvals: {state}")
        for col in ("id", "title", "amount", "tier", "state", "age", "by"):
            table.add_column(col, overflow="fold")
        for r in db.read(sql, params):
            table.add_row(
                str(r["id"])[:8], str(r["title"])[:60],
                fmt_minor(int(r["cost_minor"]), r["currency"]), r["tier"], r["state"],
                _age(r["created_at"]), str(r.get("decided_by") or "")[:18])
        console.print(table)
    finally:
        db.close()


@app.command()
def approve(
    approval_id: str = typer.Argument(help="Approval id (full uuid or first 8 chars)."),
    note: str | None = typer.Option(None, "--note"),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Approve a pending request."""
    _decide(approval_id, "approve", note, actor)


@app.command()
def reject(
    approval_id: str = typer.Argument(help="Approval id (full uuid or first 8 chars)."),
    note: str | None = typer.Option(None, "--note"),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Reject a pending request."""
    _decide(approval_id, "reject", note, actor)


def _decide(approval_id: str, decision: str, note: str | None, actor: str) -> None:
    db = _db()
    try:
        row = db.read_one(
            "select id::text as id, title, cost_minor, currency, tier, state, "
            "replace(id::text,'-','') as flat from engine.approval_requests "
            "where id::text = %s or replace(id::text,'-','') like %s",
            (approval_id, f"{approval_id.replace('-', '')}%"))
        if not row:
            err_console.print(f"[red]no approval matching {approval_id}[/]")
            raise typer.Exit(1)
        if row["state"] != "pending":
            err_console.print(f"[yellow]{row['id']} is already {row['state']}[/]")
            raise typer.Exit(2)

        gate = FinanceGatekeeper(db, get_config())
        try:
            state = gate.decide(str(row["id"]), decision, actor, note)
        except Exception as exc:
            err_console.print(f"[red]refused:[/] {exc}")
            raise typer.Exit(3) from exc
        colour = "green" if state in ("approved", "auto_approved") else "yellow"
        console.print(
            f"[{colour}]{state}[/] {row['title']} "
            f"({fmt_minor(int(row['cost_minor']), row['currency'])}, {row['tier']})")
    finally:
        db.close()


# --------------------------------------------------------------------- spend
@app.command()
def spend(
    agent: str = typer.Option(
        ..., "--agent",
        help="Registered agent handle requesting the spend, eg worker:arbitrage."),
    title: str = typer.Option(..., "--title"),
    amount: str = typer.Option(..., "--amount", help="Dollars, eg 75 or $1,200.50."),
    rationale: str = typer.Option("", "--rationale"),
    vendor: str | None = typer.Option(None, "--vendor"),
    source_url: str | None = typer.Option(None, "--url"),
    roi: str | None = typer.Option(None, "--roi"),
    idempotency_key: str | None = typer.Option(None, "--key"),
) -> None:
    """Ask the gatekeeper to authorise a spend, and print the decision."""
    db = _db()
    try:
        gate = FinanceGatekeeper(db, get_config())
        intent = gate.preflight(
            agent, title, _dollars_to_minor(amount), rationale=rationale, vendor=vendor,
            source_url=source_url, expected_roi=roi, idempotency_key=idempotency_key)
        console.print(intent.describe())
        if intent.approval_id:
            console.print(f"approval id: [bold]{intent.approval_id}[/]")
        console.print(f"envelope: {json.dumps(gate.card_envelope(agent), default=str)}")
        raise typer.Exit(0 if intent.execute else 4)
    finally:
        db.close()


# -------------------------------------------------------------------- ledger
@app.command("ledger")
def ledger(
    limit: int = typer.Option(25, "--limit"),
    hustle: str | None = typer.Option(None, "--hustle"),
    account: str | None = typer.Option(None, "--account"),
) -> None:
    """Show recent ledger entries."""
    db = _db()
    try:
        sql = "select id, occurred_at, entry_type, amount_minor, currency, description, " \
              "account, reversed_by from engine.ledger_entries"
        clauses, params = [], []
        if hustle:
            clauses.append("hustle = %s::engine.hustle_kind")
            params.append(hustle)
        if account:
            clauses.append("account = %s")
            params.append(account)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by occurred_at desc limit %s"
        params.append(limit)
        table = Table(title="ledger")
        for col in ("id", "when", "type", "amount", "description", "rev"):
            table.add_column(col, overflow="fold")
        for r in db.read(sql, params):
            amount = int(r["amount_minor"])
            colour = "green" if amount > 0 else "red"
            table.add_row(
                str(r["id"]), r["occurred_at"].strftime("%m-%d %H:%M"), r["entry_type"],
                f"[{colour}]{fmt_minor(amount, r['currency'])}[/]",
                str(r["description"])[:70], "yes" if r["reversed_by"] else "")
        console.print(table)
    finally:
        db.close()


@app.command("ledger-revert")
def ledger_revert(
    entry_id: int = typer.Argument(help="Ledger entry id to compensate."),
    reason: str = typer.Option(..., "--reason"),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Reverse a ledger entry with a compensating entry (history is never deleted)."""
    db = _db()
    try:
        try:
            reversal = db.call("reverse_ledger_entry", entry_id, reason, actor)
        except Exception as exc:
            err_console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        console.print(f"posted reversal {reversal} for entry {entry_id}")
    finally:
        db.close()


# ------------------------------------------------------------------- targets
@app.command("targets")
def targets(
    status_filter: str = typer.Option("all", "--status"),
    limit: int = typer.Option(25, "--limit"),
) -> None:
    """List sourced opportunities."""
    db = _db()
    try:
        sql = ("select id, hustle, source, title, status, acquisition_cost_minor, "
               "resale_floor_minor, expected_margin_pct from engine.arbitrage_targets")
        params: list = []
        if status_filter != "all":
            sql += " where status = %s::engine.target_status"
            params.append(status_filter)
        sql += " order by discovered_at desc limit %s"
        params.append(limit)
        table = Table(title=f"targets: {status_filter}")
        for col in ("id", "hustle", "source", "title", "status", "cost", "floor", "margin"):
            table.add_column(col, overflow="fold")
        for r in db.read(sql, params):
            table.add_row(
                str(r["id"])[:8], r["hustle"], str(r["source"])[:14], str(r["title"])[:40],
                r["status"],
                fmt_minor(int(r["acquisition_cost_minor"] or 0)),
                fmt_minor(int(r["resale_floor_minor"] or 0)),
                f"{float(r['expected_margin_pct']):.1f}%" if r["expected_margin_pct"] else "-")
        console.print(table)
    finally:
        db.close()


# ------------------------------------------------------------------ settings
@app.command("settings")
def settings_cmd(
    key: str | None = typer.Argument(None),
    value: str | None = typer.Option(None, "--set", help="JSON or plain text value."),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Read one setting, list all, or set one."""
    db = _db()
    try:
        access = SettingsAccessor(db)
        if key and value is not None:
            parsed: object = value
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                pass
            previous = access.get(key, None)
            access.set(key, parsed, actor=actor)
            console.print(f"{key}: {previous!r} -> {parsed!r}")
            return
        if key:
            console.print(json.dumps(access.get(key, None), indent=2, default=str))
            return
        table = Table(title="settings")
        for col in ("key", "value", "updated by"):
            table.add_column(col, overflow="fold")
        for r in access.all_for_display():
            table.add_row(r["key"], json.dumps(r["value"], default=str)[:70],
                          str(r.get("updated_by") or ""))
        console.print(table)
    finally:
        db.close()


@app.command("dry-run")
def dry_run(
    off: bool = typer.Option(False, "--off", help="Arm live external effects."),
    on: bool = typer.Option(False, "--on", help="Go back to simulating."),
    actor: str = typer.Option("cli", "--actor"),
) -> None:
    """Show or change the live-money switch."""
    db = _db()
    try:
        current = db.is_dry_run()
        if off == on:
            console.print(f"dry_run is [bold]{str(current).lower()}[/]")
            return
        wanted = not off
        db.set_setting("engine.dry_run", wanted, actor=actor, description="set via cli")
        console.print(f"dry_run {str(current).lower()} -> "
                      f"[bold]{str(wanted).lower()}[/]"
                      + ("  ⚠️ LIVE MONEY ARMED" if off else ""))
    finally:
        db.close()


# --------------------------------------------------------------------- runs
@app.command()
def supervisor(
    once: bool = typer.Option(False, "--once", help="Run a single tick and exit."),
) -> None:
    """Run the supervisor loop (or one tick)."""
    import asyncio

    from .supervisor import Supervisor

    sup = Supervisor()
    sup.migrate()
    if once:
        result = asyncio.run(sup.tick())
        console.print(json.dumps(result.as_dict(), indent=2, default=str))
        asyncio.run(sup.shutdown())
        return
    try:
        asyncio.run(sup.run_forever())
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.run(sup.shutdown())


@app.command()
def api() -> None:
    """Run the HTTP API."""
    from .main import run_api

    run_api()


@app.command()
def bot() -> None:
    """Run the Telegram approval bot."""
    from .telegram_bot import main as bot_main

    bot_main()


@app.command()
def worker(name: str = typer.Argument(help="funnel | arbitrage | whitelabel")) -> None:
    """Run one hustle worker loop."""
    workers = {
        "funnel": "engine.workers.funnel_worker:FunnelWorker",
        "arbitrage": "engine.workers.arbitrage_worker:ArbitrageWorker",
        "whitelabel": "engine.workers.whitelabel_worker:WhiteLabelWorker",
    }
    if name not in workers:
        err_console.print(f"[red]unknown worker {name!r}; "
                          f"choose from {', '.join(workers)}[/]")
        raise typer.Exit(1)
    module, cls = workers[name].split(":")
    import importlib

    getattr(importlib.import_module(module), cls)().run()


@app.command()
def jobs(name: str = typer.Argument(help="digest | rollup | expire | sourcing | budget"),
         once: bool = typer.Option(True, "--once/--loop")) -> None:
    """Run a scheduled job once, or start the scheduler."""
    from .jobs import JobRunner

    runner = JobRunner()
    try:
        if once:
            console.print(json.dumps(runner.run_once(name), default=str))
        else:
            runner.run_forever()
    finally:
        runner.close()


@app.command()
def seed() -> None:
    """Re-apply seeds and demo fixtures without touching migrated schema."""
    from .seeds import apply_seeds

    db = _db()
    try:
        apply_seeds(db)
        console.print("[green]seeds applied[/]")
    finally:
        db.close()


def _age(value: object) -> str:
    import datetime as dt

    if not isinstance(value, dt.datetime):
        return "-"
    delta = dt.datetime.now(tz=dt.UTC) - value
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400 * 7:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


if __name__ == "__main__":
    app()
