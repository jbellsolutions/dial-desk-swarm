#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-${DIALDESK_API_BASE_URL:-http://127.0.0.1:${PORT:-8080}}}"
export BASE_URL

python3 - <<'PY'
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
readiness_profile = os.getenv("DIALDESK_DOCTOR_PROFILE", "production")
launch_profile = os.getenv("DIALDESK_LAUNCH_PROFILE", "vps")
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

common_headers = {"User-Agent": "dialdesk-ops-doctor/1.0"}
if token:
    common_headers["Authorization"] = f"Bearer {token}"

passes: list[str] = []
warnings: list[str] = []
failures: list[str] = []


def request(method: str, path: str, body: dict | None = None, auth: bool = True):
    headers = dict(common_headers if auth else {"User-Agent": common_headers["User-Agent"]})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = response.read().decode("utf-8")
            if not payload:
                return {}
            ctype = response.headers.get("Content-Type", "")
            return json.loads(payload) if "json" in ctype or payload[:1] in "[{" else payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise RuntimeError(f"{method} {path} returned 401. Set APP_TOKEN or run from a directory with the deployed .env.")
        raise RuntimeError(f"{method} {path} returned {exc.code}: {detail[:500]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}")


def ok(label: str, detail: str) -> None:
    passes.append(label)
    print(f"[pass] {label}: {detail}")


def warn(label: str, detail: str) -> None:
    warnings.append(label)
    print(f"[warn] {label}: {detail}")


def fail(label: str, detail: str) -> None:
    failures.append(label)
    print(f"[fail] {label}: {detail}")


def run(label: str, fn, required: bool = True) -> None:
    try:
        fn()
    except Exception as exc:
        if required:
            fail(label, str(exc))
        else:
            warn(label, str(exc))


def check_health() -> None:
    data = request("GET", "/health", auth=False)
    status = data.get("status") if isinstance(data, dict) else data
    if status != "up":
        raise RuntimeError(f"expected health up, got {status!r}")
    ok("Health", "API is up")


def check_dashboard() -> None:
    html = request("GET", "/dashboard", auth=False)
    required = ["Operator Controls", "/api/ops/control-room/action", "runtimeComponent"]
    missing = [item for item in required if item not in html]
    if missing:
        raise RuntimeError(f"dashboard missing {missing}")
    ok("Dashboard", "operator controls are present")


def check_status() -> None:
    data = request("GET", "/api/status")
    metrics = data.get("metrics", {})
    ok(
        "Status",
        f"clients={metrics.get('active_clients')}, tasks={len(data.get('tasks', []))}, approvals={len(data.get('approvals', []))}",
    )


def check_control_room() -> None:
    data = request("GET", "/api/ops/control-room")
    controls = {control.get("action") for control in data.get("operator_controls", [])}
    required = {"run_watchdog", "run_autonomy_drill", "run_launch_gate", "run_readiness"}
    missing = sorted(required - controls)
    if missing:
        raise RuntimeError(f"control room missing actions {missing}")
    ok("Control Room", f"status={data.get('status')}, attention={len(data.get('attention', []))}")


def check_health_action() -> None:
    data = request("POST", "/api/ops/control-room/action", {"action": "run_health", "actor": "ops_doctor"})
    result = data.get("result", {})
    if result.get("status") not in {"ok", "degraded"}:
        raise RuntimeError(f"health action returned {result}")
    ok("Control Action", f"run_health={result.get('status')}")


def check_readiness() -> None:
    data = request(
        "POST",
        "/api/ops/control-room/action",
        {"action": "run_readiness", "actor": "ops_doctor", "profile": readiness_profile},
    )
    result = data.get("result", {})
    status = result.get("status")
    summary = result.get("summary", "")
    if status == "blocked":
        warn("Readiness", summary)
    else:
        ok("Readiness", f"{readiness_profile}={status}, score={result.get('score')}")


def check_launch_gate() -> None:
    data = request(
        "POST",
        "/api/ops/control-room/action",
        {"action": "run_launch_gate", "actor": "ops_doctor", "profile": launch_profile},
    )
    result = data.get("result", {})
    status = result.get("status")
    summary = result.get("summary", "")
    if status == "blocked":
        warn("Launch Gate", summary)
    else:
        ok("Launch Gate", f"{launch_profile}={status}, score={result.get('score')}")


def check_proof_pack() -> None:
    data = request("GET", "/api/ops/proof-pack")
    primary = data.get("primary", {})
    signals = data.get("signals", [])
    runs = request("GET", "/api/ops/proof-runs?" + urllib.parse.urlencode({"limit": 5}))
    if "scripts/business_stack_probe.sh" not in primary.get("command", ""):
        raise RuntimeError(f"proof-pack primary command unexpected: {primary}")
    if not any(signal.get("proof") == "Browser stack" for signal in signals):
        raise RuntimeError(f"proof-pack signals missing browser stack: {signals}")
    if not any(signal.get("proof") == "Runtime self-audit" for signal in signals):
        raise RuntimeError(f"proof-pack signals missing runtime self-audit: {signals}")
    ok("Proof Pack", f"signals={len(signals)}, recent_runs={len(runs)}")


def check_memory() -> None:
    data = request("GET", "/api/memory/health")
    sqlite = data.get("sqlite")
    obsidian = data.get("obsidian", {})
    notion = data.get("notion", {})
    if sqlite is not True and not (isinstance(sqlite, dict) and sqlite.get("configured", True)):
        raise RuntimeError(f"sqlite memory unexpected: {sqlite}")
    detail = f"obsidian={obsidian.get('configured')}, notion={notion.get('configured')}, queued={notion.get('queued')}"
    if not (obsidian.get("configured") or notion.get("configured")):
        warn("Memory", detail)
    else:
        ok("Memory", detail)


def check_browser() -> None:
    data = request("GET", "/api/browser/operators")
    alive = [operator for operator in data if operator.get("alive")]
    detail = f"alive={len(alive)}, known={len(data)}"
    if alive:
        ok("Browser Operator", detail)
    else:
        warn("Browser Operator", detail)


def check_coordination() -> None:
    data = request("GET", "/api/agents/coordination")
    ok("Agent Coordination", f"threads={len(data.get('threads', []))}, agents={len(data.get('inboxes', {}))}")


def check_outbox() -> None:
    data = request("GET", "/api/outbox?" + urllib.parse.urlencode({"limit": 10}))
    queued = [item for item in data if item.get("status") == "queued"]
    ok("Outbox", f"recent={len(data)}, queued={len(queued)}")


print(f"=== DialDesk Ops Doctor ===")
print(f"Base URL: {base_url}")
print(f"Auth: {'token' if token else 'open'}")

run("Health", check_health)
run("Dashboard", check_dashboard)
run("Status", check_status)
run("Control Room", check_control_room)
run("Control Action", check_health_action)
run("Readiness", check_readiness)
run("Launch Gate", check_launch_gate)
run("Proof Pack", check_proof_pack)
run("Memory", check_memory, required=False)
run("Browser Operator", check_browser, required=False)
run("Agent Coordination", check_coordination)
run("Outbox", check_outbox)

print(f"Doctor summary: {len(passes)} pass, {len(warnings)} warning, {len(failures)} failure")
if failures:
    sys.exit(2)
PY
