#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-${DIALDESK_API_BASE_URL:-http://127.0.0.1:${PORT:-8080}}}"
BUSINESS_PROBE_INCLUDE_BROWSER="${BUSINESS_PROBE_INCLUDE_BROWSER:-1}"
export BASE_URL

STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
START_SECONDS="$(date +%s)"
CHECK_ARGS=()
RUN_FAILURES=0

run_probe() {
  local name="$1"
  shift
  echo
  echo "=== Running $name ==="
  set +e
  "$@"
  local exit_code=$?
  set -e
  if [ "$exit_code" -eq 0 ]; then
    CHECK_ARGS+=("$name|passed|exit_code=0")
  else
    CHECK_ARGS+=("$name|failed|exit_code=$exit_code")
    RUN_FAILURES=$((RUN_FAILURES + 1))
  fi
}

record_proof_run() {
  local status="$1"
  local summary="$2"
  local duration_ms="$3"
  shift 3
  PROOF_STATUS="$status" \
    PROOF_SUMMARY="$summary" \
    PROOF_STARTED_AT="$STARTED_AT" \
    PROOF_COMPLETED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    PROOF_DURATION_MS="$duration_ms" \
    PROOF_COMMAND="BASE_URL=$BASE_URL PROBE_URL=${PROBE_URL:-$BASE_URL/health} bash scripts/business_stack_probe.sh" \
    python3 - "$@" <<'PY'
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
token = os.getenv("APP_TOKEN", "")
if not token:
    env_path = pathlib.Path(".env")
    if env_path.exists():
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "APP_TOKEN":
                token = value.strip().strip('"').strip("'")
                break

checks = []
for item in sys.argv[1:]:
    name, status, detail = (item.split("|", 2) + ["", ""])[:3]
    checks.append({"name": name, "status": status, "detail": detail})

body = {
    "proof_type": "business_stack",
    "status": os.environ["PROOF_STATUS"],
    "summary": os.environ["PROOF_SUMMARY"],
    "source": "business_stack_probe",
    "started_at": os.environ["PROOF_STARTED_AT"],
    "completed_at": os.environ["PROOF_COMPLETED_AT"],
    "duration_ms": int(os.environ["PROOF_DURATION_MS"]),
    "command": os.environ["PROOF_COMMAND"],
    "checks": checks,
    "metadata": {
        "base_url": base_url,
        "include_browser": os.getenv("BUSINESS_PROBE_INCLUDE_BROWSER", "1"),
    },
}
headers = {"Content-Type": "application/json", "User-Agent": "dialdesk-business-stack-probe/1.0"}
if token:
    headers["Authorization"] = f"Bearer {token}"

try:
    req = urllib.request.Request(
        f"{base_url}/api/ops/proof-runs",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
        print(f"[pass] Proof run recorded: {payload.get('id')} status={payload.get('status')}")
except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
    print(f"[warn] Proof run could not be recorded: {exc}")
PY
}

echo "=== DialDesk Business Stack Probe ==="
echo "Base URL: $BASE_URL"
echo "Include browser stack: $BUSINESS_PROBE_INCLUDE_BROWSER"

run_probe "ops doctor" bash scripts/ops_doctor.sh
run_probe "memory stack" bash scripts/memory_stack_probe.sh
run_probe "Slack stack" bash scripts/slack_stack_probe.sh

if [ "$BUSINESS_PROBE_INCLUDE_BROWSER" = "1" ]; then
  export PROBE_URL="${PROBE_URL:-$BASE_URL/health}"
  run_probe "browser stack" bash scripts/browser_stack_probe.sh
else
  echo
  echo "=== Skipping browser stack ==="
  CHECK_ARGS+=("browser stack|skipped|BUSINESS_PROBE_INCLUDE_BROWSER=0")
fi

echo
COMPLETED_SECONDS="$(date +%s)"
DURATION_MS=$(((COMPLETED_SECONDS - START_SECONDS) * 1000))
if [ "$RUN_FAILURES" -eq 0 ]; then
  SUMMARY="DialDesk business stack probe passed: ${#CHECK_ARGS[@]} proof(s) completed."
  record_proof_run "passed" "$SUMMARY" "$DURATION_MS" "${CHECK_ARGS[@]}"
  echo "$SUMMARY"
else
  SUMMARY="DialDesk business stack probe failed: $RUN_FAILURES proof(s) failed out of ${#CHECK_ARGS[@]}."
  record_proof_run "failed" "$SUMMARY" "$DURATION_MS" "${CHECK_ARGS[@]}"
  echo "$SUMMARY"
  exit 2
fi
