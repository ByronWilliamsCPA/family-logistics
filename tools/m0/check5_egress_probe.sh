#!/usr/bin/env bash
# M0 check 5: outbound reachability from the Docker host for Family Logistics.
#
# Run ON the Docker host as a user allowed to run docker.
# Sends no credentials. For each target it records DNS resolution, TLS
# handshake and the HTTP status of an unauthenticated request, first from the
# host and then from a throwaway container attached to the Docker network the
# service will use.
#
# Usage:
#   APPRISE_URL=<apprise status URL> tools/m0/check5_egress_probe.sh \
#     [--network NAME] [--apprise-url URL] [--inference-url URL]
#
# APPRISE_URL (or --apprise-url) is required. INFERENCE_URL (or
# --inference-url) is optional; when set, it is expected NOT to be reachable
# until the model route is opened. Site values come from the private
# deployment repository; nothing site-specific is defaulted here.
#
# Exit codes: 0 all required targets reachable, 1 a required target failed,
# 2 usage or prerequisite error.
set -euo pipefail

# curlimages/curl 8.22.0, pinned by digest.
CURL_IMAGE="curlimages/curl:8.22.0@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777"
NETWORK="bridge"
APPRISE_URL="${APPRISE_URL:-}"
INFERENCE_URL="${INFERENCE_URL:-}"

usage() {
  sed -n '2,20p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --network) NETWORK="${2:?--network needs a value}"; shift 2 ;;
    --apprise-url) APPRISE_URL="${2:?--apprise-url needs a value}"; shift 2 ;;
    --inference-url) INFERENCE_URL="${2:?--inference-url needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for tool in curl getent docker; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing prerequisite: $tool" >&2; exit 2; }
done

# name|url|required (yes = must be reachable, no = informational, never = must NOT be reachable)
if [[ -z "$APPRISE_URL" ]]; then
  echo "APPRISE_URL is not set (export it or pass --apprise-url)" >&2
  exit 2
fi

TARGETS=(
  "icloud-caldav|https://caldav.icloud.com/|yes"
  "google-routes|https://routes.googleapis.com/|no"
  "apprise|${APPRISE_URL}|yes"
)
if [[ -n "$INFERENCE_URL" ]]; then
  TARGETS+=("inference|${INFERENCE_URL}|never")
fi

failures=0

report() {
  local where="$1" name="$2" required="$3" dns="$4" code="$5"
  local verdict="ok"
  if [[ "$required" == "yes" && "$code" == "000" ]]; then
    verdict="FAIL"; failures=$((failures + 1))
  elif [[ "$required" == "never" && "$code" != "000" ]]; then
    verdict="UNEXPECTED (reachable before the model route is live)"
  elif [[ "$required" == "never" ]]; then
    verdict="ok (not reachable yet, as expected)"
  fi
  printf '%-10s %-14s dns=%-4s http=%-4s %s\n' "$where" "$name" "$dns" "$code" "$verdict"
}

host_of() {
  local url="$1"
  url="${url#*://}"
  url="${url%%/*}"
  printf '%s' "${url%%:*}"
}

echo "== From the Docker host"
for entry in "${TARGETS[@]}"; do
  IFS='|' read -r name url required <<<"$entry"
  host="$(host_of "$url")"
  dns="no"
  if [[ "$host" =~ ^[0-9.]+$ ]] || getent hosts "$host" >/dev/null 2>&1; then dns="yes"; fi
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null || true)"
  report "host" "$name" "$required" "$dns" "${code:-000}"
done

echo
echo "== From a container on network '${NETWORK}'"
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  echo "network '${NETWORK}' does not exist yet; rerun with --network once the stack network is created" >&2
  exit 2
fi
for entry in "${TARGETS[@]}"; do
  IFS='|' read -r name url required <<<"$entry"
  code="$(docker run --rm --network "$NETWORK" --read-only --cap-drop ALL \
    --security-opt no-new-privileges "$CURL_IMAGE" \
    -sS -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null || true)"
  report "container" "$name" "$required" "n/a" "${code:-000}"
done

echo
echo "Note: TLS failures show as http=000. iCloud usually answers 401 or 3xx without"
echo "credentials, which proves DNS, TCP and TLS succeeded."
if [[ "$failures" -gt 0 ]]; then
  echo "RESULT: FAIL (${failures} required target(s) unreachable)"
  exit 1
fi
echo "RESULT: PASS"
