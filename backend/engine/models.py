"""Request/response shapes for the HTTP API.

Amounts cross the wire as minor units (ints) everywhere. `money_fmt` is provided
in responses so the dashboard can render without repeating rounding rules, and
the client never has to guess whether a float is dollars or cents. That ambiguity
is where a decimal-point bug in a money UI comes from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

HustleKind = Literal["funnel", "arbitrage", "whitelabel"]
AgentStatus = Literal["running", "paused", "error", "disabled"]


class Money(BaseModel):
    minor: int
    currency: str = "USD"
    formatted: str

    @classmethod
    def of(cls, minor: int, currency: str = "USD") -> Money:
        from .finance_gatekeeper import fmt_minor

        return cls(minor=int(minor), currency=currency, formatted=fmt_minor(minor, currency))


class SpendRequest(BaseModel):
    """An agent asking permission to spend."""

    agent: str = Field(min_length=1, max_length=64, examples=["worker:arbitrage"])
    title: str = Field(min_length=3, max_length=300)
    cost_minor: int = Field(ge=0, description="Amount in minor units (cents).")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    rationale: str = ""
    source_url: str | None = None
    expected_roi: str | None = None
    hustle: HustleKind | None = None
    vendor: str | None = None
    idempotency_key: str | None = Field(
        default=None,
        description="Replay guard. Re-sending the same key returns the original decision.",
    )
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class SpendDecision(BaseModel):
    approval_id: str
    tier: Literal["tier1", "tier2", "tier3"]
    state: str
    execute: bool
    reason: str
    money: Money
    dry_run: bool


class DecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    actor: str = Field(min_length=1, max_length=120, examples=["webui:owner@example.com"])
    note: str | None = None


class DecisionResult(BaseModel):
    approval_id: str
    state: str
    # Mirrored from Postgres' answer so the caller never trusts its own request.
    already_decided: bool = False


class DesiredStateRequest(BaseModel):
    desired_state: AgentStatus


class SettingRequest(BaseModel):
    value: Any
    description: str | None = None


class TargetCreate(BaseModel):
    hustle: HustleKind
    source: str = Field(min_length=1, max_length=80)
    external_ref: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=3, max_length=400)
    url: str | None = None
    acquisition_cost_minor: int | None = Field(default=None, ge=0)
    resale_floor_minor: int | None = Field(default=None, ge=0)
    currency: str = "USD"
    evidence_count: int = Field(default=0, ge=0)
    scoring_rationale: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class FunnelCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    offer: str = Field(min_length=3, max_length=300)
    price_minor: int = Field(ge=0)
    currency: str = "USD"
    trial_days: int = Field(default=7, ge=0, le=365)
    url: str | None = None
    cancel_url: str | None = None
    merchant_account: str = "funnel"


class LedgerEntryCreate(BaseModel):
    entry_type: Literal[
        "charge", "refund", "dispute", "dispute_reversal", "chargeback", "fee",
        "grant", "revenue", "transfer_in", "transfer_out", "expense",
    ]
    amount_minor: int = Field(ge=0, description="Magnitude; sign is derived from entry_type.")
    description: str = Field(min_length=3, max_length=500)
    currency: str = "USD"
    account: str = "operating"
    hustle: HustleKind | None = None
    agent: str | None = None
    approval_id: str | None = None
    external_provider: str | None = None
    external_id: str | None = None
    dedupe_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RevertRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)
    actor: str = "api"


class HealthResponse(BaseModel):
    ok: bool
    service: str
    runtime: str
    env: str
    dry_run: bool
    version: str
    time: datetime
    checks: dict[str, str]
    warnings: list[str] = Field(default_factory=list)


class DockerCmdRequest(BaseModel):
    service: str
    command: str
    as_root: bool = False


class DryRunRequest(BaseModel):
    dry_run: bool
    actor: str = Field(min_length=1, max_length=120)
    note: str | None = None


def row_to_json(row: dict[str, Any]) -> dict[str, Any]:
    """Make a DB row JSON-serialisable: UUID, Decimal and datetime are not native."""
    from decimal import Decimal
    from uuid import UUID

    def conv(value: Any) -> Any:
        if isinstance(value, (UUID, datetime)):
            return str(value)
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            return {k: conv(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [conv(v) for v in value]
        return value

    return {k: conv(v) for k, v in row.items()}
