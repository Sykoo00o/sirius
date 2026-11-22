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

# Two counters, because one number made plan mode contradict its own prose.
#
# BAD counts blockers: they stop --apply from starting anything. ADV counts advisories — the three
# unset Stripe keys, a loose .env mode, SUPERVISOR_ENABLED appearing where nothing reads it — all of
# which are the posture this deploy is *asked* to hold during rehearsal. With one counter, plan mode
# exited 1 on a correctly configured rehearsal box while its own text said to expect those WARNs, so
# a non-zero exit stopped meaning "something is wrong" and an operator had to read the output every
# time to learn what "failed" was worth. At --apply the advisories still show; they just do not
# block, because refusing to boot a rehearsal funnel over a deliberately absent Stripe key would
# only teach whoever deployed it to pass --apply blindly.
say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok      %s\n' "$*"; }
warn() { printf '  WARN    %s\n' "$*"; ADV=$((ADV + 1)); }
fail() { printf '  FAIL    %s\n' "$*"; BAD=$((BAD + 1)); }
BAD=0
ADV=0

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
# Who holds 8090 decides whether this is a collision or a redeploy, so name the holder with the
# unit rather than a pid. The first version of this check called *every* listener a collision,
# which meant the second `--apply` — the routine one, a week later with a changed .env — could not
# run: funnel-app.service was holding its own port. Worse, the run reported FAIL, refused to start
# anything, and left the OLD process serving on the NEW unit files, so the fix installed and never
# took effect while `systemctl is-active` stayed cheerfully green.
WHO_8090=""
if [ -n "$PORT_TOOL" ] && port_busy 8090; then
  # Which unit owns the listening pid — asked of the *listener*, not of funnel-app's MainPID,
  # because if a stray uvicorn ever held 8090 while the unit was also up, trusting MainPID would
  # call someone else's socket a redeploy. Four spellings tried on this host, in the order they
  # turned out to be useless, then useful:
  #   * `ps -o unit=` prints a cgroup path, not a unit name, so it cannot be compared to one;
  #   * `systemctl status <pid>`'s first line is decorated (bullet), and the sed meant for it
  #     silently printed nothing — which reads as "not ours" about a process that demonstrably was.
  #     BSD sed compounds this: it demands text after `q}`, so the same one-liner behaves
  #     differently on the deploy box and on the laptop it was written on;
  #   * `systemctl show <pid> --property=FragmentPath` returned empty on this machine (non-root,
  #     no polkit agent — GetUnitByPID is not one of the free queries);
  #   * /proc/<pid>/cgroup works with no privileges, no decoration and nothing to interpret.
  # `systemctl status <pid>`'s "Loaded: loaded (<path>" line is kept as the fallback, since a host
  # with no procfs gets a named "unknown" rather than a wrong verdict.
  RS_PID=$(systemctl show -p MainPID --value funnel-app.service 2>/dev/null || echo "")
  LISTENER_PID=$(ss -ltnp 2>/dev/null | sed -n 's/.*:8090 .*pid=\([0-9]*\).*/\1/p' | head -1)
  LISTENER_UNIT=""
  if [ -n "$LISTENER_PID" ]; then
    SLICE=$(sed -n 's|^0::/||p' "/proc/$LISTENER_PID/cgroup" 2>/dev/null | tail -1)
    case "$SLICE" in *.service) LISTENER_UNIT="${SLICE##*/}" ;; esac
    if [ -z "$LISTENER_UNIT" ]; then
      FRAG=$(systemctl status "$LISTENER_PID" --no-pager 2>/dev/null \
             | grep -o 'Loaded: loaded (/[^;(]*' | head -1 | sed 's|.*/||')
      LISTENER_UNIT="${FRAG%. }"
    fi
  fi
  if [ "$LISTENER_UNIT" = "funnel-app.service" ]; then
    WHO_8090="funnel-app.service"
  else
    WHO_8090="pid=${LISTENER_PID:-?} unit=${LISTENER_UNIT:-unknown}${RS_PID:+, funnel-app MainPID=$RS_PID}"
  fi
fi
# One PORT_TOOL guard for the whole section. An earlier edit left the 8000/3100 checks stranded
# outside it, which on a host with neither ss nor lsof (macOS, where `ss` does not exist) would run
# port_busy with PORT_TOOL empty and report neighbours as down for the wrong reason. The WARN about
# the missing tool is printed at the top of the script, so nothing is silenced here.
if [ -n "$PORT_TOOL" ]; then
  if [ -n "$WHO_8090" ]; then
    if [ "$WHO_8090" = "funnel-app.service" ]; then
      ok "8090 is held by funnel-app.service (pid $(systemctl show -p MainPID --value funnel-app.service)): a redeploy, so this run must restart it"
    else
      fail "something already listens on 8090 ($WHO_8090) and it is not funnel-app.service — find it before installing
              ss -ltnp | grep ':8090'"
    fi
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
    #
    # Second reason not to trust the exit status, learned the hard way: the OLD form
    # `verify ... | grep -v '^$' && warn || ok` graded grep's status, so verify's real findings
    # vanished behind a "verify clean". The check was believed, and it was wrong.
    # `systemd-analyze verify` also exits 0 for findings it *ignores* (an invalid Documentation= URI
    # is one), so a clean exit is not evidence of a clean unit either way. This is why the decision
    # is taken on the captured text, and why `install`-time output below is shown, not swallowed.
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
  # Exit 0 when only advisories fired: a rehearsal box is supposed to look like this. Any FAIL is a 1.
  if [ "$ADV" -gt 0 ] && [ "$BAD" = 0 ]; then
    say "($ADV advisor$([ "$ADV" = 1 ] || echo y)/ies noted — expected during rehearsal, and they do not block the deploy)"
  fi
  [ "$BAD" -gt 0 ] && say "(and resolve the $BAD blocker(s) above first)"
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
# Install first, then verify, then decide whether to bring the service up. Previously the FAIL
# raised above fell through to `enable --now` and exited 0 anyway, so a unit with known findings
# went live and the run read as a success; a check that cannot stop the thing it is checking is a
# report, not a gate.
install -m 0644 "$TMP/funnel-app.service"      "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-notices.service"  "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-notices.timer"    "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-metrics.service"  "$UNITS_DIR/"
install -m 0644 "$TMP/funnel-metrics.timer"    "$UNITS_DIR/"
systemctl daemon-reload
# Verify the INSTALLED files, not the temp copies. An install that mangles a path is exactly the
# failure this catches, and it cannot be seen from the source side. Output is echoed, not eaten:
# on the first real apply this line printed an "Invalid URL, ignoring" finding to a terminal that
# had already been told the units were fine by a grep two lines earlier.
INSTALLED=$(systemd-analyze verify "$UNITS_DIR"/funnel-app.service "$UNITS_DIR"/funnel-notices.service \
           "$UNITS_DIR"/funnel-notices.timer "$UNITS_DIR"/funnel-metrics.service \
           "$UNITS_DIR"/funnel-metrics.timer 2>&1 || true)
if ! printf '%s\n' "$INSTALLED" | grep -qE "funnel-(app|notices|metrics)"; then
  ok "installed units verify clean (no findings for any funnel unit)"
else
  say "  systemd-analyze findings on the installed units:"
  printf '%s\n' "$INSTALLED" | grep -E "funnel-(app|notices|metrics)" | sed 's/^/            /'
fi
if [ "$BAD" -gt 0 ]; then
  # Units are on disk and daemon-reloaded, deliberately not started: the host keeps whatever ran
  # before, and a corrected re-run of this script picks up from here.
  fail "$BAD blocker(s) unresolved, so nothing was started. Fix them and re-run --apply."
  say ""
  say "units installed (not started): $UNITS_DIR/funnel-*.{service,timer}"
  exit 1
fi
systemctl enable funnel-app.service funnel-notices.timer funnel-metrics.timer >/dev/null
ok "enabled: funnel-app.service funnel-notices.timer funnel-metrics.timer"
if [ "$WHO_8090" = "funnel-app.service" ]; then
  # A redeploy, so the *restart* is the step that matters. `enable --now` on an already-active unit
  # is a no-op: it does not reload ExecStart or the EnvironmentFile, and settings are read at import
  # time. That silently leaves the previous process serving traffic under the unit files you just
  # replaced, which is how a fix can install cleanly and still not be running.
  #
  # No `reset-failed` here. The counter survives a successful restart, and a number that clears
  # itself the moment you look at it is worth half as much as one that keeps the story: this host
  # still shows the 50 restarts the untokenised WorkingDirectory cost it, which is the evidence the
  # unit file is now correct.
  old_pid=$(systemctl show -p MainPID --value funnel-app.service)
  systemctl restart funnel-app.service
  new_pid=$(sleep 2; systemctl show -p MainPID --value funnel-app.service)
  if [ -n "$new_pid" ] && [ "$new_pid" != "0" ] && [ "$new_pid" != "$old_pid" ]; then
    ok "restarted funnel-app.service into the new unit (pid $old_pid -> $new_pid)"
  else
    fail "restarted funnel-app.service but the main pid did not change (still ${new_pid:-none}); the old process may still hold 8090
            systemctl status funnel-app --no-pager && journalctl -u funnel-app -n 30 --no-pager"
  fi
else
  systemctl start funnel-app.service
  ok "started funnel-app.service (8090 was free)"
fi
# `enable` alone only wires the timers for the *next boot*. Start them now, or the first install
# leaves both daily jobs dormant until a reboot nobody schedules: observed on the kegeldoc apply,
# where both timers read enabled/inactive and `list-timers` did not show them. Idempotent on a
# redeploy, where they are already running.
systemctl start funnel-notices.timer funnel-metrics.timer
ok "timers running: $(systemctl is-active funnel-notices.timer) / $(systemctl is-active funnel-metrics.timer) (funnel-notices.timer / funnel-metrics.timer)"
# Printed as an observation, not a verdict, and the reason is worth keeping. The first version of
# this line claimed the counter "survives a successful restart" and skipped `reset-failed` for that
# reason. Measured one commit later, in the very apply that proved the fix: the unit had flapped 50
# times under the untokenised WorkingDirectory, and after this restart reported 0. `--value` reads
# the live unit, and a restart clears what the start-failure rate limiter was holding.
#
# So the number printed here says roughly nothing about the history, and the deploy log — not this
# field — is where "it flapped 50 times before the fix" survives. Kept because it is cheap and
# because a *non-zero* figure in a steady state is still meaningful: if this ever reads 7 on a
# restart you did not cause, something is failing and recovering on its own.
say "  info      NRestarts after restart: $(systemctl show -p NRestarts --value funnel-app.service) \
(0 here is not evidence: a restart clears it)"
say ""
say "next: systemctl status funnel-app --no-pager && curl -s http://127.0.0.1:8090/health | head -40"
say "then: point the Cloudflare Tunnel hostname at 127.0.0.1:8090 and register the Stripe webhook"
exit 0
