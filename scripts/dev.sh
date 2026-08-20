#!/usr/bin/env bash
#
# The local dev loop. docs/07-infra-deploy.md#local-development.
#
#   ./scripts/dev.sh up                    emulator + API (--reload) + Vite
#   ./scripts/dev.sh seed                  a demo user, project, and 8 tasks
#   ./scripts/dev.sh tick [--loop 60s]     call /internal/tick locally
#   ./scripts/dev.sh test [api|web|e2e]    test subsets
#   ./scripts/dev.sh lint                  ruff, mypy, eslint --fix, prettier, tsc, tf fmt
#   ./scripts/dev.sh doctor                check the machine prerequisites
#   ./scripts/dev.sh gen-ordering-vectors  regenerate the cross-language order-key vectors
#   ./scripts/dev.sh gen-event-vectors     regenerate the stored-ADK-event vectors
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="$REPO_ROOT/apps/api"
WEB_DIR="$REPO_ROOT/apps/web"

FIRESTORE_PORT="${FIRESTORE_PORT:-8081}"
API_PORT="${API_PORT:-8080}"
LOCAL_PROJECT="${GOOGLE_CLOUD_PROJECT:-demo-coach-local}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------------------

# The Firestore emulator is a Java jar and the Cloud SDK bundles Python but no JRE. Its
# minimum JRE tracks the Cloud SDK and rises over time, and when the floor moves past the
# installed JRE the emulator refuses to start outright rather than degrading — taking
# every backend test with it.
#
# This check deliberately does NOT gate on a version. It prints what is installed and
# lets the emulator's own warning through, because the emulator is the authority on its
# own floor and hard-coding a number here would just be a second thing to keep in step.
# See the floor table in docs/07-infra-deploy.md#prerequisites.
check_java() {
  if ! have java; then
    fail "No 'java' on PATH. The Firestore emulator is a Java jar and the Cloud SDK does
not bundle a JRE, so every backend test needs one. Install a JRE (Temurin, or
openjdk-NN-jre-headless) and re-run. docs/07-infra-deploy.md#prerequisites"
  fi
  local version
  version="$(java -version 2>&1 | head -1)"
  bold "java: $version"
  warn "  The emulator states its own JRE floor on startup, and that floor rises with the
  Cloud SDK. If the line below mentions a newer JRE than the one above, the emulator is
  telling you the clock is ticking — it still runs today. docs/07-infra-deploy.md"
}

check_gcloud() {
  have gcloud || fail "No 'gcloud' on PATH. Install the Cloud SDK."
  gcloud components list --only-local-state --format='value(id)' 2>/dev/null \
    | grep -qx 'cloud-firestore-emulator' \
    || warn "The 'cloud-firestore-emulator' component may not be installed.
  Install it with: gcloud components install cloud-firestore-emulator beta"
}

doctor() {
  bold "== Toolchain =="
  for tool in python3 uv node npm docker terraform tflint gcloud java; do
    if have "$tool"; then
      printf '  %-10s %s\n' "$tool" "$("$tool" --version 2>&1 | head -1)"
    else
      printf '  %-10s \033[31mMISSING\033[0m\n' "$tool"
    fi
  done
  echo
  bold "== Node version =="
  local node_major
  node_major="$(node -v 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/')"
  if [ "$node_major" != "22" ]; then
    warn "  Node $node_major, but the image pins node:22-slim. Run:
    nvm install 22 && nvm alias default 22
  The alias matters as much as 'nvm use': 'use' affects only the current shell, so a new
  shell or a restarted editor silently reverts."
  else
    echo "  Node 22 — matches the node:22-slim image pin."
  fi
  echo
  bold "== Firestore emulator prerequisites =="
  check_java
  check_gcloud
  echo
  bold "== Playwright browsers =="
  echo "  Chromium and WebKit are both required from M2: golden flow #4 (disconnect and"
  echo "  resume) runs on chromium, mobile-chrome, webkit, and mobile-safari."
  echo "  Install with:  npx playwright install --with-deps chromium webkit"
  echo "  The --with-deps half needs sudo; the browser download itself does not, so a"
  echo "  bump that needs new system libraries fails at launch rather than at install."
}

# ---------------------------------------------------------------------------------------
# Emulator
# ---------------------------------------------------------------------------------------

emulator_is_up() {
  (echo > "/dev/tcp/127.0.0.1/$FIRESTORE_PORT") >/dev/null 2>&1
}

wait_for_emulator() {
  local deadline=$((SECONDS + 90))
  until emulator_is_up; do
    [ $SECONDS -lt $deadline ] || fail "The Firestore emulator did not start within 90s."
    sleep 0.3
  done
}

EMULATOR_LOG="$REPO_ROOT/.firestore-emulator/emulator.log"

start_emulator() {
  if emulator_is_up; then
    bold "Firestore emulator already listening on 127.0.0.1:$FIRESTORE_PORT"
    return
  fi
  check_java
  check_gcloud
  bold "Starting the Firestore emulator on 127.0.0.1:$FIRESTORE_PORT"

  mkdir -p "$(dirname "$EMULATOR_LOG")"
  # Detached, with output to a log rather than inherited: a backgrounded child holding
  # this script's stdout keeps any pipe (`| tail`) open long after the script itself has
  # finished, which reads as a hang.
  #
  # The log is then grepped for the emulator's own JRE complaint, so the warning still
  # reaches the developer — that is the whole point of the preflight, and swallowing it
  # here would defeat it.
  gcloud beta emulators firestore start \
    --host-port="127.0.0.1:$FIRESTORE_PORT" \
    --project="$LOCAL_PROJECT" >"$EMULATOR_LOG" 2>&1 &
  disown || true
  wait_for_emulator

  if grep -iE 'jre|java .*version|no longer supported|deprecat' "$EMULATOR_LOG" >/dev/null 2>&1; then
    warn "The emulator said this about your JRE:"
    grep -iE 'jre|java .*version|no longer supported|deprecat' "$EMULATOR_LOG" \
      | sed 's/^/  /' >&2
    warn "  It started anyway. Record the floor in docs/07-infra-deploy.md if it moved."
  fi
}

# ---------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------

cmd_up() {
  start_emulator

  export ENV=local
  export GOOGLE_CLOUD_PROJECT="$LOCAL_PROJECT"
  export FIRESTORE_EMULATOR_HOST="127.0.0.1:$FIRESTORE_PORT"

  bold "Starting the API on http://127.0.0.1:$API_PORT (reload)"
  (cd "$API_DIR" && uv run uvicorn coach.main:app --reload --port "$API_PORT") &
  local api_pid=$!

  bold "Starting Vite on http://127.0.0.1:5173"
  (cd "$WEB_DIR" && VITE_AUTH_MODE=dev VITE_DEV_UID="${DEV_UID:-u_dev}" npm run dev) &
  local web_pid=$!

  trap 'kill $api_pid $web_pid 2>/dev/null || true; jobs -p | xargs -r kill 2>/dev/null || true' EXIT INT TERM
  wait $api_pid $web_pid
}

cmd_seed() {
  start_emulator
  ENV=local \
  GOOGLE_CLOUD_PROJECT="$LOCAL_PROJECT" \
  FIRESTORE_EMULATOR_HOST="127.0.0.1:$FIRESTORE_PORT" \
    "$API_DIR/.venv/bin/python" "$REPO_ROOT/scripts/seed.py" "$@"
}

cmd_tick() {
  # Cloud Scheduler's job, done by hand. `/internal/tick` accepts an unauthenticated call
  # when ENV=local (coach/api/routers/internal.py), so no token is minted here — and with
  # ENV=local the tick's enqueue is an in-process task rather than Cloud Tasks, which is
  # what makes the whole autonomous path exercisable on a laptop
  # (docs/05-autonomous-runs.md#local-development).
  #
  # It calls the API `dev.sh up` is already serving rather than starting one: a second
  # process would have its own in-process queue, so the runs this tick schedules would
  # execute somewhere nothing is watching.
  local url="http://127.0.0.1:$API_PORT/internal/tick"
  if ! curl -sf -o /dev/null "http://127.0.0.1:$API_PORT/livez"; then
    fail "Nothing is serving on 127.0.0.1:$API_PORT. Start the stack with './scripts/dev.sh up' first."
  fi

  if [ "${1:-}" = "--loop" ]; then
    local interval="${2:-60s}"
    bold "Calling $url every $interval — Ctrl-C to stop"
    while true; do
      _tick_once "$url"
      sleep "${interval%s}"
    done
  fi
  _tick_once "$url"
}

_tick_once() {
  local url="$1"
  local body
  if ! body="$(curl -sS -X POST -H 'Content-Type: application/json' "$url")"; then
    warn "tick failed"
    return 1
  fi
  # Pretty-printed when python is around, raw otherwise: the body is the tick's whole
  # report — what it swept, recovered, scheduled, and why it skipped the rest.
  if have python3; then
    printf '%s' "$body" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$body"
  else
    printf '%s\n' "$body"
  fi
}

cmd_test() {
  local target="${1:-all}"
  case "$target" in
    api) test_api "$@" ;;
    web) test_web ;;
    e2e) test_e2e ;;
    all) test_api && test_web && test_e2e ;;
    *) fail "Unknown test target: $target (expected api, web, e2e, or nothing)" ;;
  esac
}

test_api() {
  bold "== apps/api =="
  start_emulator
  (cd "$API_DIR" && \
    ENV=local \
    GOOGLE_CLOUD_PROJECT=demo-coach-test \
    FIRESTORE_EMULATOR_HOST="127.0.0.1:$FIRESTORE_PORT" \
    uv run pytest "${@:2}")
}

test_web() {
  bold "== apps/web =="
  (cd "$WEB_DIR" && npm run test)
}

test_e2e() {
  bold "== e2e =="
  have docker || fail "Docker is required for the e2e harness."
  local compose="docker compose -f $REPO_ROOT/docker-compose.e2e.yml"
  # shellcheck disable=SC2064
  trap "$compose down -v >/dev/null 2>&1 || true" EXIT
  $compose up --build -d --wait
  (cd "$WEB_DIR" && npm run e2e)
  $compose down -v
  trap - EXIT
}

cmd_lint() {
  bold "== ruff =="
  (cd "$API_DIR" && uv run ruff check --fix . && uv run ruff format .)
  bold "== mypy =="
  (cd "$API_DIR" && uv run mypy)
  bold "== eslint =="
  (cd "$WEB_DIR" && npm run lint:fix)
  # After eslint, never before it: an `--fix` rewrites code — an import folded into
  # `{ type Foo }`, a rule's autofix — and whatever it emits is laid out to the fixer's
  # taste. Formatting first leaves those rewrites unformatted until the next run, which
  # is a `lint` that reports green and leaves a dirty tree.
  # docs/07-infra-deploy.md#formatting-and-linting
  bold "== prettier =="
  (cd "$WEB_DIR" && npm run format)
  bold "== tsc =="
  (cd "$WEB_DIR" && npm run typecheck)
  bold "== terraform =="
  if have terraform; then
    terraform -chdir="$REPO_ROOT/infra/terraform" fmt -recursive
  else
    warn "  terraform not installed; skipping fmt"
  fi
}

cmd_gen_event_vectors() {
  # `apps/web/src/lib/transcript.ts` reads a shape this project does not define — the
  # serialized ADK `Event`, returned verbatim by GET /api/sessions/{sid}/events. This dumps
  # real events from the Python side so the web tests replay an observed shape rather than
  # an assumed one. Written after attachments silently vanished from reopened conversations
  # because the fixtures said `fileData` and Firestore says `file_data`.
  "$API_DIR/.venv/bin/python" "$REPO_ROOT/scripts/gen_event_vectors.py"
}

cmd_gen_ordering_vectors() {
  # The board's optimistic reorder is only correct while the Python and TypeScript
  # fractional-index implementations agree exactly. This regenerates the shared vectors
  # from the Python side; `apps/web/src/lib/ordering.test.ts` replays them.
  "$API_DIR/.venv/bin/python" "$REPO_ROOT/scripts/gen_ordering_vectors.py"
}

usage() {
  sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
  local command="${1:-}"
  shift || true
  case "$command" in
    up) cmd_up "$@" ;;
    seed) cmd_seed "$@" ;;
    tick) cmd_tick "$@" ;;
    test) cmd_test "$@" ;;
    lint) cmd_lint "$@" ;;
    doctor) doctor ;;
    gen-ordering-vectors) cmd_gen_ordering_vectors ;;
    gen-event-vectors) cmd_gen_event_vectors ;;
    ''|-h|--help|help) usage ;;
    *) fail "Unknown command: $command"$'\n'"$(usage)" ;;
  esac
}

main "$@"
