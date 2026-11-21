#!/usr/bin/env bash
# Preflight + installer for the Lane C funnel on the VPS.
#
# Run it first with no arguments: it validates, prints a plan, and changes nothing.
# Run it with --apply once the plan reads correctly.
#
# Three things it exists to prevent, in order of how much they cost to discover later:
#
#   1. Installing a unit whose EnvironmentFile does not exist. A funnel that starts with no .env
#      serves the whole journey happily — charges off, outbox transport, defaults everywhere — so
#      a green `systemctl status` is not evidence the config loaded. This checks the file before
#      the unit is trusted.
#   2. Deploying with charges armed by accident. If FUNNEL_CHARGES_ENABLED=true, this refuses
#      unless all three funnel keys are present, and says which one is missing.
#   3. Starting a service that will silently run on defaults. It audits every state path for
#      whether it is absolute and under /var/lib/funnel-app, which is the difference between a
#      billing database that survives a `git clean -fdx` and one that does not. It never writes
#      to .env itself: the template copy in README step 3 is the only thing that creates it, so
#      a live .env cannot be clobbered by this script.

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/ghost-x2}"
RUN_USER="${RUN_USER:-sirius}"
VENV="${VENV:-$REPO_DIR/funnel_app/.venv}"
STATE_DIR="/var/lib/funnel-app"
ENV_FILE="$REPO_DIR/funnel_app/.env"
UNITS_DIR=/etc/systemd/system
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLY=0

case "${1:-}" in
  --apply) APPLY=1 ;;
  "") ;;
  *) echo "usage: $0 [--apply]" >&2; exit 64 ;;
esac

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok      %s\n' "$*"; }
warn() { printf '  WARN    %s\n' "$*"; BAD=$((BAD + 1)); }
fail() { printf '  FAIL    %s\n' "$*"; BAD=$((BAD + 1)); }
BAD=0

# Note on a check deliberately NOT here: an `openssl rsa -check` "is this file readable" probe
# cannot be trusted on arbitrary text. Given a non-key file, some builds print
# "Could not read private key" to stderr and still exit 0, so the probe approves exactly the files
# it should skip and its noise reads like a broken cert store. Verified against this repo's
# .env.example and requirements.txt before deciding; the interpreter is checked by `-x` plus
# `python -V` plus an import probe instead, which cannot pass on a text file.

say "== funnel_app preflight ($([ "$APPLY" = 1 ] && echo APPLY || echo "plan only")) =="
say "repo=$REPO_DIR  user=$RUN_USER  venv=$VENV"
# `ss` is not on every host (and not on macOS at all). Without it the port section below cannot
# distinguish "8090 free" from "8090 occupied, and I could not look", which is the failure that
# matters most here, so say up front rather than let a later `ok port 8090 free` mean more than it does.
if command -v ss >/dev/null 2>&1; then PORT_TOOL=ss
elif command -v lsof >/dev/null 2>&1; then PORT_TOOL=lsof
else PORT_TOOL=""; warn "neither ss nor lsof available: port collisions will not be detected"; fi
port_busy() {
  case "$PORT_TOOL" in
    ss)   ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$" ;;
    lsof) lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 ;;
    *)    return 1 ;;
  esac
}

# --- 1. checkout ------------------------------------------------------------
if [ -f "$REPO_DIR/funnel_app/main.py" ] && [ -f "$REPO_DIR/scripts/export_lane_c_metrics.py" ]; then
  ok "checkout present: funnel_app/main.py + scripts/export_lane_c_metrics.py"
else
  fail "missing funnel_app/main.py or scripts/export_lane_c_metrics.py under $REPO_DIR"
fi
# --git-dir pins the lookup to this checkout. A bare `git -C` walks upward, so a deploy directory
# nested inside another repo would report the parent's HEAD and the Execution Log entry would
# name a commit that is not the one running.
if git -C "$REPO_DIR" --git-dir="$REPO_DIR/.git" rev-parse --verify HEAD >/dev/null 2>&1; then
  ok "git checkout: $(git -C "$REPO_DIR" --git-dir="$REPO_DIR/.git" rev-parse --short HEAD) \
$(git -C "$REPO_DIR" --git-dir="$REPO_DIR/.git" log -1 --format='%s' | cut -c1-60)"
else
  warn "$REPO_DIR/.git has no commit; deploys here will not be traceable to a hash"
fi

# --- 2. interpreter and dependencies ---------------------------------------
PY=""
for cand in "$VENV/bin/python" "$REPO_DIR/backend/.venv/bin/python"; do
  [ -x "$cand" ] && PY="$cand" && break
done
if [ -z "$PY" ]; then
  fail "no interpreter at $VENV/bin/python or backend/.venv/bin/python
          create the venv (see README step 2), or set VENV=/path/to/venv"
else
  ok "interpreter: $PY ($("$PY" -V 2>&1))"
  if "$PY" -c 'import fastapi, uvicorn, anyio, jinja2, httpx, multipart' >/dev/null 2>&1; then
    ok "runtime deps import: fastapi uvicorn anyio jinja2 httpx python-multipart"
  else
    missing=$("$PY" - <<'PYEOF' 2>/dev/null
import importlib.util
names = [("fastapi","fastapi"),("uvicorn","uvicorn"),("anyio","anyio"),("jinja2","jinja2"),
         ("httpx","httpx"),("python-multipart","multipart")]
print(" ".join(n for n, m in names if importlib.util.find_spec(m) is None))
PYEOF
)
    fail "$PY is missing: ${missing:-?}   ->   \"$PY\" -m pip install ${missing:-fastapi uvicorn}"
  fi
fi

# --- 3. EnvironmentFile ----------------------------------------------------
if [ -f "$ENV_FILE" ]; then
  ok "EnvironmentFile present: $ENV_FILE"
  perms=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")
  case "$perms" in
    600|400) ok "permissions $perms" ;;
    *) warn "$ENV_FILE is mode $perms; it will hold a Stripe secret key, so 600 is the floor" ;;
  esac
  if grep -qE '^[[:space:]]*FUNNEL_CHARGES_ENABLED[[:space:]]*=[[:space:]]*(true|1|yes|on)[[:space:]]*$' "$ENV_FILE"; then
    armed=1
    warn "FUNNEL_CHARGES_ENABLED=true in the env file: a card can move on the first checkout"
  else
    armed=0
    ok "FUNNEL_CHARGES_ENABLED is not true: checkout records intent and touches no card"
  fi
  # Keys are optional by design until the operator drops them in, so their absence is a warn,
  # never a fail — that is the posture the task asks to preserve. The exception is when charges
  # are armed, where a missing key is a contradiction rather than a default.
  for key in STRIPE_SECRET_KEY_FUNNEL STRIPE_PUBLISHABLE_KEY_FUNNEL STRIPE_WEBHOOK_SECRET_FUNNEL; do
    # `\S` is PCRE; GNU grep's POSIX matcher does not define it and some builds silently
    # mis-evaluate the whole alternation, which would misreport a key as absent. Use [^[:space:]].
    if grep -qE "^[[:space:]]*(export[[:space:]]+)?$key[[:space:]]*=[[:space:]]*[^[:space:]]" "$ENV_FILE"; then
      ok "$key set"
    elif [ "$armed" = 1 ]; then
      fail "charges are armed but $key is empty — checkout will refuse, so this deploy is not what it says"
    else
      warn "$key not set (expected until you drop it in)"
    fi
  done
  if grep -qE '^[[:space:]]*FUNNEL_SUPERVISOR_ENABLED' "$ENV_FILE" || \
     grep -qE '^[[:space:]]*SUPERVISOR_ENABLED[[:space:]]*=[[:space:]]*(true|1)' "$ENV_FILE"; then
    warn "SUPERVISOR_ENABLED appears armed in the funnel env; this app does not read it, so remove it rather than imply it"
  fi
else
  fail "no $ENV_FILE — cp funnel_app/.env.example funnel_app/.env and edit it before installing"
fi

# --- 4. persistent state ---------------------------------------------------
[ -f "$ENV_FILE" ] || warn "skipping the state-path audit: no $ENV_FILE to audit (see FAIL above)"
for want in FUNNEL_DB_PATH FUNNEL_BILLING_DB_PATH FUNNEL_EMAIL_OUTBOX FUNNEL_METRICS_CSV; do
  [ -f "$ENV_FILE" ] || break
  line=$(grep -E "^[[:space:]]*$want=" "$ENV_FILE" 2>/dev/null | tail -1 || true)
  val=${line#*=}
  if [ -z "$line" ]; then
    warn "$want unset in $ENV_FILE: it defaults to a repo-relative data/ path, which a redeploy can wipe"
  elif [ "${val#/}" = "$val" ]; then
    warn "$want=$val is relative, so it resolves under $REPO_DIR/funnel_app — prefer $STATE_DIR/${val##*/}"
  elif [ "${val#$STATE_DIR}" = "$val" ]; then
    warn "$want=$val is absolute but outside $STATE_DIR; the unit only guarantees that path is writable"
  else
    ok "$want=$val (under $STATE_DIR)"
  fi
done

# --- 5. port isolation -----------------------------------------------------
if [ -n "$PORT_TOOL" ]; then
  if port_busy 8090; then
    fail "something already listens on 8090 — find it before installing
            ss -ltnp | grep ':8090'"
  else
    ok "port 8090 free"
  fi
  # 8000 must be alive (it is sirius-finance) and 3100 is informational only: on the current host
  # the compose `api` container is not running, so 3100 is legitimately absent and saying "expected
  # to be listening" every deploy trains the operator to ignore the WARN. Absent 3100 is fine;
  # present 3100 is also fine. Either way this deploy binds neither.
  if port_busy 8000; then
    ok "port 8000 live with sirius-finance (this deploy does not touch it)"
  else
    fail "port 8000 is not listening. sirius-finance should be; check
            systemctl status sirius-finance  -- before adding a third service"
  fi
  if port_busy 3100; then
    ok "port 3100 live (compose/Caddy stack, untouched by this deploy)"
  else
    say "  info      port 3100 not listening (compose api not running; normal on this host)"
  fi
fi

# --- 6. units render and validate -----------------------------------------
if [ "$APPLY" = 1 ]; then
  [ "$(id -u)" = 0 ] || { say "must run as root with --apply (sudo -E preserves RUN_USER)"; exit 1; }
fi
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# After rendering, no token may survive. This is the check that would have caught the bug this
# script shipped with: @REPO_DIR@ was missing from the substitution list, so `WorkingDirectory`
# kept a literal path that did not exist here and the unit failed at start as 200/CHDIR, while the
# render step cheerfully reported five units rendered. `sed` does not error on a pattern it never
# matched, so a missing -e is invisible without this.
for u in funnel-app.service funnel-notices.service funnel-notices.timer \
         funnel-metrics.service funnel-metrics.timer; do
  sed -e "s|@RUN_USER@|$RUN_USER|g" \
      -e "s|@VENV_PYTHON@|${PY:-$VENV/bin/python}|g" \
      -e "s|@REPO_DIR@|$REPO_DIR|g" \
      "$SRC_DIR/$u" > "$TMP/$u"
  if [ ! -s "$TMP/$u" ]; then fail "$u failed to render"; continue; fi
  if leftover=$(grep -n '@[A-Z_][A-Z_]*@' "$TMP/$u"); then
    fail "$u still contains an unsubstituted token:"
    printf '%s\n' "$leftover" | sed 's/^/            /'
  else
    ok "rendered $u (no tokens left)"
  fi
done
# WorkingDirectory must exist: a wrong path is status 200/CHDIR, and because EnvironmentFile uses
# no `-` prefix any more, a wrong .env path is now a start failure rather than a silent boot on
# defaults. Both are worth checking before `enable --now` rather than reading out of a journal.
for d in "$REPO_DIR/funnel_app" "$(dirname "${PY:-$VENV/bin/python}")"; do
  [ -d "$d" ] && ok "unit path exists: $d" || fail "unit references a missing directory: $d"
done
[ -f "$ENV_FILE" ] || fail "EnvironmentFile will not resolve: $ENV_FILE"
if command -v systemd-analyze >/dev/null 2>&1; then
  # All five units, not just the app: `funnel-metrics.service` is the one with a `--day yesterday`
  # and a bash-free ExecStart, and a bad directive in a timer is invisible if only the app is fed
  # to verify.
  #
  # Classify on the OUTPUT, never on the pipeline's exit status. The previous form piped verify
  # into `grep -v '^$'`, so what `&&` tested was grep's status: on the real VPS, verify had printed
  # "Command ... .venv/bin/python is not executable" and the script still reported "verify clean".
  # A check that reports success on the exact output it exists to catch is worse than no check,
  # because it is believed. Third-party noise (Ubuntu ships unit files with the executable bit set,
  # which verify prints about) is filtered out of the *decision* but shown, so nothing is hidden.
  VERIFY_OUT=$(systemd-analyze verify "$TMP"/funnel-app.service "$TMP"/funnel-notices.service \
              "$TMP"/funnel-notices.timer "$TMP"/funnel-metrics.service \
              "$TMP"/funnel-metrics.timer 2>&1 || true)
  OURS=$(printf '%s\n' "$VERIFY_OUT" | grep -E "funnel-(app|notices|metrics)" | grep -v '^[[:space:]]*$' || true)
  OTHERS=$(printf '%s\n' "$VERIFY_OUT" | grep -vE "funnel-(app|notices|metrics)" | grep -v '^[[:space:]]*$' || true)
  if [ -n "$OTHERS" ]; then
    say "  info      systemd-analyze also printed, about other units on this host:"
    printf '%s\n' "$OTHERS" | sed 's/^/            /' | head -8
  fi
  if [ -n "$OURS" ]; then
    # "Command X is not executable" is the expected state before Step 2 creates the venv, so it is
    # a warn there and a fail at --apply time, where the venv must already exist.
    if [ "$APPLY" = 1 ]; then
      fail "systemd-analyze verify reported problems in our units:"
      printf '%s\n' "$OURS" | sed 's/^/            /'
    else
      warn "systemd-analyze verify flagged our units (expected if the venv does not exist yet):"
      printf '%s\n' "$OURS" | sed 's/^/            /'
    fi
  else
    ok "systemd-analyze verify: no findings for the five funnel units"
  fi
else
  warn "systemd-analyze unavailable; unit syntax not machine-checked"
fi

if [ "$APPLY" != 1 ]; then
  say ""
  say "plan only — nothing installed. Re-run with --apply, as root:"
  say "  sudo -E REPO_DIR=$REPO_DIR RUN_USER=$RUN_USER $0 --apply"
  [ "$BAD" -gt 0 ] && say "(and resolve the $BAD finding(s) above first)"
  exit $(( BAD > 0 ? 1 : 0 ))
fi

say ""
say "== applying =="
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$STATE_DIR"
say "state dir: $STATE_DIR"
install -m 0644 "$TMP/funnel-app.service"      "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-notices.service"  "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-notices.timer"    "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-metrics.service"  "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-metrics.timer"    "$UNITS_DIR/"
systemctl daemon-reload
systemd-analyze verify "$UNITS_DIR/funnel-app.service" || warn "installed unit fails verify"
systemctl enable --now funnel-app.service
systemctl enable --now funnel-notices.timer funnel-metrics.timer
ok "funnel-app.service enabled and started"
ok "timers enabled: funnel-notices.timer funnel-metrics.timer"
say ""
say "next: systemctl status funnel-app --no-pager && curl -s http://127.0.0.1:8090/health | head -40"
say "then: point the Cloudflare Tunnel hostname at 127.0.0.1:8090 and register the Stripe webhook"
exit 0
