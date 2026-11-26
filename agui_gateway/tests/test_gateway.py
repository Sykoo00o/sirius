"""The gateway speaks AG-UI, reads only, and fails closed. No network: finance is a mock transport."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agui_gateway import views  # noqa: E402
from agui_gateway.config import GatewaySettings  # noqa: E402
from agui_gateway.server import create_app  # noqa: E402

TOKEN = "test-gateway-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("MARKETING_", "META_", "AGUI_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGUI_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setenv("MARKETING_DB_PATH", str(tmp_path / "marketing.sqlite3"))
    monkeypatch.setenv("FUNNEL_BILLING_DB_PATH", str(tmp_path / "absent.sqlite3"))
    monkeypatch.setenv("FUNNEL_METRICS_CSV", str(tmp_path / "operator_sheet.csv"))


class Finance:
    """A fake finance agent that records every request it sees."""

    def __init__(self, health=None, status=200):
        self.requests: list[httpx.Request] = []
        self.health = health or {"ok": True, "db": True, "dry_run": True, "issuer_configured": False,
                                 "llm": {"configured": True, "providers": ["openrouter"]}}
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(self.status, json=self.health)
        if request.url.path == "/spend/envelope":
            return httpx.Response(200, json={"allowed": True, "reason": "", "remaining_minor": 4200,
                                             "agent_handle": request.url.params["agent_handle"]})
        return httpx.Response(404, json={"detail": "nope"})


def client(finance: Finance | None = None) -> TestClient:
    transport = httpx.MockTransport(finance or Finance())
    return TestClient(create_app(GatewaySettings.from_env(), finance_transport=transport))


def run_body(text: str = "", **extra):
    body = {"threadId": "t1", "runId": "r1", "state": {}, "messages": [], "tools": [],
            "context": [], "forwardedProps": {}}
    if text:
        body["messages"] = [{"id": "m1", "role": "user", "content": text}]
    body.update(extra)
    return body


def events(response) -> list[dict]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def types(evts):
    return [e["type"] for e in evts]


HAPPY = ["RUN_STARTED", "STEP_STARTED", "STATE_SNAPSHOT", "TEXT_MESSAGE_START",
         "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "STEP_FINISHED", "RUN_FINISHED"]


# ---- auth: fail closed ------------------------------------------------------

def test_no_token_configured_serves_nothing(monkeypatch):
    monkeypatch.delenv("AGUI_GATEWAY_TOKEN")
    r = client().post("/agui/marketing", json=run_body("status"), headers=AUTH)
    assert r.status_code == 503


@pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {TOKEN}", TOKEN])
def test_wrong_or_missing_token_is_401(header):
    headers = {"Accept": "text/event-stream", **({"Authorization": header} if header else {})}
    assert client().post("/agui/marketing", json=run_body("status"), headers=headers).status_code == 401


def test_healthz_is_open_and_says_nothing():
    assert client().get("/healthz").json() == {"ok": True, "phase": 0, "read_only": True}


def test_unknown_agent_is_404():
    assert client().post("/agui/stripe", json=run_body("x"), headers=AUTH).status_code == 404


def test_only_two_routes_exist():
    routes = {(r.path, tuple(sorted(r.methods))) for r in create_app().routes if hasattr(r, "methods")}
    assert routes == {("/healthz", ("GET",)), ("/agui/{agent}", ("POST",))}


# ---- AG-UI shape ------------------------------------------------------------

def test_marketing_status_streams_the_standard_run_shape():
    evts = events(client().post("/agui/marketing", json=run_body("status"), headers=AUTH))
    assert types(evts) == HAPPY
    assert evts[0]["threadId"] == "t1" and evts[0]["runId"] == "r1"
    snap = evts[2]["snapshot"]
    assert snap["agent"] == "marketing" and snap["view"] == "status" and snap["read_only"] is True
    assert snap["data"]["dry_run"] is True and snap["data"]["can_publish"] is False
    assert "nothing armed" in evts[4]["delta"]
    assert evts[3]["messageId"] == evts[4]["messageId"] == evts[5]["messageId"]


def test_forwarded_props_select_the_view_for_a_dashboard_button():
    body = run_body(forwardedProps={"view": "attribution",
                                    "params": {"since": "today", "until": "today"}})
    evts = events(client().post("/agui/marketing", json=body, headers=AUTH))
    assert types(evts) == HAPPY and evts[2]["snapshot"]["view"] == "attribution"
    assert "unavailable" in evts[4]["delta"], "no funnel DB in this test"


def test_no_view_answers_with_help_not_an_error():
    evts = events(client().post("/agui/finance", json=run_body("hello"), headers=AUTH))
    assert types(evts) == HAPPY
    names = [v["name"] for v in evts[2]["snapshot"]["data"]["views"]]
    assert names == ["health", "envelope"]


def test_bad_params_end_in_run_error():
    for text in ("funnel_health day=tomorrowish", "attribution campaign=x"):
        evts = events(client().post("/agui/marketing", json=run_body(text), headers=AUTH))
        assert types(evts) == ["RUN_STARTED", "RUN_ERROR"], text


def test_funnel_health_without_data_is_unavailable_not_halt():
    evts = events(client().post("/agui/marketing", json=run_body("funnel_health day=today"),
                                headers=AUTH))
    data = evts[2]["snapshot"]["data"]
    assert data["available"] is False and data["halt_scaling"] is False
    assert not (Path(os.environ["FUNNEL_METRICS_CSV"])).exists(), "operator sheet never written"


# ---- read-only guarantees ---------------------------------------------------

def test_resume_is_refused_because_nothing_can_be_approved_here():
    body = run_body("status", resume=[{"interruptId": "i1", "status": "resolved",
                                       "payload": {"approve": True}}])
    evts = events(client().post("/agui/marketing", json=body, headers=AUTH))
    assert types(evts) == ["RUN_STARTED", "RUN_ERROR"] and evts[1]["code"] == "read_only"


def test_client_tools_and_state_are_ignored():
    body = run_body("status", tools=[{"name": "approve_spend", "description": "x",
                                      "parameters": {"type": "object"}}],
                    state={"dry_run": False, "can_publish": True})
    evts = events(client().post("/agui/marketing", json=body, headers=AUTH))
    assert "TOOL_CALL_START" not in types(evts)
    assert evts[2]["snapshot"]["data"]["dry_run"] is True, "client state is not an authority"


def test_finance_is_read_with_get_only():
    finance = Finance()
    c = client(finance)
    events(c.post("/agui/finance", json=run_body("health"), headers=AUTH))
    events(c.post("/agui/finance", json=run_body("envelope agent_handle=worker:funnel"),
                  headers=AUTH))
    assert [(r.method, r.url.path) for r in finance.requests] == [
        ("GET", "/health"), ("GET", "/spend/envelope")]
    assert finance.requests[1].url.params["ask_issuer"] == "false"


def test_envelope_handle_must_be_allowlisted():
    finance = Finance()
    evts = events(client(finance).post("/agui/finance", json=run_body("envelope agent_handle=root"),
                                       headers=AUTH))
    assert types(evts) == ["RUN_STARTED", "RUN_ERROR"] and finance.requests == []


def test_finance_down_is_a_run_error_with_a_safe_message():
    evts = events(client(Finance(status=500)).post("/agui/finance", json=run_body("health"),
                                                   headers=AUTH))
    assert types(evts) == ["RUN_STARTED", "RUN_ERROR"] and "HTTP 500" in evts[1]["message"]


def test_secrets_never_reach_the_stream(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "meta-secret-value")
    finance = Finance(health={"ok": True, "dry_run": True, "api_key": "sk_live_should_vanish",
                              "llm": {"configured": True, "authorization": "Bearer x"}})
    c = client(finance)
    text = (c.post("/agui/marketing", json=run_body("status"), headers=AUTH).text
            + c.post("/agui/finance", json=run_body("health"), headers=AUTH).text)
    for secret in ("meta-secret-value", "sk_live_should_vanish", "Bearer x", TOKEN):
        assert secret not in text


def test_redact_is_recursive():
    assert views.redact({"a": 1, "Secret": 2, "x": [{"api_key": 3, "b": 4}]}) == {"a": 1, "x": [{"b": 4}]}


def test_gateway_source_has_no_write_paths():
    """Outbound calls are GET only, and no approval, spend-gate or switch name appears at all."""
    import re
    src = "".join(p.read_text() for p in (REPO / "agui_gateway").glob("*.py"))
    assert not re.search(r"\b(client|httpx)\.(post|put|patch|delete|request|stream)\(", src)
    for needle in ("/approvals", "/spend/gate", "SUPERVISOR_ENABLED", "FUNNEL_CHARGES_ENABLED",
                   "engine.dry_run"):
        assert needle not in src, needle
