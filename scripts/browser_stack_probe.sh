#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-${DIALDESK_API_BASE_URL:-http://127.0.0.1:${PORT:-8080}}}"
PROBE_URL="${PROBE_URL:-$BASE_URL/health}"
OPERATOR_ID="${DIALDESK_BROWSER_OPERATOR_ID:-super-browser-probe}"
MODE="${DIALDESK_BROWSER_MODE:-http}"
export BASE_URL PROBE_URL OPERATOR_ID MODE

python3 - <<'PY'
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
probe_url = os.environ["PROBE_URL"]
operator_id = os.environ["OPERATOR_ID"]
mode = os.environ["MODE"]
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

headers = {"User-Agent": "dialdesk-browser-stack-probe/1.0", "Content-Type": "application/json"}
if token:
    headers["Authorization"] = f"Bearer {token}"


def request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req_headers = dict(headers)
    if body is None:
        req_headers.pop("Content-Type", None)
    req = urllib.request.Request(f"{base_url}{path}", data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned {exc.code}: {detail[:500]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}")


print("=== DialDesk Browser Stack Probe ===")
print(f"Base URL: {base_url}")
print(f"Probe URL: {probe_url}")
print(f"Operator: {operator_id}")
print(f"Mode: {mode}")
print(f"Auth: {'token' if token else 'open'}")

job = request(
    "POST",
    "/api/browser/jobs",
    {
        "objective": "Browser stack probe: verify API to browser operator to memory writeback",
        "url": probe_url,
        "requested_by": "cto",
        "priority": 1,
        "metadata": {"source": "browser_stack_probe", "operator_id": operator_id},
    },
)
job_id = job["id"]
print(f"[pass] Browser job queued: {job_id}")

env = dict(os.environ)
env["DIALDESK_API_BASE_URL"] = base_url
env["DIALDESK_BROWSER_OPERATOR_ID"] = operator_id
env["DIALDESK_BROWSER_MODE"] = mode
if token:
    env["APP_TOKEN"] = token

completed = subprocess.run(
    [sys.executable, "-m", "src.browser_operator", "--once", "--mode", mode, "--operator-id", operator_id, "--api-base-url", base_url],
    text=True,
    capture_output=True,
    env=env,
    timeout=90,
)
if completed.returncode != 0:
    print(completed.stdout)
    print(completed.stderr, file=sys.stderr)
    raise RuntimeError(f"browser operator exited {completed.returncode}")

stdout = completed.stdout.strip()
print(stdout)
try:
    operator_result = json.loads(stdout.splitlines()[-1])
except Exception as exc:
    raise RuntimeError(f"could not parse browser operator output: {stdout}") from exc
if not operator_result.get("claimed") or not operator_result.get("completed"):
    raise RuntimeError(f"browser operator did not complete the queued job: {operator_result}")

completed_jobs = request("GET", "/api/browser/jobs?" + urllib.parse.urlencode({"status": "completed", "limit": 25}))
matching_job = next((row for row in completed_jobs if row.get("id") == job_id), None)
if not matching_job:
    raise RuntimeError(f"completed browser job {job_id} not found")
print(f"[pass] Browser job completed: {matching_job.get('status')}")

operators = request("GET", "/api/browser/operators")
operator = next((row for row in operators if row.get("operator_id") == operator_id), None)
if not operator or not operator.get("alive"):
    raise RuntimeError(f"operator heartbeat missing or stale: {operator}")
print(f"[pass] Browser operator alive: {operator.get('status')}")

memory = request("GET", "/api/memory?" + urllib.parse.urlencode({"kind": "browser_result", "limit": 25}))
matching_memory = next((row for row in memory if job_id in str(row.get("metadata", ""))), None)
if not matching_memory:
    raise RuntimeError(f"browser result memory for {job_id} not found")
print(f"[pass] Browser result memory written: {matching_memory.get('id')}")

print("Browser stack probe passed.")
PY
