# Deploying `funnel_app` to the VPS

Target state: the Lane C funnel running as `funnel-app.service` on `127.0.0.1:8090`, reached only
through a dedicated Cloudflare Tunnel hostname, with its SQLite files on a path that a redeploy
cannot wipe, and with the two daily operator jobs (notice sweep, Day-1 metrics export) on timers.

Nothing here touches the two stacks already on the host.

## The isolation, stated up front

| Service | Binds | Reached via | Database |
|---|---|---|---|
| `sirius-finance` (existing) | `127.0.0.1:8000` | existing Tunnel hostname | Supabase |
| compose + Caddy stack (existing, optional) | container `api:3100`, Caddy `:80/:443` | Caddy | Supabase |
| **`funnel-app` (this deploy)** | **`127.0.0.1:8090`** | **its own hostname** | **two local SQLite files** |

Three deliberate choices, each of which is the reason for a line in the unit file:

* **Loopback bind, no public socket.** The tunnel is the only public path in. A `0.0.0.0` bind
  would put an unauthenticated quiz-and-checkout surface on the VPS IP with nothing in front of
  it, and would make the funnel reachable by an address Stripe's webhook is not registered for.
* **Its own venv.** `funnel_app` imports nothing from `backend/engine` by design (only
  `backend/llm_router.py`, by file path). Giving it a shared interpreter means an engine
  dependency bump can break the funnel between two of your commits. The funnel needs six
  packages; the unit creates a 20MB venv for them.
* **`StateDirectory=funnel-app`.** `/var/lib/funnel-app` is created and owned by systemd before
  `ExecStart`, and sits outside the checkout. The default DB paths are repo-relative and `data/`
  is git-ignored, so `git clean -fdx` on a VPS would otherwise delete the subscription table.

`deploy/Caddyfile` is **not** involved: it belongs to the compose `webhook` profile and proxies
`api:3100`. Adding a funnel listener there would mean exposing a second app through a container
that has no route to the host's loopback. Caddy stays off this path.

## Files here

| File | Installs to |
|---|---|
| `funnel-app.service` | `/etc/systemd/system/` (via the installer) |
| `funnel-notices.service` + `.timer` | `/etc/systemd/system/` |
| `funnel-metrics.service` + `.timer` | `/etc/systemd/system/` |
| `funnel-app.env.template` | `/opt/ghost-x2/funnel_app/.env` (chmod 600) |
| `cloudflared-ingress.yml` | merge into `/etc/cloudflared/config.yml` |
| `install-funnel-app.sh` | run in place; no-arg mode changes nothing |

The `.service` files are templates containing exactly two tokens, `@RUN_USER@` and `@VENV_PYTHON@`.
Use `install-funnel-app.sh`, which substitutes and verifies them. If you install by hand, substitute
both yourself (`sed -e "s|@RUN_USER@|sirius|g" -e "s|@VENV_PYTHON@|/opt/ghost-x2/funnel_app/.venv/bin/python|g"`)
and confirm nothing is left:

```bash
grep -rn '@[A-Z_]*@' /etc/systemd/system/funnel-* || echo "clean: no unsubstituted tokens"
```

A literal `@VENV_PYTHON@` in `ExecStart` does **not** fail at `daemon-reload`, which accepts any
ExecStart spelling; it fails at start as status **203/EXEC**, i.e. on the first restart after the
one you checked. Those tokens sit on **six lines across three files** (`funnel-app.service`:
`User=`, `Group=` and one `ExecStart=`; `funnel-metrics.service` and `funnel-notices.service`:
`User=` and, in metrics only, one `ExecStart=`), and no `.timer` file carries either. Count with
the grep above rather than a per-file mental model; the source files deliberately keep their
comments free of token text so that grep cannot lie.

## Step 1 — preflight (changes nothing)

```bash
cd /opt/ghost-x2 && git pull --ff-only
sudo -E REPO_DIR=/opt/ghost-x2 RUN_USER=sirius \
    deploy/funnel_app/install-funnel-app.sh
```

It validates the checkout, the interpreter and the six runtime deps, the presence and mode of
`.env`, whether every state path lives under `/var/lib/funnel-app`, that 8090 is free while 8000
and 3100 still look alive, and that all five units render and pass `systemd-analyze verify`. It
exits non-zero if anything is a FAIL. Expect WARN lines about the three Stripe keys being unset
and about `SUPERVISOR_ENABLED` being absent: both are the posture you asked to keep.

## Step 2 — venv

```bash
cd /opt/ghost-x2/funnel_app
sudo -u sirius python3 -m venv .venv
sudo -u sirius .venv/bin/python -m pip install --upgrade pip
sudo -u sirius .venv/bin/python -m pip install \
    'fastapi>=0.110' 'uvicorn>=0.29' 'anyio>=4' 'jinja2>=3.1' 'httpx>=0.27' 'python-multipart>=0.0.9'
.venv/bin/python -c 'import fastapi,uvicorn,anyio,jinja2,httpx,multipart; print("deps ok")'
```

Playwright is not needed. It is in `funnel_app/package.json` for the offline design-capture
scripts, and `/health`'s meal audit calls `plan_builder._audit_meal_tables` in-process, not a
browser.

## Step 3 — env file

Set the two addresses and the hostname once, at the top:

```bash
FUNNEL_HOSTNAME=funnel.example.com
SUPPORT_EMAIL=support@funnel.example.com
```

The template carries three marked values on three assignment lines; no comment contains one, so a
substitution cannot corrupt an address hidden in prose. Verify on the resulting *values*, never by
grepping for the placeholder names (the header names them, so that grep matches its own
instruction). The copy below refuses to touch an existing `.env` rather than overwriting it:

```bash
# On this host the repo is /home/ubuntu/Sirius and the service user is ubuntu (matching
# sirius-finance's own User=). Everywhere this README says sirius or /opt/ghost-x2, read those as
# the generic spelling; on another box they may be right as written.
RUN_USER=ubuntu REPO=/home/ubuntu/Sirius
sudo -u "$RUN_USER" # The guard must *exit*, not just print. The obvious `test -e f && echo refuse || cp` prints
# REFUSING and then keeps going: the `||` is the cp, so the branch you wanted to skip is the branch
# the failure runs, and the script ends 0 as if it had rendered. Paste-and-continue then overwrites
# the live .env, which is the one thing this step promised not to do.
if test -e "$REPO/funnel_app/.env"; then
  echo "REFUSING: $REPO/funnel_app/.env already exists. Merge by hand; do not re-render."; exit 3
fi
sudo -u "$RUN_USER" cp deploy/funnel_app/funnel-app.env.template "$REPO/funnel_app/.env"

# A QUOTED heredoc (`<<'PY'`) with the dash as the first operand, and args after the delimiter
# line. Two failure modes this avoids, both of which mangled this render silently on the real VPS:
#   * `python - args <<PY` puts a non-option argument before the dash, so python treats the arg as
#     a *script path* and dies with "can't find __main__ module".
#   * `-c '...'` breaks on any apostrophe-delimited quote inside the program: the payload's own
#     `'${...}'` literals close the shell's quote, the tail spills into the outer shell, and the
#     path arrives mangled. A quoted heredoc passes all of it as stdin, no shell in the middle.
sudo -u "$RUN_USER" "$REPO/funnel_app/.venv/bin/python" - "$REPO" "$FUNNEL_HOSTNAME" "$SUPPORT_EMAIL" <<'PY'
from pathlib import Path
import secrets, sys
repo, host, mail = sys.argv[1:4]
p = Path(repo) / "funnel_app/.env"
t = p.read_text().replace("@FUNNEL_HOSTNAME@", host) \
                 .replace("@SUPPORT_EMAIL@", mail) \
                 .replace("@SESSION_SECRET_HEX@", secrets.token_hex(32))
# Assert on the VALUES, not on the placeholder names: the template header mentions those names in
# prose, so a name-based assertion fires on its own documentation. Test the @UPPER_SNAKE@ shape
# rather than any "@", because a support email legitimately contains one.
suspect = [l for l in t.splitlines()
           if l.startswith(("FUNNEL_PUBLIC_URL=", "FUNNEL_SUPPORT_EMAIL=", "FUNNEL_SESSION_SECRET="))]
assert len(suspect) == 3, suspect
# A leftover reads as @UPPER_SNAKE@, so that is the shape tested for, not a bare at-sign.
# A real support address contains one, and an earlier revision of this very check rejected
# "support@vegastudios.site" and aborted the render, leaving the template's own placeholders in the
# file. Same bug as the bare-at version the template header warns about, reintroduced.
import re
LEFTOVER = re.compile(r"@[A-Z][A-Z0-9_]*@")
bad = [l for l in suspect
       if LEFTOVER.search(l.split("=", 1)[1]) or not l.split("=", 1)[1].strip()]
assert not bad, f"value not substituted: {bad}"
secret = [l for l in suspect if l.startswith("FUNNEL_SESSION_SECRET=")][0].split("=", 1)[1]
assert len(secret) == 64 and all(c in "0123456789abcdef" for c in secret), "session secret is not 64 hex chars"
p.write_text(t)
print("wrote", p)
PY
sudo chmod 600 "$REPO/funnel_app/.env" && sudo chown "$RUN_USER:$RUN_USER" "$REPO/funnel_app/.env"
grep -nE '^(FUNNEL_PUBLIC_URL|FUNNEL_SUPPORT_EMAIL|FUNNEL_CHARGES_ENABLED|FUNNEL_.*DB_PATH|FUNNEL_EMAIL_OUTBOX|FUNNEL_METRICS_CSV|FUNNEL_SESSION_SECRET)=' "$REPO/funnel_app/.env" | sed 's/\(SESSION_SECRET=\)\S\{8\}\S*/\1<64-hex>/'
```

The last line is the five-second review: four absolute paths under `/var/lib/funnel-app`, one
hostname, and `FUNNEL_CHARGES_ENABLED=false`.

Leave `FUNNEL_CHARGES_ENABLED=false`. The three Stripe keys stay commented out until you drop them
in; with them absent the app serves the entire journey in rehearsal and `/health` names each
variable it still wants.

Never overwrite a live `.env` — this step assumes there is none. If there is one, merge the four
absolute paths and `FUNNEL_PUBLIC_URL` into it by hand.

## Step 4 — install and start

```bash
sudo -E REPO_DIR=/opt/ghost-x2 RUN_USER=sirius deploy/funnel_app/install-funnel-app.sh --apply
```

Which installs five units, `daemon-reload`s, creates `/var/lib/funnel-app`, and enables the app
plus both timers. Equivalent by hand:

```bash
sudo install -m 0644 /tmp/rendered/*.service /tmp/rendered/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now funnel-app.service
sudo systemctl enable --now funnel-notices.timer funnel-metrics.timer
```

Verify:

```bash
systemctl status funnel-app --no-pager -l
ss -ltnp | grep 8090                      # exactly one line, 127.0.0.1:8090
ss -ltnp | grep -E ':(8000|3100)'         # both still alive, untouched
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/health   # 200
journalctl -u funnel-app -n 30 --no-pager
ls -l /var/lib/funnel-app/                # billing.sqlite3 + responses.sqlite3 appear after first request
```

Then confirm the persistence claim rather than trusting the unit:

```bash
sudo systemctl restart funnel-app
curl -s -o /dev/null http://127.0.0.1:8090/ && ls -l /var/lib/funnel-app/*.sqlite3
# mtimes must be from before the restart, not recreated by it
```

### Smoke checks, with the routes that actually exist

Walk these rather than guessing paths: `/quiz/1` and `/account` both look like routes and both 404
(one probe during the writing of this runbook hit both). The member surface is `/dashboard`, and
the quiz entry point is `/onboarding/0`. Anonymous 3xx redirects are expected on the member pages;
what matters is that nothing returns 500.

```bash
BASE=http://127.0.0.1:8090
for p in / /health /onboarding/0 /checkout /dashboard /policy/refund /policy/privacy \
         /policy/terms /dev/responses /preview /summary; do
  printf '%-20s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE$p")"
done
# Observed on a charges-off instance with no session cookie (the state a fresh deploy boots in):
#   200  /  /health  /onboarding/0  /dashboard  /policy/{refund,privacy,terms}  /dev/responses
#   302  /checkout  /preview  /summary          <- anonymous bounce to the quiz start
# 200 on /dashboard while anonymous is not a leak: it renders the empty member shell, and the
# routes that read a subscription are the POST ones. A 500 anywhere in this list is the finding.
curl -s -X POST "$BASE/ops/send-notices"
#   {"sent":0,"failed":0,"transport":"outbox","detail":[]} on a quiet day with no SMTP configured.
#   `"transport":"outbox"` is the line that tells you mail is writing .eml files, not pretending.
curl -s -X POST "$BASE/webhooks/stripe" -H 'Content-Type: application/json' -d '{"type":"ping"}'
#   {"received":true,"result":"ignored:ping"} with no key set. An unknown event type must be
#   answered 200, because a 5xx makes Stripe retry forever and reads like an outage.
ls /var/lib/funnel-app/*.sqlite3   # both files, created at boot, not on first customer
```

## Step 5 — Cloudflare Tunnel

Give the funnel its **own hostname**, not a path prefix on the engine's. `POST /webhooks/stripe`
must be uniquely routable: a rewritten path would leave two apps owning the same URL, one of them
chosen by a rewrite rule nobody reads at 3am. Absolute URLs the funnel builds from
`FUNNEL_PUBLIC_URL`, and Stripe's own origin checks, also read better on a dedicated hostname.

```bash
sudo cp /etc/cloudflared/config.yml /etc/cloudflared/config.yml.bak.$(date +%F)
sudoedit /etc/cloudflared/config.yml
```

Add, before the catch-all (full commentary in `cloudflared-ingress.yml`):

```yaml
  - hostname: funnel.example.com
    service: http://127.0.0.1:8090
    keepAliveTimeouts: 90s
```

Then point DNS at the tunnel and bounce it:

```bash
sudo cloudflared tunnel route dns <TUNNEL_NAME_OR_UUID> funnel.example.com
sudo cloudflared tunnel ingress validate      # refuses a config with no catch-all
sudo cloudflared tunnel ingress rule https://funnel.example.com/health   # must print service: http://127.0.0.1:8090
sudo systemctl restart cloudflared
```

Verify end to end, including the exact path Stripe will use:

```bash
curl -sI https://funnel.example.com/health | head -1                  # HTTP/2 200
curl -s https://funnel.example.com/health | grep -i -A4 'billing'     # rehearsal state, keys named
```

Probe the webhook route through the tunnel, and read the code by which stage you are at (both
numbers below were taken from a running instance, not reasoned out):

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://funnel.example.com/webhooks/stripe
```

* **Before** `STRIPE_WEBHOOK_SECRET_FUNNEL` is set: `200 {"received":true,"result":"ignored:"}`.
  That is deliberate rehearsal behaviour, and the journal carries
  `STRIPE_WEBHOOK_SECRET_FUNNEL unset: accepting an unsigned webhook in rehearsal` beside it. It
  proves routing works; it does **not** prove signature verification works.
* **After** the secret is set: `400 missing Stripe-Signature header`. That is the answer you want
  from an unsigned probe, because only then is the check actually running. A `200` at this stage
  means the secret did not load (wrong file, or no restart).

Either way, an empty body reaching the handler and coming back in under a second is the tunnel
proof. The real proof is Stripe's own `Deliver` button in Step 6.

## Step 6 — Stripe webhook

In the **funnel** merchant account (never the B2B one; a foreign account's events entering our
ceilings is how a lane dies of somebody else's chargebacks), add an endpoint:

```
URL:     https://funnel.example.com/webhooks/stripe
Version: the API version pinned by STRIPE_API_VERSION, or the current one
Events:  checkout.session.completed, checkout.session.expired, invoice.paid,
         invoice.payment_failed, invoice.upcoming, customer.subscription.updated,
         customer.subscription.deleted, charge.refunded, charge.dispute.created,
         charge.dispute.closed, radar.early_warning.created
```

`radar.early_warning.created` is a paid add-on. If it is not enabled, say so in the sheet: the
fraud column then reads 0% and means *not measured*, which is a different fact from 0%.

Copy the signing secret, then add all four key lines to `.env` in one edit and restart (settings
are read at import, so a restart is the only way a changed `.env` is seen):

```bash
sudo systemctl restart funnel-app
curl -s http://127.0.0.1:8090/health | grep -i -A6 'webhook'
```

Confirm the endpoint is live from Stripe's side (`Deliver` a test event, expect a 200 and a
`webhook ... -> ...` line in `journalctl -u funnel-app`). `PHASE3_MANUAL_TEST.md` Part 2 is the
two-card walk that follows; it is the test that shows you a date the processor owns.

## Step 7 — the two daily jobs

```bash
systemctl list-timers 'funnel-*' --no-pager
sudo systemctl start funnel-notices.service     # run now, do not wait for 09:15
journalctl -u funnel-notices -n 20 --no-pager   # {"sent":0,"failed":0,...} on a quiet day
sudo systemctl start funnel-metrics.service
journalctl -u funnel-metrics -n 30 --no-pager   # the exported row, or exit 1/2 with a reason
```

Metrics exit codes, which is why this is a unit and not a crontab line: `0` exported, `1` nothing
exported (funnel never ran, or the DB predates telemetry — not an alarm on day one), `2` a kill
metric tripped. Only `2` means look tonight, and `--fail-on-ceiling` is what turns it into a red
unit instead of a number in a CSV.

Timer times (09:05 metrics, 09:15 notices, both UTC, `Persistent=true`) are opinionated, not
inherited from anything. Move them if the sheet is read at a different hour; keep metrics first so
a breach is the first thing in that morning's journal.

If you would rather have cron than a timer, the equivalent line — note that it needs the venv
python and the absolute DB paths, and that cron cannot tell exit 1 from exit 2 for you:

```cron
5 9 * * * sirius cd /opt/ghost-x2/funnel_app && .venv/bin/python ../scripts/export_lane_c_metrics.py --day yesterday --fail-on-ceiling
15 9 * * * sirius curl -sf -X POST http://127.0.0.1:8090/ops/send-notices
```

Cron *does* run a shell, unlike `ExecStart=`, so `$(date -u -d yesterday +%F)` would work here — but
`--day yesterday` is the same on a BSD box, and the two lines above still name `sirius` and
`/opt/ghost-x2`, so change both to your actual user and repo path (on this host: `ubuntu`,
`/home/ubuntu/Sirius`) or the job runs as the wrong user and fails on the state directory's 0750.

## Rollback

```bash
sudo systemctl disable --now funnel-notices.timer funnel-metrics.timer
sudo systemctl disable --now funnel-app.service
```

Stopping the unit takes the funnel offline for customers but keeps `/var/lib/funnel-app` intact, so
a re-enable resumes with every subscription row where it was. Removing the units entirely leaves
the state directory alone by design: purge it yourself only once you have copied the two
`.sqlite3` files somewhere else.

## Redeploy (later commits)

```bash
cd /opt/ghost-x2 && git pull --ff-only
sudo systemctl restart funnel-app
curl -s http://127.0.0.1:8090/health | grep -iE 'violation|billing readiness' -A3
cd funnel_app && .venv/bin/python -m pytest tests -q     # needs `pip install pytest`; not in the runtime venv
```

Record the commit hash and the verification you ran in `SIRIUS_MASTER_BLUEPRINT.md` under
"Execution Log & Progress", per the project rule.
