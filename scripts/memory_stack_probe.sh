#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-${DIALDESK_API_BASE_URL:-http://127.0.0.1:${PORT:-8080}}}"
MEMORY_PROBE_PROCESS_OUTBOX="${MEMORY_PROBE_PROCESS_OUTBOX:-0}"
export BASE_URL MEMORY_PROBE_PROCESS_OUTBOX

python3 - <<'PY'
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
process_outbox = os.getenv("MEMORY_PROBE_PROCESS_OUTBOX", "0") == "1"
token = os.getenv("APP_TOKEN", "")
local_files_required = os.getenv("MEMORY_PROBE_REQUIRE_LOCAL_FILES", "")
if not local_files_required:
    local_files_required = "1" if "127.0.0.1" in base_url or "localhost" in base_url else "0"

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

headers = {"User-Agent": "dialdesk-memory-stack-probe/1.0", "Content-Type": "application/json"}
if token:
    headers["Authorization"] = f"Bearer {token}"

warnings: list[str] = []


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


def warn(label: str, detail: str) -> None:
    warnings.append(label)
    print(f"[warn] {label}: {detail}")


def ok(label: str, detail: str) -> None:
    print(f"[pass] {label}: {detail}")


print("=== DialDesk Memory Stack Probe ===")
print(f"Base URL: {base_url}")
print(f"Auth: {'token' if token else 'open'}")

memory = request(
    "POST",
    "/api/memory",
    {
        "title": "Memory stack probe",
        "body": "This note proves DialDesk can write operating memory to SQLite, mirror to Obsidian when configured, and queue Notion sync when configured.",
        "kind": "ops_probe",
        "actor": "cto",
        "source": "memory_stack_probe",
        "tags": ["ops", "memory", "probe"],
        "metadata": {"source": "memory_stack_probe"},
    },
)
memory_id = memory["id"]
ok("SQLite memory", f"memory_id={memory_id}, notion_status={memory.get('notion_status')}")

listed = request("GET", "/api/memory?" + urllib.parse.urlencode({"kind": "ops_probe", "limit": 25}))
if not any(row.get("id") == memory_id for row in listed):
    raise RuntimeError(f"memory {memory_id} was not returned by /api/memory")
ok("Memory list", "created note is queryable")

obsidian_path = memory.get("obsidian_path") or ""
if obsidian_path:
    path = pathlib.Path(obsidian_path)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if memory_id not in text or "Memory stack probe" not in text:
            raise RuntimeError(f"Obsidian file exists but does not contain probe evidence: {obsidian_path}")
        ok("Obsidian mirror", obsidian_path)
    elif local_files_required == "1":
        raise RuntimeError(f"Obsidian path was returned but not found locally: {obsidian_path}")
    else:
        warn("Obsidian mirror", f"path returned but not readable from this machine: {obsidian_path}")
else:
    warn("Obsidian mirror", "OBSIDIAN_VAULT_PATH is not configured")

health = request("GET", "/api/memory/health")
if health.get("sqlite") is not True:
    raise RuntimeError(f"memory health did not report sqlite=true: {health}")
ok("Memory health", f"entries={health.get('total_entries')}, obsidian_written={health.get('obsidian', {}).get('written')}")

notion_status = memory.get("notion_status")
if notion_status == "queued":
    outbox = request("GET", "/api/outbox?" + urllib.parse.urlencode({"destination": "notion", "limit": 50}))
    matching = [row for row in outbox if memory_id in str(row.get("payload", ""))]
    if not matching:
        raise RuntimeError(f"Notion was queued on memory but no notion outbox item referenced {memory_id}")
    ok("Notion queue", f"outbox_id={matching[0].get('id')}, status={matching[0].get('status')}")
    if process_outbox:
        result = request("POST", "/api/outbox/process?limit=25", {})
        print(f"[info] Outbox process result: {result}")
        refreshed = request("GET", "/api/memory?" + urllib.parse.urlencode({"kind": "ops_probe", "limit": 25}))
        refreshed_row = next(row for row in refreshed if row.get("id") == memory_id)
        if refreshed_row.get("notion_status") not in {"synced", "failed", "queued"}:
            raise RuntimeError(f"unexpected Notion status after outbox process: {refreshed_row}")
        ok("Notion process", f"notion_status={refreshed_row.get('notion_status')}")
elif notion_status == "not_configured":
    warn("Notion queue", "NOTION_DATABASE_ID is not configured")
else:
    raise RuntimeError(f"unexpected memory notion_status: {notion_status}")

print(f"Memory stack probe passed with {len(warnings)} warning(s).")
PY
