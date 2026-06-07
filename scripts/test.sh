#!/bin/bash
set -euo pipefail

echo "=== Testing DialDesk Autonomous Business Runtime ==="

cd "$(dirname "$0")/.."

export DIALDESK_DB="$(mktemp -t dialdesk-test.XXXXXX.sqlite3)"
export OBSIDIAN_VAULT_PATH="$(mktemp -d -t dialdesk-obsidian.XXXXXX)"
export DIALDESK_BACKUP_DIR="$(mktemp -d -t dialdesk-backups.XXXXXX)"
export SCHEDULER_INTERVAL_SECONDS=3600
export CEO_EOD_HOUR_UTC=23
export DIALDESK_SEND_OUTREACH=0

python3 -m compileall -q src/coordinator/main.py src/browser_operator.py

python3 - <<'PY'
import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from src import browser_operator
from src.coordinator import main as coordinator
from src.integrations import sendivo_api, smartlead_api
from src.coordinator.main import DEFAULT_CLIENT_ID, app

client = TestClient(app)

assert browser_operator.page_title("<html><title>DialDesk Test</title></html>") == "DialDesk Test"
manual_browser = asyncio.run(
    browser_operator.inspect_job({"id": "manual", "objective": "Manual browser task", "url": ""}, mode="http")
)
assert manual_browser.status == "completed", manual_browser
assert manual_browser.metadata["manual_required"] is True, manual_browser

health = client.get("/health")
assert health.status_code == 200, health.text
assert health.json()["status"] == "up"

dashboard = client.get("/dashboard")
assert dashboard.status_code == 200, dashboard.text
dashboard_html = dashboard.text
assert "Operator Controls" in dashboard_html, dashboard_html
assert "/api/ops/control-room/action" in dashboard_html, dashboard_html
assert "runControlAction('run_watchdog'" in dashboard_html, dashboard_html
assert "runControlAction('run_self_audit'" in dashboard_html, dashboard_html
assert "runControlAction('create_operator_briefing'" in dashboard_html, dashboard_html
assert "runControlAction('run_launch_gate'" in dashboard_html, dashboard_html
assert "runtimeComponent" in dashboard_html, dashboard_html
assert "Proof Pack" in dashboard_html, dashboard_html
assert "proofPack" in dashboard_html, dashboard_html
assert "Proof Runs" in dashboard_html, dashboard_html
assert "proofRuns" in dashboard_html, dashboard_html
assert "Operator Briefings" in dashboard_html, dashboard_html
assert "operatorBriefings" in dashboard_html, dashboard_html

card = client.get("/agentCard").json()
assert "Appointment Setting" in card["name"], card
assert "does not offer business loans" in card["description"], card

brief = client.get("/api/ceo/daily-brief?force=true")
assert brief.status_code == 200, brief.text
overview = client.get("/api/ops/overview")
assert overview.status_code == 200, overview.text
overview_payload = overview.json()
assert overview_payload["memory"]["sqlite"] is True
assert overview_payload["memory"]["obsidian_configured"] is True
assert overview_payload["visibility"]["dashboard"] == "/dashboard"
assert overview_payload["visibility"]["activity_api"] == "/api/activity", overview_payload
assert overview_payload["visibility"]["control_room_api"] == "/api/ops/control-room", overview_payload
assert overview_payload["visibility"]["recent_activity"], overview_payload
assert overview_payload["governance"]["active_goals"] >= 8, overview_payload
assert overview_payload["governance"]["ceo_decisions"] == "/api/ceo/decisions", overview_payload
assert overview_payload["governance"]["ceo_decision_review"] == "/api/ceo/decisions/evaluate", overview_payload
assert overview_payload["production_ops"]["health_checks"] == "/api/ops/health-checks", overview_payload
assert overview_payload["production_ops"]["runtime_controls"] == "/api/ops/runtime-controls", overview_payload
assert overview_payload["production_ops"]["runtime_heartbeats"] == "/api/ops/runtime-heartbeats", overview_payload
assert overview_payload["production_ops"]["watchdog_runs"] == "/api/ops/watchdog", overview_payload
assert overview_payload["production_ops"]["run_watchdog"] == "/api/ops/watchdog/run", overview_payload
assert overview_payload["production_ops"]["self_audit_runs"] == "/api/ops/proof-runs?proof_type=runtime_self_audit", overview_payload
assert overview_payload["production_ops"]["run_self_audit"] == "/api/ops/self-audit/run", overview_payload
assert overview_payload["production_ops"]["autonomy_drills"] == "/api/ops/autonomy-drills", overview_payload
assert overview_payload["production_ops"]["run_autonomy_drill"] == "/api/ops/autonomy-drills/run", overview_payload
assert overview_payload["production_ops"]["launch_gate_runs"] == "/api/ops/launch-gate", overview_payload
assert overview_payload["production_ops"]["run_launch_gate"] == "/api/ops/launch-gate/run", overview_payload
assert overview_payload["proof_pack"]["endpoint"] == "/api/ops/proof-pack", overview_payload
assert "scripts/business_stack_probe.sh" in overview_payload["proof_pack"]["primary"]["command"], overview_payload
assert any(signal["proof"] == "Browser stack" for signal in overview_payload["proof_pack"]["signals"]), overview_payload
proof_pack = client.get("/api/ops/proof-pack")
assert proof_pack.status_code == 200, proof_pack.text
assert proof_pack.json()["primary"]["step"] == "Prove full business stack", proof_pack.json()
assert any("scripts/slack_stack_probe.sh" in command["command"] for command in proof_pack.json()["commands"]), proof_pack.json()
proof_record = client.post(
    "/api/ops/proof-runs",
    json={
        "proof_type": "business_stack",
        "status": "passed",
        "summary": "Unit test business stack proof passed.",
        "source": "unit_test",
        "duration_ms": 123,
        "command": "bash scripts/business_stack_probe.sh",
        "checks": [{"name": "ops doctor", "status": "passed", "detail": "unit"}],
    },
)
assert proof_record.status_code == 200, proof_record.text
proof_record_payload = proof_record.json()
assert proof_record_payload["proof_type"] == "business_stack", proof_record_payload
proof_runs = client.get("/api/ops/proof-runs?proof_type=business_stack")
assert proof_runs.status_code == 200, proof_runs.text
assert any(run["id"] == proof_record_payload["id"] for run in proof_runs.json()), proof_runs.json()
proof_activity = client.get("/api/activity?source=proof_run&limit=20")
assert proof_activity.status_code == 200, proof_activity.text
assert any(item["entity_id"] == proof_record_payload["id"] for item in proof_activity.json()), proof_activity.json()
manual_self_audit = client.post("/api/ops/self-audit/run", json={"requested_by": "system", "force": True})
assert manual_self_audit.status_code == 200, manual_self_audit.text
manual_self_audit_payload = manual_self_audit.json()
assert manual_self_audit_payload["proof_type"] == "runtime_self_audit", manual_self_audit_payload
assert manual_self_audit_payload["status"] in {"passed", "warning", "failed"}, manual_self_audit_payload
self_audit_checks = json.loads(manual_self_audit_payload["checks"])
assert any(check["name"] == "memory" for check in self_audit_checks), self_audit_checks
self_audit_history = client.get("/api/ops/proof-runs?proof_type=runtime_self_audit")
assert self_audit_history.status_code == 200, self_audit_history.text
assert any(run["id"] == manual_self_audit_payload["id"] for run in self_audit_history.json()), self_audit_history.json()
slack_proof_pack = client.post("/api/slack/command", json={"command": "proof-pack", "user": "Justin"})
assert slack_proof_pack.status_code == 200, slack_proof_pack.text
assert "Proof pack" in slack_proof_pack.json()["text"], slack_proof_pack.json()
slack_self_audit = client.post("/api/slack/command", json={"command": "self-audit", "text": "force", "user": "Justin"})
assert slack_self_audit.status_code == 200, slack_self_audit.text
assert "Self-audit" in slack_self_audit.json()["text"], slack_self_audit.json()
assert overview_payload["runtime"]["controls"]["scheduler"]["enabled"] == 1, overview_payload
assert overview_payload["runtime"]["controls"]["self_audit"]["enabled"] == 1, overview_payload
assert "scheduler" in overview_payload["runtime"]["heartbeats"], overview_payload
assert "watchdog" in overview_payload["runtime"]["controls"], overview_payload
assert overview_payload["operating_cycle"]["start"] == "/api/ceo/operating-cycle/start", overview_payload
runtime_controls = client.get("/api/ops/runtime-controls")
assert runtime_controls.status_code == 200, runtime_controls.text
assert any(c["component"] == "outreach" and c["enabled"] for c in runtime_controls.json()), runtime_controls.json()
runtime_heartbeats = client.get("/api/ops/runtime-heartbeats")
assert runtime_heartbeats.status_code == 200, runtime_heartbeats.text
assert any(h["component"] == "scheduler" for h in runtime_heartbeats.json()), runtime_heartbeats.json()
control_room = client.get("/api/ops/control-room")
assert control_room.status_code == 200, control_room.text
control_room_payload = control_room.json()
assert control_room_payload["status"] in {"running", "needs_attention", "critical", "stopped"}, control_room_payload
assert any(layer["layer"] == "Autonomous Scheduler" for layer in control_room_payload["how_it_works"]), control_room_payload
assert any(control["action"] == "run_watchdog" for control in control_room_payload["operator_controls"]), control_room_payload
assert any(control["action"] == "run_autonomy_drill" for control in control_room_payload["operator_controls"]), control_room_payload
assert any(control["action"] == "run_self_audit" for control in control_room_payload["operator_controls"]), control_room_payload
assert any(control["action"] == "create_operator_briefing" for control in control_room_payload["operator_controls"]), control_room_payload
assert any(control["action"] == "run_launch_gate" for control in control_room_payload["operator_controls"]), control_room_payload
assert "memory_health" in control_room_payload["live_state"], control_room_payload
control_room_action = client.post("/api/ops/control-room/action", json={"action": "run_health", "actor": "Justin"})
assert control_room_action.status_code == 200, control_room_action.text
assert control_room_action.json()["action"] == "run_health", control_room_action.json()
control_room_self_audit = client.post("/api/ops/control-room/action", json={"action": "run_self_audit", "actor": "Justin", "force": True})
assert control_room_self_audit.status_code == 200, control_room_self_audit.text
assert control_room_self_audit.json()["result"]["proof_type"] == "runtime_self_audit", control_room_self_audit.json()
operator_briefing = client.post("/api/ops/operator-briefing", json={"requested_by": "ceo", "force": True, "include_slack": True})
assert operator_briefing.status_code == 200, operator_briefing.text
operator_briefing_payload = operator_briefing.json()
assert operator_briefing_payload["type"] == "operator_briefing", operator_briefing_payload
assert "Under The Hood" in operator_briefing_payload["body"], operator_briefing_payload
reports = client.get("/api/reports?report_type=operator_briefing")
assert reports.status_code == 200, reports.text
assert any(report["id"] == operator_briefing_payload["id"] for report in reports.json()), reports.json()
report_activity = client.get("/api/activity?source=report&limit=20")
assert report_activity.status_code == 200, report_activity.text
assert any(item["entity_id"] == operator_briefing_payload["id"] for item in report_activity.json()), report_activity.json()
control_room_briefing = client.post("/api/ops/control-room/action", json={"action": "create_operator_briefing", "actor": "Justin", "force": True})
assert control_room_briefing.status_code == 200, control_room_briefing.text
assert control_room_briefing.json()["result"]["type"] == "operator_briefing", control_room_briefing.json()
slack_briefing = client.post("/api/slack/command", json={"command": "briefing", "text": "force", "user": "Justin"})
assert slack_briefing.status_code == 200, slack_briefing.text
assert "Operator Briefing" in slack_briefing.json()["text"], slack_briefing.json()
pause_worker = client.post(
    "/api/ops/runtime-controls",
    json={"component": "worker", "enabled": False, "reason": "Unit test pause", "set_by": "Justin"},
)
assert pause_worker.status_code == 200, pause_worker.text
assert pause_worker.json()["enabled"] == 0, pause_worker.json()
paused_heartbeats = client.get("/api/ops/runtime-heartbeats?status=paused").json()
assert any(h["component"] == "worker" for h in paused_heartbeats), paused_heartbeats
readiness_with_paused_worker = client.post("/api/ops/readiness/run", json={"profile": "production", "requested_by": "cto"}).json()
paused_checks = json.loads(readiness_with_paused_worker["checks"])
assert any(c["key"] == "runtime_controls" and c["status"] == "fail" for c in paused_checks), paused_checks
resume_worker = client.post(
    "/api/ops/runtime-controls",
    json={"component": "worker", "enabled": True, "reason": "Unit test resume", "set_by": "Justin"},
)
assert resume_worker.status_code == 200, resume_worker.text
assert resume_worker.json()["enabled"] == 1, resume_worker.json()
slack_runtime = client.post("/api/slack/command", json={"command": "runtime-controls", "user": "Justin"})
assert slack_runtime.status_code == 200, slack_runtime.text
assert "Runtime controls:" in slack_runtime.json()["text"], slack_runtime.json()
slack_heartbeats = client.post("/api/slack/command", json={"command": "runtime-heartbeats", "user": "Justin"})
assert slack_heartbeats.status_code == 200, slack_heartbeats.text
assert "Runtime heartbeats:" in slack_heartbeats.json()["text"], slack_heartbeats.json()
slack_control_room = client.post("/api/slack/command", json={"command": "control-room", "user": "Justin"})
assert slack_control_room.status_code == 200, slack_control_room.text
assert "Control room" in slack_control_room.json()["text"], slack_control_room.json()
watchdog = client.post("/api/ops/watchdog/run", json={"source": "unit_test", "force": True})
assert watchdog.status_code == 200, watchdog.text
watchdog_payload = watchdog.json()
assert watchdog_payload["status"] in {"degraded", "critical"}, watchdog_payload
assert watchdog_payload["incidents_opened"] >= 1, watchdog_payload
watchdog_findings = json.loads(watchdog_payload["findings"])
assert any(f["key"] == "scheduler_stale" for f in watchdog_findings), watchdog_findings
watchdog_again = client.post("/api/ops/watchdog/run", json={"source": "unit_test", "force": True}).json()
assert watchdog_again["incidents_opened"] == 0, watchdog_again
watchdog_runs = client.get("/api/ops/watchdog")
assert watchdog_runs.status_code == 200, watchdog_runs.text
assert any(run["id"] == watchdog_payload["id"] for run in watchdog_runs.json()), watchdog_runs.json()
slack_watchdog = client.post("/api/slack/command", json={"command": "watchdog", "text": "force", "user": "Justin"})
assert slack_watchdog.status_code == 200, slack_watchdog.text
assert "Watchdog" in slack_watchdog.json()["text"], slack_watchdog.json()
autonomy_drill = client.post("/api/ops/autonomy-drills/run", json={"requested_by": "ceo"})
assert autonomy_drill.status_code == 200, autonomy_drill.text
autonomy_drill_payload = autonomy_drill.json()
assert autonomy_drill_payload["status"] in {"passed", "warning"}, autonomy_drill_payload
autonomy_checks = json.loads(autonomy_drill_payload["checks"])
assert any(c["key"] == "memory" and c["status"] == "pass" for c in autonomy_checks), autonomy_checks
assert any(c["key"] == "browser_queue" and c["status"] == "pass" for c in autonomy_checks), autonomy_checks
autonomy_history = client.get("/api/ops/autonomy-drills")
assert autonomy_history.status_code == 200, autonomy_history.text
assert any(run["id"] == autonomy_drill_payload["id"] for run in autonomy_history.json()), autonomy_history.json()
slack_drill = client.post("/api/slack/command", json={"command": "autonomy-drill", "text": "no-browser", "user": "Justin"})
assert slack_drill.status_code == 200, slack_drill.text
assert "Autonomy drill" in slack_drill.json()["text"], slack_drill.json()
launch_gate = client.post("/api/ops/launch-gate/run", json={"profile": "local", "requested_by": "cto"})
assert launch_gate.status_code == 200, launch_gate.text
launch_gate_payload = launch_gate.json()
assert launch_gate_payload["status"] in {"ready", "warning"}, launch_gate_payload
launch_gate_checks = json.loads(launch_gate_payload["checks"])
assert any(c["key"] == "core_runtime_controls" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "api_service_hardened" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "browser_service_hardened" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "vps_bootstrap_script" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "business_stack_probe_script" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "ops_doctor_script" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "memory_stack_probe_script" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "slack_stack_probe_script" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
assert any(c["key"] == "browser_stack_probe_script" and c["status"] == "pass" for c in launch_gate_checks), launch_gate_checks
launch_gate_commands = json.loads(launch_gate_payload["commands"])
assert any("dial-desk-swarm.service" in c["command"] for c in launch_gate_commands), launch_gate_commands
assert any("scripts/vps_bootstrap.sh" in c["command"] for c in launch_gate_commands), launch_gate_commands
assert any("scripts/business_stack_probe.sh" in c["command"] for c in launch_gate_commands), launch_gate_commands
assert any("scripts/ops_doctor.sh" in c["command"] for c in launch_gate_commands), launch_gate_commands
assert any("scripts/memory_stack_probe.sh" in c["command"] for c in launch_gate_commands), launch_gate_commands
assert any("scripts/slack_stack_probe.sh" in c["command"] for c in launch_gate_commands), launch_gate_commands
assert any("scripts/browser_stack_probe.sh" in c["command"] for c in launch_gate_commands), launch_gate_commands
business_stack_probe_text = Path("scripts/business_stack_probe.sh").read_text()
ops_doctor_text = Path("scripts/ops_doctor.sh").read_text()
memory_stack_probe_text = Path("scripts/memory_stack_probe.sh").read_text()
slack_stack_probe_text = Path("scripts/slack_stack_probe.sh").read_text()
browser_stack_probe_text = Path("scripts/browser_stack_probe.sh").read_text()
vps_bootstrap_text = Path("scripts/vps_bootstrap.sh").read_text()
assert "scripts/ops_doctor.sh" in business_stack_probe_text, business_stack_probe_text
assert "scripts/memory_stack_probe.sh" in business_stack_probe_text, business_stack_probe_text
assert "scripts/slack_stack_probe.sh" in business_stack_probe_text, business_stack_probe_text
assert "scripts/browser_stack_probe.sh" in business_stack_probe_text, business_stack_probe_text
assert "/api/ops/proof-runs" in business_stack_probe_text, business_stack_probe_text
assert "/api/ops/control-room/action" in ops_doctor_text, ops_doctor_text
assert "/api/browser/operators" in ops_doctor_text, ops_doctor_text
assert "/api/ops/proof-pack" in ops_doctor_text, ops_doctor_text
assert "APP_TOKEN" in ops_doctor_text, ops_doctor_text
assert "/api/memory/health" in memory_stack_probe_text, memory_stack_probe_text
assert "obsidian_path" in memory_stack_probe_text, memory_stack_probe_text
assert "/api/outbox" in memory_stack_probe_text, memory_stack_probe_text
assert "APP_TOKEN" in memory_stack_probe_text, memory_stack_probe_text
assert "/api/slack/notify" in slack_stack_probe_text, slack_stack_probe_text
assert "/api/slack/command" in slack_stack_probe_text, slack_stack_probe_text
assert "/slack/command" in slack_stack_probe_text, slack_stack_probe_text
assert "SLACK_SIGNING_SECRET" in slack_stack_probe_text, slack_stack_probe_text
assert "src.browser_operator" in browser_stack_probe_text, browser_stack_probe_text
assert "/api/memory" in browser_stack_probe_text, browser_stack_probe_text
assert "APP_TOKEN" in browser_stack_probe_text, browser_stack_probe_text
assert "scripts/business_stack_probe.sh" in vps_bootstrap_text, vps_bootstrap_text
launch_gate_history = client.get("/api/ops/launch-gate?profile=local")
assert launch_gate_history.status_code == 200, launch_gate_history.text
assert any(run["id"] == launch_gate_payload["id"] for run in launch_gate_history.json()), launch_gate_history.json()
slack_launch_gate = client.post("/api/slack/command", json={"command": "launch-gate", "text": "local", "user": "Justin"})
assert slack_launch_gate.status_code == 200, slack_launch_gate.text
assert "launch gate" in slack_launch_gate.json()["text"], slack_launch_gate.json()
slack_pause = client.post("/api/slack/command", json={"command": "pause-runtime", "text": "outreach unit test", "user": "Justin"})
assert slack_pause.status_code == 200, slack_pause.text
assert slack_pause.json()["control"]["enabled"] == 0, slack_pause.json()
slack_resume = client.post("/api/slack/command", json={"command": "resume-runtime", "text": "outreach unit test", "user": "Justin"})
assert slack_resume.status_code == 200, slack_resume.text
assert slack_resume.json()["control"]["enabled"] == 1, slack_resume.json()
activity_initial = client.get("/api/activity?limit=80")
assert activity_initial.status_code == 200, activity_initial.text
assert any(item["source"] in {"task", "memory", "audit"} for item in activity_initial.json()), activity_initial.json()
assert any(item["source"] == "watchdog" for item in activity_initial.json()), activity_initial.json()
assert any(item["source"] == "autonomy_drill" for item in activity_initial.json()), activity_initial.json()
assert any(item["source"] == "launch_gate" for item in activity_initial.json()), activity_initial.json()
tasks = client.get("/api/tasks").json()
assert any(t["agent_id"] == "cmo" for t in tasks), tasks
assert any(t["agent_id"] == "coo" for t in tasks), tasks
assert any(t["agent_id"] == "cto" for t in tasks), tasks
assert any(t["agent_id"] == "sales_director" for t in tasks), tasks

operating_start = client.post("/api/ceo/operating-cycle/start", json={"requested_by": "ceo"})
assert operating_start.status_code == 200, operating_start.text
operating_start_payload = operating_start.json()
assert operating_start_payload["status"] == "running", operating_start_payload
assert operating_start_payload["morning_report_id"], operating_start_payload
assert operating_start_payload["standup_thread_id"], operating_start_payload
operating_metadata = json.loads(operating_start_payload["metadata"])
assert operating_metadata["operator_briefing_id"], operating_metadata
daily_plan = json.loads(operating_start_payload["daily_plan"])
assert {item["agent_id"] for item in daily_plan} >= {"ceo", "cmo", "coo", "cto", "sales_director", "builder"}, daily_plan
operating_tick = client.post("/api/ceo/operating-cycle/tick?source=unit-test")
assert operating_tick.status_code == 200, operating_tick.text
assert operating_tick.json()["id"] == operating_start_payload["id"], operating_tick.json()
status_after_cycle = client.get("/api/status").json()
assert any(c["id"] == operating_start_payload["id"] for c in status_after_cycle["operating_cycles"]), status_after_cycle
assert any(report["id"] == operating_metadata["operator_briefing_id"] for report in status_after_cycle["reports"]), status_after_cycle["reports"]
operating_close = client.post(
    "/api/ceo/operating-cycle/close",
    json={"requested_by": "ceo", "summary": "Unit test close of the daily operating cycle."},
)
assert operating_close.status_code == 200, operating_close.text
operating_close_payload = operating_close.json()
assert operating_close_payload["status"] == "closed", operating_close_payload
assert operating_close_payload["close_report_id"], operating_close_payload
operating_history = client.get("/api/ceo/operating-cycles?status=closed").json()
assert any(c["id"] == operating_start_payload["id"] for c in operating_history), operating_history

goals = client.get("/api/governance/goals?level=mission").json()
assert any("24/7 appointment-setting" in g["title"] for g in goals), goals
created_goal = client.post(
    "/api/governance/goals",
    json={
        "title": "Keep the business simple to operate",
        "description": "Autonomy should reduce Justin's workload, not create a complicated control room.",
        "level": "project",
        "owner_agent_id": "ceo",
        "target": "Transparent autonomous runtime",
    },
)
assert created_goal.status_code == 200, created_goal.text
budgets = client.get("/api/governance/budgets").json()
assert any(b["agent_id"] == "ceo" for b in budgets), budgets
budget_update = client.post(
    "/api/governance/budgets",
    json={"agent_id": "builder", "monthly_budget": 25, "auto_pause": True},
)
assert budget_update.status_code == 200, budget_update.text
heartbeat = client.post(
    "/api/governance/heartbeats",
    json={"agent_id": "ceo", "summary": "Daily operating loop is alive.", "metrics": {"test": True}},
)
assert heartbeat.status_code == 200, heartbeat.text
status_command = client.post("/api/slack/command", json={"command": "status", "user": "Justin"})
assert status_command.status_code == 200, status_command.text
assert "DialDesk status" in status_command.json()["text"]
budget_command = client.post("/api/slack/command", json={"command": "budget-remaining", "text": "builder", "user": "Justin"})
assert budget_command.status_code == 200, budget_command.text
assert budget_command.json()["remaining"] == 25
pause_command = client.post("/api/slack/command", json={"command": "pause-agent", "text": "sales_director testing", "user": "Justin"})
assert pause_command.status_code == 200, pause_command.text
assert pause_command.json()["agent"]["status"] == "paused"
resume_command = client.post("/api/slack/command", json={"command": "resume-agent", "text": "sales_director testing", "user": "Justin"})
assert resume_command.status_code == 200, resume_command.text
assert resume_command.json()["agent"]["status"] == "idle"

agent_message = client.post(
    "/api/agent-messages",
    json={
        "from_agent_id": "ceo",
        "to_agent_id": "cmo",
        "subject": "Review offer positioning",
        "body": "Make sure the site says appointment setting, not lending.",
        "message_type": "request",
        "priority": 1,
    },
)
assert agent_message.status_code == 200, agent_message.text
agent_message_payload = agent_message.json()
assert agent_message_payload["status"] == "unread", agent_message_payload
threads_after_message = client.get("/api/agent-threads").json()
assert any(t["thread_id"] == agent_message_payload["thread_id"] for t in threads_after_message), threads_after_message
cmo_inbox = client.get("/api/agents/cmo/inbox?status=unread").json()
assert any(m["id"] == agent_message_payload["id"] for m in cmo_inbox), cmo_inbox
coordination_room = client.get("/api/agents/coordination").json()
assert coordination_room["agents"]["cmo"]["workload"]["unread_messages"] >= 1, coordination_room
assert any(t["thread_id"] == agent_message_payload["thread_id"] for t in coordination_room["threads"]), coordination_room
reply_message = client.post(
    f"/api/agent-messages/{agent_message_payload['id']}/reply",
    json={
        "from_agent_id": "cmo",
        "body": "Positioning reviewed. DialDesk remains appointment setting, not lending.",
        "message_type": "update",
        "priority": 2,
    },
)
assert reply_message.status_code == 200, reply_message.text
reply_message_payload = reply_message.json()
assert reply_message_payload["thread_id"] == agent_message_payload["thread_id"], reply_message_payload
assert reply_message_payload["to_agent_id"] == "ceo", reply_message_payload
agent_tasks = client.get("/api/tasks?client_id=dialdesk-internal").json()
assert any(t["metadata"].find(agent_message_payload["id"]) >= 0 for t in agent_tasks), agent_tasks
read_message = client.post(f"/api/agent-messages/{agent_message_payload['id']}/read?reader=cmo")
assert read_message.status_code == 200, read_message.text
assert read_message.json()["status"] == "read", read_message.json()

standup = client.post("/api/agents/standup", json={"topic": "Morning coordination test"})
assert standup.status_code == 200, standup.text
standup_payload = standup.json()
assert len(standup_payload["messages"]) >= 7, standup_payload
assert standup_payload["memory"]["kind"] == "standup", standup_payload
agent_messages = client.get("/api/agent-messages").json()
assert any(m["thread_id"] == standup_payload["thread_id"] for m in agent_messages), agent_messages
heartbeats_after_standup = client.get("/api/governance/heartbeats").json()
assert any(h["agent_id"] == "ceo" for h in heartbeats_after_standup), heartbeats_after_standup

slack_ask = client.post(
    "/api/slack/command",
    json={"command": "ask-agent", "text": "sales_director What is the highest priority sales action?", "user": "Justin"},
)
assert slack_ask.status_code == 200, slack_ask.text
assert slack_ask.json()["message"]["to_agent_id"] == "sales_director", slack_ask.json()
slack_standup = client.post("/api/slack/command", json={"command": "standup", "text": "Slack standup test", "user": "Justin"})
assert slack_standup.status_code == 200, slack_standup.text
assert "Agent standup complete" in slack_standup.json()["text"], slack_standup.json()
slack_room = client.post("/api/slack/command", json={"command": "coordination-room", "user": "Justin"})
assert slack_room.status_code == 200, slack_room.text
assert "Coordination room:" in slack_room.json()["text"], slack_room.json()
slack_inbox = client.post("/api/slack/command", json={"command": "inbox", "text": "ceo", "user": "Justin"})
assert slack_inbox.status_code == 200, slack_inbox.text
assert "ceo inbox:" in slack_inbox.json()["text"], slack_inbox.json()
slack_decisions = client.post("/api/slack/command", json={"command": "decisions", "user": "Justin"})
assert slack_decisions.status_code == 200, slack_decisions.text
assert "CEO decisions:" in slack_decisions.json()["text"], slack_decisions.json()
slack_decision_review = client.post("/api/slack/command", json={"command": "decision-review", "text": "force", "user": "Justin"})
assert slack_decision_review.status_code == 200, slack_decision_review.text
assert "CEO decision review created" in slack_decision_review.json()["text"], slack_decision_review.json()

health_check = client.post("/api/ops/health-checks/run")
assert health_check.status_code == 200, health_check.text
health_payload = health_check.json()
assert health_payload["status"] in {"ok", "degraded"}, health_payload
health_metrics = json.loads(health_payload["metrics"])
assert "db_path" in health_metrics, health_payload
health_history = client.get("/api/ops/health-checks").json()
assert any(h["id"] == health_payload["id"] for h in health_history), health_history

backup = client.post("/api/ops/backups/create", json={"reason": "unit-test", "requested_by": "cto"})
assert backup.status_code == 200, backup.text
backup_payload = backup.json()
assert backup_payload["status"] == "completed", backup_payload
assert Path(backup_payload["path"]).exists(), backup_payload
assert backup_payload["size_bytes"] > 0, backup_payload
backup_history = client.get("/api/ops/backups").json()
assert any(b["id"] == backup_payload["id"] for b in backup_history), backup_history

readiness_local = client.post("/api/ops/readiness/run", json={"profile": "local", "requested_by": "cto"})
assert readiness_local.status_code == 200, readiness_local.text
readiness_local_payload = readiness_local.json()
assert readiness_local_payload["status"] in {"ready", "warning"}, readiness_local_payload
local_checks = json.loads(readiness_local_payload["checks"])
assert any(c["key"] == "database_file" and c["status"] == "pass" for c in local_checks), local_checks

readiness_production = client.post("/api/ops/readiness/run", json={"profile": "production", "requested_by": "cto"})
assert readiness_production.status_code == 200, readiness_production.text
readiness_production_payload = readiness_production.json()
assert readiness_production_payload["status"] == "not_ready", readiness_production_payload
production_checks = json.loads(readiness_production_payload["checks"])
assert any(c["key"] == "app_token" and c["status"] == "fail" for c in production_checks), production_checks
assert any(c["key"] == "persistent_db_path" and c["status"] == "fail" for c in production_checks), production_checks
readiness_history = client.get("/api/ops/readiness?profile=production").json()
assert any(r["id"] == readiness_production_payload["id"] for r in readiness_history), readiness_history
overview_after_readiness = client.get("/api/ops/overview").json()
assert overview_after_readiness["production_ops"]["run_readiness_check"] == "/api/ops/readiness/run"
assert overview_after_readiness["production_ops"]["latest_readiness_check"], overview_after_readiness

incident = client.post(
    "/api/ops/incidents",
    json={
        "title": "Unit test incident",
        "severity": "warning",
        "source": "unit-test",
        "details": {"area": "ops"},
    },
)
assert incident.status_code == 200, incident.text
incident_payload = incident.json()
assert incident_payload["status"] == "open", incident_payload
open_incidents = client.get("/api/ops/incidents?status=open").json()
assert any(i["id"] == incident_payload["id"] for i in open_incidents), open_incidents
resolved_incident = client.post(
    f"/api/ops/incidents/{incident_payload['id']}/resolve",
    json={"resolved_by": "cto", "resolution": "Verified incident resolution path."},
)
assert resolved_incident.status_code == 200, resolved_incident.text
assert resolved_incident.json()["status"] == "resolved", resolved_incident.json()

slack_health = client.post("/api/slack/command", json={"command": "health", "user": "Justin"})
assert slack_health.status_code == 200, slack_health.text
assert "Health check" in slack_health.json()["text"], slack_health.json()
slack_backup = client.post("/api/slack/command", json={"command": "backup-now", "user": "Justin"})
assert slack_backup.status_code == 200, slack_backup.text
assert slack_backup.json()["backup"]["status"] == "completed", slack_backup.json()
slack_incidents = client.post("/api/slack/command", json={"command": "incidents", "user": "Justin"})
assert slack_incidents.status_code == 200, slack_incidents.text
assert "open incident" in slack_incidents.json()["text"], slack_incidents.json()
slack_memory = client.post("/api/slack/command", json={"command": "memory-health", "user": "Justin"})
assert slack_memory.status_code == 200, slack_memory.text
assert "Memory:" in slack_memory.json()["text"], slack_memory.json()
slash_body = urlencode({"command": "/dialdesk", "text": "browser-jobs", "user_name": "Justin", "team_id": "T1"}).encode()
slash = client.post("/slack/command", content=slash_body, headers={"Content-Type": "application/x-www-form-urlencoded"})
assert slash.status_code == 200, slash.text
assert slash.json()["response_type"] == "ephemeral", slash.json()
assert "Browser jobs:" in slash.json()["text"], slash.json()
original_slack_signing_secret = coordinator.SLACK_SIGNING_SECRET
coordinator.SLACK_SIGNING_SECRET = "test-slack-secret"
signed_body = urlencode({"command": "/dialdesk", "text": "memory-health", "user_name": "Justin"}).encode()
timestamp = str(int(time.time()))
signature = "v0=" + hmac.new(
    coordinator.SLACK_SIGNING_SECRET.encode(),
    b"v0:" + timestamp.encode() + b":" + signed_body,
    hashlib.sha256,
).hexdigest()
signed_slash = client.post(
    "/slack/command",
    content=signed_body,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    },
)
assert signed_slash.status_code == 200, signed_slash.text
assert "Memory:" in signed_slash.json()["text"], signed_slash.json()
bad_slash = client.post(
    "/slack/command",
    content=signed_body,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": "v0=bad",
    },
)
assert bad_slash.status_code == 401, bad_slash.text
coordinator.SLACK_SIGNING_SECRET = original_slack_signing_secret

memory = client.post(
    "/api/memory",
    json={
        "title": "Test memory note",
        "body": "This should be saved to SQLite and Obsidian.",
        "kind": "decision",
        "actor": "ceo",
        "tags": ["test", "memory"],
    },
)
assert memory.status_code == 200, memory.text
memory_payload = memory.json()
assert memory_payload["id"]
assert Path(memory_payload["obsidian_path"]).exists(), memory_payload
memory_list = client.get("/api/memory?kind=decision").json()
assert any(m["title"] == "Test memory note" for m in memory_list), memory_list
memory_activity = client.get("/api/activity?source=memory&limit=20").json()
assert any(item["entity_id"] == memory_payload["id"] for item in memory_activity), memory_activity
memory_health = client.get("/api/memory/health")
assert memory_health.status_code == 200, memory_health.text
memory_health_payload = memory_health.json()
assert memory_health_payload["sqlite"] is True, memory_health_payload
assert memory_health_payload["obsidian"]["written"] >= 1, memory_health_payload

original_notion_database_id = coordinator.NOTION_DATABASE_ID
original_notion_api_key = coordinator.NOTION_API_KEY
original_send_outbox = coordinator.SEND_OUTBOX
original_deliver_notion = coordinator.deliver_notion
coordinator.NOTION_DATABASE_ID = "test-notion-db"
coordinator.NOTION_API_KEY = "test-notion-key"
sync_memory = client.post(f"/api/memory/{memory_payload['id']}/sync-notion", json={"requested_by": "ceo"})
assert sync_memory.status_code == 200, sync_memory.text
assert sync_memory.json()["notion_status"] == "queued", sync_memory.json()

def fake_deliver_notion(row):
    return True, "", {"page_id": "notion-page-test", "url": "https://notion.test/page"}

coordinator.deliver_notion = fake_deliver_notion
coordinator.SEND_OUTBOX = True
notion_processed = client.post("/api/outbox/process?limit=50")
assert notion_processed.status_code == 200, notion_processed.text
synced_memory = client.get("/api/memory?kind=decision").json()
synced_row = next(m for m in synced_memory if m["id"] == memory_payload["id"])
assert synced_row["notion_status"] == "synced", synced_row
assert "notion-page-test" in synced_row["metadata"], synced_row
coordinator.deliver_notion = original_deliver_notion
coordinator.SEND_OUTBOX = original_send_outbox
coordinator.NOTION_DATABASE_ID = original_notion_database_id
coordinator.NOTION_API_KEY = original_notion_api_key

slack = client.post(
    "/api/slack/notify",
    json={"text": "Runtime test alert", "severity": "info", "channel": "dialdesk-ops"},
)
assert slack.status_code == 200, slack.text
outbox = client.get("/api/outbox?destination=slack&status=queued").json()
assert any(o["body"] == "Runtime test alert" for o in outbox), outbox
processed = client.post("/api/outbox/process?limit=5")
assert processed.status_code == 200, processed.text
assert processed.json()["send_enabled"] is False

browser_job = client.post(
    "/api/browser/jobs",
    json={
        "objective": "Check the live DialDesk homepage and capture key offer language",
        "url": "https://dial-desk-api-production.up.railway.app/",
        "priority": 1,
    },
)
assert browser_job.status_code == 200, browser_job.text
browser_job_id = browser_job.json()["id"]
operator_heartbeat = client.post(
    "/api/browser/operators/heartbeat",
    json={"operator_id": "super-browser-test", "status": "idle", "mode": "http", "note": "Unit test operator online"},
)
assert operator_heartbeat.status_code == 200, operator_heartbeat.text
assert operator_heartbeat.json()["operator_id"] == "super-browser-test", operator_heartbeat.json()
browser_operators = client.get("/api/browser/operators").json()
assert any(o["operator_id"] == "super-browser-test" and o["alive"] for o in browser_operators), browser_operators
slack_browser_operators = client.post("/api/slack/command", json={"command": "browser-operators", "user": "Justin"})
assert slack_browser_operators.status_code == 200, slack_browser_operators.text
assert "Browser operators:" in slack_browser_operators.json()["text"], slack_browser_operators.json()
overview_after_operator = client.get("/api/ops/overview").json()
assert overview_after_operator["browser_automation"]["operators"] == "/api/browser/operators", overview_after_operator
assert overview_after_operator["browser_automation"]["online_operators"] >= 1, overview_after_operator
claimed_browser = client.post(
    "/api/browser/jobs/claim",
    json={"operator_id": "super-browser-test", "lease_seconds": 300},
)
assert claimed_browser.status_code == 200, claimed_browser.text
claimed_payload = claimed_browser.json()
assert claimed_payload["claimed"] is True, claimed_payload
assert claimed_payload["job"]["id"] == browser_job_id, claimed_payload
assert claimed_payload["job"]["status"] == "claimed", claimed_payload
heartbeat_browser = client.post(
    f"/api/browser/jobs/{browser_job_id}/heartbeat",
    json={"operator_id": "super-browser-test", "lease_seconds": 300, "note": "Still checking homepage."},
)
assert heartbeat_browser.status_code == 200, heartbeat_browser.text
assert heartbeat_browser.json()["last_heartbeat_at"], heartbeat_browser.json()
failed_once = client.post(
    f"/api/browser/jobs/{browser_job_id}/fail",
    json={"result": "Temporary browser timeout", "metadata": {"retry": True}},
)
assert failed_once.status_code == 200, failed_once.text
assert failed_once.json()["status"] == "queued", failed_once.json()
reclaimed_browser = client.post(
    "/api/browser/jobs/claim",
    json={"operator_id": "super-browser-test-2", "lease_seconds": 300},
)
assert reclaimed_browser.status_code == 200, reclaimed_browser.text
assert reclaimed_browser.json()["claimed"] is True, reclaimed_browser.json()
assert reclaimed_browser.json()["job"]["attempts"] == 2, reclaimed_browser.json()
browser_complete = client.post(
    f"/api/browser/jobs/{browser_job_id}/complete",
    json={
        "status": "completed",
        "result": "Homepage checked and offer language captured.",
        "artifacts": [{"type": "note", "label": "homepage"}],
    },
)
assert browser_complete.status_code == 200, browser_complete.text
assert browser_complete.json()["status"] == "completed"
browser_memory = client.get("/api/memory?kind=browser_result").json()
assert any(browser_job_id in m["metadata"] for m in browser_memory), browser_memory
empty_claim = client.post("/api/browser/jobs/claim", json={"operator_id": "super-browser-test"})
assert empty_claim.status_code == 200, empty_claim.text
if empty_claim.json()["claimed"]:
    queued_drill_job = empty_claim.json()["job"]
    queued_drill_metadata = json.loads(queued_drill_job["metadata"])
    assert queued_drill_metadata.get("source") == "autonomy_drill", empty_claim.json()
    drained_drill_job = client.post(
        f"/api/browser/jobs/{queued_drill_job['id']}/complete",
        json={
            "status": "completed",
            "result": "Autonomy drill browser proof drained by smoke test.",
            "metadata": {"drained_by": "unit_test"},
        },
    )
    assert drained_drill_job.status_code == 200, drained_drill_job.text
    empty_claim = client.post("/api/browser/jobs/claim", json={"operator_id": "super-browser-test"})
    assert empty_claim.status_code == 200, empty_claim.text
assert empty_claim.json()["claimed"] is False, empty_claim.json()

expired_job = client.post(
    "/api/browser/jobs",
    json={"objective": "Expired lease recovery test", "priority": 1},
)
assert expired_job.status_code == 200, expired_job.text
expired_job_id = expired_job.json()["id"]
expired_claim = client.post("/api/browser/jobs/claim", json={"operator_id": "stale-browser", "lease_seconds": 60})
assert expired_claim.status_code == 200, expired_claim.text
coordinator.STATE.db.execute(
    "UPDATE browser_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
    (expired_job_id,),
)
coordinator.STATE.db.commit()
recovered = client.post("/api/browser/jobs/recover-expired")
assert recovered.status_code == 200, recovered.text
assert recovered.json()["recovered"] >= 1, recovered.json()
recovered_rows = client.get("/api/browser/jobs?status=queued").json()
assert any(j["id"] == expired_job_id for j in recovered_rows), recovered_rows

signup = client.post(
    "/api/clients",
    json={
        "company_name": "Test Funding Co",
        "contact_name": "Taylor",
        "email": "taylor@example.com",
        "package": "human_sdr",
        "crm_url": "https://crm.example.com",
        "calendar_url": "https://cal.example.com/test",
        "monthly_volume": 2500,
        "list_source": "internal MCA submissions",
    },
)
assert signup.status_code == 200, signup.text
new_client_id = signup.json()["id"]
workflow_runs = client.get(f"/api/workflows/runs?client_id={new_client_id}").json()
assert len(workflow_runs) == 1, workflow_runs
workflow_run = workflow_runs[0]
assert workflow_run["playbook"] == "dialdesk_onboarding", workflow_run
workflow_steps = client.get(f"/api/workflows/steps?run_id={workflow_run['id']}").json()
assert len(workflow_steps) == 8, workflow_steps
assert any(step["status"] == "queued" and step["day_offset"] == 0 for step in workflow_steps), workflow_steps
due_workflows = client.post(f"/api/workflows/execute-due?client_id={new_client_id}")
assert due_workflows.status_code == 200, due_workflows.text
first_step = next(step for step in workflow_steps if step["status"] == "queued")
completed_step = client.post(
    f"/api/workflows/steps/{first_step['id']}/complete",
    json={"completed_by": "coo", "result": "Verified Day 0 workflow completion path."},
)
assert completed_step.status_code == 200, completed_step.text
assert completed_step.json()["status"] == "completed", completed_step.json()
client_after_workflow = client.get(f"/api/clients/{new_client_id}/dashboard").json()
assert client_after_workflow["workflow_runs"], client_after_workflow
assert client_after_workflow["workflow_steps"], client_after_workflow
workflow_overview = client.get("/api/ops/overview").json()
assert workflow_overview["workflow_ops"]["active_runs"] >= 1, workflow_overview

opportunity = client.post(
    "/api/opportunities",
    json={
        "company_name": "Growth Funding Group",
        "contact_name": "Morgan",
        "email": "morgan@growthfunding.example",
        "phone": "5552223333",
        "source": "landing_page",
        "package_interest": "human_sdr",
        "notes": "Asked about booked MCA/funding appointments and human SDR coverage.",
    },
)
assert opportunity.status_code == 200, opportunity.text
opportunity_payload = opportunity.json()
opportunity_id = opportunity_payload["id"]
assert opportunity_payload["score"] >= 80, opportunity_payload

sales_brief = client.post(
    "/api/sales-briefs",
    json={"opportunity_id": opportunity_id, "client_id": DEFAULT_CLIENT_ID},
)
assert sales_brief.status_code == 200, sales_brief.text
assert "Growth Funding Group" in sales_brief.json()["body"], sales_brief.json()
assert "We do not offer loans" in sales_brief.json()["body"], sales_brief.json()

payment_link = client.post(
    "/api/payment-links",
    json={"opportunity_id": opportunity_id, "client_id": DEFAULT_CLIENT_ID, "package": "human_sdr"},
)
assert payment_link.status_code == 200, payment_link.text
payment_link_payload = payment_link.json()
assert payment_link_payload["status"] == "dry_run", payment_link_payload
assert payment_link_payload["url"].endswith("/dashboard"), payment_link_payload

crm_jobs = client.get("/api/crm/sync-jobs?status=queued").json()
assert any(j["entity_id"] == opportunity_id for j in crm_jobs), crm_jobs
crm_sync_next = client.post("/api/crm/sync-next?limit=5")
assert crm_sync_next.status_code == 200, crm_sync_next.text
assert crm_sync_next.json()["processed"] >= 1, crm_sync_next.json()
crm_dry_runs = client.get("/api/crm/sync-jobs?status=dry_run").json()
assert any(j["entity_id"] == opportunity_id for j in crm_dry_runs), crm_dry_runs

pipeline_dashboard = client.get(f"/api/clients/{DEFAULT_CLIENT_ID}/dashboard").json()
assert pipeline_dashboard["opportunities"], pipeline_dashboard
assert pipeline_dashboard["sales_briefs"], pipeline_dashboard
assert pipeline_dashboard["payment_links"], pipeline_dashboard
assert pipeline_dashboard["crm_sync_jobs"], pipeline_dashboard
overview_after_pipeline = client.get("/api/ops/overview").json()
assert overview_after_pipeline["revenue_loop"]["open_opportunities"] >= 1, overview_after_pipeline
assert overview_after_pipeline["revenue_loop"]["ready_sales_briefs"] >= 1, overview_after_pipeline

lead_import = client.post(
    "/api/leads/import",
    json={
        "client_id": DEFAULT_CLIENT_ID,
        "source": "unit-test",
        "leads": [
            {
                "first_name": "Ava",
                "company": "Ava Trucking",
                "email": "ava@example.com",
                "phone": "5551112222",
                "monthly_revenue": "50000",
                "funding_amount": "75000",
                "timeline": "asap",
            },
            {"first_name": "Ava", "email": "ava@example.com", "phone": "5551112222"},
            {"first_name": "No Contact"},
            {"first_name": "Opted", "email": "opted@example.com", "opted_out": True},
        ],
    },
)
assert lead_import.status_code == 200, lead_import.text
payload = lead_import.json()
assert payload["imported"] == 1, payload
assert payload["duplicates"] == 1, payload
assert payload["suppressed"] == 2, payload
lead_id = payload["lead_ids"][0]

sms_outreach = client.post(
    "/api/outreach/jobs",
    json={
        "client_id": DEFAULT_CLIENT_ID,
        "lead_id": lead_id,
        "channel": "sms",
        "body": "Hey Ava, do you want to look at funding options this week?",
        "priority": 1,
    },
)
assert sms_outreach.status_code == 200, sms_outreach.text
sms_job_id = sms_outreach.json()["id"]
sms_exec = client.post(f"/api/outreach/jobs/{sms_job_id}/execute")
assert sms_exec.status_code == 200, sms_exec.text
assert sms_exec.json()["status"] == "dry_run", sms_exec.json()

email_outreach = client.post(
    "/api/outreach/jobs",
    json={
        "client_id": DEFAULT_CLIENT_ID,
        "lead_id": lead_id,
        "channel": "email",
        "body": "Ava, quick note on MCA funding appointment follow-up.",
        "provider_campaign_id": "smartlead-test-campaign",
        "priority": 2,
    },
)
assert email_outreach.status_code == 200, email_outreach.text
pause_outreach = client.post(
    "/api/ops/runtime-controls",
    json={"component": "outreach", "enabled": False, "reason": "Confirm queue pause", "set_by": "Justin"},
)
assert pause_outreach.status_code == 200, pause_outreach.text
paused_execute_next = client.post("/api/outreach/execute-next?limit=5")
assert paused_execute_next.status_code == 200, paused_execute_next.text
assert paused_execute_next.json()["processed"] == 0, paused_execute_next.json()
resume_outreach = client.post(
    "/api/ops/runtime-controls",
    json={"component": "outreach", "enabled": True, "reason": "Resume queue", "set_by": "Justin"},
)
assert resume_outreach.status_code == 200, resume_outreach.text
execute_next = client.post("/api/outreach/execute-next?limit=5")
assert execute_next.status_code == 200, execute_next.text
assert execute_next.json()["processed"] >= 1, execute_next.json()
outreach_heartbeat = next(h for h in client.get("/api/ops/runtime-heartbeats").json() if h["component"] == "outreach")
assert outreach_heartbeat["status"] == "ok", outreach_heartbeat
outreach_heartbeat_result = json.loads(outreach_heartbeat["last_result"])
processed_count = outreach_heartbeat_result.get("processed", outreach_heartbeat_result.get("result", {}).get("processed", 0))
assert processed_count >= 1, outreach_heartbeat
runtime_activity = client.get("/api/activity?source=runtime_heartbeat&limit=20").json()
assert any(a["entity_id"] == "outreach" for a in runtime_activity), runtime_activity
dry_runs = client.get("/api/outreach/jobs?status=dry_run").json()
assert len(dry_runs) >= 2, dry_runs
dashboard_after_outreach = client.get(f"/api/clients/{DEFAULT_CLIENT_ID}/dashboard").json()
assert dashboard_after_outreach["metrics"]["sms_sent"] >= 1, dashboard_after_outreach
assert dashboard_after_outreach["metrics"]["emails_sent"] >= 1, dashboard_after_outreach
assert dashboard_after_outreach["outreach_jobs"], dashboard_after_outreach

async def fake_poll_replies(campaign_id, sdr_name="dialdesk"):
    return {
        "status": "completed",
        "campaign_id": campaign_id,
        "reply_count": 1,
        "replies": [
            {
                "lead_email": "ava@example.com",
                "reply_message": "Yes, interested. Can we book today?",
                "event_timestamp": "2026-06-07T12:00:00Z",
            }
        ],
    }


async def fake_get_logs(start_date="", end_date="", limit=100, sdr_name="dialdesk"):
    return {
        "status": "completed",
        "logs": [
            {
                "direction": "inbound",
                "from": "5551112222",
                "message": "Call me this week about funding.",
            }
        ],
    }


smartlead_api.poll_replies = fake_poll_replies
sendivo_api.get_logs = fake_get_logs

config = client.get("/api/integrations/config")
assert config.status_code == 200, config.text
assert "smartlead" in config.json(), config.json()

email_sync = client.post(
    "/api/integrations/syncs",
    json={
        "provider": "smartlead",
        "sync_type": "replies",
        "client_id": DEFAULT_CLIENT_ID,
        "provider_campaign_id": "smartlead-test-campaign",
        "priority": 1,
    },
)
assert email_sync.status_code == 200, email_sync.text
email_sync_id = email_sync.json()["id"]
email_sync_exec = client.post(f"/api/integrations/syncs/{email_sync_id}/execute")
assert email_sync_exec.status_code == 200, email_sync_exec.text
assert email_sync_exec.json()["status"] == "completed", email_sync_exec.json()
assert email_sync_exec.json()["records_imported"] == 1, email_sync_exec.json()

sms_sync = client.post(
    "/api/integrations/syncs",
    json={
        "provider": "sendivo",
        "sync_type": "logs",
        "client_id": DEFAULT_CLIENT_ID,
        "priority": 1,
    },
)
assert sms_sync.status_code == 200, sms_sync.text
sync_next = client.post("/api/integrations/sync-next?limit=5")
assert sync_next.status_code == 200, sync_next.text
assert sync_next.json()["processed"] >= 1, sync_next.json()
completed_syncs = client.get("/api/integrations/syncs?status=completed").json()
assert len(completed_syncs) >= 2, completed_syncs
dashboard_after_sync = client.get(f"/api/clients/{DEFAULT_CLIENT_ID}/dashboard").json()
assert dashboard_after_sync["metrics"]["replies"] >= 2, dashboard_after_sync
assert dashboard_after_sync["integration_syncs"], dashboard_after_sync
overview_after_sync = client.get("/api/ops/overview").json()
assert overview_after_sync["integration_sync"]["completed"] >= 2, overview_after_sync

sms = client.post(
    "/api/webhooks/sms",
    json={
        "client_id": DEFAULT_CLIENT_ID,
        "lead_id": lead_id,
        "direction": "inbound",
        "body": "Interested, can we schedule a call today?",
    },
)
assert sms.status_code == 200, sms.text
assert sms.json()["conversation"]["score"] >= 8
assert any(t["agent_id"] == "sales_director" for t in client.get("/api/tasks").json())

event = client.post(
    "/api/events",
    json={
        "type": "compliance_risk",
        "client_id": DEFAULT_CLIENT_ID,
        "source": "unit-test",
        "payload": {"issue": "copy needs review"},
        "severity": "high",
    },
)
assert event.status_code == 200, event.text
event_payload = event.json()
event_decisions = client.get("/api/ceo/decisions?decision_type=escalate_for_approval").json()
assert any(d["trigger_id"] == event_payload["id"] and d["approval_id"] for d in event_decisions), event_decisions
event_decision = next(d for d in event_decisions if d["trigger_id"] == event_payload["id"])
decision_activity = client.get("/api/activity?source=ceo_decision&limit=20").json()
assert any(a["entity_id"] == event_decision["id"] for a in decision_activity), decision_activity
decision_review = client.post("/api/ceo/decisions/evaluate", json={"source": "unit-test", "force": True})
assert decision_review.status_code == 200, decision_review.text
assert decision_review.json()["decisions_created"] >= 1, decision_review.json()
approvals = client.get("/api/approvals").json()
assert approvals, approvals
approval_id = approvals[0]["id"]
approved = client.post(
    f"/api/approvals/{approval_id}/approve",
    json={"decided_by": "Justin", "note": "Approved for test"},
)
assert approved.status_code == 200, approved.text
assert approved.json()["status"] == "approved"

dashboard = client.get(f"/api/clients/{new_client_id}/dashboard")
assert dashboard.status_code == 200, dashboard.text
assert dashboard.json()["client"]["name"] == "Test Funding Co"

stripe = client.post(
    "/api/webhooks/stripe",
    json={
        "id": "pi_test_dialdesk",
        "client_id": DEFAULT_CLIENT_ID,
        "opportunity_id": opportunity_id,
        "status": "paid",
        "amount": 750,
    },
)
assert stripe.status_code == 200, stripe.text
won_opps = client.get("/api/opportunities?status=closed_won").json()
assert any(o["id"] == opportunity_id for o in won_opps), won_opps

kill = client.post(
    "/api/kill-switch",
    json={"active": True, "scope": "global", "reason": "test stop", "set_by": "Justin"},
)
assert kill.status_code == 200, kill.text
blocked = client.post(
    "/api/leads/import",
    json={"client_id": DEFAULT_CLIENT_ID, "leads": [{"email": "blocked@example.com"}]},
)
assert blocked.status_code == 423, blocked.text

coordinator.APP_TOKEN = "test-token"
public_health = client.get("/health")
assert public_health.status_code == 200, public_health.text
unauthorized_status = client.get("/api/status")
assert unauthorized_status.status_code == 401, unauthorized_status.text
authorized_status = client.get(
    "/api/status",
    headers={"Authorization": "Bearer test-token", "X-Operator": "unit-test"},
)
assert authorized_status.status_code == 200, authorized_status.text
bad_log = client.get("/api/ops/request-log")
assert bad_log.status_code == 401, bad_log.text
request_log = client.get(
    "/api/ops/request-log?limit=50",
    headers={"X-API-Token": "test-token", "X-Operator": "unit-test"},
)
assert request_log.status_code == 200, request_log.text
request_rows = request_log.json()
assert any(r["path"] == "/api/status" and r["status_code"] == 401 for r in request_rows), request_rows
assert any(r["path"] == "/api/status" and r["status_code"] == 200 and r["actor"] == "unit-test" for r in request_rows), request_rows
coordinator.APP_TOKEN = ""

print("OK: autonomous runtime smoke tests passed")
PY

echo "=== All tests passed ==="
