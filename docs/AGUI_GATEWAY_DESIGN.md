# AG-UI gateway (`agui_gateway/`)

One [AG-UI](https://github.com/ag-ui-protocol/ag-ui) endpoint in front of this project's agents, so a
browser dashboard (or any AG-UI client) can talk to them over one protocol. The agents themselves are
unchanged: the gateway adapts what each one already exposes into AG-UI runs and events.

**Read-only (phase 0 views + phase 1 finance chat).** Nothing that arrives through the gateway can
approve, spend, pause, publish, render, or change a switch.

## 1. Where it sits

```text
 AG-UI client (Next.js dashboard later; curl / AG-UI terminal client now)
        │  POST /agui/{agent}  body: RunAgentInput   ← Authorization: Bearer $AGUI_GATEWAY_TOKEN
        ▼  text/event-stream of AG-UI events
 agui_gateway  (FastAPI, 127.0.0.1:8095, ag-ui-protocol==1.0.0)
        ├─ marketing → marketing_agent read paths (in-process; metrics export runs --offline)
        └─ finance   → GET /health, GET /spend/envelope, POST /chat/ask (read-only chat) on the
                       finance agent (AGUI_FINANCE_URL)
 Telegram bot / supervisor: unchanged, still the only place a human approves.
```

## 2. Protocol use

* **Request:** AG-UI `RunAgentInput` (`threadId`, `runId`, `messages`, `state`, `tools`, `context`,
  `forwardedProps`, `resume`).
* **View selection:** `forwardedProps.view` + `forwardedProps.params` (for dashboard buttons), else the
  last user message: `"<view> key=value ..."`, e.g. `attribution since=7daysago until=today`. On
  `finance`, any other text is a chat turn (earlier user/assistant turns in `messages` go along as
  history, last 6). `help`, an empty message, or unknown text on `marketing` returns the list of
  views as a normal run.
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
| `finance` | `chat` | free text (not reachable by keyword) | `POST /chat/ask` with `AGUI_FINANCE_CHAT_TOKEN`; the reply streams as the text message |

**Finance chat is read-only at the finance agent, not just at the gateway.** `POST /chat/ask`
(`backend/finance_agent.py`) needs `Authorization: Bearer $FINANCE_CHAT_TOKEN` (unset → 503, wrong →
401), never parses commands (`/approve …` is just text to the model), drops client `system`/`tool`
history turns, and runs `run_chat_turn` with `CHAT_HTTP_SPECS = (get_pending_transactions,)`. Since
this change `run_chat_turn` also *enforces* its `specs`: a model naming a tool it was not handed
(e.g. `approve_transaction`) gets a refusal as the tool result and nothing executes. Before, `specs`
only limited what was advertised, and `run_tool` would have run any registered tool, the Telegram
chat path included.

Dates take `YYYY-MM-DD`, `today`, `yesterday` or `Ndaysago`. No view accepts a file path.

## 4. Safety posture

* **Auth fails closed.** `AGUI_GATEWAY_TOKEN` unset → every run gets 503 and `python -m agui_gateway`
  refuses to start; a wrong or missing bearer token → 401 (constant-time compare). `GET /healthz`
  is open and returns only `{"ok", "phase", "read_only"}`. FastAPI's docs/OpenAPI routes are off.
* **Loopback by default** (`AGUI_GATEWAY_HOST=127.0.0.1`). Before any public hostname, put
  Cloudflare Access in front on the tunnel; a bearer token alone is not enough for a browser app.
* **Read-only by construction:** two routes only; outbound HTTP is `GET`, except one `POST` to the
  read-only `/chat/ask`; redirects not followed; `ask_issuer=false`; no approval, spend-gate or
  switch name appears in the package (tested).
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
(`http://127.0.0.1:8000`), `AGUI_ENVELOPE_HANDLES` (`worker:funnel`), `AGUI_HTTP_TIMEOUT` (10), `AGUI_CHAT_TIMEOUT` (60),
`AGUI_FINANCE_CHAT_TOKEN` (must equal the finance agent's `FINANCE_CHAT_TOKEN`; unset = chat off),
plus the marketing agent's own `MARKETING_*` and the funnel's `FUNNEL_*` paths.

## 6. Next phases (each a separate, reviewed change)

1. **Finance chat: done.** `POST /chat/ask` on the finance agent + the gateway's `chat` fallback.
   The reply arrives whole (the finance chat loop returns one string), so it streams as a single
   `TEXT_MESSAGE_CONTENT`, not token by token.
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
* Finance views and chat are tested against a mock, and end to end in-process (gateway → the real
  `/chat/ask` route via `httpx.ASGITransport` → a scripted model; the model's attempt to approve was
  refused). Not against the deployed finance agent, its database or a real model.
* The deployed finance agent needs `FINANCE_CHAT_TOKEN` set (and a restart) before chat works there.
* No systemd unit or tunnel hostname yet.
