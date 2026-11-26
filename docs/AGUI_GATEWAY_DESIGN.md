# AG-UI gateway (`agui_gateway/`)

One [AG-UI](https://github.com/ag-ui-protocol/ag-ui) endpoint in front of this project's agents, so a
browser dashboard (or any AG-UI client) can talk to them over one protocol. The agents themselves are
unchanged: the gateway adapts what each one already exposes into AG-UI runs and events.

**Phase 0 (this change) is read-only.** Nothing that arrives through the gateway can approve, spend,
pause, publish, render, or change a switch.

## 1. Where it sits

```text
 AG-UI client (Next.js dashboard later; curl / AG-UI terminal client now)
        │  POST /agui/{agent}  body: RunAgentInput   ← Authorization: Bearer $AGUI_GATEWAY_TOKEN
        ▼  text/event-stream of AG-UI events
 agui_gateway  (FastAPI, 127.0.0.1:8095, ag-ui-protocol==1.0.0)
        ├─ marketing → marketing_agent read paths (in-process; metrics export runs --offline)
        └─ finance   → GET /health, GET /spend/envelope on the finance agent (AGUI_FINANCE_URL)
 Telegram bot / supervisor: unchanged, still the only place a human approves.
```

## 2. Protocol use

* **Request:** AG-UI `RunAgentInput` (`threadId`, `runId`, `messages`, `state`, `tools`, `context`,
  `forwardedProps`, `resume`).
* **View selection:** `forwardedProps.view` + `forwardedProps.params` (for dashboard buttons), else the
  last user message: `"<view> key=value ..."`, e.g. `attribution since=7daysago until=today`. No
  view, or an unknown one, returns the list of views as a normal run.
* **Every successful run:** `RUN_STARTED` → `STEP_STARTED(view)` →
  `STATE_SNAPSHOT{agent, view, read_only: true, data}` → `TEXT_MESSAGE_START/CONTENT/END` (a one-line
  summary) → `STEP_FINISHED` → `RUN_FINISHED`.
* **Failures:** `RUN_STARTED` → `RUN_ERROR{code, message}` with codes `bad_params`, `view_error`
  (bad value, upstream down or non-200), `read_only` (a `resume` was sent), `internal` (exception type
  only, details stay in the server log).

## 3. Views

| Agent | View | Params | Source |
|---|---|---|---|
| `marketing` | `status` | none | `MarketingSettings.describe()` (switches, what is armed, never credentials) |
| `marketing` | `funnel_health` | `day` (default `yesterday`) | `funnel_metrics.snapshot` → `scripts/export_lane_c_metrics.py --json --offline` into the agent's private CSV |
| `marketing` | `attribution` | `since` (`7daysago`), `until` (`today`) | `funnel_metrics.by_ad` → export `--by-ad` (read-only) |
| `finance` | `health` | none | `GET /health` |
| `finance` | `envelope` | `agent_handle` (must be in `AGUI_ENVELOPE_HANDLES`) | `GET /spend/envelope?ask_issuer=false` |

Dates take `YYYY-MM-DD`, `today`, `yesterday` or `Ndaysago`. No view accepts a file path.

## 4. Safety posture

* **Auth fails closed.** `AGUI_GATEWAY_TOKEN` unset → every run gets 503 and `python -m agui_gateway`
  refuses to start; a wrong or missing bearer token → 401 (constant-time compare). `GET /healthz`
  is open and returns only `{"ok", "phase", "read_only"}`. FastAPI's docs/OpenAPI routes are off.
* **Loopback by default** (`AGUI_GATEWAY_HOST=127.0.0.1`). Before any public hostname, put
  Cloudflare Access in front on the tunnel; a bearer token alone is not enough for a browser app.
* **Read-only by construction:** two routes only; outbound HTTP is `GET` only, redirects not followed,
  `ask_issuer=false`; no approval, spend-gate or switch name appears in the package (tested).
* **The browser is not an authority:** client `tools` and `state` are ignored, and `resume` is
  refused with `read_only`, so there is no way to answer an approval through this door yet.
* **No secrets in the stream:** state is built from payloads that carry none, and `redact()` also
  drops any key that looks like a credential (`token`, `secret`, `password`, `api_key`,
  `authorization`, `cookie`), recursively.

## 5. Running it

```bash
pip install -r agui_gateway/requirements.txt      # or uv run --with ... as below
AGUI_GATEWAY_TOKEN=... python -m agui_gateway      # from the repo root
curl -N -X POST localhost:8095/agui/marketing \
  -H "Authorization: Bearer $AGUI_GATEWAY_TOKEN" -H 'Accept: text/event-stream' \
  -H 'content-type: application/json' \
  -d '{"threadId":"t1","runId":"r1","messages":[{"id":"m1","role":"user","content":"status"}]}'
```

Tests (no network; the finance agent is an `httpx.MockTransport`):

```bash
uv run --no-project --with ag-ui-protocol==1.0.0 --with fastapi --with httpx --with pytest \
  python -m pytest agui_gateway/tests -q
```

Env: `AGUI_GATEWAY_TOKEN`, `AGUI_GATEWAY_HOST`, `AGUI_GATEWAY_PORT` (8095), `AGUI_FINANCE_URL`
(`http://127.0.0.1:8000`), `AGUI_ENVELOPE_HANDLES` (`worker:funnel`), `AGUI_HTTP_TIMEOUT` (10), plus
the marketing agent's own `MARKETING_*` and the funnel's `FUNNEL_*` paths.

## 6. Next phases (each a separate, reviewed change)

1. **Finance chat.** The finance agent's plain-English chat (`run_chat_turn`, read-only tools by
   default) only runs inside the Telegram webhook today. Exposing it needs a new read-only finance
   endpoint; the gateway would stream the reply as `TEXT_MESSAGE_*`.
2. **Dashboard.** Next.js using `@ag-ui/client` `HttpAgent` (or CopilotKit) against this gateway,
   behind Cloudflare Access. Server-side token, never shipped to the browser.
3. **Approvals as interrupts.** A pending tier-2 spend ends a run with an interrupt outcome; the
   dashboard answers with `resume`; the gateway turns that into the same decision Telegram makes,
   on the same transaction row, through the finance agent's existing gate, with the logged-in user
   as actor. Needs AG-UI's interrupt contract read in full first (not done yet).
4. **Worker progress.** Funnel, arbitrage and agency worker status as `STEP_*` / `STATE_DELTA`.

Not planned: anything that arms spend, publishing, pausing or rendering.

## 7. Not verified

* Exercised with the Python SDK's own encoder, FastAPI's test client and curl. Not yet with
  `@ag-ui/client` or CopilotKit.
* The finance views were tested against a mock; the live finance agent was not running during the
  smoke test (its view correctly ended in `RUN_ERROR` "unreachable").
* No systemd unit or tunnel hostname yet.
