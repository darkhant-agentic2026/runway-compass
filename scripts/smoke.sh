#!/usr/bin/env bash
#
# Post-deploy smoke test. Used by .github/workflows/deploy-cloudrun.yml before any
# traffic is shifted, and by hand at RUNBOOK.md step 7:
#
#   ./scripts/smoke.sh https://candidate---coach-api-xxxx-uc.a.run.app
#
# It is a file rather than inline YAML so that it can be run against the local
# docker-compose harness, which is the only way to exercise it without a deployed
# revision:
#
#   docker compose -f docker-compose.e2e.yml up -d --wait
#   ./scripts/smoke.sh http://localhost:8080
#
set -euo pipefail

URL="${1:-}"
if [ -z "$URL" ]; then
  echo "usage: $0 <base-url>" >&2
  exit 2
fi

# A URL that arrived wrapped in list syntax — `['https://…']` — means the caller used a
# gcloud `--format=value(...)` expression that yielded a list rather than a scalar. curl
# reports that as "bad range specification", which names neither the cause nor the field.
case "$URL" in
  http://* | https://*) ;;
  *)
    echo "smoke: refusing to test a URL that is not http(s): ${URL}" >&2
    echo "smoke: if it looks like ['https://…'], the caller's --format returned a list." >&2
    exit 2
    ;;
esac
URL="${URL%/}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() {
  printf '  \033[31mFAIL\033[0m %s\n' "$1" >&2
  exit 1
}

echo "Smoke testing ${URL}"

# Each check captures into a variable rather than piping into `grep -q`. Under
# `set -o pipefail`, `grep -q` exiting on its first match can hand the upstream command
# an EPIPE and fail the whole pipeline — for a body small enough to be written in one
# go it usually does not, which is exactly the kind of test that passes until the day it
# does not.

# --- liveness: must not touch a dependency -------------------------------------------
body="$(curl -fsS --max-time 30 "${URL}/healthz")" || fail "/healthz did not respond"
case "$body" in
  *'"status":"ok"'*) pass "/healthz  ${body}" ;;
  *) fail "/healthz returned unexpected body: ${body}" ;;
esac

# --- readiness: proves Firestore is reachable from the revision ----------------------
body="$(curl -fsS --max-time 30 "${URL}/readyz")" || fail "/readyz did not respond"
case "$body" in
  *'"status":"ok"'*) pass "/readyz   ${body}" ;;
  *) fail "/readyz returned unexpected body: ${body}" ;;
esac

# --- the API is reachable and is NOT the SPA -----------------------------------------
# One request, capturing status and headers together: a HEAD request would be a different
# code path, and an unauthenticated /api/me must be a 401 in problem+json. This is also
# what proves the SPA catch-all is not shadowing /api/* on a real image, which
# docs/07-infra-deploy.md#container warns about and which no unit test can see.
code="$(curl -s -o "${TMP}/body" -D "${TMP}/head" -w '%{http_code}' --max-time 30 \
  "${URL}/api/me")" || fail "/api/me did not respond"
[ "$code" = "401" ] || fail "/api/me returned ${code}, expected 401"
grep -qi 'application/problem' "${TMP}/head" ||
  fail "/api/me was not problem+json — the SPA catch-all may be shadowing it: $(
    grep -i '^content-type' "${TMP}/head" || echo 'no content-type'
  )"
pass "/api/me   401 application/problem+json"

# --- the SPA is served from the same origin ------------------------------------------
body="$(curl -fsS --max-time 30 "${URL}/")" || fail "/ did not respond"
case "$body" in
  *'<!doctype html'* | *'<!DOCTYPE html'*) pass "/         serves the SPA" ;;
  *) fail "/ did not serve an HTML document" ;;
esac

echo "Smoke tests passed."
