#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-${DIALDESK_API_BASE_URL:-http://127.0.0.1:${PORT:-8080}}}"
SLACK_PROBE_PROCESS_OUTBOX="${SLACK_PROBE_PROCESS_OUTBOX:-0}"
export BASE_URL SLACK_PROBE_PROCESS_OUTBOX

python3 - <<'PY'
import hashlib
import hmac
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
process_outbox = os.getenv("SLACK_PROBE_PROCESS_OUTBOX", "0") == "1"
token = os.getenv("APP_TOKEN", "")
signing_secret = os.getenv("SLACK_SIGNING_SECRET", "")

env_path = pathlib.Path(".env")
if env_path.exists():
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if key.strip() == "APP_TOKEN" and not token:
            token = value
        elif key.strip() == "SLACK_SIGNING_SECRET" and not signing_secret:
            signing_secret = value

json_headers = {"User-Agent": "dialdesk-slack-stack-probe/1.0", "Content-Type": "application/json"}
if token:
    json_headers["Authorization"] = f"Bearer {token}"


def request_json(method: str, path: str, body: dict | None = None, headers: dict[str, str] | None = None):
    req_headers = dict(headers or json_headers)
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
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


def request_form(path: str, form: dict[str, str]):
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = {"User-Agent": "dialdesk-slack-stack-probe/1.0", "Content-Type": "application/x-www-form-urlencoded"}
    if signing_secret:
        timestamp = str(int(time.time()))
        basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
        signature = "v0=" + hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
        headers["X-Slack-Request-Timestamp"] = timestamp
        headers["X-Slack-Signature"] = signature
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {path} returned {exc.code}: {detail[:500]}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"POST {path} failed: {exc.reason}")


def ok(label: str, detail: str) -> None:
    print(f"[pass] {label}: {detail}")


print("=== DialDesk Slack Stack Probe ===")
print(f"Base URL: {base_url}")
print(f"Auth: {'token' if token else 'open'}")
print(f"Slash signing: {'configured' if signing_secret else 'open'}")

alert = request_json(
    "POST",
    "/api/slack/notify",
    {
        "text": "Slack stack probe alert",
        "severity": "info",
        "channel": "dialdesk-ops",
        "metadata": {"source": "slack_stack_probe"},
    },
)
alert_id = alert["id"]
if alert.get("destination") != "slack":
    raise RuntimeError(f"expected slack outbox destination, got {alert}")
ok("Slack outbox queue", f"outbox_id={alert_id}, status={alert.get('status')}")

queued = request_json("GET", "/api/outbox?" + urllib.parse.urlencode({"destination": "slack", "limit": 50}))
if not any(row.get("id") == alert_id for row in queued):
    raise RuntimeError(f"queued Slack outbox item {alert_id} not found")
ok("Outbox visibility", "queued Slack alert is inspectable")

status = request_json("POST", "/api/slack/command", {"command": "status", "user": "Justin"})
if "DialDesk" not in status.get("text", "") and "Status" not in status.get("text", ""):
    raise RuntimeError(f"unexpected status command response: {status}")
ok("Slack JSON command", status.get("text", "")[:160])

control = request_json("POST", "/api/slack/command", {"command": "control-room", "user": "Justin"})
if "Control room" not in control.get("text", ""):
    raise RuntimeError(f"unexpected control-room command response: {control}")
ok("Slack control command", control.get("text", "")[:160])

memory = request_json("POST", "/api/slack/command", {"command": "memory-health", "user": "Justin"})
if "Memory:" not in memory.get("text", ""):
    raise RuntimeError(f"unexpected memory command response: {memory}")
ok("Slack memory command", memory.get("text", "")[:160])

slash = request_form(
    "/slack/command",
    {"command": "/dialdesk", "text": "control-room", "user_name": "Justin", "team_id": "probe"},
)
if slash.get("response_type") != "ephemeral" or "Control room" not in slash.get("text", ""):
    raise RuntimeError(f"unexpected slash command response: {slash}")
ok("Slack slash command", slash.get("text", "")[:160])

if process_outbox:
    processed = request_json("POST", "/api/outbox/process?limit=25", {})
    ok("Outbox process", f"processed={processed.get('processed')}, send_enabled={processed.get('send_enabled')}")

print("Slack stack probe passed.")
PY
