"""FastAPI app: the control-plane HTTP surface for the engine.

Endpoints are grouped as: health, metrics, fleet, approvals, ledger, targets,
funnels, logs, settings, docker, webhooks.

Two things every mutating path shares:
* Writes go through the same Postgres gate functions the Telegram bot uses, so
  the Web UI cannot express a decision the gatekeeper would refuse.
* The unsigned-webhook path is allowed in dev and warns loudly, because the
  alternative (hard fail) makes local testing impossible, and a silent accept
  makes an unauthenticated production endpoint invisible.

CORS is restricted to localhost origins plus PUBLIC_BASE_URL. A dashboard that
talks straight to Supabase Realtime plus this API is the intended topology, and a
wildcard here would let any origin read the ledger with the publishable key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .audit import AuditLog, scrub
from .config import Config, get_config
from .db import Database, SudoRequiredError, make_migrator
from .docker_control import SERVICES, DockerControl, DockerError
from .finance_gatekeeper import FinanceGatekeeper, fmt_minor
from .models import (
    DecisionRequest,
    DecisionResult,
    DesiredStateRequest,
    DockerCmdRequest,
    DryRunRequest,
    FunnelCreate,
    HealthResponse,
    LedgerEntryCreate,
    Money,
    RevertRequest,
    SettingRequest,
    SpendDecision,
    SpendRequest,
    TargetCreate,
    row_to_json,
)
from .settings import SettingsAccessor

log = logging.getLogger("engine.api")

STATE: dict[str, Any] = {}


def get_db() -> Database:
    db = STATE.get("db")
    if db is None:
        raise HTTPException(503, "database not initialised")
    return db


def get_gate(db: Database = Depends(get_db)) -> FinanceGatekeeper:
    return STATE["gate"]


def get_settings(db: Database = Depends(get_db)) -> SettingsAccessor:
    return STATE["settings"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    from .logging_setup import setup_logging

    setup_logging("api")
    problems = cfg.validate()
    for problem in problems:
        log.warning("config: %s", problem)
    if not cfg.database_url:
        # Without a database there is no state to serve: fail at startup rather
        # than returning 503s from every route and hiding the reason.
        raise RuntimeError("DATABASE_URL is unset; the API cannot serve state")

    db = Database(cfg)
    STATE["cfg"] = cfg
    STATE["db"] = db
    STATE["settings"] = SettingsAccessor(db)
    STATE["audit"] = AuditLog(db, "telegram")
    STATE["gate"] = FinanceGatekeeper(db, cfg, STATE["audit"])
    STATE["docker"] = DockerControl(db, cfg)
    STATE["docker"].audit = STATE["audit"]
    STATE["started_at"] = time.time()
    log.info("api ready on port %s, dry_run=%s", cfg.backend_port, STATE["settings"].dry_run)
    try:
        yield
    finally:
        db.close()
        STATE.clear()


app = FastAPI(
    title="Ghost X2 engine",
    version=__version__,
    description="Supervisor, finance gatekeeper and HITL approval plane.",
    lifespan=lifespan,
)

_cfg = get_config()
_allowed_origins = [
    "http://localhost:3000", "http://localhost:3100", "http://localhost:5173",
    "http://127.0.0.1:3000", "http://127.0.0.1:5173", _cfg.public_base_url,
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in _allowed_origins if o],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Never echo an exception's text to a browser without scrubbing it first:
    a psycopg error string can contain the connection string."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": scrub(f"{type(exc).__name__}: {exc}")[:500]},
    )


# ----------------------------------------------------------------------- meta
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    cfg: Config = STATE["cfg"]
    db: Database = STATE["db"]
    settings = STATE["settings"]
    checks: dict[str, str] = {}
    warnings: list[str] = []

    try:
        db.scalar("select 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = scrub(str(exc))[:200]

    try:
        pending = make_migrator(cfg).pending()
        checks["migrations"] = "up to date" if not pending else f"{len(pending)} pending"
        if pending:
            warnings.append(f"{len(pending)} migration(s) not applied: "
                            f"{', '.join(p.name for p in pending)}")
    except Exception as exc:
        checks["migrations"] = scrub(str(exc))[:200]

    if not cfg.approval_bot:
        warnings.append("approval Telegram bot not configured: tier2 approvals will not page anyone")
    if not settings.accept_unsigned_webhooks:
        checks["webhook_signing"] = "required"
    else:
        warnings.append("unsigned webhooks are accepted (webhook.accept_unsigned=true)")

    return HealthResponse(
        ok=checks.get("database") == "ok",
        service="api",
        runtime=cfg.runtime,
        env=cfg.env,
        dry_run=settings.dry_run,
        version=__version__,
        time=datetime.now(tz=UTC),
        checks=checks,
        warnings=warnings,
    )


@app.get("/api/metrics/executive")
def executive_metrics(db: Database = Depends(get_db)) -> dict[str, Any]:
    row = db.read_one("select * from engine.vw_executive_metrics") or {}
    out = row_to_json(row)
    out["net_24h_formatted"] = fmt_minor(int(out.get("net_24h_minor") or 0))
    out["mrr_formatted"] = fmt_minor(int(out.get("mrr_minor") or 0))
    out["pending_spend_formatted"] = fmt_minor(int(out.get("pending_spend_minor") or 0))
    out["dry_run"] = db.is_dry_run()
    return out


@app.get("/api/metrics/daily")
def daily_metrics(days: int = Query(30, ge=1, le=365), db: Database = Depends(get_db)) -> list[dict]:
    return row_to_json_list(db.read(
        "select * from engine.daily_metrics where day >= current_date - (%s || ' days')::int "
        "order by day desc, hustle", (str(days),)))


# ----------------------------------------------------------------------- fleet
@app.get("/api/agents")
def list_agents(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return row_to_json_list(db.read("select * from engine.vw_agent_fleet_health order by handle"))


@app.get("/api/agents/{handle}")
def get_agent(handle: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    row = db.read_one("select * from engine.vw_agent_fleet_health where handle = %s", (handle,))
    if not row:
        raise HTTPException(404, f"unknown agent {handle}")
    history = db.read(
        """select seq, status, message, latency_ms, reported_at
             from engine.agent_heartbeats
            where agent_id = (select id from engine.agents where handle = %s)
            order by reported_at desc limit 50""",
        (handle,),
    )
    out = row_to_json(row)
    out["recent_heartbeats"] = row_to_json_list(history)
    return out


@app.patch("/api/agents/{handle}")
def set_desired_state(handle: str, body: DesiredStateRequest,
                      db: Database = Depends(get_db)) -> dict[str, Any]:
    """Set the desired state. The supervisor performs the action asynchronously.

    The API deliberately does not restart the container itself: one actor (the
    supervisor) issuing Docker verbs is what keeps restart storms bounded and the
    audit trail single-sourced.
    """
    updated = db.execute(
        "update engine.agents set desired_state = %s, updated_at = now() where handle = %s",
        (body.desired_state, handle),
    )
    if not updated:
        raise HTTPException(404, f"unknown agent {handle}")
    STATE["audit"].audit("fleet.desired_state",
                         f"desired_state of {handle} set to {body.desired_state}",
                         handle=handle, desired_state=body.desired_state)
    return {"handle": handle, "desired_state": body.desired_state,
            "note": "the supervisor applies this on its next tick"}


@app.get("/api/fleet/docker")
async def docker_fleet(service: str | None = None) -> list[dict[str, Any]]:
    try:
        return await STATE["docker"].health(service)
    except DockerError as exc:
        # No Docker on a laptop is expected, not an error worth a 500.
        raise HTTPException(503, scrub(str(exc))) from exc


@app.post("/api/fleet/{service}/{verb}")
async def fleet_verb(service: str, verb: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Direct container control for emergencies. Routine control goes through
    /api/agents/{handle} so the supervisor stays the single actor."""
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service {service}")
    if verb not in ("start", "stop", "restart"):
        raise HTTPException(400, "verb must be start, stop or restart")
    control: DockerControl = STATE["docker"]
    try:
        result = await getattr(control, verb)(service)
    except SudoRequiredError as exc:
        raise HTTPException(403, scrub(str(exc))) from exc
    except DockerError as exc:
        raise HTTPException(503, scrub(str(exc))) from exc
    STATE["audit"].audit("fleet.manual_verb", f"{verb} {service} -> exit {result.code}",
                         service=service, verb=verb, ok=result.ok, simulated=result.simulated)
    return result.as_dict()


@app.post("/api/fleet/exec")
async def fleet_exec(body: DockerCmdRequest) -> dict[str, Any]:
    control: DockerControl = STATE["docker"]
    try:
        result = await control.container_exec(body.service, body.command, as_root=body.as_root)
    except (DockerError, SudoRequiredError) as exc:
        raise HTTPException(400, scrub(str(exc))) from exc
    return result.as_dict()


# ----------------------------------------------------------------- approvals
@app.get("/api/approvals")
def list_approvals(
    state: str = Query("pending", pattern="^(pending|approved|rejected|expired|blocked|all)$"),
    limit: int = Query(50, ge=1, le=500),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    if state == "all":
        rows = db.read(
            "select * from engine.approval_requests order by created_at desc limit %s", (limit,))
    else:
        rows = db.read(
            "select * from engine.approval_requests where state = %s "
            "order by created_at desc limit %s", (state, limit))
    return row_to_json_list(rows)


@app.get("/api/approvals/queue")
def approval_queue(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return row_to_json_list(db.read("select * from engine.vw_approval_queue"))


@app.post("/api/spend", response_model=SpendDecision, status_code=201)
def request_spend(body: SpendRequest, gate: FinanceGatekeeper = Depends(get_gate)) -> SpendDecision:
    """The gatekeeper's front door. Returns a decision; a refusal is a 201 with
    execute=false, not an error, because 'not yet' is the normal tier2 answer."""
    try:
        intent = gate.preflight(
            body.agent, body.title, body.cost_minor,
            currency=body.currency, rationale=body.rationale, source_url=body.source_url,
            expected_roi=body.expected_roi, hustle=body.hustle, vendor=body.vendor,
            idempotency_key=body.idempotency_key, context=body.context,
        )
    except LookupError as exc:
        raise HTTPException(404, scrub(str(exc))) from exc
    return SpendDecision(
        approval_id=intent.approval_id, tier=intent.tier, state=intent.state,
        execute=intent.execute, reason=intent.reason, dry_run=intent.dry_run,
        money=Money.of(intent.cost_minor, intent.currency),
    )


@app.post("/api/approvals/{approval_id}/decide", response_model=DecisionResult)
def decide(approval_id: str, body: DecisionRequest,
           gate: FinanceGatekeeper = Depends(get_gate)) -> DecisionResult:
    try:
        state = gate.decide(approval_id, body.decision, body.actor, body.note)
    except LookupError as exc:
        raise HTTPException(404, scrub(str(exc))) from exc
    except Exception as exc:
        # tier3 hard block lands here: it is a refused action, not a server fault.
        message = scrub(str(exc))
        if "tier3" in message:
            raise HTTPException(409, message) from exc
        raise HTTPException(500, message) from exc
    return DecisionResult(approval_id=approval_id, state=state,
                          already_decided=state not in ("approved", "rejected"))


# -------------------------------------------------------------------- ledger
@app.get("/api/ledger")
def list_ledger(
    limit: int = Query(100, ge=1, le=1000),
    account: str | None = None,
    hustle: str | None = None,
    entry_type: str | None = None,
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = "select * from engine.ledger_entries"
    clauses: list[str] = []
    params: list[Any] = []
    if account:
        clauses.append("account = %s")
        params.append(account)
    if hustle:
        clauses.append("hustle = %s::engine.hustle_kind")
        params.append(hustle)
    if entry_type:
        clauses.append("entry_type = %s::engine.ledger_entry_type")
        params.append(entry_type)
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by occurred_at desc limit %s"
    params.append(limit)
    return row_to_json_list(db.read(sql, params))


@app.post("/api/ledger", status_code=201)
def post_ledger(body: LedgerEntryCreate, db: Database = Depends(get_db)) -> dict[str, Any]:
    entry_id = db.call(
        "post_ledger_entry", body.entry_type, body.amount_minor, body.description,
        body.currency.upper(), body.account, body.hustle, body.agent, body.approval_id,
        body.external_provider, body.external_id, body.dedupe_key,
        json.dumps(body.metadata, default=str),
    )
    return {"ledger_id": entry_id, "money": fmt_minor(body.amount_minor, body.currency)}


@app.post("/api/ledger/{entry_id}/revert")
def revert_ledger(entry_id: int, body: RevertRequest,
                  db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        reversal_id = db.call("reverse_ledger_entry", entry_id, body.reason, body.actor)
    except Exception as exc:
        message = scrub(str(exc))
        if "already reversed" in message or "not found" in message:
            raise HTTPException(409, message) from exc
        raise HTTPException(500, message) from exc
    return {"reversal_id": reversal_id, "reversed": entry_id}


# ------------------------------------------------------------------- targets
@app.get("/api/targets")
def list_targets(
    status: str | None = None, hustle: str | None = None,
    limit: int = Query(100, ge=1, le=1000), db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = "select * from engine.arbitrage_targets"
    clauses, params = [], []
    if status:
        clauses.append("status = %s::engine.target_status")
        params.append(status)
    if hustle:
        clauses.append("hustle = %s::engine.hustle_kind")
        params.append(hustle)
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by discovered_at desc limit %s"
    params.append(limit)
    return row_to_json_list(db.read(sql, params))


@app.post("/api/targets", status_code=201)
def create_target(body: TargetCreate, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Insert a sourced opportunity. Conflict on (hustle, source, external_ref)
    returns the existing row rather than duplicating a purchase pipeline."""
    margin = None
    if body.acquisition_cost_minor and body.resale_floor_minor and body.acquisition_cost_minor > 0:
        margin = round(
            (body.resale_floor_minor - body.acquisition_cost_minor)
            / body.acquisition_cost_minor * 100, 2)
    row = db.read_one(
        """
        insert into engine.arbitrage_targets
          (hustle, source, external_ref, title, url, acquisition_cost_minor,
           resale_floor_minor, currency, expected_margin_pct, evidence_count,
           scoring_rationale, payload)
        values (%s::engine.hustle_kind, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        on conflict (hustle, source, external_ref) do nothing
        returning *
        """,
        (body.hustle, body.source, body.external_ref, body.title, body.url,
         body.acquisition_cost_minor, body.resale_floor_minor, body.currency.upper(),
         margin, body.evidence_count, body.scoring_rationale,
         json.dumps(body.payload, default=str)),
    )
    if row is None:
        row = db.read_one(
            "select * from engine.arbitrage_targets where hustle = %s::engine.hustle_kind "
            "and source = %s and external_ref is not distinct from %s",
            (body.hustle, body.source, body.external_ref))
    return row_to_json(row or {})


# ------------------------------------------------------------------- funnels
@app.get("/api/funnels")
def list_funnels(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return row_to_json_list(db.read(
        """select f.*,
                  count(s.id) filter (where s.status in ('active','trialing')) as live_subs,
                  coalesce(sum(s.mrr_minor), 0)::bigint as mrr_minor
             from engine.funnels f
             left join engine.funnel_subscriptions s on s.funnel_id = f.id
            group by f.id order by f.created_at desc"""))


@app.post("/api/funnels", status_code=201)
def create_funnel(body: FunnelCreate, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Create a funnel. A funnel with a price and no cancel_url is created, but
    warned about: it is the chargeback failure mode, caught at creation time."""
    row = db.read_one(
        """
        insert into engine.funnels
          (slug, offer, url, trial_days, price_minor, currency, merchant_account,
           cancel_url, status)
        values (%s, %s, %s, %s, %s, %s, %s, %s, 'draft')
        returning *
        """,
        (body.slug, body.offer, body.url, body.trial_days, body.price_minor,
         body.currency.upper(), body.merchant_account, body.cancel_url),
    )
    if not body.cancel_url:
        STATE["audit"].danger("funnel.created_without_cancel_route",
                              f"funnel {body.slug} has no one-click cancel URL",
                              slug=body.slug)
    return row_to_json(row or {})


# ---------------------------------------------------------------------- logs
@app.get("/api/logs")
def list_logs(
    agent: str | None = None, level: str | None = None,
    audit_only: bool = False, limit: int = Query(200, ge=1, le=2000),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    sql = ("select l.*, a.handle as agent_handle from engine.agent_logs l "
           "left join engine.agents a on a.id = l.agent_id")
    clauses, params = [], []
    if agent:
        clauses.append("a.handle = %s")
        params.append(agent)
    if level:
        clauses.append("l.level = %s::engine.log_level")
        params.append(level)
    if audit_only:
        clauses.append("l.is_audit")
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by l.created_at desc limit %s"
    params.append(limit)
    return row_to_json_list(db.read(sql, params))


# ------------------------------------------------------------------ settings
@app.get("/api/settings")
def list_settings(settings: SettingsAccessor = Depends(get_settings)) -> list[dict[str, Any]]:
    return row_to_json_list(settings.all_for_display())


@app.get("/api/settings/{key}")
def get_setting(key: str, settings: SettingsAccessor = Depends(get_settings)) -> dict[str, Any]:
    value = settings.get(key, None)
    if value is None:
        raise HTTPException(404, f"setting {key} is unset")
    return {"key": key, "value": value}


@app.put("/api/settings/{key}")
def put_setting(
    key: str,
    body: SettingRequest,
    settings: SettingsAccessor = Depends(get_settings),
    audit: AuditLog = Depends(lambda: STATE["audit"]),
) -> dict[str, Any]:
    previous = settings.get(key, None)
    settings.set(key, body.value, actor="api", description=body.description or "")
    # Money-relevant switches are audit events; a knob that moves the approval
    # boundary is exactly what an auditor asks about.
    if any(marker in key for marker in ("dry_run", "cap", "tier", "stale")):
        audit.audit("setting.changed", f"{key}: {previous!r} -> {body.value!r}",
                    key=key, previous=previous, value=body.value)
    return {"key": key, "value": body.value, "previous": previous}


@app.post("/api/dry-run")
def set_dry_run(body: DryRunRequest, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Arm or disarm live external effects. This is the only route allowed to
    change engine.dry_run, so there is one place to look for who armed the card."""
    previous = db.is_dry_run()
    db.set_setting("engine.dry_run", body.dry_run, actor=body.actor,
                   description=body.note or "")
    STATE["audit"].audit(
        "engine.dry_run_changed",
        f"dry_run {previous} -> {body.dry_run} by {body.actor}",
        previous=previous, value=body.dry_run, note=body.note,
    )
    return {"dry_run": body.dry_run, "previous": previous, "changed_by": body.actor}


# ------------------------------------------------------------------ webhooks
async def _raw_body(request: Request) -> bytes:
    """Awaited by FastAPI's dependency machinery BEFORE the route body runs.

    This is what lets the webhook handlers below be plain sync `def`: reading the
    request stream must be awaited, but doing it in a dependency means the handler
    itself needs no await, and Starlette then runs the whole handler (including its
    blocking Postgres insert) off the event loop.
    """
    return await request.body()


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time compare against `sha256=<hex>` of the raw body.

    Our own format, shared with the Cloudflare Worker. Stripe's own scheme is
    verified at the edge; by the time a Worker forwards an event here it is
    re-signed with this secret, so this endpoint has one verification path.
    """
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhooks/stripe")
def stripe_webhook(
    raw: bytes = Depends(_raw_body),
    db: Database = Depends(get_db),
    settings: SettingsAccessor = Depends(get_settings),
    x_engine_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    cfg: Config = STATE["cfg"]
    unsigned = not x_engine_signature
    if cfg.webhook_signing_secret:
        if not _verify_signature(cfg.webhook_signing_secret, raw, x_engine_signature):
            # A configured secret that does not match is a rejection, always:
            # accept_unsigned covers absence, never a mismatch.
            raise HTTPException(401, "signature mismatch")
    elif unsigned:
        if not settings.accept_unsigned_webhooks:
            raise HTTPException(401, "unsigned webhook refused (webhook.accept_unsigned=false)")
        log.warning("accepting UNSIGNED stripe webhook; set WEBHOOK_SIGNING_SECRET")

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "body is not JSON") from exc

    event_type = payload.get("type")
    external_id = payload.get("id")
    # Land the event before interpreting it. If processing throws, the event is
    # still here to replay; if we returned 500 instead, Stripe redelivery would
    # eventually stop and the money event would be gone.
    row = db.read_one(
        """
        insert into engine.webhook_events (provider, event_type, external_id, payload, status)
        values ('stripe', %s, %s, %s::jsonb, 'received')
        on conflict (received_key) do nothing
        returning id, status
        """,
        (event_type, external_id, raw.decode(errors="replace")),
    )
    if row is None:
        return {"status": "duplicate", "external_id": external_id}
    return {"status": "received", "event_id": row["id"], "event_type": event_type}


@app.post("/webhooks/edge")
def edge_webhook(raw: bytes = Depends(_raw_body),
                 db: Database = Depends(get_db),
                 settings: SettingsAccessor = Depends(get_settings),
                 x_engine_signature: str | None = Header(default=None)) -> dict[str, Any]:
    """Generic ingress for Cloudflare Workers: cancellations, health probes,
    anything that needs to arrive in milliseconds and be interpreted later.

    Both webhook handlers are deliberately sync `def`, like every other DB route
    here. The body arrives pre-read via _raw_body, so nothing needs to be
    awaited; declaring them async would instead pin the insert (~127 ms to this
    region) to the event loop, where Starlette would have run it on the threadpool
    for free and let twenty concurrent webhooks land in a single wave.
    """
    cfg: Config = STATE["cfg"]
    if cfg.webhook_signing_secret:
        if not _verify_signature(cfg.webhook_signing_secret, raw, x_engine_signature):
            raise HTTPException(401, "signature mismatch")
    elif not settings.accept_unsigned_webhooks:
        raise HTTPException(401, "unsigned webhook refused")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "body is not JSON") from exc
    provider = str(payload.get("provider") or "edge")[:40]
    row = db.read_one(
        """insert into engine.webhook_events (provider, event_type, external_id, payload)
           values (%s, %s, %s, %s::jsonb)
           on conflict (received_key) do nothing returning id""",
        (provider, payload.get("event"), payload.get("id"), raw.decode(errors="replace")),
    )
    return {"status": "received" if row else "duplicate", "event_id": (row or {}).get("id")}


def row_to_json_list(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row_to_json(r) for r in rows]


def run_api() -> None:
    import uvicorn

    cfg = get_config()
    uvicorn.run(
        "engine.main:app",
        host="127.0.0.1",
        port=cfg.backend_port,
        log_level="info",
        # One worker: the scheduler lives in this process. Multiple workers would
        # mean N schedulers firing the same digest.
        workers=1,
    )


if __name__ == "__main__":
    run_api()
