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

# Fetch a path, capturing status, headers, and body. `curl -f` is deliberately not used:
# it collapses every failure into "exit 22" and throws the response away, so a 404 from
# Cloud Run's frontend and a 404 from our own SPA catch-all look identical — and those
# two have completely different causes.
fetch() {
  local path="$1"
  HTTP_CODE="$(curl -sS --max-time 30 \
    -o "${TMP}/body" -D "${TMP}/head" -w '%{http_code}' "${URL}${path}" 2>"${TMP}/err")" || {
    printf '  \033[31mFAIL\033[0m %s could not be reached: %s\n' \
      "$path" "$(cat "${TMP}/err")" >&2
    exit 1
  }
  HTTP_BODY="$(head -c 400 "${TMP}/body")"
}

# Print everything known about a bad response. Whoever reads this next should not have to
# re-run curl by hand to find out what happened.
diagnose() {
  {
    printf '  \033[31mFAIL\033[0m %s\n' "$1"
    echo "         status:  ${HTTP_CODE}"
    echo "         server:  $(awk -F': ' 'tolower($1)=="server"{print $2}' "${TMP}/head" |
      tr -d '\r')"
    echo "         type:    $(awk -F': ' 'tolower($1)=="content-type"{print $2}' \
      "${TMP}/head" | tr -d '\r')"
    echo "         body:    ${HTTP_BODY}"
    echo
    echo "         A 404 from 'Google Frontend' with an HTML body means the request never"
    echo "         reached the container — wrong host, or no revision serving this tag."
    echo "         A 404 in application/problem+json means it did reach the app, and the"
    echo "         SPA catch-all answered instead of the route."
  } >&2
  exit 1
}

# --- liveness: must not touch a dependency -------------------------------------------
fetch /livez
case "$HTTP_BODY" in
  *'"status":"ok"'*) pass "/livez    ${HTTP_BODY}" ;;
  *) diagnose "/livez did not return {\"status\":\"ok\"}" ;;
esac

# --- readiness: proves Firestore is reachable from the revision ----------------------
fetch /readyz
case "$HTTP_BODY" in
  *'"status":"ok"'*) pass "/readyz   ${HTTP_BODY}" ;;
  *) diagnose "/readyz did not return {\"status\":\"ok\"}" ;;
esac

# --- the API is reachable and is NOT the SPA -----------------------------------------
# One request, capturing status and headers together: a HEAD request would be a different
# code path, and an unauthenticated /api/me must be a 401 in problem+json. This is also
# what proves the SPA catch-all is not shadowing /api/* on a real image, which
# docs/07-infra-deploy.md#container warns about and which no unit test can see.
fetch /api/me
[ "$HTTP_CODE" = "401" ] || diagnose "/api/me returned ${HTTP_CODE}, expected 401"
grep -qi 'application/problem' "${TMP}/head" ||
  diagnose "/api/me was not problem+json — the SPA catch-all may be shadowing it"
pass "/api/me   401 application/problem+json"

# --- the SPA is served from the same origin ------------------------------------------
fetch /
case "$HTTP_BODY" in
  *'<!doctype html'* | *'<!DOCTYPE html'*) pass "/         serves the SPA" ;;
  *) diagnose "/ did not serve an HTML document" ;;
esac

echo "Smoke tests passed."
