"""DialDesk autonomous business coordinator.

This FastAPI app is the production surface for the DialDesk offer:
managed appointment setting and outreach for MCA/funding companies.
It stores durable business state, runs a CEO operating loop, and exposes
the APIs the dashboard, webhooks, and future agents use.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import inspect
import json
import logging
import os
import pathlib
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("dialdesk")


def utcnow() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return utcnow().isoformat()


def today_key() -> str:
    return utcnow().strftime("%Y-%m-%d")


def short_id() -> str:
    return uuid.uuid4().hex[:10]


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


DB_PATH = os.getenv("DIALDESK_DB", "/app/data/dialdesk.sqlite3")
PORT = int(os.getenv("PORT", "8080"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://dial-desk-api-production.up.railway.app")
APP_TOKEN = os.getenv("APP_TOKEN", "")
CEO_DAILY_HOUR_UTC = int(os.getenv("CEO_DAILY_HOUR_UTC", "8"))
CEO_EOD_HOUR_UTC = int(os.getenv("CEO_EOD_HOUR_UTC", "22"))
SCHEDULER_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60"))
HEALTH_CHECK_INTERVAL_SECONDS = int(os.getenv("DIALDESK_HEALTH_CHECK_INTERVAL_SECONDS", "300"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("DIALDESK_BACKUP_INTERVAL_SECONDS", "86400"))
SELF_AUDIT_INTERVAL_SECONDS = int(os.getenv("DIALDESK_SELF_AUDIT_INTERVAL_SECONDS", "3600"))
BROWSER_OPERATOR_POLL_SECONDS = float(os.getenv("DIALDESK_BROWSER_POLL_SECONDS", "15"))
DEFAULT_CLIENT_ID = os.getenv("DIALDESK_DEFAULT_CLIENT_ID", "dialdesk-internal")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "")
SEND_OUTBOX = os.getenv("DIALDESK_SEND_OUTBOX", "0") == "1"
SEND_OUTREACH = os.getenv("DIALDESK_SEND_OUTREACH", "0") == "1"
CREATE_PAYMENT_LINKS = os.getenv("DIALDESK_CREATE_PAYMENT_LINKS", "0") == "1"
SYNC_CRM = os.getenv("DIALDESK_SYNC_CRM", "0") == "1"
GHL_CONTACT_UPSERT_URL = os.getenv("GHL_CONTACT_UPSERT_URL", "")
DEFAULT_AGENT_MONTHLY_BUDGET = float(os.getenv("DIALDESK_DEFAULT_AGENT_MONTHLY_BUDGET", "500"))
BACKUP_DIR = os.getenv("DIALDESK_BACKUP_DIR", str(pathlib.Path(DB_PATH).parent / "backups"))

PRODUCT = {
    "name": "DialDesk",
    "company": "AI Integraterz",
    "icp": "MCA and business-funding companies",
    "truth": (
        "DialDesk sells managed outreach and appointment setting. "
        "It does not offer business loans."
    ),
    "offer": (
        "Unlimited SMS, unlimited email, AI SDR, human SDRs, lead programs, "
        "qualification, calendar booking, dashboard, and weekly reporting."
    ),
    "packages": {
        "ai_caller": {"price_monthly": 300, "label": "AI Caller"},
        "human_sdr": {"price_monthly": 750, "label": "Human SDR"},
    },
}


@dataclass
class Agent:
    id: str
    name: str
    role: str
    goal: str
    status: str = "idle"
    last_seen: str = field(default_factory=now_iso)
    tasks_completed_today: int = 0
    current_task: str | None = None
    skills: list[str] = field(default_factory=list)


AGENTS: dict[str, Agent] = {
    "ceo": Agent(
        id="ceo",
        name="CEO",
        role="Chief Executive Officer",
        goal=(
            "Run DialDesk every day: review KPIs, decide priorities, delegate work, "
            "manage approvals, and escalate only sales calls or unusual decisions."
        ),
        skills=["strategy", "daily_brief", "delegation", "approvals", "escalation"],
    ),
    "cmo": Agent(
        id="cmo",
        name="CMO",
        role="Chief Marketing Officer",
        goal=(
            "Own MCA/funding-company positioning, list strategy, SMS/email campaigns, "
            "copy, playbooks, and ROI."
        ),
        skills=["campaign_mgmt", "copywriting", "lead_programs", "analytics"],
    ),
    "cto": Agent(
        id="cto",
        name="CTO",
        role="Chief Technology Officer",
        goal=(
            "Own infrastructure, integrations, uptime, health checks, data durability, "
            "security, and production safety."
        ),
        skills=["deployment", "api_mgmt", "monitoring", "security", "troubleshooting"],
    ),
    "coo": Agent(
        id="coo",
        name="COO",
        role="Chief Operating Officer",
        goal=(
            "Own client onboarding, setup checklists, rep assignment, fulfillment, "
            "weekly reports, and guarantee swaps."
        ),
        skills=["onboarding", "fulfillment", "rep_ops", "reporting", "compliance"],
    ),
    "sales_director": Agent(
        id="sales_director",
        name="Sales Director",
        role="Sales Director",
        goal=(
            "Own qualification, AI/human SDR routing, objections, calendar booking, "
            "pipeline quality, and sales-call prep."
        ),
        skills=["sms_outreach", "email_outreach", "qualification", "booking", "closing"],
    ),
    "builder": Agent(
        id="builder",
        name="Builder",
        role="Builder",
        goal="Implement CEO/CTO-commissioned system changes without disrupting operations.",
        skills=["implementation", "scaffolding", "tooling", "restart"],
    ),
}


class Part(BaseModel):
    kind: str = "text"
    text: str = ""


class MessageRequest(BaseModel):
    parts: list[Part] = Field(default_factory=list)
    taskId: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MessageResponse(BaseModel):
    taskId: str
    state: str = "submitted"


class ChatRequest(BaseModel):
    message: str
    from_user: str = "CEO"
    client_id: str = DEFAULT_CLIENT_ID


class CampaignAction(BaseModel):
    action: str
    campaign_type: str = "sms"
    source: str = ""
    lead_count: int = 0
    budget: float = 0
    client_id: str = DEFAULT_CLIENT_ID
    name: str | None = None


class EventIn(BaseModel):
    type: str
    client_id: str = DEFAULT_CLIENT_ID
    source: str = "api"
    payload: dict[str, Any] = Field(default_factory=dict)
    severity: str = "info"


class CeoDecisionReviewIn(BaseModel):
    client_id: str | None = None
    source: str = "api"
    force: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompleteTaskIn(BaseModel):
    result: str = ""
    status: str = "completed"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecisionIn(BaseModel):
    decided_by: str = "Justin"
    note: str = ""


class LeadImportIn(BaseModel):
    client_id: str = DEFAULT_CLIENT_ID
    source: str = "manual"
    leads: list[dict[str, Any]] = Field(default_factory=list)
    csv_text: str | None = None


class KillSwitchIn(BaseModel):
    active: bool
    scope: str = "global"
    client_id: str | None = None
    reason: str = ""
    set_by: str = "Justin"


class RuntimeControlIn(BaseModel):
    component: str
    enabled: bool
    reason: str = ""
    set_by: str = "Justin"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClientCreateIn(BaseModel):
    company_name: str
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    package: str = "ai_caller"
    crm_url: str = ""
    calendar_url: str = ""
    monthly_volume: int = 0
    list_source: str = ""
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryCreateIn(BaseModel):
    title: str
    body: str
    kind: str = "note"
    actor: str = "ceo"
    client_id: str = DEFAULT_CLIENT_ID
    tags: list[str] = Field(default_factory=list)
    source: str = "api"
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryResyncIn(BaseModel):
    requested_by: str = "ceo"
    status: str = ""
    limit: int = 50
    include_synced: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SlackNotifyIn(BaseModel):
    text: str
    severity: str = "info"
    channel: str = "dialdesk-ops"
    client_id: str = DEFAULT_CLIENT_ID
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserJobCreateIn(BaseModel):
    objective: str
    url: str = ""
    client_id: str = DEFAULT_CLIENT_ID
    requested_by: str = "ceo"
    priority: int = 3
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserJobCompleteIn(BaseModel):
    status: str = "completed"
    result: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserJobClaimIn(BaseModel):
    operator_id: str = "super-browser"
    lease_seconds: int = 900
    client_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserJobHeartbeatIn(BaseModel):
    operator_id: str = "super-browser"
    lease_seconds: int = 900
    note: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserOperatorHeartbeatIn(BaseModel):
    operator_id: str = "super-browser"
    status: str = "online"
    mode: str = "auto"
    current_job_id: str = ""
    version: str = "1.0"
    note: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutreachJobCreateIn(BaseModel):
    client_id: str = DEFAULT_CLIENT_ID
    campaign_id: str | None = None
    lead_id: str
    channel: str = "sms"
    body: str
    provider_campaign_id: str = ""
    from_phone_id: str = ""
    requested_by: str = "sales_director"
    priority: int = 3
    metadata: dict[str, Any] = Field(default_factory=dict)


class IntegrationSyncCreateIn(BaseModel):
    provider: str
    sync_type: str
    client_id: str = DEFAULT_CLIENT_ID
    provider_campaign_id: str = ""
    start_date: str = ""
    end_date: str = ""
    limit: int = 100
    priority: int = 3
    requested_by: str = "cto"
    metadata: dict[str, Any] = Field(default_factory=dict)


class OpportunityCreateIn(BaseModel):
    client_id: str = DEFAULT_CLIENT_ID
    company_name: str
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    source: str = "api"
    package_interest: str = "human_sdr"
    status: str = "new"
    value: float = 0
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SalesBriefCreateIn(BaseModel):
    opportunity_id: str
    client_id: str = DEFAULT_CLIENT_ID
    requested_by: str = "sales_director"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaymentLinkCreateIn(BaseModel):
    client_id: str = DEFAULT_CLIENT_ID
    opportunity_id: str | None = None
    package: str = "human_sdr"
    amount: float | None = None
    currency: str = "usd"
    requested_by: str = "sales_director"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CrmSyncCreateIn(BaseModel):
    client_id: str = DEFAULT_CLIENT_ID
    entity_type: str
    entity_id: str
    action: str = "upsert"
    provider: str = "ghl"
    priority: int = 3
    requested_by: str = "sales_director"
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GoalCreateIn(BaseModel):
    title: str
    description: str = ""
    level: str = "project"
    client_id: str = DEFAULT_CLIENT_ID
    parent_id: str | None = None
    owner_agent_id: str = "ceo"
    priority: int = 3
    target: str = ""
    due_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BudgetSetIn(BaseModel):
    agent_id: str
    client_id: str = DEFAULT_CLIENT_ID
    monthly_budget: float
    spent: float | None = None
    auto_pause: bool = True
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatIn(BaseModel):
    agent_id: str
    client_id: str = DEFAULT_CLIENT_ID
    status: str = "alive"
    summary: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)


class SlackCommandIn(BaseModel):
    command: str
    text: str = ""
    user: str = "Justin"
    client_id: str = DEFAULT_CLIENT_ID
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentMessageCreateIn(BaseModel):
    from_agent_id: str = "ceo"
    to_agent_id: str = "ceo"
    subject: str
    body: str
    message_type: str = "update"
    client_id: str = DEFAULT_CLIENT_ID
    priority: int = 3
    thread_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentMessageReplyIn(BaseModel):
    from_agent_id: str = "ceo"
    to_agent_id: str | None = None
    subject: str | None = None
    body: str
    message_type: str = "update"
    priority: int = 3
    mark_parent_read: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentStandupCreateIn(BaseModel):
    client_id: str = DEFAULT_CLIENT_ID
    requested_by: str = "ceo"
    topic: str = "Daily autonomous business standup"
    include_agents: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IncidentCreateIn(BaseModel):
    title: str
    severity: str = "warning"
    source: str = "api"
    client_id: str = DEFAULT_CLIENT_ID
    owner_agent_id: str = "cto"
    details: dict[str, Any] = Field(default_factory=dict)


class IncidentResolveIn(BaseModel):
    resolved_by: str = "cto"
    resolution: str = ""


class BackupCreateIn(BaseModel):
    reason: str = "manual"
    requested_by: str = "cto"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReadinessCheckCreateIn(BaseModel):
    profile: str = "production"
    requested_by: str = "cto"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WatchdogRunIn(BaseModel):
    source: str = "api"
    force: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutonomyDrillRunIn(BaseModel):
    requested_by: str = "ceo"
    include_browser_job: bool = True
    include_slack: bool = True
    force_cycle: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class LaunchGateRunIn(BaseModel):
    profile: str = "vps"
    requested_by: str = "cto"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProofRunRecordIn(BaseModel):
    proof_type: str = "business_stack"
    status: str = "passed"
    summary: str = ""
    source: str = "script"
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0
    command: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)
    output: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SelfAuditRunIn(BaseModel):
    requested_by: str = "scheduler"
    force: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatorBriefingCreateIn(BaseModel):
    requested_by: str = "ceo"
    force: bool = False
    include_slack: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ControlRoomActionIn(BaseModel):
    action: str
    component: str | None = None
    profile: str = "production"
    reason: str = ""
    actor: str = "Justin"
    force: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatingCycleStartIn(BaseModel):
    requested_by: str = "ceo"
    force: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperatingCycleCloseIn(BaseModel):
    requested_by: str = "ceo"
    force: bool = False
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowLaunchIn(BaseModel):
    client_id: str
    playbook: str = "dialdesk_onboarding"
    name: str = "DialDesk Day 0-14 Launch"
    requested_by: str = "coo"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowStepCompleteIn(BaseModel):
    completed_by: str = "coo"
    result: str = ""
    status: str = "completed"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DialDeskState:
    def __init__(self, db_path: str):
        self.db_path = db_path
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_db()
        self.agents = {k: Agent(**asdict(v)) for k, v in AGENTS.items()}
        self._seed_default_client()
        self._seed_governance()
        self.log_event("ceo", "DialDesk autonomous operating system online.")

    def _init_db(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'prospect',
                package TEXT NOT NULL DEFAULT 'ai_caller',
                industry TEXT NOT NULL DEFAULT 'MCA/funding',
                owner_name TEXT,
                email TEXT,
                phone TEXT,
                crm_url TEXT,
                calendar_url TEXT,
                onboarding_stage TEXT NOT NULL DEFAULT 'not_started',
                setup_status TEXT NOT NULL DEFAULT 'not_started',
                health_score INTEGER NOT NULL DEFAULT 100,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                source TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                company TEXT,
                email TEXT,
                phone TEXT,
                monthly_revenue REAL DEFAULT 0,
                funding_amount REAL DEFAULT 0,
                existing_funder TEXT,
                timeline TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                score INTEGER NOT NULL DEFAULT 0,
                opted_out INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                type TEXT NOT NULL DEFAULT 'sms',
                source TEXT NOT NULL DEFAULT '',
                leads_total INTEGER NOT NULL DEFAULT 0,
                leads_sent INTEGER NOT NULL DEFAULT 0,
                replies INTEGER NOT NULL DEFAULT 0,
                appointments INTEGER NOT NULL DEFAULT 0,
                reply_rate REAL NOT NULL DEFAULT 0,
                budget REAL NOT NULL DEFAULT 0,
                cost_so_far REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                lead_id TEXT,
                channel TEXT NOT NULL,
                direction TEXT NOT NULL,
                body TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT 'unknown',
                score INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                lead_id TEXT,
                starts_at TEXT,
                status TEXT NOT NULL DEFAULT 'booked',
                owner TEXT NOT NULL DEFAULT 'Justin',
                source TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 3,
                due_at TEXT,
                result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                type TEXT NOT NULL,
                source TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                handled_at TEXT,
                payload TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                risk TEXT NOT NULL DEFAULT 'medium',
                requested_by TEXT NOT NULL DEFAULT 'ceo',
                decided_by TEXT,
                decision_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT,
                payload TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS ceo_decisions (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_id TEXT NOT NULL DEFAULT '',
                decision_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT 'monitor',
                assigned_agent_id TEXT NOT NULL DEFAULT '',
                task_id TEXT NOT NULL DEFAULT '',
                approval_id TEXT NOT NULL DEFAULT '',
                risk TEXT NOT NULL DEFAULT 'low',
                status TEXT NOT NULL DEFAULT 'recorded',
                decided_by TEXT NOT NULL DEFAULT 'ceo',
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS reps (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'human',
                status TEXT NOT NULL DEFAULT 'active',
                seats INTEGER NOT NULL DEFAULT 1,
                dials INTEGER NOT NULL DEFAULT 0,
                connects INTEGER NOT NULL DEFAULT 0,
                appointments INTEGER NOT NULL DEFAULT 0,
                talk_time_minutes REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'stripe',
                status TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                company_name TEXT NOT NULL,
                contact_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'api',
                package_interest TEXT NOT NULL DEFAULT 'human_sdr',
                status TEXT NOT NULL DEFAULT 'new',
                value REAL NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS sales_briefs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready',
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT 'sales_director',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS payment_links (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                opportunity_id TEXT,
                provider TEXT NOT NULL DEFAULT 'stripe',
                package TEXT NOT NULL DEFAULT 'human_sdr',
                amount REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'usd',
                status TEXT NOT NULL DEFAULT 'queued',
                url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS crm_sync_jobs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'ghl',
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'upsert',
                status TEXT NOT NULL DEFAULT 'queued',
                priority INTEGER NOT NULL DEFAULT 3,
                requested_by TEXT NOT NULL DEFAULT 'sales_director',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status_code INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                client_host TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT 'anonymous',
                auth_mode TEXT NOT NULL DEFAULT 'open',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS agent_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                agent TEXT NOT NULL,
                message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_messages (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                from_agent_id TEXT NOT NULL,
                to_agent_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'update',
                status TEXT NOT NULL DEFAULT 'unread',
                priority INTEGER NOT NULL DEFAULT 3,
                created_at TEXT NOT NULL,
                read_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS health_checks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                summary TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                metrics TEXT NOT NULL DEFAULT '{}',
                findings TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                title TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'warning',
                status TEXT NOT NULL DEFAULT 'open',
                source TEXT NOT NULL DEFAULT 'api',
                owner_agent_id TEXT NOT NULL DEFAULT 'cto',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                resolution TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS backups (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT 'manual',
                requested_by TEXT NOT NULL DEFAULT 'cto',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS readiness_checks (
                id TEXT PRIMARY KEY,
                profile TEXT NOT NULL DEFAULT 'production',
                status TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT 'cto',
                summary TEXT NOT NULL,
                required_failures INTEGER NOT NULL DEFAULT 0,
                warnings INTEGER NOT NULL DEFAULT 0,
                checks TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS watchdog_runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                summary TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'api',
                findings TEXT NOT NULL DEFAULT '[]',
                incidents_opened INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS autonomy_drills (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT 'ceo',
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                checks TEXT NOT NULL DEFAULT '[]',
                evidence TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS launch_gate_runs (
                id TEXT PRIMARY KEY,
                profile TEXT NOT NULL DEFAULT 'vps',
                status TEXT NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL,
                requested_by TEXT NOT NULL DEFAULT 'cto',
                checked_at TEXT NOT NULL,
                checks TEXT NOT NULL DEFAULT '[]',
                commands TEXT NOT NULL DEFAULT '[]',
                evidence TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS proof_runs (
                id TEXT PRIMARY KEY,
                proof_type TEXT NOT NULL DEFAULT 'business_stack',
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'script',
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                command TEXT NOT NULL DEFAULT '',
                checks TEXT NOT NULL DEFAULT '[]',
                output TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS operating_cycles (
                id TEXT PRIMARY KEY,
                day_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                last_tick_at TEXT NOT NULL,
                closed_at TEXT,
                morning_report_id TEXT NOT NULL DEFAULT '',
                close_report_id TEXT NOT NULL DEFAULT '',
                standup_thread_id TEXT NOT NULL DEFAULT '',
                requested_by TEXT NOT NULL DEFAULT 'ceo',
                summary TEXT NOT NULL DEFAULT '',
                daily_plan TEXT NOT NULL DEFAULT '[]',
                metrics_start TEXT NOT NULL DEFAULT '{}',
                metrics_current TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL,
                playbook TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                current_step INTEGER NOT NULL DEFAULT 0,
                total_steps INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS workflow_steps (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                step_order INTEGER NOT NULL,
                step_key TEXT NOT NULL,
                day_offset INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                owner_agent_id TEXT NOT NULL DEFAULT 'coo',
                status TEXT NOT NULL DEFAULT 'pending',
                due_at TEXT NOT NULL,
                task_id TEXT,
                result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS kill_switches (
                scope TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                set_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_controls (
                component TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                reason TEXT NOT NULL DEFAULT '',
                set_by TEXT NOT NULL DEFAULT 'system',
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS runtime_heartbeats (
                component TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'unknown',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_started_at TEXT NOT NULL DEFAULT '',
                last_finished_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                run_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_result TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS memory_entries (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'note',
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'ceo',
                source TEXT NOT NULL DEFAULT 'api',
                tags TEXT NOT NULL DEFAULT '[]',
                obsidian_path TEXT NOT NULL DEFAULT '',
                notion_status TEXT NOT NULL DEFAULT 'not_configured',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                severity TEXT NOT NULL DEFAULT 'info',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                payload TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS browser_jobs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                requested_by TEXT NOT NULL DEFAULT 'ceo',
                status TEXT NOT NULL DEFAULT 'queued',
                priority INTEGER NOT NULL DEFAULT 3,
                operator_id TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_heartbeat_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                artifacts TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS browser_operator_heartbeats (
                operator_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'online',
                mode TEXT NOT NULL DEFAULT 'auto',
                current_job_id TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '1.0',
                note TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                heartbeat_count INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS outreach_jobs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                campaign_id TEXT,
                lead_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_campaign_id TEXT NOT NULL DEFAULT '',
                from_phone_id TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                priority INTEGER NOT NULL DEFAULT 3,
                requested_by TEXT NOT NULL DEFAULT 'sales_director',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                result TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS integration_syncs (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                sync_type TEXT NOT NULL,
                provider_campaign_id TEXT NOT NULL DEFAULT '',
                start_date TEXT NOT NULL DEFAULT '',
                end_date TEXT NOT NULL DEFAULT '',
                limit_count INTEGER NOT NULL DEFAULT 100,
                status TEXT NOT NULL DEFAULT 'queued',
                priority INTEGER NOT NULL DEFAULT 3,
                requested_by TEXT NOT NULL DEFAULT 'cto',
                attempts INTEGER NOT NULL DEFAULT 0,
                records_seen INTEGER NOT NULL DEFAULT 0,
                records_imported INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                result TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS governance_goals (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                parent_id TEXT,
                level TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                owner_agent_id TEXT NOT NULL DEFAULT 'ceo',
                status TEXT NOT NULL DEFAULT 'active',
                priority INTEGER NOT NULL DEFAULT 3,
                target TEXT NOT NULL DEFAULT '',
                due_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS agent_budgets (
                client_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                monthly_budget REAL NOT NULL DEFAULT 0,
                spent REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'USD',
                auto_pause INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (client_id, agent_id)
            );

            CREATE TABLE IF NOT EXISTS agent_heartbeats (
                id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'alive',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metrics TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_ceo_decisions_trigger ON ceo_decisions(trigger_type, trigger_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_ceo_decisions_client ON ceo_decisions(client_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_leads_client ON leads(client_id, email, phone);
            CREATE INDEX IF NOT EXISTS idx_conversations_client ON conversations(client_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_inbox ON agent_messages(client_id, to_agent_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_messages_thread ON agent_messages(thread_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_health_checks ON health_checks(status, checked_at);
            CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, severity, created_at);
            CREATE INDEX IF NOT EXISTS idx_backups_status ON backups(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_readiness_checks ON readiness_checks(profile, status, checked_at);
            CREATE INDEX IF NOT EXISTS idx_watchdog_runs ON watchdog_runs(status, severity, checked_at);
            CREATE INDEX IF NOT EXISTS idx_autonomy_drills ON autonomy_drills(status, completed_at);
            CREATE INDEX IF NOT EXISTS idx_launch_gate_runs ON launch_gate_runs(profile, status, checked_at);
            CREATE INDEX IF NOT EXISTS idx_proof_runs ON proof_runs(proof_type, status, completed_at);
            CREATE INDEX IF NOT EXISTS idx_operating_cycles ON operating_cycles(day_key, status, last_tick_at);
            CREATE INDEX IF NOT EXISTS idx_request_log ON request_log(path, status_code, created_at);
            CREATE INDEX IF NOT EXISTS idx_workflow_runs ON workflow_runs(client_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_workflow_steps_due ON workflow_steps(status, due_at, step_order);
            CREATE INDEX IF NOT EXISTS idx_memory_client ON memory_entries(client_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, destination, created_at);
            CREATE INDEX IF NOT EXISTS idx_browser_jobs_status ON browser_jobs(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_browser_operator_heartbeats ON browser_operator_heartbeats(status, last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_outreach_jobs_status ON outreach_jobs(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_integration_syncs_status ON integration_syncs(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_opportunities_client ON opportunities(client_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_payment_links_status ON payment_links(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_crm_sync_jobs_status ON crm_sync_jobs(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_governance_goals ON governance_goals(client_id, level, status);
            CREATE INDEX IF NOT EXISTS idx_agent_heartbeats ON agent_heartbeats(client_id, agent_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_controls_enabled ON runtime_controls(enabled, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_heartbeats_status ON runtime_heartbeats(status, last_finished_at);
            """
        )
        self._ensure_column("browser_jobs", "operator_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("browser_jobs", "lease_expires_at", "TEXT")
        self._ensure_column("browser_jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("browser_jobs", "last_heartbeat_at", "TEXT")
        self._ensure_column("browser_jobs", "last_error", "TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        existing = {row["name"] for row in self.get_all(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _seed_default_client(self) -> None:
        row = self.get_one("SELECT id FROM clients WHERE id=?", (DEFAULT_CLIENT_ID,))
        if row:
            return
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO clients (
                id, name, status, package, industry, owner_name, email,
                onboarding_stage, setup_status, created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                DEFAULT_CLIENT_ID,
                "DialDesk Internal",
                "active",
                "human_sdr",
                "MCA/funding",
                "Justin",
                "justin@usingaitoscale.com",
                "active",
                "ready",
                now,
                now,
                json_dumps({"source": "seed"}),
            ),
        )
        self.db.commit()
        self.audit("system", "seed_default_client", "client", DEFAULT_CLIENT_ID)

    def _seed_governance(self) -> None:
        now = now_iso()
        defaults = [
            (
                "mission-dialdesk",
                None,
                "mission",
                "Run DialDesk as a 24/7 appointment-setting business",
                "DialDesk autonomously books qualified calls for MCA/funding companies while Justin handles approvals and sales calls.",
                "ceo",
                1,
                "Autonomous operating loop with transparent controls",
            ),
            (
                "project-mca-appointment-setting",
                "mission-dialdesk",
                "project",
                "Prove MCA/funding appointment setting end to end",
                "Run onboarding, outreach, reply handling, booking, reporting, and approvals for the first DialDesk offer.",
                "coo",
                1,
                "Qualified booked calls and weekly client reporting",
            ),
        ]
        for goal_id, parent_id, level, title, description, owner, priority, target in defaults:
            self.db.execute(
                """
                INSERT OR IGNORE INTO governance_goals (
                    id, client_id, parent_id, level, title, description, owner_agent_id,
                    priority, target, created_at, updated_at, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    goal_id,
                    DEFAULT_CLIENT_ID,
                    parent_id,
                    level,
                    title,
                    description,
                    owner,
                    priority,
                    target,
                    now,
                    now,
                    json_dumps({"seed": True}),
                ),
            )

        for agent_id, agent in self.agents.items():
            self.db.execute(
                """
                INSERT OR IGNORE INTO governance_goals (
                    id, client_id, parent_id, level, title, description, owner_agent_id,
                    priority, target, created_at, updated_at, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"agent-goal-{agent_id}",
                    DEFAULT_CLIENT_ID,
                    "project-mca-appointment-setting",
                    "agent",
                    f"{agent.name} operating goal",
                    agent.goal,
                    agent_id,
                    2,
                    "Complete delegated work without unnecessary Justin involvement",
                    now,
                    now,
                    json_dumps({"seed": True, "skills": agent.skills}),
                ),
            )
            self.db.execute(
                """
                INSERT OR IGNORE INTO agent_budgets (
                    client_id, agent_id, monthly_budget, spent, auto_pause,
                    status, created_at, updated_at, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    DEFAULT_CLIENT_ID,
                    agent_id,
                    DEFAULT_AGENT_MONTHLY_BUDGET,
                    0,
                    1,
                    "active",
                    now,
                    now,
                    json_dumps({"seed": True}),
                ),
            )
        runtime_components = {
            "scheduler": "Master autonomous loop. When disabled, only the lightweight loop heartbeat remains.",
            "ceo_decision_review": "CEO scans approvals, incidents, and failed jobs for escalations.",
            "worker": "Autonomous task worker for safe internal tasks.",
            "outbox": "Slack and Notion delivery processing.",
            "outreach": "SMS/email outreach queue execution.",
            "integration_sync": "Smartlead/Sendivo provider sync queue execution.",
            "crm_sync": "CRM write queue execution.",
            "browser_recovery": "Recovery of expired browser automation leases.",
            "workflow": "Day 0-14 onboarding and due workflow-step execution.",
            "scheduled_ops": "Scheduled health checks and backups.",
            "self_audit": "Scheduled self-audit that records proof of runtime, memory, Slack, browser, and queue health.",
            "watchdog": "Autonomous incident monitor for stale components, failed queues, and missing operators.",
        }
        for component, description in runtime_components.items():
            self.db.execute(
                """
                INSERT OR IGNORE INTO runtime_controls (
                    component, enabled, reason, set_by, updated_at, metadata
                ) VALUES (?,?,?,?,?,?)
                """,
                (component, 1, "Default enabled", "system", now, json_dumps({"description": description, "seed": True})),
            )
            self.db.execute(
                """
                INSERT OR IGNORE INTO runtime_heartbeats (
                    component, status, enabled, last_started_at, last_finished_at,
                    last_error, run_count, consecutive_failures, last_result, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    component,
                    "waiting",
                    1,
                    "",
                    "",
                    "",
                    0,
                    0,
                    json_dumps({}),
                    json_dumps({"description": description, "seed": True}),
                ),
            )
        self.db.commit()

    def create_client(self, req: ClientCreateIn) -> dict[str, Any]:
        client_id = short_id()
        now = now_iso()
        metadata = {
            **req.metadata,
            "monthly_volume": req.monthly_volume,
            "list_source": req.list_source,
            "notes": req.notes,
            "source": "api_signup",
        }
        self.db.execute(
            """
            INSERT INTO clients (
                id, name, status, package, industry, owner_name, email, phone,
                crm_url, calendar_url, onboarding_stage, setup_status,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                client_id,
                req.company_name,
                "onboarding",
                req.package,
                "MCA/funding",
                req.contact_name,
                req.email,
                req.phone,
                req.crm_url,
                req.calendar_url,
                "intake_received",
                "in_progress",
                now,
                now,
                json_dumps(metadata),
            ),
        )
        self.db.commit()
        self.audit("sales_director", "create_client", "client", client_id, model_to_dict(req))
        self.create_event("new_signup", client_id=client_id, source="client_api", payload=model_to_dict(req))
        for title, agent_id, priority in [
            ("Confirm access, intake, CRM, calendar, dialer, and compliance rules", "coo", 1),
            ("Prepare MCA/funding appointment-setting launch playbook", "cmo", 2),
            ("Create sales-call and handoff notes for new client", "sales_director", 1),
            ("Verify integration requirements for new client", "cto", 2),
        ]:
            self.create_task(title, agent_id, client_id=client_id, priority=priority, metadata={"source": "client_onboarding"})
        self.launch_workflow(
            WorkflowLaunchIn(
                client_id=client_id,
                playbook="dialdesk_onboarding",
                name="DialDesk Day 0-14 Launch",
                requested_by="coo",
                metadata={"source": "client_signup"},
            )
        )
        client = self.get_one("SELECT * FROM clients WHERE id=?", (client_id,))
        return dict(client) if client else {"id": client_id}

    def launch_workflow(self, req: WorkflowLaunchIn) -> dict[str, Any]:
        client = self.get_one("SELECT * FROM clients WHERE id=?", (req.client_id,))
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        if req.playbook != "dialdesk_onboarding":
            raise HTTPException(status_code=400, detail="Unsupported playbook")
        existing = self.get_one(
            "SELECT * FROM workflow_runs WHERE client_id=? AND playbook=? AND status='active'",
            (req.client_id, req.playbook),
        )
        if existing:
            return dict(existing)
        run_id = short_id()
        now = now_iso()
        steps = onboarding_playbook_steps()
        self.db.execute(
            """
            INSERT INTO workflow_runs (
                id, client_id, name, playbook, status, total_steps,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                req.client_id,
                req.name,
                req.playbook,
                "active",
                len(steps),
                now,
                now,
                json_dumps({"requested_by": req.requested_by, **req.metadata}),
            ),
        )
        for idx, step in enumerate(steps, start=1):
            step_id = short_id()
            self.db.execute(
                """
                INSERT INTO workflow_steps (
                    id, run_id, client_id, step_order, step_key, day_offset,
                    title, owner_agent_id, due_at, created_at, updated_at, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    step_id,
                    run_id,
                    req.client_id,
                    idx,
                    step["key"],
                    step["day_offset"],
                    step["title"],
                    step["owner_agent_id"],
                    workflow_due_at(step["day_offset"]),
                    now,
                    now,
                    json_dumps(step),
                ),
            )
        self.db.commit()
        self.audit(req.requested_by, "launch_workflow", "workflow_run", run_id, model_to_dict(req))
        self.remember(
            title=f"Workflow launched: {req.name}",
            body=f"{len(steps)} steps launched for client {client['name']}.",
            kind="workflow",
            actor=req.requested_by,
            client_id=req.client_id,
            tags=["workflow", "onboarding", req.playbook],
            source="workflow",
            metadata={"workflow_run_id": run_id, "steps": len(steps)},
        )
        self.execute_due_workflow_steps(req.client_id)
        return dict(self.get_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,)))

    def execute_due_workflow_steps(self, client_id: str | None = None, limit: int = 25) -> int:
        if not self.runtime_component_enabled("workflow"):
            return 0
        clauses = ["status='pending'", "due_at <= ?"]
        params: list[Any] = [now_iso()]
        if client_id:
            clauses.append("client_id=?")
            params.append(client_id)
        rows = self.get_all(
            f"""
            SELECT * FROM workflow_steps
            WHERE {' AND '.join(clauses)}
            ORDER BY due_at ASC, step_order ASC
            LIMIT ?
            """,
            tuple(params + [max(1, min(limit, 100))]),
        )
        queued = 0
        for row in rows:
            metadata = json_loads(row["metadata"], {})
            task = self.create_task(
                title=f"Workflow: {row['title']}",
                agent_id=row["owner_agent_id"],
                client_id=row["client_id"],
                prompt=metadata.get("prompt", ""),
                priority=metadata.get("priority", 2),
                due_at=row["due_at"],
                metadata={
                    "workflow_run_id": row["run_id"],
                    "workflow_step_id": row["id"],
                    "step_key": row["step_key"],
                    "source": "workflow",
                },
            )
            self.db.execute(
                """
                UPDATE workflow_steps
                SET status='queued', task_id=?, updated_at=?
                WHERE id=?
                """,
                (task["id"], now_iso(), row["id"]),
            )
            self.db.execute(
                """
                UPDATE workflow_runs
                SET current_step=MAX(current_step, ?), updated_at=?
                WHERE id=?
                """,
                (row["step_order"], now_iso(), row["run_id"]),
            )
            self.db.commit()
            queued += 1
        return queued

    def complete_workflow_step(self, step_id: str, req: WorkflowStepCompleteIn) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM workflow_steps WHERE id=?", (step_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Workflow step not found")
        if req.status not in {"completed", "skipped"}:
            raise HTTPException(status_code=400, detail="status must be completed or skipped")
        metadata = json_loads(row["metadata"], {})
        metadata.update(req.metadata)
        self.db.execute(
            """
            UPDATE workflow_steps
            SET status=?, result=?, completed_at=?, updated_at=?, metadata=?
            WHERE id=?
            """,
            (req.status, req.result, now_iso(), now_iso(), json_dumps(metadata), step_id),
        )
        if row["task_id"]:
            task = self.get_one("SELECT status FROM tasks WHERE id=?", (row["task_id"],))
            if task and task["status"] not in {"completed", "cancelled"}:
                self.complete_task(row["task_id"], req.result or f"Workflow step {req.status}.", req.status, {"workflow_step_id": step_id})
        remaining = self.count("workflow_steps", "run_id=? AND status NOT IN ('completed','skipped')", (row["run_id"],))
        if remaining == 0:
            self.db.execute(
                "UPDATE workflow_runs SET status='completed', completed_at=?, updated_at=? WHERE id=?",
                (now_iso(), now_iso(), row["run_id"]),
            )
        else:
            self.db.execute("UPDATE workflow_runs SET updated_at=? WHERE id=?", (now_iso(), row["run_id"]))
        self.db.commit()
        self.audit(req.completed_by, "complete_workflow_step", "workflow_step", step_id, model_to_dict(req))
        return dict(self.get_one("SELECT * FROM workflow_steps WHERE id=?", (step_id,)))

    def create_opportunity(self, req: OpportunityCreateIn) -> dict[str, Any]:
        if req.package_interest not in PRODUCT["packages"]:
            raise HTTPException(status_code=400, detail="Unknown package_interest")
        if not (clean(req.email) or clean(req.phone)):
            raise HTTPException(status_code=400, detail="Opportunity needs an email or phone")
        opp_id = short_id()
        now = now_iso()
        value = float(req.value or PRODUCT["packages"][req.package_interest]["price_monthly"])
        score = score_opportunity(model_to_dict(req))
        self.db.execute(
            """
            INSERT INTO opportunities (
                id, client_id, company_name, contact_name, email, phone, source,
                package_interest, status, value, score, notes, created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                opp_id,
                req.client_id,
                clean(req.company_name),
                clean(req.contact_name),
                clean(req.email),
                clean(req.phone),
                clean(req.source) or "api",
                req.package_interest,
                req.status,
                value,
                score,
                req.notes,
                now,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit("sales_director", "create_opportunity", "opportunity", opp_id, model_to_dict(req))
        self.remember(
            title=f"New DialDesk opportunity: {req.company_name}",
            body=(
                f"{req.contact_name or 'Unknown contact'} at {req.company_name} is interested in "
                f"{PRODUCT['packages'][req.package_interest]['label']}."
            ),
            kind="opportunity",
            actor="sales_director",
            client_id=req.client_id,
            tags=["sales", "opportunity", req.package_interest],
            source=req.source,
            metadata={"opportunity_id": opp_id, "score": score},
        )
        self.create_event(
            "new_opportunity",
            client_id=req.client_id,
            source=req.source,
            severity="info",
            payload={"opportunity_id": opp_id, "score": score, "package_interest": req.package_interest},
        )
        self.create_task(
            "Prepare sales-call brief for new DialDesk opportunity",
            "sales_director",
            client_id=req.client_id,
            priority=1 if score >= 70 else 2,
            metadata={"opportunity_id": opp_id, "source": "opportunity"},
        )
        self.queue_crm_sync(
            CrmSyncCreateIn(
                client_id=req.client_id,
                entity_type="opportunity",
                entity_id=opp_id,
                action="upsert",
                priority=2,
                payload={"source": "create_opportunity"},
            )
        )
        return dict(self.get_one("SELECT * FROM opportunities WHERE id=?", (opp_id,)))

    def create_sales_brief(self, req: SalesBriefCreateIn) -> dict[str, Any]:
        opp = self.get_one("SELECT * FROM opportunities WHERE id=? AND client_id=?", (req.opportunity_id, req.client_id))
        if not opp:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        client = self.get_one("SELECT * FROM clients WHERE id=?", (req.client_id,))
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        brief_id = short_id()
        title = f"Sales Call Brief - {opp['company_name']}"
        body = build_sales_brief_body(dict(opp), dict(client))
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO sales_briefs (
                id, client_id, opportunity_id, title, body, requested_by,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                brief_id,
                req.client_id,
                req.opportunity_id,
                title,
                body,
                req.requested_by,
                now,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.execute(
            "UPDATE opportunities SET status=?, updated_at=? WHERE id=?",
            ("brief_ready", now, req.opportunity_id),
        )
        self.db.commit()
        self.audit(req.requested_by, "create_sales_brief", "sales_brief", brief_id, model_to_dict(req))
        self.remember(
            title=title,
            body=body,
            kind="sales_brief",
            actor=req.requested_by,
            client_id=req.client_id,
            tags=["sales", "brief", "opportunity"],
            source="sales_brief",
            metadata={"brief_id": brief_id, "opportunity_id": req.opportunity_id},
        )
        self.queue_slack(
            f"Sales brief ready: {opp['company_name']}",
            severity="action",
            client_id=req.client_id,
            metadata={"brief_id": brief_id, "opportunity_id": req.opportunity_id},
        )
        return dict(self.get_one("SELECT * FROM sales_briefs WHERE id=?", (brief_id,)))

    def create_payment_link(self, req: PaymentLinkCreateIn) -> dict[str, Any]:
        if req.package not in PRODUCT["packages"]:
            raise HTTPException(status_code=400, detail="Unknown package")
        if req.opportunity_id:
            opp = self.get_one("SELECT * FROM opportunities WHERE id=? AND client_id=?", (req.opportunity_id, req.client_id))
            if not opp:
                raise HTTPException(status_code=404, detail="Opportunity not found")
        amount = float(req.amount if req.amount is not None else PRODUCT["packages"][req.package]["price_monthly"])
        link_id = short_id()
        now = now_iso()
        configured_url = stripe_payment_link_for_package(req.package)
        if CREATE_PAYMENT_LINKS and configured_url:
            status = "ready"
            url = configured_url
            result = {"status": "ready", "static_link": True, "provider": "stripe"}
        elif CREATE_PAYMENT_LINKS:
            status = "needs_configuration"
            url = ""
            result = {
                "status": "needs_configuration",
                "error": f"STRIPE_PAYMENT_LINK_{req.package.upper()} is not configured",
            }
            self.create_task(
                f"Configure Stripe payment link for {PRODUCT['packages'][req.package]['label']}",
                "cto",
                client_id=req.client_id,
                priority=1,
                metadata={"payment_link_id": link_id, "package": req.package},
            )
        else:
            status = "dry_run"
            url = f"{PUBLIC_URL.rstrip('/')}/dashboard"
            result = {
                "status": "dry_run",
                "dry_run": True,
                "reason": "DIALDESK_CREATE_PAYMENT_LINKS=0",
                "provider": "stripe",
            }
        self.db.execute(
            """
            INSERT INTO payment_links (
                id, client_id, opportunity_id, package, amount, currency, status,
                url, created_at, updated_at, result, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                link_id,
                req.client_id,
                req.opportunity_id,
                req.package,
                amount,
                req.currency.lower(),
                status,
                url,
                now,
                now,
                json_dumps(result),
                json_dumps(req.metadata),
            ),
        )
        if req.opportunity_id:
            self.db.execute(
                "UPDATE opportunities SET status=?, updated_at=? WHERE id=?",
                ("payment_link_ready" if status in {"ready", "dry_run"} else status, now, req.opportunity_id),
            )
        self.db.commit()
        self.audit(req.requested_by, "create_payment_link", "payment_link", link_id, model_to_dict(req))
        self.remember(
            title=f"Payment link {status}: {PRODUCT['packages'][req.package]['label']}",
            body=f"Amount: {amount:.2f} {req.currency.upper()}. URL: {url or 'not configured yet'}.",
            kind="payment_link",
            actor=req.requested_by,
            client_id=req.client_id,
            tags=["sales", "payment", status],
            source="stripe",
            metadata={"payment_link_id": link_id, "opportunity_id": req.opportunity_id, "result": result},
        )
        return dict(self.get_one("SELECT * FROM payment_links WHERE id=?", (link_id,)))

    def queue_crm_sync(self, req: CrmSyncCreateIn) -> dict[str, Any]:
        provider = req.provider.lower().strip()
        if provider not in {"ghl", "crm"}:
            raise HTTPException(status_code=400, detail="provider must be ghl or crm")
        if req.action.lower().strip() not in {"upsert", "create", "update"}:
            raise HTTPException(status_code=400, detail="action must be upsert, create, or update")
        job_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO crm_sync_jobs (
                id, client_id, provider, entity_type, entity_id, action, priority,
                requested_by, created_at, updated_at, payload, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                req.client_id,
                provider,
                req.entity_type.lower().strip(),
                req.entity_id,
                req.action.lower().strip(),
                req.priority,
                req.requested_by,
                now,
                now,
                json_dumps(req.payload),
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "queue_crm_sync", "crm_sync_job", job_id, model_to_dict(req))
        return dict(self.get_one("SELECT * FROM crm_sync_jobs WHERE id=?", (job_id,)))

    async def execute_crm_sync_job(self, job_id: str) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM crm_sync_jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPException(status_code=404, detail="CRM sync job not found")
        if row["status"] not in {"queued", "failed"}:
            return dict(row)
        payload = self.crm_payload_for_job(row)
        if not SYNC_CRM:
            status = "dry_run"
            result = {"status": "dry_run", "dry_run": True, "payload": payload}
            error = ""
        elif row["provider"] == "ghl" and GHL_CONTACT_UPSERT_URL and os.getenv("GHL_API_KEY"):
            try:
                headers = {"Authorization": f"Bearer {os.getenv('GHL_API_KEY')}", "Content-Type": "application/json"}
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(GHL_CONTACT_UPSERT_URL, headers=headers, json=payload)
                    response.raise_for_status()
                status = "completed"
                result = {"status": "completed", "provider": "ghl", "response": safe_json(response)}
                error = ""
            except httpx.HTTPError as exc:
                status = "failed"
                result = {"status": "failed", "error": str(exc), "payload": payload}
                error = str(exc)
        else:
            status = "needs_configuration"
            result = {
                "status": "needs_configuration",
                "error": "Set GHL_CONTACT_UPSERT_URL and GHL_API_KEY or leave DIALDESK_SYNC_CRM=0 for dry-run.",
                "payload": payload,
            }
            error = result["error"]
            self.create_task(
                "Configure CRM write endpoint for DialDesk sales sync",
                "cto",
                client_id=row["client_id"],
                priority=1,
                metadata={"crm_sync_job_id": job_id},
            )
        self.db.execute(
            """
            UPDATE crm_sync_jobs
            SET status=?, attempts=attempts+1, last_error=?, updated_at=?,
                completed_at=?, result=?
            WHERE id=?
            """,
            (
                status,
                error,
                now_iso(),
                now_iso() if status in {"completed", "dry_run"} else None,
                json_dumps(result),
                job_id,
            ),
        )
        self.db.commit()
        self.audit("sales_director", "execute_crm_sync", "crm_sync_job", job_id, {"status": status, "error": error})
        self.remember(
            title=f"CRM sync {status}: {row['entity_type']} {row['entity_id']}",
            body=error or "CRM sync payload prepared and processed.",
            kind="crm_sync",
            actor="sales_director",
            client_id=row["client_id"],
            tags=["crm", row["provider"], status],
            source=row["provider"],
            metadata={"crm_sync_job_id": job_id, "result": result},
        )
        return dict(self.get_one("SELECT * FROM crm_sync_jobs WHERE id=?", (job_id,)))

    async def execute_crm_sync_queue_once(self, limit: int = 5) -> int:
        if not self.runtime_component_enabled("crm_sync"):
            return 0
        rows = self.get_all(
            """
            SELECT id FROM crm_sync_jobs
            WHERE status IN ('queued','failed') AND attempts < 3
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        processed = 0
        for row in rows:
            await self.execute_crm_sync_job(row["id"])
            processed += 1
        return processed

    def crm_payload_for_job(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json_loads(row["payload"], {})
        if row["entity_type"] == "opportunity":
            opp = self.get_one("SELECT * FROM opportunities WHERE id=? AND client_id=?", (row["entity_id"], row["client_id"]))
            if opp:
                payload.update(
                    {
                        "company_name": opp["company_name"],
                        "contact_name": opp["contact_name"],
                        "email": opp["email"],
                        "phone": opp["phone"],
                        "source": opp["source"],
                        "package_interest": opp["package_interest"],
                        "status": opp["status"],
                        "value": opp["value"],
                        "score": opp["score"],
                        "notes": opp["notes"],
                    }
                )
        payload.setdefault("client_id", row["client_id"])
        payload.setdefault("entity_type", row["entity_type"])
        payload.setdefault("entity_id", row["entity_id"])
        payload.setdefault("action", row["action"])
        return payload

    def get_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, params).fetchone()

    def get_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    def audit(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO audit_log (created_at, actor, action, entity_type, entity_id, details)
            VALUES (?,?,?,?,?,?)
            """,
            (now_iso(), actor, action, entity_type, entity_id, json_dumps(details or {})),
        )
        self.db.commit()

    def log_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: int,
        client_host: str = "",
        actor: str = "anonymous",
        auth_mode: str = "open",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO request_log (
                created_at, method, path, status_code, duration_ms,
                client_host, actor, auth_mode, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                now_iso(),
                method,
                path,
                status_code,
                duration_ms,
                client_host,
                actor,
                auth_mode,
                json_dumps(metadata or {}),
            ),
        )
        self.db.commit()

    def remember(
        self,
        title: str,
        body: str,
        kind: str = "note",
        actor: str = "ceo",
        client_id: str = DEFAULT_CLIENT_ID,
        tags: list[str] | None = None,
        source: str = "runtime",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        memory_id = short_id()
        now = now_iso()
        tag_list = tags or []
        obsidian_path = write_obsidian_note(memory_id, title, body, kind, actor, client_id, tag_list, metadata or {})
        notion_status = "queued" if NOTION_DATABASE_ID else "not_configured"
        self.db.execute(
            """
            INSERT INTO memory_entries (
                id, client_id, kind, title, body, actor, source, tags, obsidian_path,
                notion_status, created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                memory_id,
                client_id,
                kind,
                title,
                body,
                actor,
                source,
                json_dumps(tag_list),
                obsidian_path,
                notion_status,
                now,
                now,
                json_dumps(metadata or {}),
            ),
        )
        self.db.commit()
        self.audit(actor, "remember", "memory", memory_id, {"kind": kind, "tags": tag_list})
        if NOTION_DATABASE_ID:
            self.queue_outbox(
                destination="notion",
                client_id=client_id,
                subject=title,
                body=body,
                severity="info",
                payload={
                    "memory_id": memory_id,
                    "database_id": NOTION_DATABASE_ID,
                    "kind": kind,
                    "tags": tag_list,
                    "metadata": metadata or {},
                },
            )
        return dict(self.get_one("SELECT * FROM memory_entries WHERE id=?", (memory_id,)))

    def queue_memory_notion_sync(
        self,
        memory_id: str,
        requested_by: str = "ceo",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not valid_actor(requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        memory = self.get_one("SELECT * FROM memory_entries WHERE id=?", (memory_id,))
        if not memory:
            raise HTTPException(status_code=404, detail="Memory entry not found")
        if not NOTION_DATABASE_ID:
            raise HTTPException(status_code=409, detail="NOTION_DATABASE_ID is not configured")
        tags = json_loads(memory["tags"], [])
        memory_metadata = json_loads(memory["metadata"], {})
        outbox = self.queue_outbox(
            destination="notion",
            client_id=memory["client_id"],
            subject=memory["title"],
            body=memory["body"],
            severity="info",
            payload={
                "memory_id": memory_id,
                "database_id": NOTION_DATABASE_ID,
                "kind": memory["kind"],
                "tags": tags,
                "metadata": {**memory_metadata, **(metadata or {})},
                "resync_requested_by": requested_by,
            },
        )
        memory_metadata.update(
            {
                "notion_outbox_id": outbox["id"],
                "notion_queued_at": now_iso(),
                "notion_requested_by": requested_by,
            }
        )
        self.db.execute(
            """
            UPDATE memory_entries
            SET notion_status='queued', updated_at=?, metadata=?
            WHERE id=?
            """,
            (now_iso(), json_dumps(memory_metadata), memory_id),
        )
        self.db.commit()
        self.audit(requested_by, "queue_memory_notion_sync", "memory", memory_id, {"outbox_id": outbox["id"]})
        return dict(self.get_one("SELECT * FROM memory_entries WHERE id=?", (memory_id,)))

    def queue_memory_notion_resync(self, req: MemoryResyncIn) -> dict[str, Any]:
        if not valid_actor(req.requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        if not NOTION_DATABASE_ID:
            raise HTTPException(status_code=409, detail="NOTION_DATABASE_ID is not configured")
        clauses: list[str] = []
        params: list[Any] = []
        if req.status:
            clauses.append("notion_status=?")
            params.append(req.status)
        elif not req.include_synced:
            clauses.append("notion_status!='synced'")
        where = " AND ".join(clauses) or "1=1"
        capped = max(1, min(req.limit, 250))
        rows = self.get_all(
            f"""
            SELECT id FROM memory_entries
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tuple(params + [capped]),
        )
        queued: list[str] = []
        for row in rows:
            updated = self.queue_memory_notion_sync(row["id"], req.requested_by, req.metadata)
            queued.append(updated["id"])
        return {"queued": len(queued), "memory_ids": queued, "send_enabled": SEND_OUTBOX}

    def update_memory_notion_status(
        self,
        memory_id: str,
        status: str,
        outbox_id: str,
        error: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        memory = self.get_one("SELECT * FROM memory_entries WHERE id=?", (memory_id,))
        if not memory:
            return
        metadata = json_loads(memory["metadata"], {})
        metadata.update(
            {
                "notion_status": status,
                "notion_outbox_id": outbox_id,
                "notion_updated_at": now_iso(),
            }
        )
        if error:
            metadata["notion_error"] = error
        if result:
            metadata["notion_result"] = result
            page_id = result.get("page_id")
            if page_id:
                metadata["notion_page_id"] = page_id
        self.db.execute(
            """
            UPDATE memory_entries
            SET notion_status=?, updated_at=?, metadata=?
            WHERE id=?
            """,
            (status, now_iso(), json_dumps(metadata), memory_id),
        )
        self.db.commit()

    def memory_health(self) -> dict[str, Any]:
        obsidian_missing = self.count(
            "memory_entries",
            "obsidian_path='' OR obsidian_path IS NULL",
        )
        return {
            "sqlite": True,
            "total_entries": self.count("memory_entries"),
            "obsidian": {
                "configured": bool(OBSIDIAN_VAULT_PATH),
                "vault_path": OBSIDIAN_VAULT_PATH,
                "written": self.count("memory_entries", "obsidian_path!=''"),
                "missing": obsidian_missing,
            },
            "notion": {
                "configured": bool(NOTION_API_KEY and NOTION_DATABASE_ID),
                "database_id_configured": bool(NOTION_DATABASE_ID),
                "api_key_configured": bool(NOTION_API_KEY),
                "synced": self.count("memory_entries", "notion_status='synced'"),
                "queued": self.count("memory_entries", "notion_status='queued'"),
                "failed": self.count("memory_entries", "notion_status='failed'"),
                "not_configured": self.count("memory_entries", "notion_status='not_configured'"),
            },
            "outbox": {
                "send_enabled": SEND_OUTBOX,
                "notion_queued": self.count("outbox", "destination='notion' AND status='queued'"),
                "notion_failed": self.count("outbox", "destination='notion' AND status='failed'"),
                "notion_sent": self.count("outbox", "destination='notion' AND status='sent'"),
                "slack_queued": self.count("outbox", "destination='slack' AND status='queued'"),
                "slack_failed": self.count("outbox", "destination='slack' AND status='failed'"),
            },
            "recent_memory": self.rows("memory_entries", "1=1 ORDER BY created_at DESC LIMIT 10"),
        }

    def queue_outbox(
        self,
        destination: str,
        client_id: str,
        subject: str,
        body: str,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        outbox_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO outbox (
                id, destination, client_id, subject, body, severity, created_at, updated_at, payload
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                outbox_id,
                destination,
                client_id,
                subject,
                body,
                severity,
                now,
                now,
                json_dumps(payload or {}),
            ),
        )
        self.db.commit()
        self.audit("system", "queue_outbox", "outbox", outbox_id, {"destination": destination})
        return dict(self.get_one("SELECT * FROM outbox WHERE id=?", (outbox_id,)))

    def queue_slack(
        self,
        text: str,
        severity: str = "info",
        channel: str = "dialdesk-ops",
        client_id: str = DEFAULT_CLIENT_ID,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.queue_outbox(
            destination="slack",
            client_id=client_id,
            subject=f"DialDesk {severity.upper()}",
            body=text,
            severity=severity,
            payload={"channel": channel, "metadata": metadata or {}},
        )

    def create_goal(self, req: GoalCreateIn) -> dict[str, Any]:
        level = req.level.lower().strip()
        if level not in {"mission", "project", "agent", "task"}:
            raise HTTPException(status_code=400, detail="level must be mission, project, agent, or task")
        if req.owner_agent_id not in self.agents:
            raise HTTPException(status_code=400, detail="Unknown owner_agent_id")
        goal_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO governance_goals (
                id, client_id, parent_id, level, title, description, owner_agent_id,
                priority, target, due_at, created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                goal_id,
                req.client_id,
                req.parent_id,
                level,
                req.title,
                req.description,
                req.owner_agent_id,
                req.priority,
                req.target,
                req.due_at,
                now,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.owner_agent_id, "create_goal", "governance_goal", goal_id, model_to_dict(req))
        return dict(self.get_one("SELECT * FROM governance_goals WHERE id=?", (goal_id,)))

    def set_budget(self, req: BudgetSetIn) -> dict[str, Any]:
        if req.agent_id not in self.agents:
            raise HTTPException(status_code=400, detail="Unknown agent_id")
        now = now_iso()
        existing = self.get_one(
            "SELECT spent FROM agent_budgets WHERE client_id=? AND agent_id=?",
            (req.client_id, req.agent_id),
        )
        spent = float(req.spent if req.spent is not None else (existing["spent"] if existing else 0))
        status = req.status
        if req.auto_pause and req.monthly_budget > 0 and spent >= req.monthly_budget:
            status = "paused"
            self.pause_agent(req.agent_id, f"Budget exhausted: ${spent:,.2f} / ${req.monthly_budget:,.2f}", req.client_id)
        self.db.execute(
            """
            INSERT INTO agent_budgets (
                client_id, agent_id, monthly_budget, spent, auto_pause, status,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(client_id, agent_id) DO UPDATE SET
                monthly_budget=excluded.monthly_budget,
                spent=excluded.spent,
                auto_pause=excluded.auto_pause,
                status=excluded.status,
                updated_at=excluded.updated_at,
                metadata=excluded.metadata
            """,
            (
                req.client_id,
                req.agent_id,
                req.monthly_budget,
                spent,
                int(req.auto_pause),
                status,
                now,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit("ceo", "set_budget", "agent_budget", req.agent_id, model_to_dict(req))
        return dict(self.get_one("SELECT * FROM agent_budgets WHERE client_id=? AND agent_id=?", (req.client_id, req.agent_id)))

    def record_budget_usage(
        self,
        agent_id: str,
        amount: float,
        client_id: str = DEFAULT_CLIENT_ID,
        reason: str = "",
        entity_id: str = "",
    ) -> dict[str, Any]:
        if amount <= 0:
            row = self.get_one("SELECT * FROM agent_budgets WHERE client_id=? AND agent_id=?", (client_id, agent_id))
            return dict(row) if row else {}
        row = self.get_one("SELECT * FROM agent_budgets WHERE client_id=? AND agent_id=?", (client_id, agent_id))
        if not row:
            self.set_budget(BudgetSetIn(agent_id=agent_id, client_id=client_id, monthly_budget=DEFAULT_AGENT_MONTHLY_BUDGET))
            row = self.get_one("SELECT * FROM agent_budgets WHERE client_id=? AND agent_id=?", (client_id, agent_id))
        if not row:
            raise HTTPException(status_code=500, detail="Budget row missing")
        spent = float(row["spent"]) + float(amount)
        status = row["status"]
        if row["auto_pause"] and row["monthly_budget"] > 0 and spent >= row["monthly_budget"]:
            status = "paused"
            self.pause_agent(agent_id, f"Budget exhausted: ${spent:,.2f} / ${row['monthly_budget']:,.2f}", client_id)
        self.db.execute(
            """
            UPDATE agent_budgets
            SET spent=?, status=?, updated_at=?
            WHERE client_id=? AND agent_id=?
            """,
            (spent, status, now_iso(), client_id, agent_id),
        )
        self.db.commit()
        self.audit(agent_id, "record_budget_usage", "agent_budget", agent_id, {"amount": amount, "reason": reason, "entity_id": entity_id})
        return dict(self.get_one("SELECT * FROM agent_budgets WHERE client_id=? AND agent_id=?", (client_id, agent_id)))

    def pause_agent(self, agent_id: str, reason: str = "", client_id: str = DEFAULT_CLIENT_ID) -> dict[str, Any]:
        if agent_id not in self.agents:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent = self.agents[agent_id]
        agent.status = "paused"
        agent.current_task = reason or "Paused"
        agent.last_seen = now_iso()
        self.db.execute(
            "UPDATE agent_budgets SET status='paused', updated_at=? WHERE client_id=? AND agent_id=?",
            (now_iso(), client_id, agent_id),
        )
        self.db.commit()
        self.audit("ceo", "pause_agent", "agent", agent_id, {"reason": reason})
        self.queue_slack(f"{agent.name} paused. {reason}".strip(), "warning", client_id=client_id)
        return asdict(agent)

    def resume_agent(self, agent_id: str, reason: str = "", client_id: str = DEFAULT_CLIENT_ID) -> dict[str, Any]:
        if agent_id not in self.agents:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent = self.agents[agent_id]
        agent.status = "idle"
        agent.current_task = None
        agent.last_seen = now_iso()
        self.db.execute(
            "UPDATE agent_budgets SET status='active', updated_at=? WHERE client_id=? AND agent_id=?",
            (now_iso(), client_id, agent_id),
        )
        self.db.commit()
        self.audit("ceo", "resume_agent", "agent", agent_id, {"reason": reason})
        self.queue_slack(f"{agent.name} resumed. {reason}".strip(), "info", client_id=client_id)
        return asdict(agent)

    def record_heartbeat(self, req: HeartbeatIn) -> dict[str, Any]:
        if req.agent_id not in self.agents:
            raise HTTPException(status_code=400, detail="Unknown agent_id")
        heartbeat_id = short_id()
        self.db.execute(
            """
            INSERT INTO agent_heartbeats (id, client_id, agent_id, status, summary, created_at, metrics)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                heartbeat_id,
                req.client_id,
                req.agent_id,
                req.status,
                req.summary,
                now_iso(),
                json_dumps(req.metrics),
            ),
        )
        self.db.commit()
        agent = self.agents[req.agent_id]
        if agent.status != "paused":
            agent.status = "idle" if req.status in {"alive", "ok", "idle"} else req.status
        agent.last_seen = now_iso()
        self.audit(req.agent_id, "heartbeat", "agent_heartbeat", heartbeat_id, model_to_dict(req))
        return dict(self.get_one("SELECT * FROM agent_heartbeats WHERE id=?", (heartbeat_id,)))

    def send_agent_message(self, req: AgentMessageCreateIn) -> dict[str, Any]:
        for actor in (req.from_agent_id, req.to_agent_id):
            if not valid_actor(actor):
                raise HTTPException(status_code=400, detail=f"Unknown message actor: {actor}")
        message_type = req.message_type.lower().strip()
        if message_type not in {"update", "request", "question", "handoff", "decision", "alert"}:
            raise HTTPException(status_code=400, detail="Unsupported message_type")
        message_id = short_id()
        thread_id = req.thread_id or short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO agent_messages (
                id, client_id, thread_id, from_agent_id, to_agent_id, subject, body,
                message_type, priority, created_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                message_id,
                req.client_id,
                thread_id,
                req.from_agent_id,
                req.to_agent_id,
                req.subject,
                req.body,
                message_type,
                req.priority,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.from_agent_id, "send_agent_message", "agent_message", message_id, model_to_dict(req))
        self.log_event(req.to_agent_id, f"Message from {req.from_agent_id}: {req.subject}")

        if req.to_agent_id == "justin" or req.priority <= 1 or message_type == "alert":
            self.queue_slack(
                f"{req.from_agent_id} -> {req.to_agent_id}: {req.subject}\n{req.body}",
                severity="action" if req.priority <= 1 else "info",
                client_id=req.client_id,
                metadata={"agent_message_id": message_id, "thread_id": thread_id},
            )

        if req.to_agent_id in self.agents and message_type in {"request", "question", "handoff"}:
            self.create_task(
                f"Respond to {req.from_agent_id}: {req.subject}",
                req.to_agent_id,
                client_id=req.client_id,
                prompt=req.body,
                priority=req.priority,
                metadata={
                    "agent_message_id": message_id,
                    "thread_id": thread_id,
                    "from_agent_id": req.from_agent_id,
                    "message_type": message_type,
                },
            )
        return dict(self.get_one("SELECT * FROM agent_messages WHERE id=?", (message_id,)))

    def mark_agent_message_read(self, message_id: str, reader: str = "ceo") -> dict[str, Any]:
        row = self.get_one("SELECT * FROM agent_messages WHERE id=?", (message_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Agent message not found")
        if not valid_actor(reader):
            raise HTTPException(status_code=400, detail="Unknown reader")
        self.db.execute(
            "UPDATE agent_messages SET status='read', read_at=? WHERE id=?",
            (now_iso(), message_id),
        )
        self.db.commit()
        self.audit(reader, "read_agent_message", "agent_message", message_id)
        return dict(self.get_one("SELECT * FROM agent_messages WHERE id=?", (message_id,)))

    def reply_agent_message(self, message_id: str, req: AgentMessageReplyIn) -> dict[str, Any]:
        parent = self.get_one("SELECT * FROM agent_messages WHERE id=?", (message_id,))
        if not parent:
            raise HTTPException(status_code=404, detail="Agent message not found")
        to_agent_id = req.to_agent_id or parent["from_agent_id"]
        message = self.send_agent_message(
            AgentMessageCreateIn(
                from_agent_id=req.from_agent_id,
                to_agent_id=to_agent_id,
                subject=req.subject or f"Re: {parent['subject']}",
                body=req.body,
                message_type=req.message_type,
                client_id=parent["client_id"],
                priority=req.priority,
                thread_id=parent["thread_id"],
                metadata={
                    "reply_to_message_id": message_id,
                    "parent_from_agent_id": parent["from_agent_id"],
                    "parent_to_agent_id": parent["to_agent_id"],
                    **req.metadata,
                },
            )
        )
        if req.mark_parent_read and parent["to_agent_id"] == req.from_agent_id:
            self.mark_agent_message_read(message_id, req.from_agent_id)
        self.audit(req.from_agent_id, "reply_agent_message", "agent_message", message["id"], {"reply_to": message_id})
        return message

    def agent_inbox(
        self,
        agent_id: str,
        client_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not valid_actor(agent_id):
            raise HTTPException(status_code=400, detail="Unknown agent_id")
        clauses = ["to_agent_id=?"]
        params: list[Any] = [agent_id]
        if client_id:
            clauses.append("client_id=?")
            params.append(client_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        capped = max(1, min(limit, 250))
        where = " AND ".join(clauses)
        return self.rows("agent_messages", f"{where} ORDER BY priority ASC, created_at DESC LIMIT {capped}", tuple(params))

    def agent_threads(self, client_id: str | None = None, agent_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if client_id:
            clauses.append("client_id=?")
            params.append(client_id)
        if agent_id:
            if not valid_actor(agent_id):
                raise HTTPException(status_code=400, detail="Unknown agent_id")
            clauses.append("(from_agent_id=? OR to_agent_id=?)")
            params.extend([agent_id, agent_id])
        where = " AND ".join(clauses) or "1=1"
        capped = max(1, min(limit, 250))
        summaries = self.get_all(
            f"""
            SELECT
                thread_id,
                client_id,
                MIN(created_at) AS first_at,
                MAX(created_at) AS last_at,
                COUNT(*) AS message_count,
                SUM(CASE WHEN status='unread' THEN 1 ELSE 0 END) AS unread_count,
                MIN(priority) AS highest_priority
            FROM agent_messages
            WHERE {where}
            GROUP BY thread_id, client_id
            ORDER BY last_at DESC
            LIMIT {capped}
            """,
            tuple(params),
        )
        threads: list[dict[str, Any]] = []
        for row in summaries:
            last = self.get_one(
                """
                SELECT * FROM agent_messages
                WHERE thread_id=? AND client_id=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (row["thread_id"], row["client_id"]),
            )
            participants = self.get_all(
                """
                SELECT from_agent_id, to_agent_id FROM agent_messages
                WHERE thread_id=? AND client_id=?
                """,
                (row["thread_id"], row["client_id"]),
            )
            actors = sorted({actor for participant in participants for actor in (participant["from_agent_id"], participant["to_agent_id"])})
            threads.append(
                {
                    "thread_id": row["thread_id"],
                    "client_id": row["client_id"],
                    "first_at": row["first_at"],
                    "last_at": row["last_at"],
                    "message_count": row["message_count"],
                    "unread_count": row["unread_count"] or 0,
                    "highest_priority": row["highest_priority"],
                    "participants": actors,
                    "last_message": dict(last) if last else None,
                }
            )
        return threads

    def coordination_room(self, client_id: str | None = None) -> dict[str, Any]:
        agent_rows: dict[str, Any] = {}
        for agent_id, agent in self.agents.items():
            latest_heartbeat = self.get_one(
                "SELECT * FROM agent_heartbeats WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
                (agent_id,),
            )
            budget = self.get_one(
                "SELECT * FROM agent_budgets WHERE client_id=? AND agent_id=?",
                (client_id or DEFAULT_CLIENT_ID, agent_id),
            )
            agent_rows[agent_id] = {
                "id": agent_id,
                "name": agent.name,
                "role": agent.role,
                "status": agent.status,
                "current_task": agent.current_task,
                "last_seen": agent.last_seen,
                "workload": self.agent_workload(agent_id, client_id or DEFAULT_CLIENT_ID),
                "latest_heartbeat": dict(latest_heartbeat) if latest_heartbeat else None,
                "budget": dict(budget) if budget else None,
            }
        recent_messages = self.rows(
            "agent_messages",
            "client_id=? ORDER BY created_at DESC LIMIT 20" if client_id else "1=1 ORDER BY created_at DESC LIMIT 20",
            (client_id,) if client_id else (),
        )
        return {
            "client_id": client_id or "all",
            "agents": agent_rows,
            "threads": self.agent_threads(client_id=client_id, limit=20),
            "justin_inbox": self.agent_inbox("justin", client_id=client_id, status="unread", limit=20),
            "ceo_inbox": self.agent_inbox("ceo", client_id=client_id, status="unread", limit=20),
            "high_priority": self.rows(
                "agent_messages",
                "client_id=? AND status='unread' AND priority<=1 ORDER BY created_at DESC LIMIT 20"
                if client_id
                else "status='unread' AND priority<=1 ORDER BY created_at DESC LIMIT 20",
                (client_id,) if client_id else (),
            ),
            "recent_messages": recent_messages,
        }

    def run_health_check(self, source: str = "manual") -> dict[str, Any]:
        started = utcnow()
        findings: list[dict[str, Any]] = []
        metrics = self.operating_cycle_snapshot()
        table_counts = {
            table: self.count(table)
            for table in [
                "clients",
                "tasks",
                "events",
                "approvals",
                "memory_entries",
                "outbox",
                "browser_jobs",
                "browser_operator_heartbeats",
                "outreach_jobs",
                "integration_syncs",
                "crm_sync_jobs",
                "agent_messages",
                "incidents",
                "backups",
                "readiness_checks",
            ]
        }
        metrics.update(
            {
                "table_counts": table_counts,
                "db_path": self.db_path,
                "db_size_bytes": file_size(self.db_path),
                "backup_dir": BACKUP_DIR,
                "open_incidents": self.count("incidents", "status='open'"),
                "failed_outbox": self.count("outbox", "status='failed'"),
                "failed_outreach": self.count("outreach_jobs", "status='failed'"),
                "failed_provider_syncs": self.count("integration_syncs", "status='failed'"),
                "failed_crm_syncs": self.count("crm_sync_jobs", "status='failed'"),
                "oldest_pending_task_minutes": self.oldest_age_minutes("tasks", "status='pending'"),
                "latest_backup_age_minutes": self.latest_backup_age_minutes(),
                "memory": {
                    "sqlite": True,
                    "obsidian_configured": bool(OBSIDIAN_VAULT_PATH),
                    "notion_configured": bool(NOTION_API_KEY and NOTION_DATABASE_ID),
                },
            }
        )

        try:
            self.db.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            findings.append({"severity": "critical", "component": "database", "message": str(exc)})

        if self.is_kill_switch_active():
            findings.append({"severity": "critical", "component": "kill_switch", "message": "Global kill switch is active"})
        if metrics["failed_outbox"]:
            findings.append({"severity": "warning", "component": "outbox", "message": f"{metrics['failed_outbox']} failed outbox item(s)"})
        if metrics["failed_outreach"]:
            findings.append({"severity": "warning", "component": "outreach", "message": f"{metrics['failed_outreach']} failed outreach job(s)"})
        if metrics["failed_provider_syncs"] or metrics["failed_crm_syncs"]:
            findings.append(
                {
                    "severity": "warning",
                    "component": "integrations",
                    "message": f"{metrics['failed_provider_syncs'] + metrics['failed_crm_syncs']} failed sync job(s)",
                }
            )
        if metrics["oldest_pending_task_minutes"] and metrics["oldest_pending_task_minutes"] > 1440:
            findings.append({"severity": "warning", "component": "tasks", "message": "A pending task is older than 24 hours"})
        if metrics["latest_backup_age_minutes"] is None:
            findings.append({"severity": "warning", "component": "backups", "message": "No successful backup has been recorded"})
        elif metrics["latest_backup_age_minutes"] > (BACKUP_INTERVAL_SECONDS / 60) * 2:
            findings.append({"severity": "warning", "component": "backups", "message": "Latest successful backup is stale"})
        if not pathlib.Path(self.db_path).exists():
            findings.append({"severity": "critical", "component": "database", "message": "Database file does not exist on disk"})

        severity_rank = {"info": 0, "warning": 1, "critical": 2}
        severity = "info"
        for finding in findings:
            if severity_rank[finding["severity"]] > severity_rank[severity]:
                severity = finding["severity"]
        status = "ok" if severity == "info" else "degraded" if severity == "warning" else "critical"
        summary = "All monitored systems are healthy." if not findings else "; ".join(f["message"] for f in findings[:4])
        check_id = short_id()
        duration_ms = int((utcnow() - started).total_seconds() * 1000)
        self.db.execute(
            """
            INSERT INTO health_checks (id, status, severity, summary, checked_at, duration_ms, metrics, findings)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (check_id, status, severity, summary, now_iso(), duration_ms, json_dumps(metrics), json_dumps(findings)),
        )
        self.db.commit()
        self.audit("cto", "run_health_check", "health_check", check_id, {"source": source, "status": status})
        if severity == "critical":
            self.create_incident(
                IncidentCreateIn(
                    title=f"Critical health check: {summary[:120]}",
                    severity="critical",
                    source="health_check",
                    owner_agent_id="cto",
                    details={"health_check_id": check_id, "findings": findings, "metrics": metrics},
                )
            )
        return dict(self.get_one("SELECT * FROM health_checks WHERE id=?", (check_id,)))

    def _readiness_item(
        self,
        key: str,
        label: str,
        passed: bool,
        required: bool,
        evidence: str,
        recommendation: str = "",
    ) -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "status": "pass" if passed else "fail" if required else "warn",
            "required": required,
            "evidence": evidence,
            "recommendation": recommendation,
        }

    def _path_is_persistent(self, path_text: str) -> bool:
        if path_text == ":memory:":
            return False
        try:
            path = pathlib.Path(path_text).resolve()
        except OSError:
            return False
        temp_roots = [
            pathlib.Path("/tmp"),
            pathlib.Path("/private/tmp"),
            pathlib.Path("/var/folders"),
            pathlib.Path("/private/var/folders"),
        ]
        return not any(path == root or root in path.parents for root in temp_roots)

    def _db_path_is_persistent(self) -> bool:
        return self._path_is_persistent(self.db_path)

    def readiness_items(self, profile: str) -> list[dict[str, Any]]:
        profile = profile.lower().strip()
        if profile not in {"local", "production", "live_outreach"}:
            raise HTTPException(status_code=400, detail="profile must be local, production, or live_outreach")

        provider_status = provider_config_status()
        backup_dir = pathlib.Path(BACKUP_DIR)
        latest_backup = self.get_one("SELECT * FROM backups WHERE status='completed' ORDER BY completed_at DESC LIMIT 1")
        latest_health = self.get_one("SELECT * FROM health_checks ORDER BY checked_at DESC LIMIT 1")
        latest_watchdog = self.get_one("SELECT * FROM watchdog_runs ORDER BY checked_at DESC LIMIT 1")
        active_client_count = self.count("clients", "status IN ('active','onboarding')")
        default_client = self.get_one("SELECT id FROM clients WHERE id=?", (DEFAULT_CLIENT_ID,))
        open_incidents = self.count("incidents", "status='open'")
        pending_high_approvals = self.count("approvals", "status='pending' AND risk IN ('high','critical')")
        controls = self.runtime_control_map()
        required_controls_active = all(
            bool(controls.get(component, {"enabled": 1})["enabled"])
            for component in ["scheduler", "ceo_decision_review", "worker", "workflow", "scheduled_ops", "self_audit", "watchdog"]
        )
        scheduler_heartbeat = self.get_one("SELECT * FROM runtime_heartbeats WHERE component='scheduler'")
        browser_operator = self.get_one("SELECT * FROM browser_operator_heartbeats ORDER BY last_seen_at DESC LIMIT 1")
        browser_operator_recent = bool(
            browser_operator
            and browser_operator["last_seen_at"]
            and iso_age_seconds(browser_operator["last_seen_at"]) <= max(BROWSER_OPERATOR_POLL_SECONDS * 4, 120)
        )
        scheduler_recent = bool(
            scheduler_heartbeat
            and scheduler_heartbeat["status"] in {"ok", "paused"}
            and scheduler_heartbeat["last_finished_at"]
            and iso_age_seconds(scheduler_heartbeat["last_finished_at"]) <= max(SCHEDULER_INTERVAL_SECONDS * 2, 60)
        )
        failed_runtime_components = self.count("runtime_heartbeats", "status='failed'")
        rows = [
            self._readiness_item(
                "product_truth",
                "DialDesk product truth is MCA/funding appointment setting, not lending",
                "appointment setting" in PRODUCT["truth"].lower() and "does not offer business loans" in PRODUCT["truth"].lower(),
                True,
                PRODUCT["truth"],
                "Keep product language aligned before sending traffic or onboarding clients.",
            ),
            self._readiness_item(
                "database_file",
                "Durable SQLite database exists",
                pathlib.Path(self.db_path).exists() and self.db_path != ":memory:",
                True,
                self.db_path,
                "Set DIALDESK_DB to a persistent volume path.",
            ),
            self._readiness_item(
                "default_client",
                "DialDesk internal client exists",
                bool(default_client),
                True,
                DEFAULT_CLIENT_ID,
                "Seed or restore the default internal DialDesk client.",
            ),
            self._readiness_item(
                "scheduler",
                "Scheduler loop is configured",
                SCHEDULER_INTERVAL_SECONDS > 0 and CEO_DAILY_HOUR_UTC in range(0, 24),
                True,
                f"interval={SCHEDULER_INTERVAL_SECONDS}s, ceo_hour_utc={CEO_DAILY_HOUR_UTC}",
                "Set SCHEDULER_INTERVAL_SECONDS above 0 and CEO_DAILY_HOUR_UTC from 0 to 23.",
            ),
            self._readiness_item(
                "runtime_controls",
                "Core runtime controls are enabled",
                required_controls_active,
                profile != "local",
                ", ".join(f"{k}={'on' if v['enabled'] else 'paused'}" for k, v in controls.items()),
                "Resume scheduler, CEO review, worker, workflow, scheduled ops, and watchdog before trusting unattended operation.",
            ),
            self._readiness_item(
                "runtime_heartbeat",
                "Scheduler proof-of-life is recent",
                scheduler_recent,
                profile != "local",
                scheduler_heartbeat["last_finished_at"] if scheduler_heartbeat else "No scheduler heartbeat",
                "Confirm the service process is running and the scheduler loop is checking in.",
            ),
            self._readiness_item(
                "runtime_failures",
                "No runtime components are failing",
                failed_runtime_components == 0,
                profile != "local",
                str(failed_runtime_components),
                "Inspect /api/ops/runtime-heartbeats and resolve failed runtime components.",
            ),
            self._readiness_item(
                "watchdog",
                "Latest watchdog run is usable",
                bool(latest_watchdog and latest_watchdog["status"] in {"ok", "degraded"}),
                profile != "local",
                latest_watchdog["summary"] if latest_watchdog else "No watchdog run recorded",
                "Run POST /api/ops/watchdog/run and resolve critical findings.",
            ),
            self._readiness_item(
                "kill_switch",
                "Global kill switch is off",
                not self.is_kill_switch_active(),
                True,
                "active" if self.is_kill_switch_active() else "off",
                "Turn off the global kill switch only when operations should run.",
            ),
            self._readiness_item(
                "health_check",
                "Latest health check is usable",
                bool(latest_health and latest_health["status"] in {"ok", "degraded"}),
                profile != "local",
                latest_health["summary"] if latest_health else "No health check recorded",
                "Run POST /api/ops/health-checks/run and resolve critical findings.",
            ),
            self._readiness_item(
                "backups",
                "Successful database backup exists",
                bool(latest_backup and pathlib.Path(latest_backup["path"]).exists()),
                profile != "local",
                latest_backup["path"] if latest_backup else "No successful backup recorded",
                "Run POST /api/ops/backups/create and configure DIALDESK_BACKUP_DIR.",
            ),
            self._readiness_item(
                "active_clients",
                "At least one active/onboarding client exists",
                active_client_count > 0,
                profile != "local",
                str(active_client_count),
                "Create or restore the internal DialDesk client and any active clients.",
            ),
            self._readiness_item(
                "open_incidents",
                "No open production incidents",
                open_incidents == 0,
                False,
                str(open_incidents),
                "Resolve open incidents before trusting fully autonomous operation.",
            ),
            self._readiness_item(
                "memory",
                "Operator memory is configured",
                bool(OBSIDIAN_VAULT_PATH or (NOTION_API_KEY and NOTION_DATABASE_ID)),
                False,
                f"obsidian={bool(OBSIDIAN_VAULT_PATH)}, notion={bool(NOTION_API_KEY and NOTION_DATABASE_ID)}",
                "Set OBSIDIAN_VAULT_PATH and/or Notion credentials so decisions persist outside SQLite.",
            ),
            self._readiness_item(
                "slack_visibility",
                "Slack/outbox visibility is configured",
                bool(SLACK_WEBHOOK_URL and SEND_OUTBOX),
                False,
                f"webhook={bool(SLACK_WEBHOOK_URL)}, send_outbox={SEND_OUTBOX}",
                "Set SLACK_WEBHOOK_URL and DIALDESK_SEND_OUTBOX=1 when alerts should leave the app.",
            ),
            self._readiness_item(
                "browser_queue",
                "Browser automation queue is available",
                True,
                False,
                "/api/browser/jobs",
                "Run an external browser operator that claims, heartbeats, and completes browser jobs.",
            ),
            self._readiness_item(
                "browser_operator",
                "Browser/super-browser operator is online",
                browser_operator_recent,
                profile == "production",
                browser_operator["last_seen_at"] if browser_operator else "No browser operator heartbeat",
                "Run `python -m src.browser_operator` or the browser-operator service so browser automation works unattended.",
            ),
            self._readiness_item(
                "sms_provider",
                "Sendivo SMS provider is configured",
                bool(provider_status["sendivo"]["sms_send"]),
                profile == "live_outreach",
                f"sendivo_sms={provider_status['sendivo']['sms_send']}",
                "Set SENDIVO_BEARER_AUTH before enabling live SMS sends.",
            ),
            self._readiness_item(
                "email_provider",
                "Smartlead email provider is configured",
                bool(provider_status["smartlead"]["email_send"]),
                profile == "live_outreach",
                f"smartlead_email={provider_status['smartlead']['email_send']}",
                "Set SMARTLEAD_API_KEY before enabling live email sends.",
            ),
            self._readiness_item(
                "crm_sync",
                "CRM sync is configured",
                bool(provider_status["ghl"]["configured"] and provider_status["ghl"]["contact_upsert_url_configured"]),
                profile == "live_outreach",
                f"ghl={provider_status['ghl']}",
                "Set GHL credentials and contact upsert URL before live CRM sync.",
            ),
            self._readiness_item(
                "payment_links",
                "Stripe/payment links are configured",
                bool(
                    provider_status["stripe"]["ai_caller_link_configured"]
                    and provider_status["stripe"]["human_sdr_link_configured"]
                ),
                False,
                f"stripe={provider_status['stripe']}",
                "Configure package payment links before letting sales close without manual follow-up.",
            ),
            self._readiness_item(
                "dry_run_outreach",
                "Live outreach sending is armed",
                SEND_OUTREACH,
                profile == "live_outreach",
                f"DIALDESK_SEND_OUTREACH={int(SEND_OUTREACH)}",
                "Keep dry-run for testing; set DIALDESK_SEND_OUTREACH=1 only when suppression/compliance are ready.",
            ),
            self._readiness_item(
                "pending_high_approvals",
                "No high-risk approvals are waiting",
                pending_high_approvals == 0,
                profile == "live_outreach",
                str(pending_high_approvals),
                "Clear high-risk approvals before arming live outreach.",
            ),
        ]

        if profile in {"production", "live_outreach"}:
            rows.extend(
                [
                    self._readiness_item(
                        "app_token",
                        "Control-plane API token is configured",
                        bool(APP_TOKEN),
                        True,
                        "configured" if APP_TOKEN else "missing",
                        "Set APP_TOKEN before exposing /api/*, /messages, or task endpoints.",
                    ),
                    self._readiness_item(
                        "persistent_db_path",
                        "Database path appears production-persistent",
                        self._db_path_is_persistent(),
                        True,
                        self.db_path,
                        "Use an attached persistent volume, not /tmp or a local test directory.",
                    ),
                    self._readiness_item(
                        "backup_directory",
                        "Backup directory is configured",
                        bool(BACKUP_DIR)
                        and (backup_dir.exists() or backup_dir.parent.exists())
                        and self._path_is_persistent(BACKUP_DIR),
                        True,
                        BACKUP_DIR,
                        "Set DIALDESK_BACKUP_DIR to a persistent backup path.",
                    ),
                ]
            )

        return rows

    def run_readiness_check(self, req: ReadinessCheckCreateIn) -> dict[str, Any]:
        if not valid_actor(req.requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        profile = req.profile.lower().strip()
        checks = self.readiness_items(profile)
        required_failures = sum(1 for item in checks if item["required"] and item["status"] == "fail")
        warnings = sum(1 for item in checks if not item["required"] and item["status"] == "warn")
        score = max(0, min(100, 100 - required_failures * 25 - warnings * 5))
        status = "not_ready" if required_failures else "warning" if warnings else "ready"
        failed_labels = (
            [item["label"] for item in checks if item["status"] == "fail"]
            + [item["label"] for item in checks if item["status"] == "warn"]
        )
        summary = (
            f"{profile} readiness is {status} with score {score}."
            if not failed_labels
            else f"{profile} readiness is {status}: " + "; ".join(failed_labels[:4])
        )
        check_id = short_id()
        self.db.execute(
            """
            INSERT INTO readiness_checks (
                id, profile, status, score, checked_at, requested_by, summary,
                required_failures, warnings, checks, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                check_id,
                profile,
                status,
                score,
                now_iso(),
                req.requested_by,
                summary,
                required_failures,
                warnings,
                json_dumps(checks),
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "run_readiness_check", "readiness_check", check_id, {"profile": profile, "status": status})
        if status == "not_ready":
            try:
                self.create_task(
                    f"Fix {profile} readiness blockers",
                    "cto",
                    priority=1,
                    prompt=summary,
                    metadata={"readiness_check_id": check_id, "profile": profile, "required_failures": required_failures},
                )
            except HTTPException as exc:
                self.audit("readiness", "task_creation_skipped", "readiness_check", check_id, {"detail": exc.detail})
            self.queue_slack(
                f"Readiness check not ready for {profile}: {summary}",
                severity="warning",
                metadata={"readiness_check_id": check_id, "profile": profile},
            )
        return dict(self.get_one("SELECT * FROM readiness_checks WHERE id=?", (check_id,)))

    def create_incident(self, req: IncidentCreateIn) -> dict[str, Any]:
        severity = req.severity.lower().strip()
        if severity not in {"info", "warning", "high", "critical"}:
            raise HTTPException(status_code=400, detail="severity must be info, warning, high, or critical")
        if req.owner_agent_id not in self.agents:
            raise HTTPException(status_code=400, detail="Unknown owner_agent_id")
        incident_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO incidents (
                id, client_id, title, severity, source, owner_agent_id,
                created_at, updated_at, details
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                incident_id,
                req.client_id,
                req.title,
                severity,
                req.source,
                req.owner_agent_id,
                now,
                now,
                json_dumps(req.details),
            ),
        )
        self.db.commit()
        self.audit(req.source, "create_incident", "incident", incident_id, model_to_dict(req))
        self.create_task(
            f"Investigate incident: {req.title}",
            req.owner_agent_id,
            client_id=req.client_id,
            priority=1 if severity in {"high", "critical"} else 2,
            metadata={"incident_id": incident_id, "severity": severity},
        )
        self.queue_slack(
            f"Incident opened: {req.title}",
            severity="critical" if severity == "critical" else "warning",
            client_id=req.client_id,
            metadata={"incident_id": incident_id, "severity": severity},
        )
        return dict(self.get_one("SELECT * FROM incidents WHERE id=?", (incident_id,)))

    def resolve_incident(self, incident_id: str, req: IncidentResolveIn) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Incident not found")
        if row["status"] == "resolved":
            return dict(row)
        if not valid_actor(req.resolved_by):
            raise HTTPException(status_code=400, detail="Unknown resolved_by")
        self.db.execute(
            """
            UPDATE incidents
            SET status='resolved', updated_at=?, resolved_at=?, resolved_by=?, resolution=?
            WHERE id=?
            """,
            (now_iso(), now_iso(), req.resolved_by, req.resolution, incident_id),
        )
        self.db.commit()
        self.audit(req.resolved_by, "resolve_incident", "incident", incident_id, {"resolution": req.resolution})
        self.remember(
            title=f"Incident resolved: {row['title']}",
            body=req.resolution or "Incident marked resolved.",
            kind="incident",
            actor=req.resolved_by,
            client_id=row["client_id"],
            tags=["incident", "resolved", row["severity"]],
            source="incident",
            metadata={"incident_id": incident_id},
        )
        return dict(self.get_one("SELECT * FROM incidents WHERE id=?", (incident_id,)))

    def create_backup(self, req: BackupCreateIn) -> dict[str, Any]:
        backup_id = short_id()
        now = now_iso()
        pathlib.Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
        target = pathlib.Path(BACKUP_DIR) / f"dialdesk-{today_key()}-{backup_id}.sqlite3"
        self.db.execute(
            """
            INSERT INTO backups (id, status, path, reason, requested_by, created_at, metadata)
            VALUES (?,?,?,?,?,?,?)
            """,
            (backup_id, "running", str(target), req.reason, req.requested_by, now, json_dumps(req.metadata)),
        )
        self.db.commit()
        status = "completed"
        error = ""
        size_bytes = 0
        try:
            backup_conn = sqlite3.connect(str(target))
            self.db.backup(backup_conn)
            backup_conn.close()
            size_bytes = file_size(str(target))
        except sqlite3.Error as exc:
            status = "failed"
            error = str(exc)
        self.db.execute(
            """
            UPDATE backups
            SET status=?, size_bytes=?, completed_at=?, last_error=?
            WHERE id=?
            """,
            (status, size_bytes, now_iso() if status == "completed" else None, error, backup_id),
        )
        self.db.commit()
        self.audit(req.requested_by, "create_backup", "backup", backup_id, {"status": status, "error": error})
        if status == "failed":
            self.create_incident(
                IncidentCreateIn(
                    title="Database backup failed",
                    severity="critical",
                    source="backup",
                    owner_agent_id="cto",
                    details={"backup_id": backup_id, "error": error},
                )
            )
        else:
            self.remember(
                title="Database backup completed",
                body=f"Backup written to {target} ({size_bytes} bytes).",
                kind="backup",
                actor=req.requested_by,
                client_id=DEFAULT_CLIENT_ID,
                tags=["backup", "database", "ops"],
                source="backup",
                metadata={"backup_id": backup_id, "path": str(target), "size_bytes": size_bytes},
            )
        return dict(self.get_one("SELECT * FROM backups WHERE id=?", (backup_id,)))

    def maybe_run_scheduled_ops(self) -> None:
        if not self.runtime_component_enabled("scheduled_ops"):
            return
        latest_health = self.get_one("SELECT checked_at FROM health_checks ORDER BY checked_at DESC LIMIT 1")
        if not latest_health or iso_age_seconds(latest_health["checked_at"]) >= HEALTH_CHECK_INTERVAL_SECONDS:
            self.run_health_check("scheduler")
        latest_backup = self.get_one("SELECT created_at FROM backups WHERE status='completed' ORDER BY created_at DESC LIMIT 1")
        if not latest_backup or iso_age_seconds(latest_backup["created_at"]) >= BACKUP_INTERVAL_SECONDS:
            self.create_backup(BackupCreateIn(reason="scheduled", requested_by="cto"))

    def self_audit_add_check(self, checks: list[dict[str, Any]], name: str, status: str, detail: str, required: bool = False) -> None:
        checks.append({"name": name, "status": status, "detail": detail, "required": required})

    def run_self_audit(self, req: SelfAuditRunIn) -> dict[str, Any]:
        requested_by = req.requested_by.strip().lower()
        if not valid_actor(requested_by) and requested_by not in {"scheduler", "self_audit"}:
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        started_at = now_iso()
        checks: list[dict[str, Any]] = []
        memory = self.memory_health()
        controls = self.runtime_control_map()
        scheduler_heartbeat = self.get_one("SELECT * FROM runtime_heartbeats WHERE component='scheduler'")
        scheduler_age = iso_age_seconds(scheduler_heartbeat["last_finished_at"]) if scheduler_heartbeat and scheduler_heartbeat["last_finished_at"] else None
        scheduler_recent = scheduler_age is not None and scheduler_age <= max(SCHEDULER_INTERVAL_SECONDS * 3, 180)
        browser_operators = self.browser_operators()
        online_browser_operators = [operator for operator in browser_operators if operator["alive"]]
        slack_outbox_total = self.count("outbox", "destination='slack'")
        slack_outbox_queued = self.count("outbox", "destination='slack' AND status='queued'")
        latest_watchdog = self.get_one("SELECT * FROM watchdog_runs ORDER BY checked_at DESC LIMIT 1")
        latest_launch_gate = self.get_one("SELECT * FROM launch_gate_runs ORDER BY checked_at DESC LIMIT 1")
        latest_business_proof = self.get_one("SELECT * FROM proof_runs WHERE proof_type='business_stack' ORDER BY completed_at DESC LIMIT 1")
        current_cycle = self.get_one("SELECT * FROM operating_cycles WHERE day_key=? ORDER BY started_at DESC LIMIT 1", (today_key(),))
        failed_runtime_components = self.rows("runtime_heartbeats", "status='failed' ORDER BY last_finished_at DESC")
        failed_queues = {
            "outbox": self.count("outbox", "status='failed'"),
            "outreach": self.count("outreach_jobs", "status='failed'"),
            "integration_sync": self.count("integration_syncs", "status='failed'"),
            "crm_sync": self.count("crm_sync_jobs", "status='failed'"),
            "browser_jobs": self.count("browser_jobs", "status='failed'"),
        }
        critical_incidents = self.count("incidents", "status='open' AND severity='critical'")

        self.self_audit_add_check(
            checks,
            "scheduler heartbeat",
            "passed" if scheduler_recent else "warning",
            f"last_finished_at={scheduler_heartbeat['last_finished_at'] if scheduler_heartbeat else 'none'}",
        )
        self.self_audit_add_check(
            checks,
            "core runtime controls",
            "passed" if all(bool(controls.get(component, {"enabled": 1})["enabled"]) for component in ["scheduler", "ceo_decision_review", "worker", "workflow", "scheduled_ops", "self_audit", "watchdog"]) else "failed",
            ", ".join(f"{component}={'on' if row['enabled'] else 'paused'}" for component, row in sorted(controls.items())),
            required=True,
        )
        self.self_audit_add_check(
            checks,
            "runtime failures",
            "passed" if not failed_runtime_components else "failed",
            f"{len(failed_runtime_components)} failed component(s)",
            required=True,
        )
        self.self_audit_add_check(
            checks,
            "memory",
            "passed" if memory["sqlite"] else "failed",
            f"entries={memory['total_entries']}, obsidian={memory['obsidian']['configured']}, notion={memory['notion']['configured']}",
            required=True,
        )
        self.self_audit_add_check(
            checks,
            "obsidian mirror",
            "passed" if memory["obsidian"]["configured"] and memory["obsidian"]["written"] else "warning",
            f"configured={memory['obsidian']['configured']}, written={memory['obsidian']['written']}",
        )
        self.self_audit_add_check(
            checks,
            "notion queue",
            "passed" if memory["notion"]["configured"] else "warning",
            f"configured={memory['notion']['configured']}, queued={memory['notion']['queued']}, failed={memory['notion']['failed']}",
        )
        self.self_audit_add_check(
            checks,
            "slack visibility",
            "passed" if SLACK_WEBHOOK_URL or slack_outbox_total else "warning",
            f"webhook_configured={bool(SLACK_WEBHOOK_URL)}, queued={slack_outbox_queued}",
        )
        self.self_audit_add_check(
            checks,
            "browser operator",
            "passed" if online_browser_operators else "warning",
            f"online={len(online_browser_operators)}, known={len(browser_operators)}",
        )
        self.self_audit_add_check(
            checks,
            "ceo operating cycle",
            "passed" if current_cycle else "warning",
            current_cycle["summary"] if current_cycle else "No operating cycle for today yet.",
        )
        self.self_audit_add_check(
            checks,
            "watchdog",
            "passed" if latest_watchdog and latest_watchdog["status"] in {"ok", "degraded"} else "warning",
            latest_watchdog["summary"] if latest_watchdog else "No watchdog run yet.",
        )
        self.self_audit_add_check(
            checks,
            "launch gate",
            "passed" if latest_launch_gate and latest_launch_gate["status"] in {"ready", "warning"} else "warning",
            latest_launch_gate["summary"] if latest_launch_gate else "No launch gate run yet.",
        )
        self.self_audit_add_check(
            checks,
            "business proof",
            "passed" if latest_business_proof and latest_business_proof["status"] in {"passed", "warning"} else "warning",
            latest_business_proof["summary"] if latest_business_proof else "No full business-stack proof recorded yet.",
        )
        self.self_audit_add_check(
            checks,
            "failed queues",
            "passed" if not any(failed_queues.values()) else "failed",
            json_dumps(failed_queues),
            required=True,
        )
        self.self_audit_add_check(
            checks,
            "critical incidents",
            "passed" if critical_incidents == 0 else "failed",
            f"{critical_incidents} open critical incident(s)",
            required=True,
        )
        self.self_audit_add_check(
            checks,
            "kill switch",
            "passed" if not self.is_kill_switch_active() else "failed",
            "off" if not self.is_kill_switch_active() else "active",
            required=True,
        )

        failures = [check for check in checks if check["status"] == "failed"]
        required_failures = [check for check in failures if check.get("required")]
        warnings = [check for check in checks if check["status"] == "warning"]
        status = "failed" if required_failures else "warning" if failures or warnings else "passed"
        summary = f"Runtime self-audit {status}: {len(required_failures)} blocker(s), {len(warnings)} warning(s)."
        return self.record_proof_run(
            ProofRunRecordIn(
                proof_type="runtime_self_audit",
                status=status,
                summary=summary,
                source=requested_by,
                started_at=started_at,
                completed_at=now_iso(),
                command="POST /api/ops/self-audit/run",
                checks=checks,
                metadata={
                    **req.metadata,
                    "force": req.force,
                    "failed_queues": failed_queues,
                    "critical_incidents": critical_incidents,
                    "online_browser_operators": len(online_browser_operators),
                },
            )
        )

    def maybe_run_self_audit(self) -> dict[str, Any] | None:
        if not self.runtime_component_enabled("self_audit"):
            return None
        latest = self.get_one("SELECT completed_at FROM proof_runs WHERE proof_type='runtime_self_audit' ORDER BY completed_at DESC LIMIT 1")
        if latest and iso_age_seconds(latest["completed_at"]) < SELF_AUDIT_INTERVAL_SECONDS:
            return {"skipped": True, "reason": "recent_self_audit", "latest_completed_at": latest["completed_at"]}
        return self.run_self_audit(SelfAuditRunIn(requested_by="scheduler", metadata={"source": "scheduled_ops"}))

    def open_incident_by_source_and_title(self, source: str, title: str, client_id: str = DEFAULT_CLIENT_ID) -> sqlite3.Row | None:
        return self.get_one(
            """
            SELECT * FROM incidents
            WHERE status='open' AND source=? AND title=? AND client_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (source, title, client_id),
        )

    def create_incident_once(self, req: IncidentCreateIn) -> tuple[dict[str, Any], bool]:
        existing = self.open_incident_by_source_and_title(req.source, req.title, req.client_id)
        if existing:
            return dict(existing), False
        return self.create_incident(req), True

    def watchdog_add_finding(
        self,
        findings: list[dict[str, Any]],
        key: str,
        status: str,
        severity: str,
        title: str,
        summary: str,
        source: str,
        owner_agent_id: str = "cto",
        client_id: str = DEFAULT_CLIENT_ID,
        details: dict[str, Any] | None = None,
        incident: bool = True,
    ) -> None:
        findings.append(
            {
                "key": key,
                "status": status,
                "severity": severity,
                "title": title,
                "summary": summary,
                "source": source,
                "owner_agent_id": owner_agent_id,
                "client_id": client_id,
                "details": details or {},
                "incident": incident,
            }
        )

    def run_watchdog(self, req: WatchdogRunIn) -> dict[str, Any]:
        if not self.runtime_component_enabled("watchdog") and req.source != "api":
            return {
                "id": "",
                "status": "paused",
                "severity": "info",
                "summary": "Watchdog paused by runtime control.",
                "checked_at": now_iso(),
                "source": req.source,
                "findings": "[]",
                "incidents_opened": 0,
                "metadata": json_dumps(req.metadata),
            }

        findings: list[dict[str, Any]] = []
        scheduler_heartbeat = self.get_one("SELECT * FROM runtime_heartbeats WHERE component='scheduler'")
        scheduler_age = iso_age_seconds(scheduler_heartbeat["last_finished_at"]) if scheduler_heartbeat else None
        scheduler_stale_after = max(SCHEDULER_INTERVAL_SECONDS * 3, 180)
        if self.runtime_component_enabled("scheduler") and (
            not scheduler_heartbeat
            or not scheduler_heartbeat["last_finished_at"]
            or scheduler_age is None
            or scheduler_age > scheduler_stale_after
        ):
            self.watchdog_add_finding(
                findings,
                "scheduler_stale",
                "fail",
                "critical",
                "Scheduler heartbeat stale",
                "The master autonomous loop has not checked in recently.",
                "watchdog_scheduler",
                details={
                    "last_finished_at": scheduler_heartbeat["last_finished_at"] if scheduler_heartbeat else "",
                    "age_seconds": scheduler_age,
                    "stale_after_seconds": scheduler_stale_after,
                },
            )

        for row in self.rows("runtime_heartbeats", "status='failed' ORDER BY last_finished_at DESC"):
            self.watchdog_add_finding(
                findings,
                f"runtime_failed_{row['component']}",
                "fail",
                "high" if int(row["consecutive_failures"] or 0) >= 3 else "warning",
                f"Runtime component failing: {row['component']}",
                row["last_error"] or f"{row['component']} reported failure.",
                "watchdog_runtime",
                details={
                    "component": row["component"],
                    "consecutive_failures": row["consecutive_failures"],
                    "last_finished_at": row["last_finished_at"],
                    "last_error": row["last_error"],
                },
            )

        browser_jobs_waiting = self.count("browser_jobs", "status IN ('queued','claimed')")
        browser_jobs_failed = self.count("browser_jobs", "status='failed'")
        online_browser_operators = [operator for operator in self.browser_operators() if operator["alive"]]
        if (req.force or browser_jobs_waiting > 0) and not online_browser_operators:
            self.watchdog_add_finding(
                findings,
                "browser_operator_offline",
                "fail",
                "warning",
                "Browser operator offline",
                "Browser jobs need a live browser/super-browser operator.",
                "watchdog_browser_operator",
                details={"waiting_jobs": browser_jobs_waiting, "known_operators": self.browser_operators()[:5], "forced": req.force},
            )
        if browser_jobs_failed:
            self.watchdog_add_finding(
                findings,
                "browser_jobs_failed",
                "fail",
                "warning",
                "Browser jobs failing",
                f"{browser_jobs_failed} browser job(s) are failed.",
                "watchdog_browser_jobs",
                details={"failed_jobs": browser_jobs_failed},
            )

        queue_checks = [
            ("outbox", "Outbox delivery failures", "outbox", "status='failed'", "watchdog_outbox"),
            ("outreach_jobs", "Outreach jobs failing", "outreach", "status='failed'", "watchdog_outreach"),
            ("integration_syncs", "Provider sync jobs failing", "integration_sync", "status='failed'", "watchdog_integration_sync"),
            ("crm_sync_jobs", "CRM sync jobs failing", "crm_sync", "status='failed'", "watchdog_crm_sync"),
        ]
        for table, title, key, where, source in queue_checks:
            failed = self.count(table, where)
            if failed:
                self.watchdog_add_finding(
                    findings,
                    f"{key}_failed",
                    "fail",
                    "warning",
                    title,
                    f"{failed} {key.replace('_', ' ')} item(s) are failed.",
                    source,
                    details={"table": table, "failed": failed},
                )

        stale_queue_checks = [
            ("outbox", "Outbox queue stuck", "outbox", "status='queued'", 60),
            ("outreach_jobs", "Outreach queue stuck", "outreach", "status='queued'", 60),
            ("integration_syncs", "Provider sync queue stuck", "integration_sync", "status='queued'", 60),
            ("crm_sync_jobs", "CRM sync queue stuck", "crm_sync", "status='queued'", 60),
            ("browser_jobs", "Browser job queue stuck", "browser_jobs", "status='queued'", 30),
        ]
        for table, title, key, where, stale_minutes in stale_queue_checks:
            age = self.oldest_age_minutes(table, where)
            if age is not None and age >= stale_minutes:
                self.watchdog_add_finding(
                    findings,
                    f"{key}_stale",
                    "fail",
                    "warning",
                    title,
                    f"Oldest queued {key.replace('_', ' ')} item is {age} minute(s) old.",
                    f"watchdog_{key}",
                    details={"table": table, "oldest_age_minutes": age, "stale_after_minutes": stale_minutes},
                )

        severity_order = {"info": 0, "warning": 1, "high": 2, "critical": 3}
        highest_severity = "info"
        for finding in findings:
            if severity_order[finding["severity"]] > severity_order[highest_severity]:
                highest_severity = finding["severity"]
        failures = [finding for finding in findings if finding["status"] == "fail"]
        status = "ok" if not failures else "critical" if highest_severity == "critical" else "degraded"

        incidents_opened = 0
        for finding in failures:
            if not finding.get("incident", True):
                continue
            _, opened = self.create_incident_once(
                IncidentCreateIn(
                    title=finding["title"],
                    severity=finding["severity"],
                    source=finding["source"],
                    client_id=finding["client_id"],
                    owner_agent_id=finding["owner_agent_id"],
                    details={**finding["details"], "watchdog_key": finding["key"]},
                )
            )
            if opened:
                incidents_opened += 1

        run_id = short_id()
        checked_at = now_iso()
        summary = (
            "Watchdog ok: all autonomous proof-of-life checks are healthy."
            if status == "ok"
            else f"Watchdog {status}: {len(failures)} issue(s), {incidents_opened} new incident(s)."
        )
        self.db.execute(
            """
            INSERT INTO watchdog_runs (
                id, status, severity, summary, checked_at, source,
                findings, incidents_opened, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                status,
                highest_severity,
                summary,
                checked_at,
                req.source,
                json_dumps(findings),
                incidents_opened,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.source, "run_watchdog", "watchdog_run", run_id, {"status": status, "severity": highest_severity, "findings": len(findings)})
        if status != "ok":
            self.queue_slack(
                summary,
                severity="critical" if highest_severity == "critical" else "warning",
                metadata={"watchdog_run_id": run_id, "status": status, "severity": highest_severity},
            )
        return dict(self.get_one("SELECT * FROM watchdog_runs WHERE id=?", (run_id,)))

    def autonomy_drill_check(
        self,
        checks: list[dict[str, Any]],
        key: str,
        label: str,
        passed: bool,
        required: bool,
        evidence: Any,
        recommendation: str = "",
    ) -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "status": "pass" if passed else "fail" if required else "warn",
                "required": required,
                "evidence": evidence,
                "recommendation": recommendation,
            }
        )

    def run_autonomy_drill(self, req: AutonomyDrillRunIn) -> dict[str, Any]:
        if not valid_actor(req.requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        drill_id = short_id()
        started_at = now_iso()
        checks: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {}

        try:
            control_room_before = self.control_room()
            evidence["control_room_before"] = {
                "status": control_room_before["status"],
                "attention_count": len(control_room_before["attention"]),
                "operator_controls": [control["action"] for control in control_room_before["operator_controls"]],
            }
            self.autonomy_drill_check(
                checks,
                "control_room",
                "Control room read model is available",
                bool(control_room_before.get("how_it_works") and control_room_before.get("operator_controls")),
                True,
                evidence["control_room_before"],
                "Fix /api/ops/control-room before relying on unattended operation.",
            )
        except Exception as exc:
            evidence["control_room_before_error"] = str(exc)
            self.autonomy_drill_check(checks, "control_room", "Control room read model is available", False, True, str(exc))

        try:
            cycle = self.start_operating_cycle(
                OperatingCycleStartIn(
                    requested_by="ceo",
                    force=req.force_cycle,
                    metadata={"source": "autonomy_drill", "drill_id": drill_id},
                )
            )
            evidence["operating_cycle"] = {"id": cycle["id"], "status": cycle["status"], "morning_report_id": cycle["morning_report_id"]}
            self.autonomy_drill_check(
                checks,
                "operating_cycle",
                "CEO operating cycle can start or resume",
                bool(cycle["id"] and cycle["morning_report_id"]),
                True,
                evidence["operating_cycle"],
                "Check CEO daily brief and operating cycle persistence.",
            )
        except Exception as exc:
            evidence["operating_cycle_error"] = str(exc)
            self.autonomy_drill_check(checks, "operating_cycle", "CEO operating cycle can start or resume", False, True, str(exc))

        try:
            memory = self.remember(
                title=f"Autonomy Drill - {today_key()}",
                body=(
                    "DialDesk autonomy drill executed. This proves the runtime can create durable memory, "
                    "mirror to Obsidian when configured, and queue Notion sync when configured."
                ),
                kind="autonomy_drill",
                actor=req.requested_by,
                client_id=DEFAULT_CLIENT_ID,
                tags=["autonomy", "drill", "proof"],
                source="autonomy_drill",
                metadata={"drill_id": drill_id, **req.metadata},
            )
            evidence["memory"] = {
                "id": memory["id"],
                "obsidian_path": memory["obsidian_path"],
                "notion_status": memory["notion_status"],
            }
            self.autonomy_drill_check(
                checks,
                "memory",
                "Durable memory write works",
                bool(memory["id"]),
                True,
                evidence["memory"],
                "Check SQLite path and memory_entries table.",
            )
            self.autonomy_drill_check(
                checks,
                "obsidian_memory",
                "Obsidian mirror is configured or explicitly absent",
                bool(memory["obsidian_path"] or not OBSIDIAN_VAULT_PATH),
                False,
                evidence["memory"],
                "Set OBSIDIAN_VAULT_PATH so operating memory mirrors to markdown.",
            )
            self.autonomy_drill_check(
                checks,
                "notion_memory",
                "Notion memory sync is configured or explicitly absent",
                bool(memory["notion_status"] == "queued" or not NOTION_DATABASE_ID),
                False,
                evidence["memory"],
                "Set NOTION_API_KEY and NOTION_DATABASE_ID so memory queues Notion sync.",
            )
        except Exception as exc:
            evidence["memory_error"] = str(exc)
            self.autonomy_drill_check(checks, "memory", "Durable memory write works", False, True, str(exc))

        if req.include_slack:
            try:
                slack = self.queue_slack(
                    f"Autonomy drill {drill_id} ran. Check /api/ops/autonomy-drills and /api/ops/control-room.",
                    severity="info",
                    client_id=DEFAULT_CLIENT_ID,
                    metadata={"drill_id": drill_id, "kind": "autonomy_drill"},
                )
                evidence["slack_outbox"] = {"id": slack["id"], "status": slack["status"], "destination": slack["destination"]}
                self.autonomy_drill_check(
                    checks,
                    "slack_visibility",
                    "Slack-visible outbox path works",
                    bool(slack["id"] and slack["destination"] == "slack"),
                    True,
                    evidence["slack_outbox"],
                    "Check outbox and SLACK_WEBHOOK_URL/SEND_OUTBOX configuration.",
                )
            except Exception as exc:
                evidence["slack_outbox_error"] = str(exc)
                self.autonomy_drill_check(checks, "slack_visibility", "Slack-visible outbox path works", False, True, str(exc))

        try:
            message = self.send_agent_message(
                AgentMessageCreateIn(
                    from_agent_id="ceo",
                    to_agent_id="cto",
                    subject=f"Autonomy drill proof {drill_id}",
                    body="Confirm runtime proof paths are visible in the control room and dashboard.",
                    message_type="update",
                    client_id=DEFAULT_CLIENT_ID,
                    priority=2,
                    metadata={"drill_id": drill_id, "source": "autonomy_drill"},
                )
            )
            evidence["agent_message"] = {"id": message["id"], "thread_id": message["thread_id"], "to_agent_id": message["to_agent_id"]}
            self.autonomy_drill_check(
                checks,
                "agent_coordination",
                "Agents can coordinate through durable messages",
                bool(message["id"] and message["thread_id"]),
                True,
                evidence["agent_message"],
                "Check agent_messages and coordination-room APIs.",
            )
        except Exception as exc:
            evidence["agent_message_error"] = str(exc)
            self.autonomy_drill_check(checks, "agent_coordination", "Agents can coordinate through durable messages", False, True, str(exc))

        if req.include_browser_job:
            try:
                browser_job = self.create_browser_job(
                    BrowserJobCreateIn(
                        objective=f"Autonomy drill proof {drill_id}: inspect DialDesk health endpoint",
                        url=f"{PUBLIC_URL.rstrip('/')}/health",
                        requested_by=req.requested_by,
                        priority=3,
                        metadata={"drill_id": drill_id, "source": "autonomy_drill"},
                    )
                )
                evidence["browser_job"] = {"id": browser_job["id"], "status": browser_job["status"], "url": browser_job["url"]}
                self.autonomy_drill_check(
                    checks,
                    "browser_queue",
                    "Browser/super-browser queue accepts work",
                    bool(browser_job["id"] and browser_job["status"] == "queued"),
                    True,
                    evidence["browser_job"],
                    "Start the browser operator if queued jobs do not complete.",
                )
            except Exception as exc:
                evidence["browser_job_error"] = str(exc)
                self.autonomy_drill_check(checks, "browser_queue", "Browser/super-browser queue accepts work", False, True, str(exc))

        try:
            health = self.run_health_check("autonomy_drill")
            evidence["health_check"] = {"id": health["id"], "status": health["status"], "summary": health["summary"]}
            self.autonomy_drill_check(
                checks,
                "health_check",
                "Production health check can run",
                health["status"] in {"ok", "degraded"},
                False,
                evidence["health_check"],
                "Resolve critical health findings before 24/7 unattended operation.",
            )
        except Exception as exc:
            evidence["health_check_error"] = str(exc)
            self.autonomy_drill_check(checks, "health_check", "Production health check can run", False, False, str(exc))

        try:
            watchdog = self.run_watchdog(WatchdogRunIn(source="autonomy_drill", force=False, metadata={"drill_id": drill_id}))
            evidence["watchdog"] = {"id": watchdog["id"], "status": watchdog["status"], "severity": watchdog["severity"], "summary": watchdog["summary"]}
            self.autonomy_drill_check(
                checks,
                "watchdog",
                "Autonomous watchdog can run",
                bool(watchdog["id"] and watchdog["status"] in {"ok", "degraded", "critical"}),
                True,
                evidence["watchdog"],
                "Fix watchdog before trusting autonomous monitoring.",
            )
            self.autonomy_drill_check(
                checks,
                "watchdog_clean",
                "Watchdog reports no critical blockers",
                watchdog["status"] != "critical",
                False,
                evidence["watchdog"],
                "Inspect /api/ops/watchdog and open incidents.",
            )
        except Exception as exc:
            evidence["watchdog_error"] = str(exc)
            self.autonomy_drill_check(checks, "watchdog", "Autonomous watchdog can run", False, True, str(exc))

        try:
            readiness = self.run_readiness_check(
                ReadinessCheckCreateIn(profile="production", requested_by="cto", metadata={"source": "autonomy_drill", "drill_id": drill_id})
            )
            evidence["production_readiness"] = {
                "id": readiness["id"],
                "status": readiness["status"],
                "score": readiness["score"],
                "summary": readiness["summary"],
            }
            self.autonomy_drill_check(
                checks,
                "production_readiness",
                "Production readiness has been evaluated",
                readiness["status"] in {"ready", "warning"},
                False,
                evidence["production_readiness"],
                "Clear production readiness blockers before VPS/24/7 launch.",
            )
        except Exception as exc:
            evidence["production_readiness_error"] = str(exc)
            self.autonomy_drill_check(checks, "production_readiness", "Production readiness has been evaluated", False, False, str(exc))

        required_failures = [check for check in checks if check["required"] and check["status"] == "fail"]
        warnings = [check for check in checks if check["status"] == "warn"]
        score = max(0, min(100, 100 - len(required_failures) * 25 - len(warnings) * 5))
        status = "failed" if required_failures else "warning" if warnings else "passed"
        summary = (
            f"Autonomy drill {status}: {len(checks)} check(s), "
            f"{len(required_failures)} required failure(s), {len(warnings)} warning(s)."
        )
        completed_at = now_iso()
        self.db.execute(
            """
            INSERT INTO autonomy_drills (
                id, status, score, summary, requested_by, started_at, completed_at,
                checks, evidence, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                drill_id,
                status,
                score,
                summary,
                req.requested_by,
                started_at,
                completed_at,
                json_dumps(checks),
                json_dumps(evidence),
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "run_autonomy_drill", "autonomy_drill", drill_id, {"status": status, "score": score})
        self.queue_slack(
            summary,
            severity="critical" if status == "failed" else "warning" if status == "warning" else "info",
            metadata={"drill_id": drill_id, "status": status, "score": score},
        )
        return dict(self.get_one("SELECT * FROM autonomy_drills WHERE id=?", (drill_id,)))

    def launch_gate_check(
        self,
        checks: list[dict[str, Any]],
        key: str,
        label: str,
        passed: bool,
        required: bool,
        evidence: Any,
        recommendation: str = "",
    ) -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "status": "pass" if passed else "fail" if required else "warn",
                "required": required,
                "evidence": evidence,
                "recommendation": recommendation,
            }
        )

    def vps_launch_commands(self) -> list[dict[str, str]]:
        return [
            {
                "step": "Bootstrap VPS services",
                "command": "APP_DIR=/opt/dial-desk-swarm SERVICE_USER=$(id -un) bash scripts/vps_bootstrap.sh",
                "why": "Installs dependencies, syncs the repo, creates persistent dirs, installs API/browser systemd services, and runs the launch gate.",
            },
            {
                "step": "Create persistent data dirs",
                "command": "mkdir -p data/backups data/browser-artifacts",
                "why": "Keeps SQLite, backups, and browser artifacts on disk across restarts.",
            },
            {
                "step": "Install API service",
                "command": "sudo cp systemd/dial-desk-swarm.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now dial-desk-swarm",
                "why": "Manual fallback for running the autonomous FastAPI runtime 24/7 with restart policy.",
            },
            {
                "step": "Install browser operator service",
                "command": "sudo cp systemd/dial-desk-browser-operator.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now dial-desk-browser-operator",
                "why": "Manual fallback for running the browser/super-browser worker 24/7 beside the API.",
            },
            {
                "step": "Verify control room",
                "command": "BASE_URL=http://127.0.0.1:8080 bash scripts/ops_doctor.sh",
                "why": "Confirms health, dashboard controls, auth, control room, readiness, launch gate, memory, browser operator visibility, coordination, and outbox visibility.",
            },
            {
                "step": "Prove full business stack",
                "command": "BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/business_stack_probe.sh",
                "why": "Runs the deploy doctor plus memory, Slack, and browser stack probes as one go/no-go command.",
            },
            {
                "step": "Run launch gate",
                "command": "BASE_URL=http://127.0.0.1:8080 DIALDESK_LAUNCH_PROFILE=vps bash scripts/ops_doctor.sh",
                "why": "Records launch evidence and blockers through the authenticated operator control room path.",
            },
            {
                "step": "Run autonomy proof",
                "command": "curl -s -X POST http://127.0.0.1:8080/api/ops/autonomy-drills/run -H 'Content-Type: application/json' -d '{\"requested_by\":\"ceo\"}' | jq .summary",
                "why": "Proves the business can operate through the real runtime paths.",
            },
            {
                "step": "Prove memory stack",
                "command": "BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh",
                "why": "Writes operating memory, verifies SQLite queryability, checks Obsidian markdown when configured, and confirms Notion outbox queueing when configured.",
            },
            {
                "step": "Prove Slack stack",
                "command": "BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh",
                "why": "Queues a Slack-visible alert, verifies outbox visibility, and proves JSON plus slash-command control paths.",
            },
            {
                "step": "Prove browser stack",
                "command": "BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/browser_stack_probe.sh",
                "why": "Queues browser work, runs the super-browser operator once, verifies operator heartbeat, and confirms the result is written to memory.",
            },
        ]

    def run_launch_gate(self, req: LaunchGateRunIn) -> dict[str, Any]:
        if not valid_actor(req.requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        profile = req.profile.lower().strip()
        if profile not in {"vps", "railway", "local"}:
            raise HTTPException(status_code=400, detail="profile must be vps, railway, or local")

        checks: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {}
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        api_service = repo_root / "systemd" / "dial-desk-swarm.service"
        browser_service = repo_root / "systemd" / "dial-desk-browser-operator.service"
        vps_bootstrap = repo_root / "scripts" / "vps_bootstrap.sh"
        business_stack_probe = repo_root / "scripts" / "business_stack_probe.sh"
        ops_doctor = repo_root / "scripts" / "ops_doctor.sh"
        memory_stack_probe = repo_root / "scripts" / "memory_stack_probe.sh"
        slack_stack_probe = repo_root / "scripts" / "slack_stack_probe.sh"
        browser_stack_probe = repo_root / "scripts" / "browser_stack_probe.sh"
        docker_compose = repo_root / "docker-compose.yml"
        api_service_text = api_service.read_text() if api_service.exists() else ""
        browser_service_text = browser_service.read_text() if browser_service.exists() else ""
        vps_bootstrap_text = vps_bootstrap.read_text() if vps_bootstrap.exists() else ""
        business_stack_probe_text = business_stack_probe.read_text() if business_stack_probe.exists() else ""
        ops_doctor_text = ops_doctor.read_text() if ops_doctor.exists() else ""
        memory_stack_probe_text = memory_stack_probe.read_text() if memory_stack_probe.exists() else ""
        slack_stack_probe_text = slack_stack_probe.read_text() if slack_stack_probe.exists() else ""
        browser_stack_probe_text = browser_stack_probe.read_text() if browser_stack_probe.exists() else ""
        docker_compose_text = docker_compose.read_text() if docker_compose.exists() else ""

        controls = self.runtime_control_map()
        core_components = ["scheduler", "ceo_decision_review", "worker", "outbox", "browser_recovery", "workflow", "scheduled_ops", "self_audit", "watchdog"]
        enabled_core = {component: bool(controls.get(component, {"enabled": 1})["enabled"]) for component in core_components}
        latest_backup = self.get_one("SELECT * FROM backups WHERE status='completed' ORDER BY completed_at DESC LIMIT 1")
        latest_health = self.get_one("SELECT * FROM health_checks ORDER BY checked_at DESC LIMIT 1")
        latest_watchdog = self.get_one("SELECT * FROM watchdog_runs ORDER BY checked_at DESC LIMIT 1")
        latest_drill = self.get_one("SELECT * FROM autonomy_drills ORDER BY completed_at DESC LIMIT 1")
        latest_readiness = self.get_one("SELECT * FROM readiness_checks WHERE profile='production' ORDER BY checked_at DESC LIMIT 1")
        browser_operators = self.browser_operators()
        online_browser_operators = [operator for operator in browser_operators if operator["alive"]]
        memory = self.memory_health()
        providers = provider_config_status()

        evidence.update(
            {
                "profile": profile,
                "repo_root": str(repo_root),
                "db_path": self.db_path,
                "backup_dir": BACKUP_DIR,
                "public_url": PUBLIC_URL,
                "core_components": enabled_core,
                "provider_config": providers,
            }
        )

        self.launch_gate_check(
            checks,
            "service_manifests",
            "VPS service manifests exist",
            api_service.exists() and browser_service.exists(),
            profile == "vps",
            {
                "api_service": str(api_service),
                "browser_service": str(browser_service),
                "bootstrap": str(vps_bootstrap),
                "business_stack_probe": str(business_stack_probe),
                "ops_doctor": str(ops_doctor),
                "memory_stack_probe": str(memory_stack_probe),
                "slack_stack_probe": str(slack_stack_probe),
                "browser_stack_probe": str(browser_stack_probe),
            },
            "Keep both systemd service files in the repo and install them on the VPS.",
        )
        self.launch_gate_check(
            checks,
            "api_service_hardened",
            "API service loads .env and restarts 24/7",
            bool(
                "EnvironmentFile=-/opt/dial-desk-swarm/.env" in api_service_text
                and "Restart=always" in api_service_text
                and "DIALDESK_DB=/opt/dial-desk-swarm/data/dialdesk.sqlite3" in api_service_text
                and "DIALDESK_BACKUP_DIR=/opt/dial-desk-swarm/data/backups" in api_service_text
                and "uvicorn src.coordinator.main:app" in api_service_text
            ),
            profile == "vps",
            {"api_service": str(api_service), "checks": ["EnvironmentFile", "Restart=always", "DIALDESK_DB", "DIALDESK_BACKUP_DIR", "uvicorn"]},
            "Update systemd/dial-desk-swarm.service so the API process loads .env, uses persistent paths, and restarts always.",
        )
        self.launch_gate_check(
            checks,
            "browser_service_hardened",
            "Browser operator service loads .env and restarts 24/7",
            bool(
                "EnvironmentFile=-/opt/dial-desk-swarm/.env" in browser_service_text
                and "Restart=always" in browser_service_text
                and "python -m src.browser_operator" in browser_service_text
                and "DIALDESK_API_BASE_URL=http://127.0.0.1:8080" in browser_service_text
                and "DIALDESK_BROWSER_ARTIFACT_DIR=/opt/dial-desk-swarm/data/browser-artifacts" in browser_service_text
            ),
            profile == "vps",
            {"browser_service": str(browser_service), "checks": ["EnvironmentFile", "Restart=always", "browser_operator", "API_BASE_URL", "artifact_dir"]},
            "Update systemd/dial-desk-browser-operator.service so the browser operator runs beside the API and restarts always.",
        )
        self.launch_gate_check(
            checks,
            "vps_bootstrap_script",
            "VPS bootstrap installs both 24/7 services",
            bool(
                vps_bootstrap.exists()
                and "dial-desk-swarm.service" in vps_bootstrap_text
                and "dial-desk-browser-operator.service" in vps_bootstrap_text
                and "systemctl enable --now dial-desk-swarm" in vps_bootstrap_text
                and "systemctl enable --now dial-desk-browser-operator" in vps_bootstrap_text
                and "scripts/business_stack_probe.sh" in vps_bootstrap_text
                and "DIALDESK_LAUNCH_PROFILE=vps" in vps_bootstrap_text
            ),
            profile == "vps",
            str(vps_bootstrap),
            "Use scripts/vps_bootstrap.sh so the API and browser operator are installed together and verified.",
        )
        self.launch_gate_check(
            checks,
            "business_stack_probe_script",
            "Business stack probe runs all deploy proofs",
            bool(
                business_stack_probe.exists()
                and "scripts/ops_doctor.sh" in business_stack_probe_text
                and "scripts/memory_stack_probe.sh" in business_stack_probe_text
                and "scripts/slack_stack_probe.sh" in business_stack_probe_text
                and "scripts/browser_stack_probe.sh" in business_stack_probe_text
                and "BUSINESS_PROBE_INCLUDE_BROWSER" in business_stack_probe_text
                and "/api/ops/proof-runs" in business_stack_probe_text
            ),
            profile == "vps",
            str(business_stack_probe),
            "Run BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/business_stack_probe.sh for a single deploy proof.",
        )
        self.launch_gate_check(
            checks,
            "ops_doctor_script",
            "Ops doctor verifies the deployed control plane",
            bool(
                ops_doctor.exists()
                and "/api/ops/control-room" in ops_doctor_text
                and "/api/ops/control-room/action" in ops_doctor_text
                and "/api/memory/health" in ops_doctor_text
                and "/api/browser/operators" in ops_doctor_text
                and "/api/agents/coordination" in ops_doctor_text
                and "/api/ops/proof-pack" in ops_doctor_text
                and "Authorization" in ops_doctor_text
            ),
            profile == "vps",
            str(ops_doctor),
            "Keep scripts/ops_doctor.sh available so a VPS can prove health, auth, dashboard, memory, browser, Slack/outbox, and coordination after deploy.",
        )
        self.launch_gate_check(
            checks,
            "memory_stack_probe_script",
            "Memory stack probe can prove SQLite, Obsidian, and Notion queueing",
            bool(
                memory_stack_probe.exists()
                and "/api/memory" in memory_stack_probe_text
                and "/api/memory/health" in memory_stack_probe_text
                and "/api/outbox" in memory_stack_probe_text
                and "obsidian_path" in memory_stack_probe_text
                and "APP_TOKEN" in memory_stack_probe_text
            ),
            profile == "vps",
            str(memory_stack_probe),
            "Run BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh after configuring OBSIDIAN_VAULT_PATH and Notion credentials.",
        )
        self.launch_gate_check(
            checks,
            "slack_stack_probe_script",
            "Slack stack probe can prove outbox and command control",
            bool(
                slack_stack_probe.exists()
                and "/api/slack/notify" in slack_stack_probe_text
                and "/api/slack/command" in slack_stack_probe_text
                and "/slack/command" in slack_stack_probe_text
                and "/api/outbox" in slack_stack_probe_text
                and "SLACK_SIGNING_SECRET" in slack_stack_probe_text
                and "APP_TOKEN" in slack_stack_probe_text
            ),
            profile == "vps",
            str(slack_stack_probe),
            "Run BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh after configuring Slack webhook/signing credentials.",
        )
        self.launch_gate_check(
            checks,
            "browser_stack_probe_script",
            "Browser stack probe can prove API-to-operator-to-memory",
            bool(
                browser_stack_probe.exists()
                and "/api/browser/jobs" in browser_stack_probe_text
                and "src.browser_operator" in browser_stack_probe_text
                and "/api/browser/operators" in browser_stack_probe_text
                and "/api/memory" in browser_stack_probe_text
                and "APP_TOKEN" in browser_stack_probe_text
            ),
            profile == "vps",
            str(browser_stack_probe),
            "Run BASE_URL=http://127.0.0.1:8080 bash scripts/browser_stack_probe.sh after starting the API and browser operator.",
        )
        self.launch_gate_check(
            checks,
            "docker_compose",
            "Docker Compose includes API and browser operator services",
            docker_compose.exists() and "browser-operator" in docker_compose_text and "restart: unless-stopped" in docker_compose_text,
            profile in {"railway", "local"},
            str(docker_compose),
            "Use docker-compose with the browser profile for local/container 24/7 operation.",
        )
        self.launch_gate_check(
            checks,
            "persistent_database",
            "Database path is persistent",
            pathlib.Path(self.db_path).exists() and self._db_path_is_persistent(),
            profile != "local",
            self.db_path,
            "Set DIALDESK_DB to a persistent VPS path such as /opt/dial-desk-swarm/data/dialdesk.sqlite3.",
        )
        self.launch_gate_check(
            checks,
            "persistent_backup_dir",
            "Backup directory is persistent",
            self._path_is_persistent(BACKUP_DIR) and (pathlib.Path(BACKUP_DIR).exists() or pathlib.Path(BACKUP_DIR).parent.exists()),
            profile != "local",
            BACKUP_DIR,
            "Set DIALDESK_BACKUP_DIR to a persistent VPS path.",
        )
        self.launch_gate_check(
            checks,
            "app_token",
            "Operator API token is configured",
            bool(APP_TOKEN),
            profile != "local",
            "configured" if APP_TOKEN else "missing",
            "Set APP_TOKEN before exposing /api/*, /messages, or /tasks/*.",
        )
        self.launch_gate_check(
            checks,
            "core_runtime_controls",
            "Core autonomous components are enabled",
            all(enabled_core.values()),
            True,
            enabled_core,
            "Resume paused runtime controls from /api/ops/runtime-controls or /dialdesk resume-runtime.",
        )
        self.launch_gate_check(
            checks,
            "global_kill_switch",
            "Global kill switch is off",
            not self.is_kill_switch_active(),
            True,
            "active" if self.is_kill_switch_active() else "off",
            "Turn off the global kill switch only when operations should run.",
        )
        self.launch_gate_check(
            checks,
            "memory_obsidian",
            "Obsidian memory mirror is configured",
            bool(OBSIDIAN_VAULT_PATH),
            False,
            {"configured": bool(OBSIDIAN_VAULT_PATH), "written": memory["obsidian"]["written"]},
            "Set OBSIDIAN_VAULT_PATH to mirror operating memory into markdown.",
        )
        self.launch_gate_check(
            checks,
            "memory_notion",
            "Notion memory sync is configured",
            bool(NOTION_API_KEY and NOTION_DATABASE_ID),
            False,
            {"configured": bool(NOTION_API_KEY and NOTION_DATABASE_ID), "notion": memory["notion"]},
            "Set NOTION_API_KEY and NOTION_DATABASE_ID to queue memory into Notion.",
        )
        self.launch_gate_check(
            checks,
            "slack_visibility",
            "Slack visibility is configured",
            bool(SLACK_WEBHOOK_URL and SLACK_SIGNING_SECRET),
            False,
            {"webhook": bool(SLACK_WEBHOOK_URL), "signing_secret": bool(SLACK_SIGNING_SECRET), "send_outbox": SEND_OUTBOX},
            "Set SLACK_WEBHOOK_URL, SLACK_SIGNING_SECRET, and DIALDESK_SEND_OUTBOX=1 for live Slack visibility.",
        )
        self.launch_gate_check(
            checks,
            "browser_operator",
            "Browser/super-browser operator is online",
            bool(online_browser_operators),
            profile != "local",
            {"online": len(online_browser_operators), "known": browser_operators[:5]},
            "Start the browser operator service and confirm /api/browser/operators shows it alive.",
        )
        self.launch_gate_check(
            checks,
            "latest_backup",
            "Successful backup exists",
            bool(latest_backup and pathlib.Path(latest_backup["path"]).exists()),
            profile != "local",
            dict(latest_backup) if latest_backup else "none",
            "Run POST /api/ops/backups/create before launch.",
        )
        self.launch_gate_check(
            checks,
            "latest_health_check",
            "Latest health check is usable",
            bool(latest_health and latest_health["status"] in {"ok", "degraded"}),
            profile != "local",
            dict(latest_health) if latest_health else "none",
            "Run POST /api/ops/health-checks/run and resolve critical findings.",
        )
        self.launch_gate_check(
            checks,
            "latest_watchdog",
            "Watchdog has recent evidence",
            bool(latest_watchdog and latest_watchdog["status"] in {"ok", "degraded"}),
            profile != "local",
            dict(latest_watchdog) if latest_watchdog else "none",
            "Run POST /api/ops/watchdog/run.",
        )
        self.launch_gate_check(
            checks,
            "latest_autonomy_drill",
            "Autonomy proof has been run",
            bool(latest_drill and latest_drill["status"] in {"passed", "warning"}),
            profile != "local",
            dict(latest_drill) if latest_drill else "none",
            "Run POST /api/ops/autonomy-drills/run.",
        )
        self.launch_gate_check(
            checks,
            "production_readiness",
            "Production readiness has been checked",
            bool(latest_readiness and latest_readiness["status"] in {"ready", "warning"}),
            False,
            dict(latest_readiness) if latest_readiness else "none",
            "Run POST /api/ops/readiness/run with profile production.",
        )
        self.launch_gate_check(
            checks,
            "live_outreach_safe_default",
            "Live outreach is intentionally controlled",
            not SEND_OUTREACH or (providers["sendivo"]["sms_send"] and providers["smartlead"]["email_send"]),
            True,
            {"send_outreach": SEND_OUTREACH, "sendivo_sms": providers["sendivo"]["sms_send"], "smartlead_email": providers["smartlead"]["email_send"]},
            "Keep DIALDESK_SEND_OUTREACH=0 until provider credentials, suppression, and approvals are ready.",
        )

        required_failures = [check for check in checks if check["required"] and check["status"] == "fail"]
        warnings = [check for check in checks if check["status"] == "warn"]
        score = max(0, min(100, 100 - len(required_failures) * 25 - len(warnings) * 5))
        status = "blocked" if required_failures else "warning" if warnings else "ready"
        summary = f"{profile} launch gate {status}: {len(required_failures)} blocker(s), {len(warnings)} warning(s), score {score}."
        run_id = short_id()
        commands = self.vps_launch_commands()
        self.db.execute(
            """
            INSERT INTO launch_gate_runs (
                id, profile, status, score, summary, requested_by, checked_at,
                checks, commands, evidence, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                profile,
                status,
                score,
                summary,
                req.requested_by,
                now_iso(),
                json_dumps(checks),
                json_dumps(commands),
                json_dumps(evidence),
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "run_launch_gate", "launch_gate", run_id, {"profile": profile, "status": status, "score": score})
        if status == "blocked":
            self.create_task(
                f"Clear {profile} launch blockers",
                "cto",
                priority=1,
                prompt=summary,
                metadata={"launch_gate_id": run_id, "required_failures": required_failures},
            )
            self.queue_slack(summary, severity="warning", metadata={"launch_gate_id": run_id, "profile": profile})
        return dict(self.get_one("SELECT * FROM launch_gate_runs WHERE id=?", (run_id,)))

    def record_proof_run(self, req: ProofRunRecordIn) -> dict[str, Any]:
        proof_type = re.sub(r"[^a-zA-Z0-9_-]+", "_", req.proof_type.strip().lower()).strip("_") or "business_stack"
        status = req.status.strip().lower()
        if status not in {"passed", "warning", "failed", "running", "skipped"}:
            raise HTTPException(status_code=400, detail="status must be passed, warning, failed, running, or skipped")
        run_id = short_id()
        started_at = req.started_at or now_iso()
        completed_at = req.completed_at or now_iso()
        summary = req.summary.strip() or f"{proof_type} proof {status}."
        self.db.execute(
            """
            INSERT INTO proof_runs (
                id, proof_type, status, summary, source, started_at, completed_at,
                duration_ms, command, checks, output, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                proof_type,
                status,
                summary,
                req.source,
                started_at,
                completed_at,
                max(0, int(req.duration_ms)),
                req.command,
                json_dumps(req.checks),
                req.output[-12000:],
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.source or "script", "record_proof_run", "proof_run", run_id, {"proof_type": proof_type, "status": status})
        if status == "failed":
            try:
                self.create_task(
                    f"Investigate failed {proof_type} proof",
                    "cto",
                    priority=1,
                    prompt=summary,
                    metadata={"proof_run_id": run_id, "source": req.source},
                )
            except HTTPException:
                self.queue_slack(summary, severity="warning", metadata={"proof_run_id": run_id, "proof_type": proof_type})
        return dict(self.get_one("SELECT * FROM proof_runs WHERE id=?", (run_id,)))

    def oldest_age_minutes(self, table: str, where: str) -> float | None:
        row = self.get_one(f"SELECT created_at FROM {table} WHERE {where} ORDER BY created_at ASC LIMIT 1")
        if not row:
            return None
        return round(iso_age_seconds(row["created_at"]) / 60, 2)

    def latest_backup_age_minutes(self) -> float | None:
        row = self.get_one("SELECT completed_at FROM backups WHERE status='completed' ORDER BY completed_at DESC LIMIT 1")
        if not row or not row["completed_at"]:
            return None
        return round(iso_age_seconds(row["completed_at"]) / 60, 2)

    def create_agent_standup(self, req: AgentStandupCreateIn) -> dict[str, Any]:
        if not valid_actor(req.requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        include_agents = req.include_agents or list(self.agents.keys())
        unknown = [agent_id for agent_id in include_agents if agent_id not in self.agents]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown agents: {', '.join(unknown)}")
        thread_id = short_id()
        messages: list[dict[str, Any]] = []
        kickoff = self.send_agent_message(
            AgentMessageCreateIn(
                from_agent_id=req.requested_by,
                to_agent_id="ceo",
                subject=req.topic,
                body="Standup opened. Each operator reports current state, blockers, and next action.",
                message_type="update",
                client_id=req.client_id,
                priority=2,
                thread_id=thread_id,
                metadata={"standup": True, **req.metadata},
            )
        )
        messages.append(kickoff)
        for agent_id in include_agents:
            summary = self.agent_standup_summary(agent_id, req.client_id)
            message = self.send_agent_message(
                AgentMessageCreateIn(
                    from_agent_id=agent_id,
                    to_agent_id="ceo",
                    subject=f"{AGENTS[agent_id].name} standup",
                    body=summary,
                    message_type="update",
                    client_id=req.client_id,
                    priority=2,
                    thread_id=thread_id,
                    metadata={"standup": True, **req.metadata},
                )
            )
            messages.append(message)
            self.record_heartbeat(
                HeartbeatIn(
                    agent_id=agent_id,
                    client_id=req.client_id,
                    status="alive",
                    summary=summary,
                    metrics=self.agent_workload(agent_id, req.client_id),
                )
            )
        body = "\n\n".join(f"{msg['from_agent_id']}: {msg['body']}" for msg in messages[1:])
        memory = self.remember(
            title=f"Agent Standup - {today_key()}",
            body=body,
            kind="standup",
            actor=req.requested_by,
            client_id=req.client_id,
            tags=["agents", "standup", "coordination"],
            source="agent_standup",
            metadata={"thread_id": thread_id, "message_count": len(messages), **req.metadata},
        )
        self.queue_slack(
            f"Agent standup complete: {req.topic}\nThread: {thread_id}\nMessages: {len(messages)}",
            severity="info",
            client_id=req.client_id,
            metadata={"thread_id": thread_id, "memory_id": memory["id"]},
        )
        return {"thread_id": thread_id, "messages": messages, "memory": memory}

    def agent_workload(self, agent_id: str, client_id: str = DEFAULT_CLIENT_ID) -> dict[str, Any]:
        return {
            "pending_tasks": self.count("tasks", "client_id=? AND agent_id=? AND status='pending'", (client_id, agent_id)),
            "working_tasks": self.count("tasks", "client_id=? AND agent_id=? AND status='working'", (client_id, agent_id)),
            "unread_messages": self.count("agent_messages", "client_id=? AND to_agent_id=? AND status='unread'", (client_id, agent_id)),
            "completed_today": self.count(
                "tasks",
                "client_id=? AND agent_id=? AND status='completed' AND date(completed_at)=date('now')",
                (client_id, agent_id),
            ),
        }

    def agent_standup_summary(self, agent_id: str, client_id: str = DEFAULT_CLIENT_ID) -> str:
        workload = self.agent_workload(agent_id, client_id)
        agent = self.agents[agent_id]
        focus = autonomous_task_result(f"{agent.name} standup", agent_id)
        return (
            f"Status: {agent.status}. Current focus: {agent.current_task or 'none'}.\n"
            f"Open work: {workload['pending_tasks']} pending, {workload['working_tasks']} working, "
            f"{workload['unread_messages']} unread message(s).\n"
            f"Completed today: {workload['completed_today']}.\n"
            f"Next action: {focus}"
        )

    def handle_slack_command(self, req: SlackCommandIn) -> dict[str, Any]:
        command = req.command.strip().lower().lstrip("/")
        parts = [p for p in req.text.strip().split() if p]
        response: dict[str, Any]

        if command in {"dialdesk", "crew-status", "status"}:
            metrics = self.metrics()
            open_tasks = self.count("tasks", "status IN ('pending','working')")
            pending_approvals = self.count("approvals", "status='pending'")
            response = {
                "text": (
                    f"DialDesk status: {metrics['active_clients']} active client(s), "
                    f"{open_tasks} open task(s), {pending_approvals} pending approval(s)."
                ),
                "metrics": metrics,
            }
        elif command == "budget-remaining":
            agent_id = parts[0] if parts else "ceo"
            row = self.get_one("SELECT * FROM agent_budgets WHERE client_id=? AND agent_id=?", (req.client_id, agent_id))
            if not row:
                raise HTTPException(status_code=404, detail="Budget not found")
            remaining = float(row["monthly_budget"]) - float(row["spent"])
            response = {"text": f"{agent_id} budget remaining: ${remaining:,.2f}", "budget": dict(row), "remaining": remaining}
        elif command == "pause-agent":
            if not parts:
                raise HTTPException(status_code=400, detail="pause-agent requires an agent id")
            agent_id = parts[0]
            reason = " ".join(parts[1:]) or f"Paused by {req.user}"
            response = {"text": f"{agent_id} paused.", "agent": self.pause_agent(agent_id, reason, req.client_id)}
        elif command == "resume-agent":
            if not parts:
                raise HTTPException(status_code=400, detail="resume-agent requires an agent id")
            agent_id = parts[0]
            reason = " ".join(parts[1:]) or f"Resumed by {req.user}"
            response = {"text": f"{agent_id} resumed.", "agent": self.resume_agent(agent_id, reason, req.client_id)}
        elif command == "approve-action":
            if not parts:
                raise HTTPException(status_code=400, detail="approve-action requires an approval id")
            response = {"text": f"Approval {parts[0]} approved.", "approval": self.decide_approval(parts[0], "approved", req.user, "Approved from Slack command")}
        elif command == "reject-action":
            if not parts:
                raise HTTPException(status_code=400, detail="reject-action requires an approval id")
            response = {"text": f"Approval {parts[0]} rejected.", "approval": self.decide_approval(parts[0], "rejected", req.user, "Rejected from Slack command")}
        elif command == "kill-switch":
            mode = parts[0].lower() if parts else ""
            if mode not in {"on", "off"}:
                raise HTTPException(status_code=400, detail="kill-switch requires on or off")
            row = self.set_kill_switch(
                KillSwitchIn(
                    active=mode == "on",
                    scope="global",
                    reason=" ".join(parts[1:]) or f"Set from Slack by {req.user}",
                    set_by=req.user,
                )
            )
            response = {"text": f"Global kill switch {mode}.", "kill_switch": row}
        elif command in {"standup", "agent-standup"}:
            standup = self.create_agent_standup(
                AgentStandupCreateIn(
                    client_id=req.client_id,
                    requested_by="ceo",
                    topic=req.text.strip() or "Slack-requested agent standup",
                    metadata={"requested_from": "slack", "user": req.user},
                )
            )
            response = {"text": f"Agent standup complete. Thread {standup['thread_id']}.", "standup": standup}
        elif command in {"coordination", "coordination-room", "room"}:
            room = self.coordination_room(req.client_id)
            unread = sum(agent["workload"]["unread_messages"] for agent in room["agents"].values())
            top_thread = room["threads"][0] if room["threads"] else None
            thread_text = (
                f" Top thread {top_thread['thread_id']} has {top_thread['unread_count']} unread message(s)."
                if top_thread
                else " No active threads yet."
            )
            response = {
                "text": (
                    f"Coordination room: {len(room['agents'])} agent(s), "
                    f"{len(room['threads'])} thread(s), {unread} unread agent message(s), "
                    f"{len(room['justin_inbox'])} waiting on Justin.{thread_text}"
                ),
                "coordination_room": room,
            }
        elif command in {"control-room", "ops-room", "under-the-hood"}:
            room = self.control_room()
            attention_preview = "; ".join(f"{item['severity']}: {item['title']}" for item in room["attention"][:3])
            response = {
                "text": (
                    f"Control room {room['status']}: {room['summary']}"
                    + (f" Attention: {attention_preview}" if attention_preview else "")
                ),
                "control_room": room,
            }
        elif command == "inbox":
            agent_id = parts[0] if parts else "justin"
            messages = self.agent_inbox(agent_id, client_id=req.client_id, status="unread", limit=10)
            preview = "; ".join(f"{m['from_agent_id']}: {m['subject']}" for m in messages[:3])
            response = {
                "text": f"{agent_id} inbox: {len(messages)} unread message(s)." + (f" {preview}" if preview else ""),
                "messages": messages,
            }
        elif command in {"decisions", "ceo-decisions"}:
            decisions = self.rows("ceo_decisions", "client_id=? ORDER BY created_at DESC LIMIT 5", (req.client_id,))
            preview = "; ".join(f"{d['status']}: {d['summary']}" for d in decisions[:3])
            response = {
                "text": f"CEO decisions: {len(decisions)} recent decision(s)." + (f" {preview}" if preview else ""),
                "decisions": decisions,
            }
        elif command in {"decision-review", "ceo-review"}:
            review = self.run_ceo_decision_review(
                CeoDecisionReviewIn(client_id=req.client_id, source="slack", force="force" in parts, metadata={"user": req.user})
            )
            response = {
                "text": f"CEO decision review created {review['decisions_created']} decision(s).",
                "review": review,
            }
        elif command in {"send-agent", "ask-agent"}:
            if len(parts) < 2:
                raise HTTPException(status_code=400, detail=f"{command} requires an agent id and message")
            to_agent_id = parts[0]
            body = " ".join(parts[1:])
            msg = self.send_agent_message(
                AgentMessageCreateIn(
                    from_agent_id="justin",
                    to_agent_id=to_agent_id,
                    subject=f"Slack {command} from {req.user}",
                    body=body,
                    message_type="question" if command == "ask-agent" else "update",
                    client_id=req.client_id,
                    priority=1 if command == "ask-agent" else 2,
                    metadata={"requested_from": "slack", "user": req.user},
                )
            )
            response = {"text": f"Message sent to {to_agent_id}.", "message": msg}
        elif command in {"health", "run-health"}:
            check = self.run_health_check("slack")
            response = {"text": f"Health check {check['status']}: {check['summary']}", "health_check": check}
        elif command in {"backup", "backup-now"}:
            backup = self.create_backup(BackupCreateIn(reason="slack", requested_by="cto", metadata={"user": req.user}))
            response = {"text": f"Backup {backup['status']}: {backup['path']}", "backup": backup}
        elif command == "incidents":
            incidents = self.rows("incidents", "status='open' ORDER BY created_at DESC LIMIT 10")
            response = {"text": f"{len(incidents)} open incident(s).", "incidents": incidents}
        elif command in {"memory", "memory-health"}:
            health = self.memory_health()
            response = {
                "text": (
                    f"Memory: {health['total_entries']} entry(s). "
                    f"Obsidian written: {health['obsidian']['written']}. "
                    f"Notion synced/queued/failed: "
                    f"{health['notion']['synced']}/{health['notion']['queued']}/{health['notion']['failed']}."
                ),
                "memory_health": health,
            }
        elif command == "readiness":
            profile = parts[0] if parts else "production"
            check = self.run_readiness_check(ReadinessCheckCreateIn(profile=profile, requested_by="cto", metadata={"source": "slack", "user": req.user}))
            response = {"text": f"{profile} readiness {check['status']} ({check['score']}): {check['summary']}", "readiness": check}
        elif command in {"watchdog", "run-watchdog"}:
            run = self.run_watchdog(WatchdogRunIn(source="slack", force="force" in parts, metadata={"user": req.user}))
            response = {
                "text": f"Watchdog {run['status']} ({run['severity']}): {run['summary']}",
                "watchdog": run,
            }
        elif command in {"self-audit", "runtime-audit", "run-self-audit"}:
            audit = self.run_self_audit(SelfAuditRunIn(requested_by="slack", force="force" in parts, metadata={"user": req.user}))
            response = {
                "text": f"Self-audit {audit['status']}: {audit['summary']}",
                "self_audit": audit,
            }
        elif command in {"autonomy-drill", "autonomy-proof", "proof"}:
            drill = self.run_autonomy_drill(
                AutonomyDrillRunIn(
                    requested_by="ceo",
                    include_browser_job="no-browser" not in parts,
                    include_slack=True,
                    force_cycle="force" in parts,
                    metadata={"source": "slack", "user": req.user},
                )
            )
            response = {
                "text": f"Autonomy drill {drill['status']} ({drill['score']}): {drill['summary']}",
                "autonomy_drill": drill,
            }
        elif command in {"launch-gate", "vps-launch", "launch-check"}:
            profile = parts[0] if parts and parts[0] in {"vps", "railway", "local"} else "vps"
            gate = self.run_launch_gate(LaunchGateRunIn(profile=profile, requested_by="cto", metadata={"source": "slack", "user": req.user}))
            checks = json_loads(gate["checks"], [])
            blockers = [check["key"] for check in checks if check["status"] == "fail" and check["required"]]
            response = {
                "text": (
                    f"{profile} launch gate {gate['status']} ({gate['score']}): {gate['summary']}"
                    + (f" Blockers: {', '.join(blockers[:5])}." if blockers else "")
                ),
                "launch_gate": gate,
            }
        elif command in {"proof-pack", "proof-runs", "business-proof"}:
            proof_pack = self.ops_overview()["proof_pack"]
            latest = proof_pack["latest_proof_runs"][0] if proof_pack["latest_proof_runs"] else None
            primary = proof_pack.get("primary", {})
            response = {
                "text": (
                    f"Proof pack: {latest['status']} {latest['proof_type']} - {latest['summary']}"
                    if latest
                    else f"Proof pack has not recorded a run yet. Run: {primary.get('command', '')}"
                ),
                "proof_pack": proof_pack,
            }
        elif command in {"browser", "browser-jobs"}:
            queued = self.count("browser_jobs", "status='queued'")
            claimed = self.count("browser_jobs", "status='claimed'")
            completed = self.count("browser_jobs", "status='completed'")
            failed = self.count("browser_jobs", "status='failed'")
            operators = self.browser_operators()
            online = [operator for operator in operators if operator["alive"]]
            response = {
                "text": (
                    f"Browser jobs: {queued} queued, {claimed} claimed, {completed} completed, {failed} failed. "
                    f"Operators online: {len(online)}."
                ),
                "browser_jobs": {"queued": queued, "claimed": claimed, "completed": completed, "failed": failed},
                "browser_operators": operators,
            }
        elif command in {"browser-operators", "super-browser"}:
            operators = self.browser_operators()
            preview = "; ".join(f"{o['operator_id']}={o['status']}" for o in operators[:5])
            response = {
                "text": f"Browser operators: {len([o for o in operators if o['alive']])} online, {len(operators)} known." + (f" {preview}" if preview else ""),
                "browser_operators": operators,
            }
        elif command == "outbox":
            queued = self.count("outbox", "status='queued'")
            failed = self.count("outbox", "status='failed'")
            sent = self.count("outbox", "status='sent'")
            response = {"text": f"Outbox: {queued} queued, {failed} failed, {sent} sent.", "outbox": {"queued": queued, "failed": failed, "sent": sent}}
        elif command in {"cycle", "operating-cycle"}:
            cycle = self.tick_operating_cycle("slack")
            response = {"text": f"CEO cycle {cycle['status']}: {cycle['summary']}", "operating_cycle": cycle}
        elif command in {"briefing", "operator-briefing", "under-the-hood"}:
            briefing = self.create_operator_briefing(
                OperatorBriefingCreateIn(
                    requested_by="slack",
                    force="force" in parts,
                    include_slack=False,
                    metadata={"user": req.user, "source": "slack"},
                )
            )
            preview = "\n".join(briefing["body"].splitlines()[:12])
            response = {"text": preview, "operator_briefing": briefing}
        elif command in {"runtime", "runtime-controls"}:
            controls = self.runtime_controls()
            heartbeats = self.runtime_heartbeats()
            paused = [row["component"] for row in controls if not row["enabled"]]
            failed = [row["component"] for row in heartbeats if row["status"] == "failed"]
            response = {
                "text": (
                    f"Runtime controls: {len(controls) - len(paused)} enabled, {len(paused)} paused."
                    + (f" Paused: {', '.join(paused)}." if paused else "")
                    + (f" Failed: {', '.join(failed)}." if failed else "")
                ),
                "controls": controls,
                "heartbeats": heartbeats,
            }
        elif command in {"runtime-heartbeats", "heartbeats"}:
            heartbeats = self.runtime_heartbeats()
            latest = sorted(heartbeats, key=lambda row: row["last_finished_at"] or "", reverse=True)[:5]
            preview = "; ".join(f"{row['component']}={row['status']}" for row in latest)
            response = {
                "text": f"Runtime heartbeats: {len(heartbeats)} component(s)." + (f" {preview}" if preview else ""),
                "heartbeats": heartbeats,
            }
        elif command in {"pause-runtime", "resume-runtime"}:
            if not parts:
                raise HTTPException(status_code=400, detail=f"{command} requires a component")
            component = parts[0]
            enabled = command == "resume-runtime"
            control = self.set_runtime_control(
                RuntimeControlIn(
                    component=component,
                    enabled=enabled,
                    reason=" ".join(parts[1:]) or f"{'Resumed' if enabled else 'Paused'} from Slack by {req.user}",
                    set_by=req.user,
                    metadata={"source": "slack"},
                )
            )
            response = {"text": f"{control['component']} {'enabled' if control['enabled'] else 'paused'}.", "control": control}
        else:
            response = {
                "text": (
                    "Supported commands: status, budget-remaining [agent], "
                    "pause-agent <agent>, resume-agent <agent>, approve-action <id>, "
                    "reject-action <id>, kill-switch on|off, standup, "
                    "send-agent <agent> <message>, ask-agent <agent> <question>, "
                    "coordination-room, control-room, inbox [agent], decisions, decision-review [force], "
                    "runtime-controls, pause-runtime <component>, resume-runtime <component>, "
                    "runtime-heartbeats, "
                    "health, backup-now, incidents, memory-health, readiness [profile], watchdog [force], self-audit, autonomy-drill, launch-gate, "
                    "briefing, browser-jobs, browser-operators, outbox, operating-cycle."
                )
            }

        self.audit(req.user, "slack_command", "slack_command", command, {"text": req.text, "response": response})
        self.remember(
            title=f"Slack command: /{command}",
            body=response["text"],
            kind="operator_command",
            actor=req.user,
            client_id=req.client_id,
            tags=["slack", "command", command],
            source="slack",
            metadata={"request": model_to_dict(req), "response": response},
        )
        return response

    def create_browser_job(self, req: BrowserJobCreateIn) -> dict[str, Any]:
        job_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO browser_jobs (
                id, client_id, objective, url, requested_by, status, priority,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                req.client_id,
                req.objective,
                req.url,
                req.requested_by,
                "queued",
                req.priority,
                now,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "create_browser_job", "browser_job", job_id, model_to_dict(req))
        self.create_task(
            title=f"Browser automation: {req.objective[:80]}",
            agent_id="cto",
            client_id=req.client_id,
            priority=req.priority,
            metadata={"browser_job_id": job_id, "url": req.url, "requires_browser": True},
        )
        self.queue_slack(
            f"Browser job queued: {req.objective}\nURL: {req.url or 'not provided'}",
            severity="info",
            client_id=req.client_id,
            metadata={"browser_job_id": job_id},
        )
        return dict(self.get_one("SELECT * FROM browser_jobs WHERE id=?", (job_id,)))

    def record_browser_operator_heartbeat(self, req: BrowserOperatorHeartbeatIn) -> dict[str, Any]:
        operator_id = clean(req.operator_id) or "super-browser"
        status = clean(req.status).lower() or "online"
        if status not in {"online", "idle", "claimed", "working", "completed", "failed", "offline"}:
            raise HTTPException(status_code=400, detail="Unsupported browser operator status")
        now = now_iso()
        existing = self.get_one("SELECT * FROM browser_operator_heartbeats WHERE operator_id=?", (operator_id,))
        self.db.execute(
            """
            INSERT INTO browser_operator_heartbeats (
                operator_id, status, mode, current_job_id, version, note,
                first_seen_at, last_seen_at, heartbeat_count, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(operator_id) DO UPDATE SET
                status=excluded.status,
                mode=excluded.mode,
                current_job_id=excluded.current_job_id,
                version=excluded.version,
                note=excluded.note,
                last_seen_at=excluded.last_seen_at,
                heartbeat_count=browser_operator_heartbeats.heartbeat_count + 1,
                metadata=excluded.metadata
            """,
            (
                operator_id,
                status,
                req.mode,
                req.current_job_id,
                req.version,
                req.note,
                existing["first_seen_at"] if existing else now,
                now,
                1,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(operator_id, "browser_operator_heartbeat", "browser_operator", operator_id, model_to_dict(req))
        self.record_runtime_heartbeat(
            "browser_operator",
            "ok" if status != "failed" else "failed",
            result={"operator_id": operator_id, "status": status, "job_id": req.current_job_id},
            error=req.note if status == "failed" else "",
            metadata={"mode": req.mode, "version": req.version},
        )
        return dict(self.get_one("SELECT * FROM browser_operator_heartbeats WHERE operator_id=?", (operator_id,)))

    def browser_operators(self) -> list[dict[str, Any]]:
        rows = self.rows("browser_operator_heartbeats", "1=1 ORDER BY last_seen_at DESC")
        for row in rows:
            row["age_seconds"] = round(iso_age_seconds(row["last_seen_at"]), 2) if row["last_seen_at"] else None
            row["alive"] = bool(row["last_seen_at"] and iso_age_seconds(row["last_seen_at"]) <= max(BROWSER_OPERATOR_POLL_SECONDS * 4, 120))
        return rows

    def claim_browser_job(self, req: BrowserJobClaimIn) -> dict[str, Any]:
        self.recover_expired_browser_jobs()
        clauses = ["status='queued'"]
        params: list[Any] = []
        if req.client_id:
            clauses.append("client_id=?")
            params.append(req.client_id)
        row = self.get_one(
            f"""
            SELECT * FROM browser_jobs
            WHERE {' AND '.join(clauses)}
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """,
            tuple(params),
        )
        if not row:
            return {"claimed": False, "job": None}
        lease_expires_at = lease_until(req.lease_seconds)
        metadata = json_loads(row["metadata"], {})
        metadata.update(req.metadata)
        self.db.execute(
            """
            UPDATE browser_jobs
            SET status='claimed', operator_id=?, lease_expires_at=?,
                attempts=attempts+1, last_heartbeat_at=?, updated_at=?, metadata=?
            WHERE id=? AND status='queued'
            """,
            (
                req.operator_id,
                lease_expires_at,
                now_iso(),
                now_iso(),
                json_dumps(metadata),
                row["id"],
            ),
        )
        self.db.commit()
        claimed = self.get_one("SELECT * FROM browser_jobs WHERE id=?", (row["id"],))
        if not claimed or claimed["status"] != "claimed" or claimed["operator_id"] != req.operator_id:
            return {"claimed": False, "job": None}
        self.audit(req.operator_id, "claim_browser_job", "browser_job", row["id"], {"lease_expires_at": lease_expires_at})
        return {"claimed": True, "job": dict(claimed)}

    def heartbeat_browser_job(self, job_id: str, req: BrowserJobHeartbeatIn) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM browser_jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Browser job not found")
        if row["status"] != "claimed":
            raise HTTPException(status_code=409, detail=f"Browser job is {row['status']}, not claimed")
        if row["operator_id"] and row["operator_id"] != req.operator_id:
            raise HTTPException(status_code=409, detail="Browser job claimed by another operator")
        metadata = json_loads(row["metadata"], {})
        metadata.update(req.metadata)
        if req.note:
            metadata["last_heartbeat_note"] = req.note
        self.db.execute(
            """
            UPDATE browser_jobs
            SET lease_expires_at=?, last_heartbeat_at=?, updated_at=?, metadata=?
            WHERE id=?
            """,
            (lease_until(req.lease_seconds), now_iso(), now_iso(), json_dumps(metadata), job_id),
        )
        self.db.commit()
        self.audit(req.operator_id, "heartbeat_browser_job", "browser_job", job_id, {"note": req.note})
        return dict(self.get_one("SELECT * FROM browser_jobs WHERE id=?", (job_id,)))

    def fail_browser_job(self, job_id: str, req: BrowserJobCompleteIn) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM browser_jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Browser job not found")
        status = "failed" if row["attempts"] >= 3 else "queued"
        metadata = json_loads(row["metadata"], {})
        metadata.update(req.metadata)
        self.db.execute(
            """
            UPDATE browser_jobs
            SET status=?, result=?, artifacts=?, metadata=?, last_error=?,
                operator_id='', lease_expires_at=NULL, updated_at=?
            WHERE id=?
            """,
            (
                status,
                req.result,
                json_dumps(req.artifacts),
                json_dumps(metadata),
                req.result or "Browser job failed",
                now_iso(),
                job_id,
            ),
        )
        self.db.commit()
        self.audit("browser", "fail_browser_job", "browser_job", job_id, {"status": status, "error": req.result})
        if status == "failed":
            self.create_incident(
                IncidentCreateIn(
                    title=f"Browser job failed: {row['objective'][:100]}",
                    severity="warning",
                    source="browser",
                    owner_agent_id="cto",
                    details={"browser_job_id": job_id, "error": req.result},
                )
            )
        return dict(self.get_one("SELECT * FROM browser_jobs WHERE id=?", (job_id,)))

    def complete_browser_job(self, job_id: str, req: BrowserJobCompleteIn) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM browser_jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Browser job not found")
        if row["status"] not in {"claimed", "queued"}:
            raise HTTPException(status_code=409, detail=f"Browser job is {row['status']}")
        metadata = json_loads(row["metadata"], {})
        metadata.update(req.metadata)
        self.db.execute(
            """
            UPDATE browser_jobs
            SET status=?, result=?, artifacts=?, metadata=?, completed_at=?, updated_at=?,
                operator_id='', lease_expires_at=NULL, last_error=''
            WHERE id=?
            """,
            (
                req.status,
                req.result,
                json_dumps(req.artifacts),
                json_dumps(metadata),
                now_iso() if req.status in {"completed", "failed", "cancelled"} else None,
                now_iso(),
                job_id,
            ),
        )
        self.db.commit()
        self.audit("browser", "complete_browser_job", "browser_job", job_id, {"status": req.status})
        if req.result:
            self.remember(
                title=f"Browser job result: {row['objective'][:60]}",
                body=req.result,
                kind="browser_result",
                actor="browser",
                client_id=row["client_id"],
                tags=["browser", "automation"],
                source="browser_job",
                metadata={"browser_job_id": job_id, "artifacts": req.artifacts},
            )
        return dict(self.get_one("SELECT * FROM browser_jobs WHERE id=?", (job_id,)))

    def recover_expired_browser_jobs(self) -> int:
        if not self.runtime_component_enabled("browser_recovery"):
            return 0
        rows = self.get_all(
            """
            SELECT * FROM browser_jobs
            WHERE status='claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
            ORDER BY lease_expires_at ASC
            """,
            (now_iso(),),
        )
        recovered = 0
        for row in rows:
            status = "failed" if row["attempts"] >= 3 else "queued"
            error = f"Browser lease expired for operator {row['operator_id'] or 'unknown'}"
            self.db.execute(
                """
                UPDATE browser_jobs
                SET status=?, operator_id='', lease_expires_at=NULL, last_error=?, updated_at=?
                WHERE id=?
                """,
                (status, error, now_iso(), row["id"]),
            )
            self.db.commit()
            self.audit("system", "recover_expired_browser_job", "browser_job", row["id"], {"status": status, "error": error})
            if status == "failed":
                self.create_incident(
                    IncidentCreateIn(
                        title=f"Browser job abandoned: {row['objective'][:100]}",
                        severity="warning",
                        source="browser_recovery",
                        owner_agent_id="cto",
                        details={"browser_job_id": row["id"], "error": error},
                    )
                )
            recovered += 1
        return recovered

    def queue_outreach(self, req: OutreachJobCreateIn) -> dict[str, Any]:
        if self.is_kill_switch_active(req.client_id):
            raise HTTPException(status_code=423, detail="Kill switch active")
        if self.agents["sales_director"].status == "paused":
            raise HTTPException(status_code=423, detail="Sales Director paused")
        channel = req.channel.lower().strip()
        if channel not in {"sms", "email"}:
            raise HTTPException(status_code=400, detail="channel must be sms or email")
        lead = self.get_one("SELECT * FROM leads WHERE id=? AND client_id=?", (req.lead_id, req.client_id))
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if lead["opted_out"] or lead["status"] == "suppressed":
            raise HTTPException(status_code=409, detail="Lead is suppressed")
        if channel == "sms" and not lead["phone"]:
            raise HTTPException(status_code=400, detail="Lead has no phone")
        if channel == "email" and not lead["email"]:
            raise HTTPException(status_code=400, detail="Lead has no email")

        job_id = short_id()
        provider = "sendivo" if channel == "sms" else "smartlead"
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO outreach_jobs (
                id, client_id, campaign_id, lead_id, channel, provider,
                provider_campaign_id, from_phone_id, body, priority, requested_by,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                req.client_id,
                req.campaign_id,
                req.lead_id,
                channel,
                provider,
                req.provider_campaign_id,
                req.from_phone_id,
                req.body,
                req.priority,
                req.requested_by,
                now,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "queue_outreach", "outreach_job", job_id, model_to_dict(req))
        self.queue_slack(
            f"Outreach queued via {provider}: {channel} to {lead['company'] or lead['email'] or lead['phone']}",
            severity="info",
            client_id=req.client_id,
            metadata={"outreach_job_id": job_id, "lead_id": req.lead_id},
        )
        return dict(self.get_one("SELECT * FROM outreach_jobs WHERE id=?", (job_id,)))

    async def execute_outreach_job(self, job_id: str) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM outreach_jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Outreach job not found")
        if row["status"] not in {"queued", "failed"}:
            return dict(row)
        if self.is_kill_switch_active(row["client_id"]):
            self._mark_outreach(row, "blocked", {"error": "Kill switch active"}, "Kill switch active")
            return dict(self.get_one("SELECT * FROM outreach_jobs WHERE id=?", (job_id,)))

        lead = self.get_one("SELECT * FROM leads WHERE id=? AND client_id=?", (row["lead_id"], row["client_id"]))
        if not lead:
            self._mark_outreach(row, "failed", {"error": "Lead not found"}, "Lead not found")
            return dict(self.get_one("SELECT * FROM outreach_jobs WHERE id=?", (job_id,)))
        if lead["opted_out"] or lead["status"] == "suppressed":
            self._mark_outreach(row, "suppressed", {"error": "Lead suppressed"}, "Lead suppressed")
            return dict(self.get_one("SELECT * FROM outreach_jobs WHERE id=?", (job_id,)))

        dry_run = not SEND_OUTREACH
        result: dict[str, Any]
        if dry_run:
            result = {
                "status": "completed",
                "dry_run": True,
                "provider": row["provider"],
                "channel": row["channel"],
                "lead_id": row["lead_id"],
            }
        elif row["channel"] == "sms":
            from src.integrations import sendivo_api

            result = await sendivo_api.send_sms(
                to=lead["phone"],
                body=row["body"],
                from_phone_id=row["from_phone_id"],
                campaign_id=row["provider_campaign_id"],
                sdr_name="dialdesk",
            )
        else:
            from src.integrations import smartlead_api

            result = await smartlead_api.send_to_sequence(
                campaign_id=row["provider_campaign_id"],
                lead_email=lead["email"],
                lead_first_name=lead["first_name"] or "",
                lead_company=lead["company"] or "",
                sdr_name="dialdesk",
                icp_tier=lead_tier(lead["score"]),
            )

        ok = result.get("status") == "completed"
        status = "sent" if ok and not result.get("stub") and not result.get("dry_run") else "dry_run" if ok else "failed"
        error = "" if ok else result.get("error", "provider failed")
        self._mark_outreach(row, status, result, error)
        if ok:
            self.record_conversation(
                row["channel"],
                "outbound",
                row["body"],
                row["client_id"],
                row["lead_id"],
                {"outreach_job_id": row["id"], "provider_result": result},
            )
            self.remember(
                title=f"Outreach {status}: {row['channel']} to {lead['company'] or lead['email'] or lead['phone']}",
                body=row["body"],
                kind="outreach",
                actor="sales_director",
                client_id=row["client_id"],
                tags=["outreach", row["channel"], status],
                source=row["provider"],
                metadata={"outreach_job_id": row["id"], "result": result},
            )
        return dict(self.get_one("SELECT * FROM outreach_jobs WHERE id=?", (job_id,)))

    def _mark_outreach(
        self,
        row: sqlite3.Row,
        status: str,
        result: dict[str, Any],
        last_error: str = "",
    ) -> None:
        self.db.execute(
            """
            UPDATE outreach_jobs
            SET status=?, attempts=attempts+1, last_error=?, updated_at=?, sent_at=?, result=?
            WHERE id=?
            """,
            (
                status,
                last_error,
                now_iso(),
                now_iso() if status in {"sent", "dry_run"} else None,
                json_dumps(result),
                row["id"],
            ),
        )
        self.db.commit()
        self.audit("sales_director", "execute_outreach", "outreach_job", row["id"], {"status": status, "error": last_error})

    async def execute_outreach_queue_once(self, limit: int = 5) -> int:
        if self.is_kill_switch_active() or not self.runtime_component_enabled("outreach"):
            return 0
        rows = self.get_all(
            """
            SELECT id FROM outreach_jobs
            WHERE status IN ('queued','failed') AND attempts < 3
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        processed = 0
        for row in rows:
            await self.execute_outreach_job(row["id"])
            processed += 1
        return processed

    def queue_integration_sync(self, req: IntegrationSyncCreateIn) -> dict[str, Any]:
        provider = req.provider.lower().strip()
        sync_type = req.sync_type.lower().strip()
        allowed = {
            ("smartlead", "replies"),
            ("smartlead", "stats"),
            ("sendivo", "logs"),
            ("sendivo", "billing"),
        }
        if (provider, sync_type) not in allowed:
            raise HTTPException(status_code=400, detail="Unsupported provider/sync_type")
        sync_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO integration_syncs (
                id, client_id, provider, sync_type, provider_campaign_id, start_date,
                end_date, limit_count, priority, requested_by, created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sync_id,
                req.client_id,
                provider,
                sync_type,
                req.provider_campaign_id,
                req.start_date,
                req.end_date,
                max(1, min(req.limit, 500)),
                req.priority,
                req.requested_by,
                now,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "queue_integration_sync", "integration_sync", sync_id, model_to_dict(req))
        return dict(self.get_one("SELECT * FROM integration_syncs WHERE id=?", (sync_id,)))

    async def execute_integration_sync(self, sync_id: str) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM integration_syncs WHERE id=?", (sync_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Integration sync not found")
        if row["status"] not in {"queued", "failed"}:
            return dict(row)

        try:
            if row["provider"] == "smartlead" and row["sync_type"] == "replies":
                result, seen, imported = await self._sync_smartlead_replies(row)
            elif row["provider"] == "smartlead" and row["sync_type"] == "stats":
                result, seen, imported = await self._sync_smartlead_stats(row)
            elif row["provider"] == "sendivo" and row["sync_type"] == "logs":
                result, seen, imported = await self._sync_sendivo_logs(row)
            elif row["provider"] == "sendivo" and row["sync_type"] == "billing":
                result, seen, imported = await self._sync_sendivo_billing(row)
            else:
                raise ValueError("Unsupported sync")
            status = "completed" if result.get("status") == "completed" else "failed"
            error = "" if status == "completed" else result.get("error", "sync failed")
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
            seen = 0
            imported = 0
            status = "failed"
            error = str(exc)

        self.db.execute(
            """
            UPDATE integration_syncs
            SET status=?, attempts=attempts+1, records_seen=?, records_imported=?,
                last_error=?, updated_at=?, completed_at=?, result=?
            WHERE id=?
            """,
            (
                status,
                seen,
                imported,
                error,
                now_iso(),
                now_iso() if status == "completed" else None,
                json_dumps(result),
                sync_id,
            ),
        )
        self.db.commit()
        self.audit("cto", "execute_integration_sync", "integration_sync", sync_id, {"status": status, "error": error})
        self.remember(
            title=f"Integration sync {status}: {row['provider']} {row['sync_type']}",
            body=f"Seen: {seen}. Imported: {imported}. Error: {error or 'none'}.",
            kind="integration_sync",
            actor="cto",
            client_id=row["client_id"],
            tags=["integration", row["provider"], row["sync_type"], status],
            source=row["provider"],
            metadata={"sync_id": sync_id, "result": result},
        )
        if error:
            self.create_event(
                "integration_failure",
                client_id=row["client_id"],
                source=row["provider"],
                severity="high",
                payload={"sync_id": sync_id, "error": error},
            )
        return dict(self.get_one("SELECT * FROM integration_syncs WHERE id=?", (sync_id,)))

    async def execute_integration_sync_queue_once(self, limit: int = 5) -> int:
        if not self.runtime_component_enabled("integration_sync"):
            return 0
        rows = self.get_all(
            """
            SELECT id FROM integration_syncs
            WHERE status IN ('queued','failed') AND attempts < 3
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        processed = 0
        for row in rows:
            await self.execute_integration_sync(row["id"])
            processed += 1
        return processed

    async def _sync_smartlead_replies(self, row: sqlite3.Row) -> tuple[dict[str, Any], int, int]:
        from src.integrations import smartlead_api

        if not row["provider_campaign_id"]:
            return {"status": "failed", "error": "provider_campaign_id required"}, 0, 0
        result = await smartlead_api.poll_replies(row["provider_campaign_id"])
        replies = result.get("replies", []) if isinstance(result, dict) else []
        imported = 0
        for reply in replies:
            body = clean(reply.get("reply_message") or reply.get("message") or reply.get("text"))
            if not body:
                continue
            lead = self.find_lead(row["client_id"], email=reply.get("lead_email"))
            conv = self.record_conversation(
                "email",
                "inbound",
                body,
                row["client_id"],
                lead["id"] if lead else None,
                {"sync_id": row["id"], "provider": "smartlead", "raw": reply},
            )
            imported += 1 if conv else 0
        if result.get("stub"):
            result["note"] = "Smartlead not configured; no replies imported."
        return result, len(replies), imported

    async def _sync_smartlead_stats(self, row: sqlite3.Row) -> tuple[dict[str, Any], int, int]:
        from src.integrations import smartlead_api

        if not row["provider_campaign_id"]:
            return {"status": "failed", "error": "provider_campaign_id required"}, 0, 0
        result = await smartlead_api.get_stats(row["provider_campaign_id"])
        return result, 1 if result.get("status") == "completed" else 0, 0

    async def _sync_sendivo_logs(self, row: sqlite3.Row) -> tuple[dict[str, Any], int, int]:
        from src.integrations import sendivo_api

        result = await sendivo_api.get_logs(
            start_date=row["start_date"],
            end_date=row["end_date"],
            limit=row["limit_count"],
        )
        logs = extract_sendivo_logs(result)
        imported = 0
        for log in logs:
            direction = normalize_direction(log.get("direction") or log.get("type") or log.get("status"))
            if direction != "inbound":
                continue
            body = clean(log.get("message") or log.get("body") or log.get("text"))
            if not body:
                continue
            phone = clean(log.get("from") or log.get("phone") or log.get("fromNumber") or log.get("number"))
            lead = self.find_lead(row["client_id"], phone=phone)
            conv = self.record_conversation(
                "sms",
                "inbound",
                body,
                row["client_id"],
                lead["id"] if lead else None,
                {"sync_id": row["id"], "provider": "sendivo", "raw": log},
            )
            imported += 1 if conv else 0
        if result.get("stub"):
            result["note"] = "Sendivo not configured; no logs imported."
        return result, len(logs), imported

    async def _sync_sendivo_billing(self, row: sqlite3.Row) -> tuple[dict[str, Any], int, int]:
        from src.integrations import sendivo_api

        result = await sendivo_api.get_billing(start_date=row["start_date"], end_date=row["end_date"])
        return result, 1 if result.get("status") == "completed" else 0, 0

    def find_lead(self, client_id: str, email: str | None = None, phone: str | None = None) -> sqlite3.Row | None:
        email_clean = clean(email)
        phone_clean = clean(phone)
        if not email_clean and not phone_clean:
            return None
        return self.get_one(
            """
            SELECT * FROM leads
            WHERE client_id=? AND (
                (? != '' AND lower(coalesce(email,''))=lower(?))
                OR (? != '' AND coalesce(phone,'')=?)
            )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (client_id, email_clean, email_clean, phone_clean, phone_clean),
        )

    def log_event(self, agent: str, message: str) -> None:
        stamp = utcnow().strftime("%H:%M")
        self.db.execute(
            "INSERT INTO agent_log (time, agent, message) VALUES (?,?,?)",
            (stamp, agent, message),
        )
        self.db.commit()

    def rows(self, table: str, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        rows = self.get_all(sql, params)
        return [dict(r) for r in rows]

    def create_task(
        self,
        title: str,
        agent_id: str,
        client_id: str = DEFAULT_CLIENT_ID,
        prompt: str = "",
        priority: int = 3,
        due_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        if agent_id not in self.agents:
            raise HTTPException(status_code=400, detail=f"Unknown agent: {agent_id}")
        if self.agents[agent_id].status == "paused":
            raise HTTPException(status_code=423, detail=f"Agent paused: {agent_id}")
        task_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO tasks (
                id, client_id, title, prompt, agent_id, status, priority, due_at,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                task_id,
                client_id,
                title,
                prompt,
                agent_id,
                status,
                priority,
                due_at,
                now,
                now,
                json_dumps(metadata or {}),
            ),
        )
        self.db.commit()
        self.audit("ceo" if agent_id != "ceo" else "system", "create_task", "task", task_id)
        self.log_event(agent_id, f"Task {task_id}: {title}")
        if priority <= 1:
            self.queue_slack(
                f"Priority task for {AGENTS[agent_id].name}: {title}",
                severity="action",
                client_id=client_id,
                metadata={"task_id": task_id, "agent_id": agent_id},
            )
        return dict(self.get_one("SELECT * FROM tasks WHERE id=?", (task_id,)))

    def complete_task(
        self,
        task_id: str,
        result: str,
        status: str = "completed",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        merged = json_loads(row["metadata"], {})
        merged.update(metadata or {})
        self.db.execute(
            """
            UPDATE tasks
            SET status=?, result=?, metadata=?, completed_at=?, updated_at=?
            WHERE id=?
            """,
            (status, result, json_dumps(merged), now_iso(), now_iso(), task_id),
        )
        self.db.commit()
        self.audit(row["agent_id"], "complete_task", "task", task_id, {"status": status})
        self.log_event(row["agent_id"], f"Completed {task_id}: {result[:80]}")
        self.remember(
            title=f"Task {status}: {row['title']}",
            body=result or f"Task marked {status}.",
            kind="task_result",
            actor=row["agent_id"],
            client_id=row["client_id"],
            tags=["task", status, row["agent_id"]],
            source="task",
            metadata={"task_id": task_id},
        )
        agent = self.agents.get(row["agent_id"])
        if agent:
            agent.tasks_completed_today += 1
            agent.status = "idle"
            agent.current_task = None
            agent.last_seen = now_iso()
        return dict(self.get_one("SELECT * FROM tasks WHERE id=?", (task_id,)))

    def recent_decision_exists(self, trigger_type: str, trigger_id: str, force: bool = False) -> bool:
        if force:
            return False
        row = self.get_one(
            """
            SELECT id FROM ceo_decisions
            WHERE trigger_type=? AND trigger_id=? AND date(created_at)=date('now')
            LIMIT 1
            """,
            (trigger_type, trigger_id),
        )
        return bool(row)

    def record_ceo_decision(
        self,
        client_id: str,
        trigger_type: str,
        trigger_id: str,
        decision_type: str,
        summary: str,
        rationale: str,
        action: str = "monitor",
        assigned_agent_id: str = "",
        task_id: str = "",
        approval_id: str = "",
        risk: str = "low",
        status: str = "recorded",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if assigned_agent_id and assigned_agent_id not in self.agents:
            raise HTTPException(status_code=400, detail=f"Unknown assigned_agent_id: {assigned_agent_id}")
        decision_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO ceo_decisions (
                id, client_id, trigger_type, trigger_id, decision_type, summary,
                rationale, action, assigned_agent_id, task_id, approval_id, risk,
                status, decided_by, created_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                decision_id,
                client_id,
                trigger_type,
                trigger_id,
                decision_type,
                summary,
                rationale,
                action,
                assigned_agent_id,
                task_id,
                approval_id,
                risk,
                status,
                "ceo",
                now,
                json_dumps(metadata or {}),
            ),
        )
        self.db.commit()
        self.audit("ceo", "record_decision", "ceo_decision", decision_id, {"trigger_type": trigger_type, "trigger_id": trigger_id})
        self.log_event("ceo", f"Decision {decision_id}: {summary}")
        self.remember(
            title=f"CEO Decision: {summary}",
            body=f"Decision: {decision_type}\nAction: {action}\nRisk: {risk}\n\nRationale: {rationale}",
            kind="ceo_decision",
            actor="ceo",
            client_id=client_id,
            tags=["ceo", "decision", decision_type, action],
            source="ceo_decision",
            metadata={
                "decision_id": decision_id,
                "trigger_type": trigger_type,
                "trigger_id": trigger_id,
                "task_id": task_id,
                "approval_id": approval_id,
                **(metadata or {}),
            },
        )
        return dict(self.get_one("SELECT * FROM ceo_decisions WHERE id=?", (decision_id,)))

    def decide_for_event(self, event: sqlite3.Row) -> dict[str, Any]:
        payload = json_loads(event["payload"], {})
        event_type = event["type"]
        client_id = event["client_id"]
        severity = event["severity"]
        task_id = ""
        approval_id = ""
        assigned_agent_id = ""
        action = "monitor"
        risk = "low"
        status = "recorded"
        decision_type = "monitor_event"
        summary = f"Monitor {event_type.replace('_', ' ')}"
        rationale = "The event did not match a high-confidence autonomous action rule, so the CEO recorded it for visibility."

        if event_type in {"high_spend", "compliance_risk", "new_campaign", "vendor_change"}:
            approval = self.create_approval(
                client_id=client_id,
                approval_type=event_type,
                title=f"Approval required: {event_type.replace('_', ' ')}",
                risk="high" if event_type == "compliance_risk" else "medium",
                payload=payload,
            )
            approval_id = approval["id"]
            action = "request_approval"
            decision_type = "escalate_for_approval"
            risk = approval["risk"]
            status = "waiting_on_approval"
            summary = f"Escalated {event_type.replace('_', ' ')} for approval"
            rationale = "Spend, compliance, campaign launch, and vendor changes are guarded actions that require explicit operator approval."
        elif event_type in {"new_signup", "payment_succeeded"}:
            task = self.create_task(
                "Start client onboarding checklist",
                "coo",
                client_id=client_id,
                priority=1,
                metadata={"event_id": event["id"], **payload},
            )
            task_id = task["id"]
            assigned_agent_id = "coo"
            action = "delegate_task"
            decision_type = "start_onboarding"
            risk = "medium"
            status = "task_created"
            summary = "Delegated client onboarding"
            rationale = "Signup/payment events should immediately start fulfillment so client launch does not wait for Justin."
        elif event_type in {"booked_call", "hot_reply"}:
            task = self.create_task(
                "Prepare sales-call brief and next action",
                "sales_director",
                client_id=client_id,
                priority=1,
                metadata={"event_id": event["id"], **payload},
            )
            task_id = task["id"]
            assigned_agent_id = "sales_director"
            action = "delegate_task"
            decision_type = "prepare_sales_follow_up"
            risk = "medium"
            status = "task_created"
            summary = "Delegated sales follow-up"
            rationale = "Hot replies and booked calls are revenue events. Sales Director prepares the brief while Justin stays focused on the live call."
        elif event_type in {"integration_failure", "health_check_failed"}:
            task = self.create_task(
                "Investigate production integration failure",
                "cto",
                client_id=client_id,
                priority=1,
                metadata={"event_id": event["id"], **payload},
            )
            task_id = task["id"]
            assigned_agent_id = "cto"
            action = "delegate_task"
            decision_type = "protect_runtime"
            risk = "high"
            status = "task_created"
            summary = "Delegated production failure investigation"
            rationale = "Broken integrations threaten 24/7 operations, so CTO gets an immediate priority task."
        elif event_type in {"campaign_underperforming", "reply_spike"}:
            task = self.create_task(
                "Review campaign performance and recommend optimization",
                "cmo",
                client_id=client_id,
                priority=2,
                metadata={"event_id": event["id"], **payload},
            )
            task_id = task["id"]
            assigned_agent_id = "cmo"
            action = "delegate_task"
            decision_type = "optimize_campaign"
            risk = "medium" if severity in {"high", "critical"} else "low"
            status = "task_created"
            summary = "Delegated campaign optimization"
            rationale = "Performance changes should become marketing work, not sit as passive metrics."
        elif event_type in {"rep_underperforming", "client_health_drop"}:
            task = self.create_task(
                "Review fulfillment risk and recovery plan",
                "coo",
                client_id=client_id,
                priority=1,
                metadata={"event_id": event["id"], **payload},
            )
            task_id = task["id"]
            assigned_agent_id = "coo"
            action = "delegate_task"
            decision_type = "protect_fulfillment"
            risk = "high"
            status = "task_created"
            summary = "Delegated fulfillment recovery"
            rationale = "Client health and rep performance risks require COO recovery planning before churn or guarantee problems grow."

        return self.record_ceo_decision(
            client_id=client_id,
            trigger_type="event",
            trigger_id=event["id"],
            decision_type=decision_type,
            summary=summary,
            rationale=rationale,
            action=action,
            assigned_agent_id=assigned_agent_id,
            task_id=task_id,
            approval_id=approval_id,
            risk=risk,
            status=status,
            metadata={"event_type": event_type, "severity": severity, "payload": payload},
        )

    def run_ceo_decision_review(self, req: CeoDecisionReviewIn) -> dict[str, Any]:
        client_clause = "client_id=?" if req.client_id else "1=1"
        params = (req.client_id,) if req.client_id else ()
        decisions: list[dict[str, Any]] = []

        approvals = self.get_all(
            f"""
            SELECT * FROM approvals
            WHERE {client_clause} AND status='pending' AND risk IN ('high','critical')
            ORDER BY created_at ASC
            LIMIT 10
            """,
            params,
        )
        for approval in approvals:
            trigger_id = approval["id"]
            if self.recent_decision_exists("approval", trigger_id, req.force):
                continue
            message = self.send_agent_message(
                AgentMessageCreateIn(
                    from_agent_id="ceo",
                    to_agent_id="justin",
                    subject=f"Approval waiting: {approval['title']}",
                    body=(
                        f"{approval['risk']} approval is waiting for operator decision.\n"
                        f"Type: {approval['type']}\nApproval ID: {approval['id']}"
                    ),
                    message_type="decision",
                    client_id=approval["client_id"],
                    priority=1,
                    metadata={"approval_id": approval["id"], "source": req.source},
                )
            )
            decisions.append(
                self.record_ceo_decision(
                    client_id=approval["client_id"],
                    trigger_type="approval",
                    trigger_id=trigger_id,
                    decision_type="escalate_pending_approval",
                    summary=f"Escalated approval to Justin: {approval['title']}",
                    rationale="High-risk approvals are one of the few things Justin should touch directly.",
                    action="notify_justin",
                    risk=approval["risk"],
                    status="waiting_on_justin",
                    approval_id=approval["id"],
                    metadata={"message_id": message["id"], "source": req.source, **req.metadata},
                )
            )

        incidents = self.get_all(
            f"""
            SELECT * FROM incidents
            WHERE {client_clause} AND status='open' AND severity IN ('warning','high','critical')
            ORDER BY created_at ASC
            LIMIT 10
            """,
            params,
        )
        for incident in incidents:
            trigger_id = incident["id"]
            if self.recent_decision_exists("incident", trigger_id, req.force):
                continue
            task = self.create_task(
                f"Resolve incident: {incident['title']}",
                incident["owner_agent_id"] or "cto",
                client_id=incident["client_id"],
                priority=1 if incident["severity"] in {"high", "critical"} else 2,
                metadata={"incident_id": incident["id"], "source": req.source},
            )
            decisions.append(
                self.record_ceo_decision(
                    client_id=incident["client_id"],
                    trigger_type="incident",
                    trigger_id=trigger_id,
                    decision_type="delegate_incident_resolution",
                    summary=f"Delegated incident resolution: {incident['title']}",
                    rationale="Open incidents are operational risk and should become owned work until resolved.",
                    action="delegate_task",
                    assigned_agent_id=incident["owner_agent_id"] or "cto",
                    task_id=task["id"],
                    risk=incident["severity"],
                    status="task_created",
                    metadata={"source": req.source, **req.metadata},
                )
            )

        for table, label in [
            ("outbox", "Failed Slack/Notion outbox delivery"),
            ("browser_jobs", "Failed browser automation job"),
            ("integration_syncs", "Failed provider sync"),
            ("crm_sync_jobs", "Failed CRM sync job"),
        ]:
            failed = self.get_all(
                f"SELECT * FROM {table} WHERE {client_clause} AND status='failed' ORDER BY updated_at DESC LIMIT 5",
                params,
            )
            for row in failed:
                trigger_id = f"{table}:{row['id']}"
                if self.recent_decision_exists("failed_job", trigger_id, req.force):
                    continue
                task = self.create_task(
                    f"Investigate {label.lower()}",
                    "cto",
                    client_id=row["client_id"],
                    priority=1,
                    metadata={"source_table": table, "source_id": row["id"], "source": req.source},
                )
                decisions.append(
                    self.record_ceo_decision(
                        client_id=row["client_id"],
                        trigger_type="failed_job",
                        trigger_id=trigger_id,
                        decision_type="repair_failed_system_work",
                        summary=f"Delegated repair: {label}",
                        rationale="Failed delivery, browser, provider, or CRM work can break 24/7 execution, so CTO gets a repair task.",
                        action="delegate_task",
                        assigned_agent_id="cto",
                        task_id=task["id"],
                        risk="high",
                        status="task_created",
                        metadata={"source_table": table, "source_id": row["id"], "source": req.source, **req.metadata},
                    )
                )

        if not decisions and req.force:
            decisions.append(
                self.record_ceo_decision(
                    client_id=req.client_id or DEFAULT_CLIENT_ID,
                    trigger_type="review",
                    trigger_id=f"{today_key()}:{req.source}:{short_id()}",
                    decision_type="continue_autonomous_operations",
                    summary="No escalations needed",
                    rationale="The review found no high-risk approvals, open incidents, or failed system jobs requiring CEO action.",
                    action="monitor",
                    risk="low",
                    status="recorded",
                    metadata={"source": req.source, **req.metadata},
                )
            )

        return {
            "source": req.source,
            "client_id": req.client_id or "all",
            "decisions_created": len(decisions),
            "decisions": decisions,
        }

    def create_event(
        self,
        event_type: str,
        client_id: str = DEFAULT_CLIENT_ID,
        source: str = "api",
        payload: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> dict[str, Any]:
        event_id = short_id()
        self.db.execute(
            """
            INSERT INTO events (id, client_id, type, source, severity, created_at, payload)
            VALUES (?,?,?,?,?,?,?)
            """,
            (event_id, client_id, event_type, source, severity, now_iso(), json_dumps(payload or {})),
        )
        self.db.commit()
        self.audit(source, "create_event", "event", event_id, {"type": event_type})
        if severity in {"high", "critical"}:
            self.queue_slack(
                f"{severity.upper()} event: {event_type}\nClient: {client_id}",
                severity=severity,
                client_id=client_id,
                metadata={"event_id": event_id, "payload": payload or {}},
            )
        self._handle_event(event_id)
        return dict(self.get_one("SELECT * FROM events WHERE id=?", (event_id,)))

    def _handle_event(self, event_id: str) -> None:
        row = self.get_one("SELECT * FROM events WHERE id=?", (event_id,))
        if not row or row["status"] == "handled":
            return
        decision = self.decide_for_event(row)
        self.db.execute(
            "UPDATE events SET status='handled', handled_at=? WHERE id=?",
            (now_iso(), event_id),
        )
        self.db.commit()
        self.audit("ceo", "handle_event", "event", event_id, {"type": row["type"], "decision_id": decision["id"]})

    def create_approval(
        self,
        client_id: str,
        approval_type: str,
        title: str,
        payload: dict[str, Any] | None = None,
        risk: str = "medium",
        requested_by: str = "ceo",
    ) -> dict[str, Any]:
        approval_id = short_id()
        self.db.execute(
            """
            INSERT INTO approvals (
                id, client_id, type, title, risk, requested_by, created_at, payload
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                approval_id,
                client_id,
                approval_type,
                title,
                risk,
                requested_by,
                now_iso(),
                json_dumps(payload or {}),
            ),
        )
        self.db.commit()
        self.audit(requested_by, "request_approval", "approval", approval_id)
        self.log_event("ceo", f"Approval needed {approval_id}: {title}")
        self.queue_slack(
            f"Approval needed: {title}",
            severity=risk,
            client_id=client_id,
            metadata={"approval_id": approval_id, "requested_by": requested_by},
        )
        return dict(self.get_one("SELECT * FROM approvals WHERE id=?", (approval_id,)))

    def decide_approval(self, approval_id: str, status: str, decided_by: str, note: str) -> dict[str, Any]:
        row = self.get_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not row:
            raise HTTPException(status_code=404, detail="Approval not found")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="Approval already decided")
        self.db.execute(
            """
            UPDATE approvals
            SET status=?, decided_by=?, decision_note=?, decided_at=?
            WHERE id=?
            """,
            (status, decided_by, note, now_iso(), approval_id),
        )
        self.db.commit()
        self.audit(decided_by, status, "approval", approval_id, {"note": note})
        self.log_event("ceo", f"Approval {approval_id} {status} by {decided_by}")
        self.remember(
            title=f"Approval {status}: {row['title']}",
            body=note or f"{decided_by} marked this approval {status}.",
            kind="decision",
            actor=decided_by,
            client_id=row["client_id"],
            tags=["approval", status, row["type"]],
            source="approval",
            metadata={"approval_id": approval_id},
        )
        return dict(self.get_one("SELECT * FROM approvals WHERE id=?", (approval_id,)))

    def is_kill_switch_active(self, client_id: str | None = None) -> bool:
        global_row = self.get_one("SELECT active FROM kill_switches WHERE scope='global'")
        if global_row and global_row["active"]:
            return True
        if client_id:
            row = self.get_one("SELECT active FROM kill_switches WHERE scope=?", (f"client:{client_id}",))
            return bool(row and row["active"])
        return False

    def runtime_controls(self) -> list[dict[str, Any]]:
        return self.rows("runtime_controls", "1=1 ORDER BY component ASC")

    def runtime_control_map(self) -> dict[str, dict[str, Any]]:
        return {row["component"]: row for row in self.runtime_controls()}

    def runtime_component_enabled(self, component: str) -> bool:
        row = self.get_one("SELECT enabled FROM runtime_controls WHERE component=?", (component,))
        return bool(row is None or row["enabled"])

    def runtime_heartbeats(self) -> list[dict[str, Any]]:
        return self.rows("runtime_heartbeats", "1=1 ORDER BY component ASC")

    def record_runtime_heartbeat(
        self,
        component: str,
        status: str,
        started_at: str | None = None,
        result: dict[str, Any] | None = None,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        component = component.strip().lower().replace("-", "_")
        now = now_iso()
        row = self.get_one("SELECT * FROM runtime_heartbeats WHERE component=?", (component,))
        previous_failures = int(row["consecutive_failures"]) if row else 0
        previous_runs = int(row["run_count"]) if row else 0
        consecutive_failures = previous_failures + 1 if status in {"failed", "error"} else 0
        enabled = 1 if self.runtime_component_enabled(component) else 0
        self.db.execute(
            """
            INSERT INTO runtime_heartbeats (
                component, status, enabled, last_started_at, last_finished_at,
                last_error, run_count, consecutive_failures, last_result, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(component) DO UPDATE SET
                status=excluded.status,
                enabled=excluded.enabled,
                last_started_at=excluded.last_started_at,
                last_finished_at=excluded.last_finished_at,
                last_error=excluded.last_error,
                run_count=excluded.run_count,
                consecutive_failures=excluded.consecutive_failures,
                last_result=excluded.last_result,
                metadata=excluded.metadata
            """,
            (
                component,
                status,
                enabled,
                started_at or now,
                now,
                error,
                previous_runs + 1,
                consecutive_failures,
                json_dumps(result or {}),
                json_dumps(metadata or {}),
            ),
        )
        self.db.commit()
        if consecutive_failures == 3:
            self.create_incident(
                IncidentCreateIn(
                    title=f"Runtime component failing: {component}",
                    severity="warning",
                    source="runtime_heartbeat",
                    owner_agent_id="cto",
                    details={"component": component, "error": error, "consecutive_failures": consecutive_failures},
                )
            )
        return dict(self.get_one("SELECT * FROM runtime_heartbeats WHERE component=?", (component,)))

    def set_runtime_control(self, req: RuntimeControlIn) -> dict[str, Any]:
        allowed = {
            "scheduler",
            "ceo_decision_review",
            "worker",
            "outbox",
            "outreach",
            "integration_sync",
            "crm_sync",
            "browser_recovery",
            "workflow",
            "scheduled_ops",
            "self_audit",
            "watchdog",
        }
        component = req.component.strip().lower().replace("-", "_")
        if component not in allowed:
            raise HTTPException(status_code=400, detail=f"Unsupported runtime component: {req.component}")
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO runtime_controls (component, enabled, reason, set_by, updated_at, metadata)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(component) DO UPDATE SET
                enabled=excluded.enabled,
                reason=excluded.reason,
                set_by=excluded.set_by,
                updated_at=excluded.updated_at,
                metadata=excluded.metadata
            """,
            (
                component,
                1 if req.enabled else 0,
                req.reason,
                req.set_by,
                now,
                json_dumps(req.metadata),
            ),
        )
        self.db.commit()
        action = "enable_runtime_component" if req.enabled else "pause_runtime_component"
        self.audit(req.set_by, action, "runtime_control", component, model_to_dict(req))
        self.remember(
            title=f"Runtime {'enabled' if req.enabled else 'paused'}: {component}",
            body=req.reason or f"{req.set_by} set {component} enabled={req.enabled}.",
            kind="runtime_control",
            actor=req.set_by,
            client_id=DEFAULT_CLIENT_ID,
            tags=["runtime", "control", component, "enabled" if req.enabled else "paused"],
            source="runtime_control",
            metadata={"component": component, "enabled": req.enabled, **req.metadata},
        )
        self.queue_slack(
            f"Runtime {'enabled' if req.enabled else 'paused'}: {component}\n{req.reason}",
            severity="info" if req.enabled else "warning",
            metadata={"component": component, "enabled": req.enabled},
        )
        current_heartbeat = self.get_one("SELECT * FROM runtime_heartbeats WHERE component=?", (component,))
        if not req.enabled:
            self.record_runtime_heartbeat(component, "paused", result={"reason": req.reason, "set_by": req.set_by})
        elif current_heartbeat and current_heartbeat["status"] == "paused":
            self.record_runtime_heartbeat(component, "waiting", result={"reason": req.reason, "set_by": req.set_by})
        return dict(self.get_one("SELECT * FROM runtime_controls WHERE component=?", (component,)))

    def set_kill_switch(self, req: KillSwitchIn) -> dict[str, Any]:
        scope = "global" if req.scope == "global" else f"client:{req.client_id or DEFAULT_CLIENT_ID}"
        self.db.execute(
            """
            INSERT INTO kill_switches (scope, active, reason, set_by, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(scope) DO UPDATE SET
                active=excluded.active,
                reason=excluded.reason,
                set_by=excluded.set_by,
                updated_at=excluded.updated_at
            """,
            (scope, int(req.active), req.reason, req.set_by, now_iso()),
        )
        self.db.commit()
        self.audit(req.set_by, "set_kill_switch", "kill_switch", scope, asdict_like(req))
        self.queue_slack(
            f"Kill switch {'enabled' if req.active else 'disabled'} for {scope}: {req.reason}",
            severity="critical" if req.active else "info",
            client_id=req.client_id or DEFAULT_CLIENT_ID,
            metadata={"scope": scope, "set_by": req.set_by},
        )
        return dict(self.get_one("SELECT * FROM kill_switches WHERE scope=?", (scope,)))

    def import_leads(self, req: LeadImportIn) -> dict[str, Any]:
        leads = list(req.leads)
        if req.csv_text:
            reader = csv.DictReader(io.StringIO(req.csv_text))
            leads.extend(dict(row) for row in reader)

        imported = 0
        duplicates = 0
        suppressed = 0
        created_ids: list[str] = []

        for raw in leads:
            email = clean(raw.get("email"))
            phone = clean(raw.get("phone") or raw.get("cell") or raw.get("mobile"))
            if not email and not phone:
                suppressed += 1
                continue
            if is_truthy(raw.get("opted_out")) or is_truthy(raw.get("do_not_contact")):
                suppressed += 1
                continue
            duplicate = self.get_one(
                """
                SELECT id FROM leads
                WHERE client_id=? AND (
                    (? != '' AND lower(coalesce(email,''))=lower(?))
                    OR (? != '' AND coalesce(phone,'')=?)
                )
                """,
                (req.client_id, email, email, phone, phone),
            )
            if duplicate:
                duplicates += 1
                continue

            lead_id = short_id()
            score = score_lead(raw)
            now = now_iso()
            self.db.execute(
                """
                INSERT INTO leads (
                    id, client_id, source, first_name, last_name, company, email, phone,
                    monthly_revenue, funding_amount, existing_funder, timeline, score,
                    created_at, updated_at, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lead_id,
                    req.client_id,
                    req.source,
                    clean(raw.get("first_name")),
                    clean(raw.get("last_name")),
                    clean(raw.get("company") or raw.get("business_name")),
                    email,
                    phone,
                    money(raw.get("monthly_revenue") or raw.get("revenue")),
                    money(raw.get("funding_amount") or raw.get("amount")),
                    clean(raw.get("existing_funder") or raw.get("current_lender")),
                    clean(raw.get("timeline") or raw.get("urgency")),
                    score,
                    now,
                    now,
                    json_dumps(raw),
                ),
            )
            imported += 1
            created_ids.append(lead_id)

        self.db.commit()
        self.audit(
            "sales_director",
            "import_leads",
            "client",
            req.client_id,
            {"imported": imported, "duplicates": duplicates, "suppressed": suppressed},
        )
        if imported:
            self.create_event(
                "leads_imported",
                client_id=req.client_id,
                source="lead_import",
                payload={"count": imported, "lead_ids": created_ids},
            )
            self.remember(
                title=f"Imported {imported} leads from {req.source}",
                body=(
                    f"Imported {imported} new leads. "
                    f"Duplicates: {duplicates}. Suppressed: {suppressed}."
                ),
                kind="lead_import",
                actor="sales_director",
                client_id=req.client_id,
                tags=["leads", "import", req.source],
                source="lead_import",
                metadata={"lead_ids": created_ids, "duplicates": duplicates, "suppressed": suppressed},
            )
        return {
            "success": True,
            "imported": imported,
            "duplicates": duplicates,
            "suppressed": suppressed,
            "lead_ids": created_ids,
        }

    def record_conversation(
        self,
        channel: str,
        direction: str,
        body: str,
        client_id: str = DEFAULT_CLIENT_ID,
        lead_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conv_id = short_id()
        intent, score = score_conversation(body)
        self.db.execute(
            """
            INSERT INTO conversations (
                id, client_id, lead_id, channel, direction, body, intent, score,
                created_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                conv_id,
                client_id,
                lead_id,
                channel,
                direction,
                body,
                intent,
                score,
                now_iso(),
                json_dumps(metadata or {}),
            ),
        )
        self.db.commit()
        self.audit(channel, "record_conversation", "conversation", conv_id, {"intent": intent, "score": score})
        if intent == "opt_out" and lead_id:
            self.db.execute(
                "UPDATE leads SET opted_out=1, status='suppressed', updated_at=? WHERE id=?",
                (now_iso(), lead_id),
            )
            self.db.commit()
            self.audit(channel, "suppress_lead", "lead", lead_id, {"conversation_id": conv_id})
            self.remember(
                title=f"Lead opted out via {channel}",
                body=body,
                kind="suppression",
                actor=channel,
                client_id=client_id,
                tags=["suppression", "opt_out", channel],
                source=channel,
                metadata={"conversation_id": conv_id, "lead_id": lead_id},
            )
        if score >= 8:
            self.create_event(
                "hot_reply",
                client_id=client_id,
                source=channel,
                severity="high",
                payload={"conversation_id": conv_id, "lead_id": lead_id, "intent": intent, "score": score},
            )
        return dict(self.get_one("SELECT * FROM conversations WHERE id=?", (conv_id,)))

    def create_appointment(
        self,
        client_id: str,
        lead_id: str | None,
        starts_at: str | None,
        source: str,
        summary: str,
        owner: str = "Justin",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        appt_id = short_id()
        now = now_iso()
        self.db.execute(
            """
            INSERT INTO appointments (
                id, client_id, lead_id, starts_at, owner, source, summary,
                created_at, updated_at, metadata
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                appt_id,
                client_id,
                lead_id,
                starts_at,
                owner,
                source,
                summary,
                now,
                now,
                json_dumps(metadata or {}),
            ),
        )
        self.db.commit()
        self.create_event(
            "booked_call",
            client_id=client_id,
            source=source,
            severity="high",
            payload={"appointment_id": appt_id, "lead_id": lead_id, "starts_at": starts_at},
        )
        self.remember(
            title=f"Booked call: {summary}",
            body=f"Appointment booked for {starts_at or 'time pending'} with owner {owner}.",
            kind="appointment",
            actor=source,
            client_id=client_id,
            tags=["appointment", "booking"],
            source=source,
            metadata={"appointment_id": appt_id, "lead_id": lead_id},
        )
        return dict(self.get_one("SELECT * FROM appointments WHERE id=?", (appt_id,)))

    def create_daily_brief(self, force: bool = False) -> dict[str, Any]:
        existing = self.get_one(
            "SELECT * FROM reports WHERE type='ceo_daily_brief' AND date(created_at)=?",
            (today_key(),),
        )
        if existing and not force:
            return dict(existing)

        metrics = self.metrics()
        pending_approvals = self.count("approvals", "status='pending'")
        open_tasks = self.count("tasks", "status IN ('pending','working')")
        failures = self.count("events", "severity IN ('high','critical') AND date(created_at)=date('now')")
        body = (
            f"CEO Daily Brief - {today_key()}\n\n"
            f"- Active clients: {metrics['active_clients']}\n"
            f"- Leads sourced today: {metrics['leads_sourced_today']}\n"
            f"- SMS sent today: {metrics['sms_sent_today']}\n"
            f"- Emails sent today: {metrics['emails_sent_today']}\n"
            f"- Appointments booked: {metrics['appointments_booked']}\n"
            f"- Pending approvals: {pending_approvals}\n"
            f"- Open tasks: {open_tasks}\n"
            f"- High-priority events today: {failures}\n\n"
            "Plan: keep acquisition measured, protect deliverability, move onboarding blockers, "
            "review hot replies, and escalate only approvals or sales calls to Justin."
        )
        report_id = short_id()
        self.db.execute(
            """
            INSERT INTO reports (id, client_id, type, title, body, status, created_at, metadata)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                report_id,
                DEFAULT_CLIENT_ID,
                "ceo_daily_brief",
                f"CEO Daily Brief - {today_key()}",
                body,
                "ready",
                now_iso(),
                json_dumps(metrics),
            ),
        )
        self.db.commit()
        self.audit("ceo", "create_daily_brief", "report", report_id)
        self.remember(
            title=f"CEO Daily Brief - {today_key()}",
            body=body,
            kind="daily_brief",
            actor="ceo",
            client_id=DEFAULT_CLIENT_ID,
            tags=["ceo", "daily_brief", "plan"],
            source="scheduler",
            metadata={"report_id": report_id, **metrics},
        )
        self.queue_slack(
            body,
            severity="info",
            client_id=DEFAULT_CLIENT_ID,
            metadata={"report_id": report_id, "kind": "daily_brief"},
        )

        for title, agent_id, priority in [
            ("Review campaign performance and list opportunities", "cmo", 2),
            ("Review onboarding blockers and client health", "coo", 2),
            ("Run production health check", "cto", 1),
            ("Review hot replies and booked sales calls", "sales_director", 1),
        ]:
            self.create_task(
                title=title,
                agent_id=agent_id,
                client_id=DEFAULT_CLIENT_ID,
                priority=priority,
                metadata={"source": "ceo_daily_brief", "report_id": report_id},
            )
        self.log_event("ceo", "Daily operating brief created and delegated.")
        return dict(self.get_one("SELECT * FROM reports WHERE id=?", (report_id,)))

    def create_operator_briefing(self, req: OperatorBriefingCreateIn) -> dict[str, Any]:
        requested_by = req.requested_by.strip().lower()
        if not valid_actor(requested_by) and requested_by not in {"operator", "dashboard"}:
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        existing = self.get_one(
            "SELECT * FROM reports WHERE type='operator_briefing' AND date(created_at)=? ORDER BY created_at DESC LIMIT 1",
            (today_key(),),
        )
        if existing and not req.force:
            return dict(existing)

        metrics = self.operating_cycle_snapshot()
        control_room = self.control_room()
        overview = self.ops_overview()
        memory = self.memory_health()
        latest_self_audit = self.rows("proof_runs", "proof_type='runtime_self_audit' ORDER BY completed_at DESC LIMIT 1")
        latest_business_proof = self.rows("proof_runs", "proof_type='business_stack' ORDER BY completed_at DESC LIMIT 1")
        latest_watchdog = self.rows("watchdog_runs", "1=1 ORDER BY checked_at DESC LIMIT 1")
        latest_launch_gate = self.rows("launch_gate_runs", "1=1 ORDER BY checked_at DESC LIMIT 1")
        latest_cycle = self.rows("operating_cycles", "1=1 ORDER BY started_at DESC LIMIT 1")
        failed_components = [row["component"] for row in self.runtime_heartbeats() if row["status"] == "failed"]
        paused_components = [row["component"] for row in self.runtime_controls() if not row["enabled"]]
        online_browser_operators = [row for row in self.browser_operators() if row["alive"]]
        attention = control_room["attention"][:8]
        layers = control_room["how_it_works"]
        next_actions = [
            f"{item['owner']}: {item['action']}"
            for item in attention[:5]
        ] or [
            "CEO: keep the operating cycle running and watch approvals.",
            "CTO: keep proof runs, backups, memory, Slack, and browser operator healthy.",
            "Sales Director: review hot replies and booked-call prep.",
        ]
        body = "\n".join(
            [
                f"Operator Briefing - {today_key()}",
                "",
                "Business State",
                f"- Control room: {control_room['status']} - {control_room['summary']}",
                f"- Active clients: {metrics['active_clients']}",
                f"- Open tasks: {metrics['open_tasks']}",
                f"- Pending approvals: {metrics['approvals_pending']}",
                f"- Open incidents: {metrics['open_incidents']}",
                f"- Appointments booked today: {metrics['appointments_booked']}",
                "",
                "Autonomy Proof",
                f"- Runtime self-audit: {latest_self_audit[0]['status'] if latest_self_audit else 'not recorded'}"
                + (f" - {latest_self_audit[0]['summary']}" if latest_self_audit else ""),
                f"- Business stack proof: {latest_business_proof[0]['status'] if latest_business_proof else 'not recorded'}"
                + (f" - {latest_business_proof[0]['summary']}" if latest_business_proof else ""),
                f"- Watchdog: {latest_watchdog[0]['status'] if latest_watchdog else 'not recorded'}"
                + (f" - {latest_watchdog[0]['summary']}" if latest_watchdog else ""),
                f"- Launch gate: {latest_launch_gate[0]['status'] if latest_launch_gate else 'not recorded'}"
                + (f" - {latest_launch_gate[0]['summary']}" if latest_launch_gate else ""),
                "",
                "Memory And Visibility",
                f"- SQLite memory entries: {memory['total_entries']}",
                f"- Obsidian configured/written: {memory['obsidian']['configured']}/{memory['obsidian']['written']}",
                f"- Notion configured queued/failed: {memory['notion']['configured']} {memory['notion']['queued']}/{memory['notion']['failed']}",
                f"- Slack outbox queued/failed: {overview['visibility']['queued_outbox']}/{overview['visibility']['failed_outbox']}",
                f"- Browser operators online: {len(online_browser_operators)}",
                "",
                "Under The Hood",
                *[f"- {layer['layer']}: {layer['what_it_does']} Evidence: {', '.join(layer['evidence'])}" for layer in layers],
                "",
                "Runtime Exceptions",
                f"- Failed components: {', '.join(failed_components) if failed_components else 'none'}",
                f"- Paused components: {', '.join(paused_components) if paused_components else 'none'}",
                f"- Current CEO cycle: {latest_cycle[0]['summary'] if latest_cycle else 'not started'}",
                "",
                "Next Actions",
                *[f"- {action}" for action in next_actions],
            ]
        )
        report_id = short_id()
        metadata = {
            **req.metadata,
            "control_room_status": control_room["status"],
            "active_clients": metrics["active_clients"],
            "open_tasks": metrics["open_tasks"],
            "pending_approvals": metrics["approvals_pending"],
            "open_incidents": metrics["open_incidents"],
            "latest_self_audit_id": latest_self_audit[0]["id"] if latest_self_audit else "",
            "latest_business_proof_id": latest_business_proof[0]["id"] if latest_business_proof else "",
            "online_browser_operators": len(online_browser_operators),
        }
        self.db.execute(
            """
            INSERT INTO reports (id, client_id, type, title, body, status, created_at, metadata)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                report_id,
                DEFAULT_CLIENT_ID,
                "operator_briefing",
                f"Operator Briefing - {today_key()}",
                body,
                "ready",
                now_iso(),
                json_dumps(metadata),
            ),
        )
        self.db.commit()
        self.audit(requested_by, "create_operator_briefing", "report", report_id, metadata)
        self.remember(
            title=f"Operator Briefing - {today_key()}",
            body=body,
            kind="operator_briefing",
            actor=requested_by,
            client_id=DEFAULT_CLIENT_ID,
            tags=["operator", "briefing", "under_the_hood", "ops"],
            source="operator_briefing",
            metadata={"report_id": report_id, **metadata},
        )
        if req.include_slack:
            self.queue_slack(
                body,
                severity="info" if control_room["status"] == "running" else "warning",
                client_id=DEFAULT_CLIENT_ID,
                metadata={"report_id": report_id, "kind": "operator_briefing"},
            )
        self.log_event("ceo", "Operator briefing created.")
        return dict(self.get_one("SELECT * FROM reports WHERE id=?", (report_id,)))

    def operating_cycle_plan(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "agent_id": "ceo",
                "focus": "Protect the business, clear approvals, watch exceptions, and keep Justin focused on sales calls.",
                "signals": {
                    "pending_approvals": self.count("approvals", "status='pending'"),
                    "open_incidents": self.count("incidents", "status='open'"),
                },
            },
            {
                "agent_id": "cmo",
                "focus": "Improve MCA/funding offer, lists, copy, and campaign ROI.",
                "signals": {"campaigns": self.count("campaigns"), "replies_today": metrics["replies_today"]},
            },
            {
                "agent_id": "coo",
                "focus": "Move onboarding, launch checklists, fulfillment, and weekly client reporting.",
                "signals": {"active_workflows": metrics["active_workflows"], "workflow_steps_due": metrics["workflow_steps_due"]},
            },
            {
                "agent_id": "cto",
                "focus": "Keep the runtime healthy, backed up, secure, integrated, and deploy-ready.",
                "signals": {"open_incidents": metrics["open_incidents"], "health_checks_today": metrics["health_checks_today"]},
            },
            {
                "agent_id": "sales_director",
                "focus": "Qualify hot replies, prepare sales calls, and protect booked-call follow-up.",
                "signals": {
                    "appointments_booked": metrics["appointments_booked"],
                    "sales_briefs_ready": metrics["sales_briefs_ready"],
                },
            },
            {
                "agent_id": "builder",
                "focus": "Build approved improvements only after the operating system exposes the need.",
                "signals": {"open_tasks": self.count("tasks", "status IN ('pending','working') AND agent_id='builder'")},
            },
        ]

    def operating_cycle_snapshot(self) -> dict[str, Any]:
        metrics = self.metrics()
        metrics.update(
            {
                "tasks_completed_today": self.count("tasks", "status='completed' AND date(completed_at)=date('now')"),
                "tasks_created_today": self.count("tasks", "date(created_at)=date('now')"),
                "events_created_today": self.count("events", "date(created_at)=date('now')"),
                "approvals_created_today": self.count("approvals", "date(created_at)=date('now')"),
                "approvals_pending": self.count("approvals", "status='pending'"),
                "open_tasks": self.count("tasks", "status IN ('pending','working')"),
            }
        )
        return metrics

    def start_operating_cycle(self, req: OperatingCycleStartIn) -> dict[str, Any]:
        if not valid_actor(req.requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        existing = self.get_one("SELECT * FROM operating_cycles WHERE day_key=?", (today_key(),))
        if existing and not req.force:
            return dict(existing)

        metrics = self.operating_cycle_snapshot()
        daily_plan = self.operating_cycle_plan(metrics)
        brief = self.create_daily_brief(force=False)
        standup_thread_id = ""
        if not existing:
            try:
                standup = self.create_agent_standup(
                    AgentStandupCreateIn(
                        requested_by="ceo",
                        topic=f"CEO operating cycle start - {today_key()}",
                        include_agents=["cmo", "coo", "cto", "sales_director", "builder"],
                        metadata={"source": "operating_cycle_start"},
                    )
                )
                standup_thread_id = standup["thread_id"]
            except HTTPException as exc:
                self.audit("ceo", "standup_skipped", "operating_cycle", today_key(), {"detail": exc.detail})

        cycle_id = existing["id"] if existing else short_id()
        now = now_iso()
        summary = (
            f"CEO operating cycle running for {today_key()}: "
            f"{metrics['active_clients']} active client(s), {metrics['open_tasks']} open task(s), "
            f"{metrics['approvals_pending']} approval(s), {metrics['open_incidents']} incident(s)."
        )
        if existing:
            self.db.execute(
                """
                UPDATE operating_cycles
                SET status='running', last_tick_at=?, morning_report_id=?, summary=?,
                    closed_at=NULL, daily_plan=?, metrics_current=?, metadata=?
                WHERE id=?
                """,
                (
                    now,
                    brief["id"],
                    summary,
                    json_dumps(daily_plan),
                    json_dumps(metrics),
                    json_dumps({"forced": req.force, **req.metadata}),
                    cycle_id,
                ),
            )
        else:
            self.db.execute(
                """
                INSERT INTO operating_cycles (
                    id, day_key, status, started_at, last_tick_at, morning_report_id,
                    standup_thread_id, requested_by, summary, daily_plan, metrics_start,
                    metrics_current, metadata
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cycle_id,
                    today_key(),
                    "running",
                    now,
                    now,
                    brief["id"],
                    standup_thread_id,
                    req.requested_by,
                    summary,
                    json_dumps(daily_plan),
                    json_dumps(metrics),
                    json_dumps(metrics),
                    json_dumps(req.metadata),
                ),
            )
        self.db.commit()
        self.audit(req.requested_by, "start_operating_cycle", "operating_cycle", cycle_id, {"force": req.force})
        operator_briefing = self.create_operator_briefing(
            OperatorBriefingCreateIn(
                requested_by="ceo",
                force=False,
                include_slack=True,
                metadata={"source": "operating_cycle_start", "cycle_id": cycle_id},
            )
        )
        cycle = self.get_one("SELECT * FROM operating_cycles WHERE id=?", (cycle_id,))
        cycle_metadata = json_loads(cycle["metadata"], {}) if cycle else {}
        cycle_metadata["operator_briefing_id"] = operator_briefing["id"]
        self.db.execute("UPDATE operating_cycles SET metadata=? WHERE id=?", (json_dumps(cycle_metadata), cycle_id))
        self.db.commit()
        self.remember(
            title=f"CEO Operating Cycle Started - {today_key()}",
            body=summary,
            kind="operating_cycle",
            actor="ceo",
            client_id=DEFAULT_CLIENT_ID,
            tags=["ceo", "operating_cycle", "daily_plan"],
            source="operating_cycle",
            metadata={
                "cycle_id": cycle_id,
                "report_id": brief["id"],
                "operator_briefing_id": operator_briefing["id"],
                "standup_thread_id": standup_thread_id,
            },
        )
        self.queue_slack(summary, severity="info", metadata={"cycle_id": cycle_id, "kind": "operating_cycle_start"})
        self.log_event("ceo", "Operating cycle started.")
        return dict(self.get_one("SELECT * FROM operating_cycles WHERE id=?", (cycle_id,)))

    def tick_operating_cycle(self, source: str = "scheduler") -> dict[str, Any]:
        cycle = self.start_operating_cycle(OperatingCycleStartIn(requested_by="ceo", metadata={"source": source}))
        if cycle["status"] == "closed":
            return cycle
        if utcnow().hour >= CEO_EOD_HOUR_UTC:
            return self.close_operating_cycle(OperatingCycleCloseIn(requested_by="ceo", metadata={"source": source}))
        metrics = self.operating_cycle_snapshot()
        summary = (
            f"Cycle tick: {metrics['open_tasks']} open task(s), {metrics['approvals_pending']} pending approval(s), "
            f"{metrics['open_incidents']} open incident(s), {metrics['appointments_booked']} appointment(s) booked today."
        )
        self.db.execute(
            """
            UPDATE operating_cycles
            SET last_tick_at=?, summary=?, metrics_current=?
            WHERE id=?
            """,
            (now_iso(), summary, json_dumps(metrics), cycle["id"]),
        )
        self.db.commit()
        self.audit(source, "tick_operating_cycle", "operating_cycle", cycle["id"], {"summary": summary})
        return dict(self.get_one("SELECT * FROM operating_cycles WHERE id=?", (cycle["id"],)))

    def close_operating_cycle(self, req: OperatingCycleCloseIn) -> dict[str, Any]:
        if not valid_actor(req.requested_by):
            raise HTTPException(status_code=400, detail="Unknown requested_by")
        cycle = self.get_one("SELECT * FROM operating_cycles WHERE day_key=?", (today_key(),))
        if not cycle:
            cycle = self.start_operating_cycle(
                OperatingCycleStartIn(requested_by=req.requested_by, metadata={"source": "close_without_existing_cycle"})
            )
            cycle = self.get_one("SELECT * FROM operating_cycles WHERE id=?", (cycle["id"],))
        if cycle and cycle["status"] == "closed" and not req.force:
            return dict(cycle)

        metrics = self.operating_cycle_snapshot()
        body = (
            f"CEO End Of Day - {today_key()}\n\n"
            f"- Active clients: {metrics['active_clients']}\n"
            f"- Tasks created today: {metrics['tasks_created_today']}\n"
            f"- Tasks completed today: {metrics['tasks_completed_today']}\n"
            f"- Pending approvals: {metrics['approvals_pending']}\n"
            f"- Open incidents: {metrics['open_incidents']}\n"
            f"- Appointments booked: {metrics['appointments_booked']}\n"
            f"- Replies today: {metrics['replies_today']}\n"
            f"- Outreach queued: {metrics['outreach_queued']}\n\n"
            f"{req.summary or 'CEO closed the day, kept the queue moving, and left escalations visible for Justin.'}"
        )
        report_id = short_id()
        self.db.execute(
            """
            INSERT INTO reports (id, client_id, type, title, body, status, created_at, metadata)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                report_id,
                DEFAULT_CLIENT_ID,
                "ceo_end_of_day",
                f"CEO End Of Day - {today_key()}",
                body,
                "ready",
                now_iso(),
                json_dumps(metrics),
            ),
        )
        self.db.execute(
            """
            UPDATE operating_cycles
            SET status='closed', last_tick_at=?, closed_at=?, close_report_id=?,
                summary=?, metrics_current=?, metadata=?
            WHERE id=?
            """,
            (
                now_iso(),
                now_iso(),
                report_id,
                req.summary or body.split("\n\n", 1)[0],
                json_dumps(metrics),
                json_dumps({"closed_by": req.requested_by, "forced": req.force, **req.metadata}),
                cycle["id"],
            ),
        )
        self.db.commit()
        self.audit(req.requested_by, "close_operating_cycle", "operating_cycle", cycle["id"], {"report_id": report_id})
        self.remember(
            title=f"CEO End Of Day - {today_key()}",
            body=body,
            kind="end_of_day",
            actor="ceo",
            client_id=DEFAULT_CLIENT_ID,
            tags=["ceo", "operating_cycle", "end_of_day"],
            source="operating_cycle",
            metadata={"cycle_id": cycle["id"], "report_id": report_id},
        )
        self.queue_slack(body, severity="info", metadata={"cycle_id": cycle["id"], "report_id": report_id})
        self.log_event("ceo", "Operating cycle closed.")
        return dict(self.get_one("SELECT * FROM operating_cycles WHERE id=?", (cycle["id"],)))

    def create_weekly_report(self, client_id: str) -> dict[str, Any]:
        metrics = self.client_dashboard(client_id)["metrics"]
        body = (
            "Weekly Debrief\n\n"
            f"- SMS sent: {metrics['sms_sent']}\n"
            f"- Emails sent: {metrics['emails_sent']}\n"
            f"- Replies: {metrics['replies']}\n"
            f"- Appointments: {metrics['appointments']}\n"
            f"- Open conversations: {metrics['open_conversations']}\n\n"
            "Recommendation: review reply quality, rotate the strongest offer, "
            "and prioritize booked-call follow-up."
        )
        report_id = short_id()
        self.db.execute(
            """
            INSERT INTO reports (id, client_id, type, title, body, status, created_at, metadata)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                report_id,
                client_id,
                "weekly_debrief",
                f"Weekly Debrief - {today_key()}",
                body,
                "draft",
                now_iso(),
                json_dumps(metrics),
            ),
        )
        self.db.commit()
        self.audit("coo", "create_weekly_report", "report", report_id)
        self.remember(
            title=f"Weekly Debrief - {today_key()}",
            body=body,
            kind="weekly_report",
            actor="coo",
            client_id=client_id,
            tags=["weekly_report", "client"],
            source="weekly_report",
            metadata={"report_id": report_id},
        )
        self.create_task(
            "Review and send weekly client debrief",
            "coo",
            client_id=client_id,
            priority=2,
            metadata={"report_id": report_id},
        )
        return dict(self.get_one("SELECT * FROM reports WHERE id=?", (report_id,)))

    def metrics(self) -> dict[str, Any]:
        leads_today = self.count("leads", "date(created_at)=date('now')")
        appts = self.count("appointments", "date(created_at)=date('now')")
        active_clients = self.count("clients", "status='active'")
        replies = self.count("conversations", "direction='inbound' AND date(created_at)=date('now')")
        sms_sent = self.count("conversations", "channel='sms' AND direction='outbound' AND date(created_at)=date('now')")
        emails_sent = self.count("conversations", "channel='email' AND direction='outbound' AND date(created_at)=date('now')")
        revenue_pipeline = self.sum("payments", "amount", "status IN ('succeeded','paid')")
        return {
            "leads_sourced_today": leads_today,
            "sms_sent_today": sms_sent,
            "emails_sent_today": emails_sent,
            "appointments_booked": appts,
            "replies_today": replies,
            "active_clients": active_clients,
            "revenue_pipeline": revenue_pipeline,
            "outreach_queued": self.count("outreach_jobs", "status='queued'"),
            "outreach_sent": self.count("outreach_jobs", "status='sent'"),
            "outreach_dry_run": self.count("outreach_jobs", "status='dry_run'"),
            "outreach_failed": self.count("outreach_jobs", "status='failed'"),
            "sync_queued": self.count("integration_syncs", "status='queued'"),
            "sync_completed": self.count("integration_syncs", "status='completed'"),
            "sync_failed": self.count("integration_syncs", "status='failed'"),
            "opportunities_open": self.count("opportunities", "status NOT IN ('closed_won','closed_lost')"),
            "sales_briefs_ready": self.count("sales_briefs", "status='ready'"),
            "payment_links_ready": self.count("payment_links", "status IN ('ready','dry_run')"),
            "crm_sync_queued": self.count("crm_sync_jobs", "status='queued'"),
            "agent_unread_messages": self.count("agent_messages", "status='unread'"),
            "agent_messages_today": self.count("agent_messages", "date(created_at)=date('now')"),
            "open_incidents": self.count("incidents", "status='open'"),
            "health_checks_today": self.count("health_checks", "date(checked_at)=date('now')"),
            "readiness_checks_today": self.count("readiness_checks", "date(checked_at)=date('now')"),
            "operating_cycles_today": self.count("operating_cycles", "date(started_at)=date('now')"),
            "watchdog_runs_today": self.count("watchdog_runs", "date(checked_at)=date('now')"),
            "autonomy_drills_today": self.count("autonomy_drills", "date(completed_at)=date('now')"),
            "launch_gate_runs_today": self.count("launch_gate_runs", "date(checked_at)=date('now')"),
            "ceo_decisions_today": self.count("ceo_decisions", "date(created_at)=date('now')"),
            "ceo_decisions_waiting": self.count("ceo_decisions", "status IN ('waiting_on_approval','waiting_on_justin')"),
            "runtime_components_failed": self.count("runtime_heartbeats", "status='failed'"),
            "runtime_components_paused": self.count("runtime_heartbeats", "status='paused'"),
            "successful_backups": self.count("backups", "status='completed'"),
            "unauthorized_requests_today": self.count("request_log", "status_code=401 AND date(created_at)=date('now')"),
            "browser_jobs_queued": self.count("browser_jobs", "status='queued'"),
            "browser_jobs_claimed": self.count("browser_jobs", "status='claimed'"),
            "browser_jobs_failed": self.count("browser_jobs", "status='failed'"),
            "active_workflows": self.count("workflow_runs", "status='active'"),
            "workflow_steps_due": self.count("workflow_steps", "status='pending' AND due_at <= ?", (now_iso(),)),
            "cost_today": 0.0,
        }

    def client_dashboard(self, client_id: str) -> dict[str, Any]:
        client = self.get_one("SELECT * FROM clients WHERE id=?", (client_id,))
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")
        metrics = {
            "leads": self.count("leads", "client_id=?", (client_id,)),
            "sms_sent": self.count("conversations", "client_id=? AND channel='sms' AND direction='outbound'", (client_id,)),
            "emails_sent": self.count("conversations", "client_id=? AND channel='email' AND direction='outbound'", (client_id,)),
            "replies": self.count("conversations", "client_id=? AND direction='inbound'", (client_id,)),
            "appointments": self.count("appointments", "client_id=?", (client_id,)),
            "open_conversations": self.count("conversations", "client_id=? AND status='open'", (client_id,)),
            "open_tasks": self.count("tasks", "client_id=? AND status IN ('pending','working')", (client_id,)),
            "pending_approvals": self.count("approvals", "client_id=? AND status='pending'", (client_id,)),
        }
        return {
            "client": dict(client),
            "metrics": metrics,
            "setup_checklist": setup_checklist(dict(client)),
            "campaigns": self.rows("campaigns", "client_id=?", (client_id,)),
            "recent_conversations": self.rows("conversations", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "appointments": self.rows("appointments", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "reports": self.rows("reports", "client_id=? ORDER BY created_at DESC LIMIT 10", (client_id,)),
            "opportunities": self.rows("opportunities", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "sales_briefs": self.rows("sales_briefs", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "payment_links": self.rows("payment_links", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "agent_messages": self.rows("agent_messages", "client_id=? ORDER BY created_at DESC LIMIT 30", (client_id,)),
            "ceo_decisions": self.rows("ceo_decisions", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "memory": self.rows("memory_entries", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "browser_jobs": self.rows("browser_jobs", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "outreach_jobs": self.rows("outreach_jobs", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "integration_syncs": self.rows("integration_syncs", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "crm_sync_jobs": self.rows("crm_sync_jobs", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "incidents": self.rows("incidents", "client_id=? ORDER BY created_at DESC LIMIT 20", (client_id,)),
            "health_checks": self.rows("health_checks ORDER BY checked_at DESC LIMIT 10"),
            "readiness_checks": self.rows("readiness_checks ORDER BY checked_at DESC LIMIT 10"),
            "watchdog_runs": self.rows("watchdog_runs ORDER BY checked_at DESC LIMIT 10"),
            "autonomy_drills": self.rows("autonomy_drills ORDER BY completed_at DESC LIMIT 10"),
            "launch_gate_runs": self.rows("launch_gate_runs ORDER BY checked_at DESC LIMIT 10"),
            "operating_cycles": self.rows("operating_cycles ORDER BY started_at DESC LIMIT 10"),
            "backups": self.rows("backups ORDER BY created_at DESC LIMIT 10"),
            "request_log": self.rows("request_log ORDER BY created_at DESC LIMIT 20"),
            "workflow_runs": self.rows("workflow_runs", "client_id=? ORDER BY created_at DESC LIMIT 10", (client_id,)),
            "workflow_steps": self.rows("workflow_steps", "client_id=? ORDER BY step_order ASC LIMIT 50", (client_id,)),
        }

    def activity_item(
        self,
        source: str,
        occurred_at: str,
        title: str,
        summary: str = "",
        status: str = "",
        actor: str = "",
        client_id: str = DEFAULT_CLIENT_ID,
        entity_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "occurred_at": occurred_at,
            "title": title,
            "summary": summary,
            "status": status,
            "actor": actor,
            "client_id": client_id,
            "entity_id": entity_id,
            "metadata": metadata or {},
        }

    def activity_feed(
        self,
        limit: int = 100,
        client_id: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        capped = max(1, min(limit, 250))
        items: list[dict[str, Any]] = []
        client_clause = "client_id=?" if client_id else "1=1"
        client_params: tuple[Any, ...] = (client_id,) if client_id else ()

        for row in self.get_all(f"SELECT * FROM tasks WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "task",
                    row["created_at"],
                    row["title"],
                    row["prompt"] or row["result"],
                    row["status"],
                    row["agent_id"],
                    row["client_id"],
                    row["id"],
                    json_loads(row["metadata"], {}),
                )
            )
        for row in self.get_all(f"SELECT * FROM events WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            payload = json_loads(row["payload"], {})
            items.append(
                self.activity_item(
                    "event",
                    row["created_at"],
                    row["type"],
                    f"{row['source']} event with {row['severity']} severity",
                    row["status"],
                    row["source"],
                    row["client_id"],
                    row["id"],
                    payload,
                )
            )
        for row in self.get_all(f"SELECT * FROM approvals WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "approval",
                    row["created_at"],
                    row["title"],
                    row["decision_note"],
                    row["status"],
                    row["requested_by"],
                    row["client_id"],
                    row["id"],
                    {"risk": row["risk"], "type": row["type"], "payload": json_loads(row["payload"], {})},
                )
            )
        for row in self.get_all(f"SELECT * FROM ceo_decisions WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "ceo_decision",
                    row["created_at"],
                    row["summary"],
                    row["rationale"][:280],
                    row["status"],
                    row["decided_by"],
                    row["client_id"],
                    row["id"],
                    {
                        "decision_type": row["decision_type"],
                        "action": row["action"],
                        "risk": row["risk"],
                        "task_id": row["task_id"],
                        "approval_id": row["approval_id"],
                        "trigger_type": row["trigger_type"],
                        "trigger_id": row["trigger_id"],
                    },
                )
            )
        for row in self.get_all(f"SELECT * FROM memory_entries WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "memory",
                    row["created_at"],
                    row["title"],
                    row["body"][:280],
                    row["notion_status"],
                    row["actor"],
                    row["client_id"],
                    row["id"],
                    {"kind": row["kind"], "source": row["source"], "tags": json_loads(row["tags"], [])},
                )
            )
        for row in self.get_all(f"SELECT * FROM reports WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "report",
                    row["created_at"],
                    row["title"],
                    row["body"][:280],
                    row["status"],
                    row["type"],
                    row["client_id"],
                    row["id"],
                    {"type": row["type"], "delivered_at": row["delivered_at"]},
                )
            )
        for row in self.get_all(f"SELECT * FROM outbox WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "outbox",
                    row["created_at"],
                    row["subject"] or row["destination"],
                    row["body"][:280],
                    row["status"],
                    "outbox",
                    row["client_id"],
                    row["id"],
                    {"destination": row["destination"], "severity": row["severity"], "attempts": row["attempts"]},
                )
            )
        for row in self.get_all(f"SELECT * FROM browser_jobs WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "browser_job",
                    row["created_at"],
                    row["objective"],
                    row["result"][:280] if row["result"] else row["url"],
                    row["status"],
                    row["requested_by"],
                    row["client_id"],
                    row["id"],
                    {"url": row["url"], "operator_id": row["operator_id"], "attempts": row["attempts"]},
                )
            )
        if not client_id:
            for row in self.get_all("SELECT * FROM watchdog_runs ORDER BY checked_at DESC LIMIT 50"):
                items.append(
                    self.activity_item(
                        "watchdog",
                        row["checked_at"],
                        row["summary"],
                        row["findings"][:280],
                        row["status"],
                        row["source"],
                        DEFAULT_CLIENT_ID,
                        row["id"],
                        {
                            "severity": row["severity"],
                            "incidents_opened": row["incidents_opened"],
                            "findings": json_loads(row["findings"], []),
                        },
                    )
                )
            for row in self.get_all("SELECT * FROM runtime_heartbeats ORDER BY last_finished_at DESC LIMIT 50"):
                items.append(
                    self.activity_item(
                        "runtime_heartbeat",
                        row["last_finished_at"] or row["last_started_at"],
                        f"Runtime {row['component']}",
                        row["last_error"] or row["last_result"][:280],
                        row["status"],
                        "system",
                        DEFAULT_CLIENT_ID,
                        row["component"],
                        {
                            "enabled": bool(row["enabled"]),
                            "run_count": row["run_count"],
                            "consecutive_failures": row["consecutive_failures"],
                            "last_result": json_loads(row["last_result"], {}),
                        },
                    )
                )
        for row in self.get_all(f"SELECT * FROM incidents WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "incident",
                    row["created_at"],
                    row["title"],
                    row["resolution"],
                    row["status"],
                    row["owner_agent_id"],
                    row["client_id"],
                    row["id"],
                    {"severity": row["severity"], "source": row["source"]},
                )
            )
        for row in self.get_all("SELECT * FROM operating_cycles ORDER BY started_at DESC LIMIT 25"):
            if client_id and client_id != DEFAULT_CLIENT_ID:
                continue
            items.append(
                self.activity_item(
                    "operating_cycle",
                    row["started_at"],
                    f"CEO operating cycle {row['day_key']}",
                    row["summary"],
                    row["status"],
                    row["requested_by"],
                    DEFAULT_CLIENT_ID,
                    row["id"],
                    {"morning_report_id": row["morning_report_id"], "close_report_id": row["close_report_id"]},
                )
            )
        for row in self.get_all("SELECT * FROM autonomy_drills ORDER BY completed_at DESC LIMIT 25"):
            if client_id and client_id != DEFAULT_CLIENT_ID:
                continue
            items.append(
                self.activity_item(
                    "autonomy_drill",
                    row["completed_at"],
                    row["summary"],
                    row["checks"][:280],
                    row["status"],
                    row["requested_by"],
                    DEFAULT_CLIENT_ID,
                    row["id"],
                    {"score": row["score"], "evidence": json_loads(row["evidence"], {})},
                )
            )
        for row in self.get_all("SELECT * FROM launch_gate_runs ORDER BY checked_at DESC LIMIT 25"):
            if client_id and client_id != DEFAULT_CLIENT_ID:
                continue
            items.append(
                self.activity_item(
                    "launch_gate",
                    row["checked_at"],
                    row["summary"],
                    row["checks"][:280],
                    row["status"],
                    row["requested_by"],
                    DEFAULT_CLIENT_ID,
                    row["id"],
                    {"score": row["score"], "profile": row["profile"], "commands": json_loads(row["commands"], [])},
                )
            )
        for row in self.get_all("SELECT * FROM proof_runs ORDER BY completed_at DESC LIMIT 25"):
            if client_id and client_id != DEFAULT_CLIENT_ID:
                continue
            items.append(
                self.activity_item(
                    "proof_run",
                    row["completed_at"],
                    row["summary"],
                    row["output"][:280] if row["output"] else row["command"],
                    row["status"],
                    row["source"],
                    DEFAULT_CLIENT_ID,
                    row["id"],
                    {
                        "proof_type": row["proof_type"],
                        "duration_ms": row["duration_ms"],
                        "checks": json_loads(row["checks"], []),
                    },
                )
            )
        for row in self.get_all(f"SELECT * FROM agent_messages WHERE {client_clause} ORDER BY created_at DESC LIMIT 50", client_params):
            items.append(
                self.activity_item(
                    "agent_message",
                    row["created_at"],
                    row["subject"],
                    row["body"][:280],
                    row["status"],
                    row["from_agent_id"],
                    row["client_id"],
                    row["id"],
                    {"to_agent_id": row["to_agent_id"], "thread_id": row["thread_id"], "message_type": row["message_type"]},
                )
            )
        if not client_id:
            for row in self.get_all("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 50"):
                items.append(
                    self.activity_item(
                        "audit",
                        row["created_at"],
                        f"{row['actor']} {row['action']}",
                        f"{row['entity_type']}:{row['entity_id']}",
                        "recorded",
                        row["actor"],
                        DEFAULT_CLIENT_ID,
                        str(row["id"]),
                        json_loads(row["details"], {}),
                    )
                )

        if source:
            items = [item for item in items if item["source"] == source]
        items.sort(key=lambda item: item["occurred_at"] or "", reverse=True)
        return items[:capped]

    def count(self, table: str, where: str = "1=1", params: tuple[Any, ...] = ()) -> int:
        row = self.get_one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)
        return int(row["n"] if row else 0)

    def sum(self, table: str, column: str, where: str = "1=1", params: tuple[Any, ...] = ()) -> float:
        row = self.get_one(f"SELECT COALESCE(SUM({column}),0) AS n FROM {table} WHERE {where}", params)
        return float(row["n"] if row else 0)

    def process_outbox_once(self, limit: int = 10) -> int:
        if not SEND_OUTBOX or not self.runtime_component_enabled("outbox"):
            return 0
        rows = self.get_all(
            """
            SELECT * FROM outbox
            WHERE status IN ('queued','failed') AND attempts < 3
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        processed = 0
        for row in rows:
            result_metadata: dict[str, Any] = {}
            if row["destination"] == "slack":
                ok, error, result_metadata = deliver_slack(row)
            elif row["destination"] == "notion":
                ok, error, result_metadata = deliver_notion(row)
            else:
                ok, error = False, f"unsupported destination: {row['destination']}"
            processed += 1
            self.db.execute(
                """
                UPDATE outbox
                SET status=?, attempts=attempts+1, last_error=?, updated_at=?, sent_at=?
                WHERE id=?
                """,
                (
                    "sent" if ok else "failed",
                    error,
                    now_iso(),
                    now_iso() if ok else None,
                    row["id"],
                ),
            )
            self.db.commit()
            if row["destination"] == "notion":
                payload = json_loads(row["payload"], {})
                memory_id = payload.get("memory_id")
                if memory_id:
                    self.update_memory_notion_status(
                        memory_id,
                        "synced" if ok else "failed",
                        row["id"],
                        error,
                        result_metadata,
                    )
            self.audit("outbox", "deliver", "outbox", row["id"], {"ok": ok, "error": error})
        return processed

    def control_room_attention(self) -> list[dict[str, Any]]:
        attention: list[dict[str, Any]] = []
        for incident in self.rows("incidents", "status='open' ORDER BY created_at DESC LIMIT 10"):
            attention.append(
                {
                    "severity": incident["severity"],
                    "kind": "incident",
                    "title": incident["title"],
                    "owner": incident["owner_agent_id"],
                    "created_at": incident["created_at"],
                    "action": f"Resolve or assign incident {incident['id']}.",
                    "entity_id": incident["id"],
                }
            )
        for approval in self.rows("approvals", "status='pending' ORDER BY created_at DESC LIMIT 10"):
            attention.append(
                {
                    "severity": approval["risk"],
                    "kind": "approval",
                    "title": approval["title"],
                    "owner": "justin",
                    "created_at": approval["created_at"],
                    "action": f"Approve or reject approval {approval['id']}.",
                    "entity_id": approval["id"],
                }
            )
        for heartbeat in self.rows("runtime_heartbeats", "status='failed' ORDER BY last_finished_at DESC LIMIT 10"):
            attention.append(
                {
                    "severity": "high" if int(heartbeat["consecutive_failures"] or 0) >= 3 else "warning",
                    "kind": "runtime_heartbeat",
                    "title": f"{heartbeat['component']} is failing",
                    "owner": "cto",
                    "created_at": heartbeat["last_finished_at"],
                    "action": "Inspect runtime heartbeat and related incidents.",
                    "entity_id": heartbeat["component"],
                }
            )
        paused = [row for row in self.runtime_controls() if not row["enabled"]]
        for control in paused:
            attention.append(
                {
                    "severity": "warning",
                    "kind": "runtime_control",
                    "title": f"{control['component']} is paused",
                    "owner": control["set_by"],
                    "created_at": control["updated_at"],
                    "action": f"Resume {control['component']} when safe.",
                    "entity_id": control["component"],
                }
            )
        if self.count("browser_jobs", "status IN ('queued','claimed')") and not any(operator["alive"] for operator in self.browser_operators()):
            attention.append(
                {
                    "severity": "warning",
                    "kind": "browser_operator",
                    "title": "Browser jobs are waiting with no live operator",
                    "owner": "cto",
                    "created_at": now_iso(),
                    "action": "Start the browser/super-browser operator service.",
                    "entity_id": "browser_operator",
                }
            )
        if self.count("outbox", "status='failed'"):
            attention.append(
                {
                    "severity": "warning",
                    "kind": "outbox",
                    "title": "Outbox delivery failures exist",
                    "owner": "cto",
                    "created_at": now_iso(),
                    "action": "Inspect /api/outbox?status=failed and provider credentials.",
                    "entity_id": "outbox",
                }
            )
        severity_order = {"critical": 0, "high": 1, "warning": 2, "medium": 3, "low": 4, "info": 5}
        attention.sort(key=lambda item: (severity_order.get(item["severity"], 5), item["created_at"] or ""), reverse=False)
        return attention[:25]

    def control_room(self) -> dict[str, Any]:
        controls = self.runtime_controls()
        heartbeats = self.runtime_heartbeats()
        attention = self.control_room_attention()
        critical_attention = [item for item in attention if item["severity"] in {"critical", "high"}]
        paused_controls = [row for row in controls if not row["enabled"]]
        failed_heartbeats = [row for row in heartbeats if row["status"] == "failed"]
        kill_switch_active = self.is_kill_switch_active()
        status = (
            "stopped"
            if kill_switch_active
            else "critical"
            if critical_attention or failed_heartbeats
            else "needs_attention"
            if attention or paused_controls
            else "running"
        )
        latest_watchdog = self.rows("watchdog_runs", "1=1 ORDER BY checked_at DESC LIMIT 1")
        latest_readiness = self.rows("readiness_checks", "1=1 ORDER BY checked_at DESC LIMIT 1")
        return {
            "status": status,
            "summary": (
                "Global kill switch is active; outreach/import operations are stopped."
                if kill_switch_active
                else f"{len(attention)} attention item(s), {len(paused_controls)} paused runtime component(s), {len(failed_heartbeats)} failed heartbeat(s)."
            ),
            "how_it_works": [
                {
                    "layer": "Entrypoints",
                    "what_it_does": "FastAPI exposes landing, dashboard, A2A messages, webhooks, Slack commands, and operator APIs.",
                    "evidence": ["/", "/dashboard", "/messages", "/slack/command", "/api/status"],
                },
                {
                    "layer": "Durable Memory",
                    "what_it_does": "SQLite is source of truth; Obsidian mirrors notes when configured; Notion sync runs through the durable outbox.",
                    "evidence": ["memory_entries", "/api/memory/health", "/api/outbox"],
                },
                {
                    "layer": "Autonomous Scheduler",
                    "what_it_does": "The scheduler ticks the CEO cycle, decision review, queues, browser recovery, workflow steps, scheduled ops, self-audit, and watchdog.",
                    "evidence": ["runtime_controls", "runtime_heartbeats", "proof_runs", "/api/ops/runtime-heartbeats"],
                },
                {
                    "layer": "CEO And Agents",
                    "what_it_does": "CEO creates plans and decisions; CMO, COO, CTO, Sales Director, and Builder work from durable tasks and messages.",
                    "evidence": ["tasks", "ceo_decisions", "agent_messages", "/api/agents/coordination"],
                },
                {
                    "layer": "Revenue And Fulfillment",
                    "what_it_does": "Leads, outreach jobs, replies, opportunities, payment handoffs, CRM sync, workflows, appointments, and reports are stored as business objects.",
                    "evidence": ["leads", "outreach_jobs", "opportunities", "crm_sync_jobs", "workflow_runs", "appointments"],
                },
                {
                    "layer": "Browser/Super-Browser",
                    "what_it_does": "Browser work is queued durably, claimed by an external operator, heartbeated, completed, retried, and written back to memory.",
                    "evidence": ["/api/browser/jobs", "/api/browser/operators", "browser_jobs", "browser_operator_heartbeats"],
                },
                {
                    "layer": "Guardrails",
                    "what_it_does": "Approvals, kill switches, runtime controls, suppression, audit log, readiness, health checks, backups, and watchdog keep autonomy bounded.",
                    "evidence": ["approvals", "kill_switches", "audit_log", "readiness_checks", "watchdog_runs"],
                },
                {
                    "layer": "Visibility",
                    "what_it_does": "Dashboard, Slack, activity feed, control room, and ops overview show what the business is doing and why.",
                    "evidence": ["/dashboard", "/api/activity", "/api/ops/control-room", "/api/ops/overview"],
                },
            ],
            "operator_controls": [
                {"label": "Run watchdog", "action": "run_watchdog", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk watchdog force"},
                {"label": "Run autonomy drill", "action": "run_autonomy_drill", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk autonomy-drill"},
                {"label": "Run self-audit", "action": "run_self_audit", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk self-audit"},
                {"label": "Create operator briefing", "action": "create_operator_briefing", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk briefing"},
                {"label": "Run VPS launch gate", "action": "run_launch_gate", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk launch-gate"},
                {"label": "Run production readiness", "action": "run_readiness", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk readiness production"},
                {"label": "Run health check", "action": "run_health", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk health"},
                {"label": "Create backup", "action": "create_backup", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk backup-now"},
                {"label": "Pause runtime component", "action": "pause_runtime", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk pause-runtime <component>"},
                {"label": "Resume runtime component", "action": "resume_runtime", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk resume-runtime <component>"},
                {"label": "Global kill switch on", "action": "kill_switch_on", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk kill-switch on <reason>"},
                {"label": "Global kill switch off", "action": "kill_switch_off", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk kill-switch off <reason>"},
                {"label": "Process outbox", "action": "process_outbox", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk outbox"},
                {"label": "Recover browser jobs", "action": "recover_browser_jobs", "endpoint": "/api/ops/control-room/action", "slack": "/dialdesk browser-jobs"},
            ],
            "attention": attention,
            "live_state": {
                "metrics": self.metrics(),
                "runtime_controls": controls,
                "runtime_heartbeats": heartbeats,
                "kill_switches": self.rows("kill_switches"),
                "latest_watchdog": latest_watchdog[0] if latest_watchdog else None,
                "latest_autonomy_drill": self.rows("autonomy_drills", "1=1 ORDER BY completed_at DESC LIMIT 1"),
                "latest_launch_gate": self.rows("launch_gate_runs", "1=1 ORDER BY checked_at DESC LIMIT 1"),
                "latest_self_audit": self.rows("proof_runs", "proof_type='runtime_self_audit' ORDER BY completed_at DESC LIMIT 1"),
                "latest_readiness": latest_readiness[0] if latest_readiness else None,
                "open_incidents": self.rows("incidents", "status='open' ORDER BY created_at DESC LIMIT 10"),
                "pending_approvals": self.rows("approvals", "status='pending' ORDER BY created_at DESC LIMIT 10"),
                "browser_operators": self.browser_operators(),
                "memory_health": self.memory_health(),
                "coordination": self.coordination_room(),
                "recent_activity": self.activity_feed(limit=20),
            },
            "deployment": {
                "public_url": PUBLIC_URL,
                "database": self.db_path,
                "database_persistent": self._db_path_is_persistent(),
                "app_token_configured": bool(APP_TOKEN),
                "scheduler_interval_seconds": SCHEDULER_INTERVAL_SECONDS,
                "self_audit_interval_seconds": SELF_AUDIT_INTERVAL_SECONDS,
                "backup_dir": BACKUP_DIR,
                "backup_dir_persistent": self._path_is_persistent(BACKUP_DIR),
                "providers": provider_config_status(),
                "send_outbox": SEND_OUTBOX,
                "send_outreach": SEND_OUTREACH,
                "browser_operator_poll_seconds": BROWSER_OPERATOR_POLL_SECONDS,
            },
        }

    def run_control_room_action(self, req: ControlRoomActionIn) -> dict[str, Any]:
        action = req.action.strip().lower().replace("-", "_")
        metadata = {"source": "control_room", **req.metadata}
        if action == "run_watchdog":
            result = self.run_watchdog(WatchdogRunIn(source=req.actor, force=req.force, metadata=metadata))
        elif action == "run_autonomy_drill":
            result = self.run_autonomy_drill(AutonomyDrillRunIn(requested_by=req.actor, force_cycle=req.force, metadata=metadata))
        elif action == "run_self_audit":
            result = self.run_self_audit(SelfAuditRunIn(requested_by=req.actor, force=req.force, metadata=metadata))
        elif action == "create_operator_briefing":
            result = self.create_operator_briefing(
                OperatorBriefingCreateIn(requested_by=req.actor, force=req.force, include_slack=True, metadata=metadata)
            )
        elif action == "run_launch_gate":
            result = self.run_launch_gate(LaunchGateRunIn(profile=req.profile, requested_by="cto", metadata=metadata))
        elif action == "run_readiness":
            result = self.run_readiness_check(ReadinessCheckCreateIn(profile=req.profile, requested_by="cto", metadata=metadata))
        elif action == "run_health":
            result = self.run_health_check("control_room")
        elif action == "create_backup":
            result = self.create_backup(BackupCreateIn(reason=req.reason or "control_room", requested_by="cto", metadata=metadata))
        elif action == "pause_runtime":
            if not req.component:
                raise HTTPException(status_code=400, detail="component is required for pause_runtime")
            result = self.set_runtime_control(
                RuntimeControlIn(
                    component=req.component,
                    enabled=False,
                    reason=req.reason or f"Paused from control room by {req.actor}",
                    set_by=req.actor,
                    metadata=metadata,
                )
            )
        elif action == "resume_runtime":
            if not req.component:
                raise HTTPException(status_code=400, detail="component is required for resume_runtime")
            result = self.set_runtime_control(
                RuntimeControlIn(
                    component=req.component,
                    enabled=True,
                    reason=req.reason or f"Resumed from control room by {req.actor}",
                    set_by=req.actor,
                    metadata=metadata,
                )
            )
        elif action in {"kill_switch_on", "kill_switch_off"}:
            result = self.set_kill_switch(
                KillSwitchIn(
                    active=action == "kill_switch_on",
                    scope="global",
                    reason=req.reason or f"{action.replace('_', ' ')} from control room by {req.actor}",
                    set_by=req.actor,
                )
            )
        elif action == "process_outbox":
            result = {"processed": self.process_outbox_once(limit=25)}
        elif action == "recover_browser_jobs":
            result = {"recovered": self.recover_expired_browser_jobs()}
        elif action == "tick_cycle":
            result = self.tick_operating_cycle("control_room")
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported control room action: {req.action}")
        self.audit(req.actor, f"control_room_{action}", "control_room", action, model_to_dict(req))
        return {"action": action, "result": result, "control_room": self.control_room()}

    def ops_overview(self) -> dict[str, Any]:
        proof_commands = self.vps_launch_commands()
        latest_health_check = self.rows("health_checks", "1=1 ORDER BY checked_at DESC LIMIT 1")
        latest_readiness_check = self.rows("readiness_checks", "1=1 ORDER BY checked_at DESC LIMIT 1")
        latest_watchdog_run = self.rows("watchdog_runs", "1=1 ORDER BY checked_at DESC LIMIT 1")
        latest_autonomy_drill = self.rows("autonomy_drills", "1=1 ORDER BY completed_at DESC LIMIT 1")
        latest_launch_gate = self.rows("launch_gate_runs", "1=1 ORDER BY checked_at DESC LIMIT 1")
        latest_backup = self.rows("backups", "1=1 ORDER BY created_at DESC LIMIT 1")
        latest_self_audit = self.rows("proof_runs", "proof_type='runtime_self_audit' ORDER BY completed_at DESC LIMIT 1")
        browser_operators = self.browser_operators()
        proof_primary = next(
            (command for command in proof_commands if command["step"] == "Prove full business stack"),
            proof_commands[0] if proof_commands else {},
        )
        return {
            "runtime": {
                "service": "DialDesk Autonomous Appointment Setting OS",
                "database": self.db_path,
                "public_url": PUBLIC_URL,
                "scheduler_interval_seconds": SCHEDULER_INTERVAL_SECONDS,
                "ceo_daily_hour_utc": CEO_DAILY_HOUR_UTC,
                "ceo_eod_hour_utc": CEO_EOD_HOUR_UTC,
                "controls": self.runtime_control_map(),
                "heartbeats": {row["component"]: row for row in self.runtime_heartbeats()},
            },
            "truth": PRODUCT["truth"],
            "operators": {
                "ceo": "Daily review, decisions, delegation, approvals, escalation.",
                "cmo": "Campaigns, offer, lists, copy, ROI.",
                "coo": "Onboarding, launch checklist, fulfillment, weekly reports.",
                "cto": "Infrastructure, integrations, health, browser jobs.",
                "sales_director": "Qualification, replies, bookings, sales-call prep.",
                "builder": "Approved system improvements.",
            },
            "state_tables": [
                "clients",
                "leads",
                "campaigns",
                "conversations",
                "appointments",
                "tasks",
                "events",
                "ceo_decisions",
                "approvals",
                "reports",
                "reps",
                "payments",
                "opportunities",
                "sales_briefs",
                "payment_links",
                "crm_sync_jobs",
                "audit_log",
                "memory_entries",
                "outbox",
                "browser_jobs",
                "outreach_jobs",
                "integration_syncs",
                "governance_goals",
                "agent_budgets",
                "agent_heartbeats",
                "agent_messages",
                "health_checks",
                "incidents",
                "backups",
                "readiness_checks",
                "operating_cycles",
                "request_log",
                "workflow_runs",
                "workflow_steps",
                "kill_switches",
                "runtime_controls",
                "runtime_heartbeats",
                "watchdog_runs",
                "autonomy_drills",
                "launch_gate_runs",
                "proof_runs",
            ],
            "memory": {
                "sqlite": True,
                "obsidian_configured": bool(OBSIDIAN_VAULT_PATH),
                "notion_configured": bool(NOTION_API_KEY and NOTION_DATABASE_ID),
                "entries": self.count("memory_entries"),
                "health": self.memory_health(),
            },
            "visibility": {
                "dashboard": "/dashboard",
                "status_api": "/api/status",
                "activity_api": "/api/activity",
                "control_room_api": "/api/ops/control-room",
                "slack_webhook_configured": bool(SLACK_WEBHOOK_URL),
                "queued_outbox": self.count("outbox", "status='queued'"),
                "failed_outbox": self.count("outbox", "status='failed'"),
                "auth_required": bool(APP_TOKEN),
                "recent_requests": self.rows("request_log", "1=1 ORDER BY created_at DESC LIMIT 10"),
                "unauthorized_requests_today": self.count("request_log", "status_code=401 AND date(created_at)=date('now')"),
                "recent_activity": self.activity_feed(limit=15),
            },
            "browser_automation": {
                "queue": "/api/browser/jobs",
                "queued": self.count("browser_jobs", "status='queued'"),
                "claimed": self.count("browser_jobs", "status='claimed'"),
                "completed": self.count("browser_jobs", "status='completed'"),
                "failed": self.count("browser_jobs", "status='failed'"),
                "operators": "/api/browser/operators",
                "operator_heartbeat": "/api/browser/operators/heartbeat",
                "online_operators": len([row for row in browser_operators if row["alive"]]),
                "latest_operators": browser_operators[:5],
                "expired_claims": self.count(
                    "browser_jobs",
                    "status='claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                    (now_iso(),),
                ),
            },
            "proof_pack": {
                "endpoint": "/api/ops/proof-pack",
                "primary": proof_primary,
                "commands": proof_commands,
                "latest_health_check": latest_health_check,
                "latest_readiness_check": latest_readiness_check,
                "latest_watchdog_run": latest_watchdog_run,
                "latest_autonomy_drill": latest_autonomy_drill,
                "latest_launch_gate": latest_launch_gate,
                "latest_self_audit": latest_self_audit,
                "latest_backup": latest_backup,
                "latest_proof_runs": self.rows("proof_runs", "1=1 ORDER BY completed_at DESC LIMIT 10"),
                "signals": [
                    {
                        "proof": "Full business stack",
                        "status": "primary",
                        "command": proof_primary.get("command", ""),
                    },
                    {
                        "proof": "Launch gate",
                        "status": latest_launch_gate[0]["status"] if latest_launch_gate else "not_run",
                        "command": "BASE_URL=http://127.0.0.1:8080 DIALDESK_LAUNCH_PROFILE=vps bash scripts/ops_doctor.sh",
                    },
                    {
                        "proof": "Runtime self-audit",
                        "status": latest_self_audit[0]["status"] if latest_self_audit else "not_run",
                        "command": "POST /api/ops/self-audit/run",
                    },
                    {
                        "proof": "Autonomy drill",
                        "status": latest_autonomy_drill[0]["status"] if latest_autonomy_drill else "not_run",
                        "command": "curl -s -X POST http://127.0.0.1:8080/api/ops/autonomy-drills/run -H 'Content-Type: application/json' -d '{\"requested_by\":\"ceo\"}' | jq .summary",
                    },
                    {
                        "proof": "Memory stack",
                        "status": "configured" if (OBSIDIAN_VAULT_PATH or (NOTION_API_KEY and NOTION_DATABASE_ID)) else "local_only",
                        "command": "BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh",
                    },
                    {
                        "proof": "Slack stack",
                        "status": "configured" if (SLACK_WEBHOOK_URL or SLACK_SIGNING_SECRET) else "needs_config",
                        "command": "BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh",
                    },
                    {
                        "proof": "Browser stack",
                        "status": "online" if any(row["alive"] for row in browser_operators) else "not_seen",
                        "command": "BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/browser_stack_probe.sh",
                    },
                ],
            },
            "outreach_execution": {
                "queue": "/api/outreach/jobs",
                "send_enabled": SEND_OUTREACH,
                "queued": self.count("outreach_jobs", "status='queued'"),
                "sent": self.count("outreach_jobs", "status='sent'"),
                "dry_run": self.count("outreach_jobs", "status='dry_run'"),
                "failed": self.count("outreach_jobs", "status='failed'"),
            },
            "integration_sync": {
                "queue": "/api/integrations/syncs",
                "configured": provider_config_status(),
                "queued": self.count("integration_syncs", "status='queued'"),
                "completed": self.count("integration_syncs", "status='completed'"),
                "failed": self.count("integration_syncs", "status='failed'"),
            },
            "revenue_loop": {
                "opportunities": "/api/opportunities",
                "sales_briefs": "/api/sales-briefs",
                "payment_links": "/api/payment-links",
                "crm_sync_jobs": "/api/crm/sync-jobs",
                "open_opportunities": self.count("opportunities", "status NOT IN ('closed_won','closed_lost')"),
                "ready_sales_briefs": self.count("sales_briefs", "status='ready'"),
                "ready_payment_links": self.count("payment_links", "status IN ('ready','dry_run')"),
                "queued_crm_sync_jobs": self.count("crm_sync_jobs", "status='queued'"),
                "payment_link_creation_enabled": CREATE_PAYMENT_LINKS,
                "crm_sync_enabled": SYNC_CRM,
            },
            "governance": {
                "goals": "/api/governance/goals",
                "budgets": "/api/governance/budgets",
                "heartbeats": "/api/governance/heartbeats",
                "coordination_room": "/api/agents/coordination",
                "agent_threads": "/api/agent-threads",
                "agent_inbox": "/api/agents/{agent_id}/inbox",
                "agent_messages": "/api/agent-messages",
                "agent_standup": "/api/agents/standup",
                "ceo_decisions": "/api/ceo/decisions",
                "ceo_decision_review": "/api/ceo/decisions/evaluate",
                "slack_commands": "/api/slack/command",
                "active_goals": self.count("governance_goals", "status='active'"),
                "paused_agents": self.count("agent_budgets", "status='paused'"),
                "unread_agent_messages": self.count("agent_messages", "status='unread'"),
                "messages_today": self.count("agent_messages", "date(created_at)=date('now')"),
                "decisions_today": self.count("ceo_decisions", "date(created_at)=date('now')"),
                "decisions_waiting": self.count("ceo_decisions", "status IN ('waiting_on_approval','waiting_on_justin')"),
                "open_threads": len(self.agent_threads(limit=100)),
                "justin_unread": self.count("agent_messages", "to_agent_id='justin' AND status='unread'"),
                "ceo_unread": self.count("agent_messages", "to_agent_id='ceo' AND status='unread'"),
                "recent_decisions": self.rows("ceo_decisions", "1=1 ORDER BY created_at DESC LIMIT 10"),
                "latest_heartbeats": self.rows("agent_heartbeats", "1=1 ORDER BY created_at DESC LIMIT 6"),
                "recent_messages": self.rows("agent_messages", "1=1 ORDER BY created_at DESC LIMIT 10"),
            },
            "operating_cycle": {
                "current": self.rows("operating_cycles", "day_key=? ORDER BY started_at DESC LIMIT 1", (today_key(),)),
                "history": "/api/ceo/operating-cycles",
                "start": "/api/ceo/operating-cycle/start",
                "tick": "/api/ceo/operating-cycle/tick",
                "close": "/api/ceo/operating-cycle/close",
                "daily_hour_utc": CEO_DAILY_HOUR_UTC,
                "eod_hour_utc": CEO_EOD_HOUR_UTC,
            },
            "production_ops": {
                "health_checks": "/api/ops/health-checks",
                "run_health_check": "/api/ops/health-checks/run",
                "runtime_controls": "/api/ops/runtime-controls",
                "runtime_heartbeats": "/api/ops/runtime-heartbeats",
                "readiness_checks": "/api/ops/readiness",
                "run_readiness_check": "/api/ops/readiness/run",
                "incidents": "/api/ops/incidents",
                "backups": "/api/ops/backups",
                "create_backup": "/api/ops/backups/create",
                "health_interval_seconds": HEALTH_CHECK_INTERVAL_SECONDS,
                "backup_interval_seconds": BACKUP_INTERVAL_SECONDS,
                "backup_dir": BACKUP_DIR,
                "latest_health_check": latest_health_check,
                "latest_readiness_check": latest_readiness_check,
                "runtime_failed_components": self.count("runtime_heartbeats", "status='failed'"),
                "runtime_paused_components": self.count("runtime_heartbeats", "status='paused'"),
                "latest_runtime_heartbeats": self.rows("runtime_heartbeats", "1=1 ORDER BY last_finished_at DESC LIMIT 10"),
                "watchdog_runs": "/api/ops/watchdog",
                "run_watchdog": "/api/ops/watchdog/run",
                "latest_watchdog_run": latest_watchdog_run,
                "self_audit_runs": "/api/ops/proof-runs?proof_type=runtime_self_audit",
                "run_self_audit": "/api/ops/self-audit/run",
                "self_audit_interval_seconds": SELF_AUDIT_INTERVAL_SECONDS,
                "latest_self_audit": latest_self_audit,
                "autonomy_drills": "/api/ops/autonomy-drills",
                "run_autonomy_drill": "/api/ops/autonomy-drills/run",
                "latest_autonomy_drill": latest_autonomy_drill,
                "launch_gate_runs": "/api/ops/launch-gate",
                "run_launch_gate": "/api/ops/launch-gate/run",
                "latest_launch_gate": latest_launch_gate,
                "proof_runs": "/api/ops/proof-runs",
                "record_proof_run": "/api/ops/proof-runs",
                "latest_proof_runs": self.rows("proof_runs", "1=1 ORDER BY completed_at DESC LIMIT 10"),
                "open_incidents": self.count("incidents", "status='open'"),
                "latest_incidents": self.rows("incidents", "1=1 ORDER BY created_at DESC LIMIT 10"),
                "latest_backup": latest_backup,
                "db_size_bytes": file_size(self.db_path),
            },
            "workflow_ops": {
                "runs": "/api/workflows/runs",
                "steps": "/api/workflows/steps",
                "launch": "/api/workflows/launch",
                "execute_due": "/api/workflows/execute-due",
                "active_runs": self.count("workflow_runs", "status='active'"),
                "due_steps": self.count("workflow_steps", "status='pending' AND due_at <= ?", (now_iso(),)),
                "queued_steps": self.count("workflow_steps", "status='queued'"),
                "completed_steps": self.count("workflow_steps", "status='completed'"),
                "recent_runs": self.rows("workflow_runs", "1=1 ORDER BY created_at DESC LIMIT 10"),
            },
            "guardrails": {
                "approval_queue": "/api/approvals",
                "global_kill_switch": bool(
                    (self.get_one("SELECT active FROM kill_switches WHERE scope='global'") or {"active": 0})["active"]
                ),
                "pending_approvals": self.count("approvals", "status='pending'"),
            },
        }

    def run_worker_once(self) -> int:
        if self.is_kill_switch_active() or not self.runtime_component_enabled("worker"):
            return 0
        rows = self.get_all(
            """
            SELECT * FROM tasks
            WHERE status='pending'
            ORDER BY priority ASC, created_at ASC
            LIMIT 5
            """
        )
        processed = 0
        for row in rows:
            agent = self.agents.get(row["agent_id"])
            if agent and agent.status == "paused":
                continue
            if agent:
                agent.status = "working"
                agent.current_task = row["title"]
                agent.last_seen = now_iso()
            metadata = json_loads(row["metadata"], {})
            if metadata.get("requires_human") or metadata.get("requires_browser"):
                continue
            self.db.execute(
                "UPDATE tasks SET status='working', updated_at=? WHERE id=?",
                (now_iso(), row["id"]),
            )
            self.db.commit()
            result = autonomous_task_result(row["title"], row["agent_id"])
            self.complete_task(row["id"], result, "completed", {"worker": "autonomous"})
            processed += 1
        return processed


def asdict_like(model: BaseModel) -> dict[str, Any]:
    return model_to_dict(model)


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:64] or "note"


def write_obsidian_note(
    memory_id: str,
    title: str,
    body: str,
    kind: str,
    actor: str,
    client_id: str,
    tags: list[str],
    metadata: dict[str, Any],
) -> str:
    if not OBSIDIAN_VAULT_PATH:
        return ""
    try:
        root = pathlib.Path(OBSIDIAN_VAULT_PATH).expanduser()
        folder = root / "DialDesk" / today_key()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{slugify(title)}-{memory_id}.md"
        frontmatter = {
            "id": memory_id,
            "created_at": now_iso(),
            "kind": kind,
            "actor": actor,
            "client_id": client_id,
            "tags": tags,
            "metadata": metadata,
        }
        content = (
            "---\n"
            f"{json.dumps(frontmatter, indent=2)}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"{body.strip()}\n"
        )
        path.write_text(content, encoding="utf-8")
        return str(path)
    except OSError as exc:
        logger.warning("Obsidian write failed: %s", exc)
        return ""


def deliver_slack(row: sqlite3.Row) -> tuple[bool, str, dict[str, Any]]:
    if not SLACK_WEBHOOK_URL:
        return False, "SLACK_WEBHOOK_URL not configured", {}
    payload = json_loads(row["payload"], {})
    channel = payload.get("channel", "dialdesk-ops")
    text = (
        f"*{row['subject']}*\n"
        f"Client: `{row['client_id']}`\n"
        f"Severity: `{row['severity']}`\n\n"
        f"{row['body']}"
    )
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(SLACK_WEBHOOK_URL, json={"text": text, "channel": channel})
            response.raise_for_status()
        return True, "", {"status_code": response.status_code}
    except httpx.HTTPError as exc:
        return False, str(exc), {}


def deliver_notion(row: sqlite3.Row) -> tuple[bool, str, dict[str, Any]]:
    if not (NOTION_API_KEY and NOTION_DATABASE_ID):
        return False, "NOTION_API_KEY or NOTION_DATABASE_ID not configured", {}
    payload = json_loads(row["payload"], {})
    memory_id = payload.get("memory_id", row["id"])
    page = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": row["subject"][:2000]}}]},
            "Kind": {"rich_text": [{"text": {"content": payload.get("kind", row["destination"])[:2000]}}]},
            "Client": {"rich_text": [{"text": {"content": row["client_id"][:2000]}}]},
            "Memory ID": {"rich_text": [{"text": {"content": str(memory_id)[:2000]}}]},
        },
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": row["body"][:2000]}}]},
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post("https://api.notion.com/v1/pages", headers=headers, json=page)
            response.raise_for_status()
        payload = safe_json(response)
        result = {"status_code": response.status_code}
        if isinstance(payload, dict):
            result["page_id"] = payload.get("id", "")
            result["url"] = payload.get("url", "")
        return True, "", result
    except httpx.HTTPError as exc:
        return False, str(exc), {}


def clean(value: Any) -> str:
    return str(value or "").strip()


def money(value: Any) -> float:
    text = re.sub(r"[^0-9.]", "", str(value or ""))
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def stripe_payment_link_for_package(package: str) -> str:
    key = f"STRIPE_PAYMENT_LINK_{package.upper()}"
    return os.getenv(key, "")


def score_opportunity(raw: dict[str, Any]) -> int:
    score = 20
    if clean(raw.get("email")):
        score += 20
    if clean(raw.get("phone")):
        score += 20
    if clean(raw.get("company_name")):
        score += 15
    if clean(raw.get("contact_name")):
        score += 10
    if raw.get("package_interest") == "human_sdr":
        score += 10
    if clean(raw.get("notes")):
        score += 5
    return min(score, 100)


def build_sales_brief_body(opportunity: dict[str, Any], client: dict[str, Any]) -> str:
    package = PRODUCT["packages"].get(opportunity.get("package_interest"), PRODUCT["packages"]["human_sdr"])
    contact = opportunity.get("contact_name") or "Unknown contact"
    return (
        f"Sales Call Brief\n\n"
        f"Prospect: {opportunity.get('company_name')}\n"
        f"Contact: {contact}\n"
        f"Email: {opportunity.get('email') or 'not provided'}\n"
        f"Phone: {opportunity.get('phone') or 'not provided'}\n"
        f"Source: {opportunity.get('source')}\n"
        f"Interest: {package['label']} at ${package['price_monthly']:,.0f}/month\n"
        f"Score: {opportunity.get('score')}/100\n\n"
        f"Positioning: DialDesk is managed appointment setting for MCA/funding companies. "
        f"We sell outreach, qualification, follow-up, AI/human SDR execution, booking, "
        f"and weekly reporting. We do not offer loans.\n\n"
        f"Recommended pitch: keep the call simple. Confirm they want more qualified MCA/funding "
        f"appointments, ask what list/source they already have, ask whether they prefer AI SDR "
        f"or human SDR coverage, then move them to onboarding once payment/access is handled.\n\n"
        f"Internal client account: {client.get('name')} ({client.get('id')}).\n"
        f"Notes: {opportunity.get('notes') or 'No notes yet.'}"
    )


def safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:2000]


def file_size(path: str) -> int:
    try:
        return pathlib.Path(path).stat().st_size
    except OSError:
        return 0


def iso_age_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (utcnow() - parsed).total_seconds())
    except ValueError:
        return 0.0


def lease_until(seconds: int) -> str:
    bounded = max(60, min(int(seconds or 900), 3600))
    return (utcnow() + timedelta(seconds=bounded)).isoformat()


def workflow_due_at(day_offset: int) -> str:
    return (utcnow() + timedelta(days=max(0, int(day_offset or 0)))).isoformat()


def onboarding_playbook_steps() -> list[dict[str, Any]]:
    return [
        {
            "key": "day0_access_intake",
            "day_offset": 0,
            "title": "Day 0: confirm intake, CRM, calendar, dialer, compliance, and access",
            "owner_agent_id": "coo",
            "priority": 1,
            "prompt": "Confirm every launch access item and escalate missing access before outreach begins.",
        },
        {
            "key": "day0_offer_scripts",
            "day_offset": 0,
            "title": "Day 0: prepare MCA/funding offer angles, SMS copy, email copy, and SDR script",
            "owner_agent_id": "cmo",
            "priority": 1,
            "prompt": "Prepare launch copy for managed MCA/funding appointment setting. Do not position DialDesk as a lender.",
        },
        {
            "key": "day1_lead_import",
            "day_offset": 1,
            "title": "Day 1: import, dedupe, score, and suppress launch lead list",
            "owner_agent_id": "sales_director",
            "priority": 1,
            "prompt": "Import client lead list, dedupe it, apply suppression, and flag missing contact fields.",
        },
        {
            "key": "day2_integration_qa",
            "day_offset": 2,
            "title": "Day 2: verify Sendivo, email, CRM, calendar, webhook, and reporting sync",
            "owner_agent_id": "cto",
            "priority": 1,
            "prompt": "Run integration QA and create incidents for broken provider paths.",
        },
        {
            "key": "day3_launch_approval",
            "day_offset": 3,
            "title": "Day 3: review launch readiness and request campaign approval",
            "owner_agent_id": "coo",
            "priority": 1,
            "prompt": "Confirm launch readiness and create approval if campaign send/call activity should begin.",
        },
        {
            "key": "day5_reply_booking_review",
            "day_offset": 5,
            "title": "Day 5: review replies, booking quality, no-shows, and follow-up gaps",
            "owner_agent_id": "sales_director",
            "priority": 2,
            "prompt": "Review early response quality and create next-action tasks for hot replies and no-shows.",
        },
        {
            "key": "day7_first_report",
            "day_offset": 7,
            "title": "Day 7: create first client launch report",
            "owner_agent_id": "coo",
            "priority": 2,
            "prompt": "Create a first-week client debrief with sends, replies, bookings, blockers, and recommendations.",
        },
        {
            "key": "day14_optimization",
            "day_offset": 14,
            "title": "Day 14: optimize campaign, list source, SDR routing, and guarantee risk",
            "owner_agent_id": "ceo",
            "priority": 2,
            "prompt": "Review client outcome and decide whether to scale, change copy, change list source, or adjust SDR coverage.",
        },
    ]


def is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "opted_out", "do_not_contact"}


def valid_actor(actor: str) -> bool:
    return actor in AGENTS or actor in {"justin", "system", "browser", "slack", "calendar", "stripe", "crm"}


PUBLIC_PATHS = {"/", "/dashboard", "/dashboard.html", "/health", "/agentCard", "/openapi.json", "/docs", "/redoc", "/favicon.ico"}


def path_requires_auth(path: str) -> bool:
    if not APP_TOKEN:
        return False
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return False
    return path.startswith("/api/") or path in {"/messages"} or path.startswith("/tasks/")


def request_token(request: Request) -> str:
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer.split(" ", 1)[1].strip()
    return request.headers.get("x-api-token", "").strip()


def request_actor(request: Request, authorized: bool) -> str:
    header_actor = clean(request.headers.get("x-operator"))
    if header_actor:
        return header_actor[:80]
    return "operator" if authorized else "anonymous"


def token_is_valid(token: str) -> bool:
    return bool(APP_TOKEN and token and secrets.compare_digest(token, APP_TOKEN))


def verify_slack_signature(headers: dict[str, str], raw_body: bytes) -> None:
    if not SLACK_SIGNING_SECRET:
        return
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Slack timestamp") from exc
    if abs(time.time() - ts) > 60 * 5:
        raise HTTPException(status_code=401, detail="Stale Slack request")
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    digest = hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


def slack_form_to_command(raw_body: bytes) -> SlackCommandIn:
    values = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    command = (values.get("command", ["dialdesk"])[0] or "dialdesk").strip().lstrip("/")
    text = (values.get("text", [""])[0] or "").strip()
    user = (
        values.get("user_name", [""])[0]
        or values.get("user_id", [""])[0]
        or values.get("user", [""])[0]
        or "Slack"
    )
    client_id = values.get("client_id", [DEFAULT_CLIENT_ID])[0] or DEFAULT_CLIENT_ID
    if command == "dialdesk" and text:
        parts = text.split(maxsplit=1)
        command = parts[0].strip().lstrip("/") or "status"
        text = parts[1] if len(parts) > 1 else ""
    metadata = {
        "team_id": values.get("team_id", [""])[0],
        "channel_id": values.get("channel_id", [""])[0],
        "channel_name": values.get("channel_name", [""])[0],
        "response_url_present": bool(values.get("response_url", [""])[0]),
        "trigger_id_present": bool(values.get("trigger_id", [""])[0]),
        "source": "slack_slash_command",
    }
    return SlackCommandIn(command=command, text=text, user=user, client_id=client_id, metadata=metadata)


def score_lead(raw: dict[str, Any]) -> int:
    score = 0
    revenue = money(raw.get("monthly_revenue") or raw.get("revenue"))
    amount = money(raw.get("funding_amount") or raw.get("amount"))
    if revenue >= 20000:
        score += 30
    if amount >= 50000:
        score += 25
    if clean(raw.get("phone")):
        score += 15
    if clean(raw.get("email")):
        score += 10
    if clean(raw.get("existing_funder") or raw.get("current_lender")):
        score += 10
    if clean(raw.get("timeline") or raw.get("urgency")).lower() in {"now", "asap", "urgent", "1", "2"}:
        score += 10
    return min(score, 100)


def lead_tier(score: int | float | None) -> str:
    try:
        value = int(score or 0)
    except (TypeError, ValueError):
        value = 0
    if value >= 80:
        return "A"
    if value >= 50:
        return "B"
    return "C"


def provider_config_status() -> dict[str, Any]:
    return {
        "sendivo": {
            "sms_send": bool(os.getenv("SENDIVO_BEARER_AUTH")),
            "logs": bool(os.getenv("SENDIVO_BEARER_AUTH")),
            "base_url": os.getenv("SENDIVO_BASE_URL", "https://app.sendivo.io/api/v1"),
        },
        "smartlead": {
            "email_send": bool(os.getenv("SMARTLEAD_API_KEY")),
            "reply_poll": bool(os.getenv("SMARTLEAD_API_KEY")),
            "base_url": os.getenv("SMARTLEAD_BASE_URL", "https://server.smartlead.ai/api/v1"),
        },
        "ghl": {
            "configured": bool(os.getenv("GHL_API_KEY") and os.getenv("GHL_LOCATION_ID")),
            "sync_enabled": SYNC_CRM,
            "contact_upsert_url_configured": bool(GHL_CONTACT_UPSERT_URL),
        },
        "stripe": {
            "configured": bool(os.getenv("STRIPE_SECRET_KEY")),
            "payment_link_creation_enabled": CREATE_PAYMENT_LINKS,
            "ai_caller_link_configured": bool(stripe_payment_link_for_package("ai_caller")),
            "human_sdr_link_configured": bool(stripe_payment_link_for_package("human_sdr")),
        },
        "slack": {
            "webhook_configured": bool(SLACK_WEBHOOK_URL),
            "send_enabled": SEND_OUTBOX,
        },
        "notion": {
            "configured": bool(NOTION_API_KEY and NOTION_DATABASE_ID),
        },
        "outreach": {
            "send_enabled": SEND_OUTREACH,
        },
    }


def normalize_direction(value: Any) -> str:
    text = clean(value).lower()
    if text in {"inbound", "incoming", "received", "reply", "replied", "mo"}:
        return "inbound"
    return "outbound"


def extract_sendivo_logs(result: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("logs", "data", "items", "messages"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for nested_key in ("logs", "data", "items", "messages"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def score_conversation(body: str) -> tuple[str, int]:
    text = body.lower()
    if any(w in text for w in ["stop", "unsubscribe", "remove me"]):
        return "opt_out", 0
    score = 3
    intent = "neutral"
    if any(w in text for w in ["interested", "need funding", "how much", "terms", "approved", "apply"]):
        intent = "funding_interest"
        score += 4
    if any(w in text for w in ["call", "talk", "schedule", "appointment", "book"]):
        intent = "booking_intent"
        score += 3
    if any(w in text for w in ["today", "asap", "now", "this week"]):
        score += 2
    return intent, min(score, 10)


def setup_checklist(client: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = json_loads(client.get("metadata"), {})
    return [
        {"key": "intake", "label": "Intake completed", "done": client.get("onboarding_stage") != "not_started"},
        {"key": "crm", "label": "CRM access connected", "done": bool(client.get("crm_url") or metadata.get("crm_connected"))},
        {"key": "calendar", "label": "Calendar connected", "done": bool(client.get("calendar_url") or metadata.get("calendar_connected"))},
        {"key": "lead_list", "label": "Lead list imported", "done": bool(metadata.get("lead_list_ready"))},
        {"key": "campaign", "label": "Campaign playbook approved", "done": bool(metadata.get("campaign_ready"))},
        {"key": "rep", "label": "AI or human SDR assigned", "done": bool(metadata.get("rep_assigned"))},
    ]


def autonomous_task_result(title: str, agent_id: str) -> str:
    if agent_id == "cto":
        return "Health check queued; no critical failures detected in durable runtime."
    if agent_id == "coo":
        return "Onboarding and fulfillment queue reviewed; blockers converted into tasks or approvals."
    if agent_id == "cmo":
        return "Campaign and offer metrics reviewed; optimization recommendations recorded."
    if agent_id == "sales_director":
        return "Hot replies, bookings, and qualification queue reviewed for sales follow-up."
    return f"{title} reviewed."


def route_agent(prompt: str) -> tuple[str, str, float]:
    p = prompt.lower()
    budget = 0.0
    if "$" in prompt:
        match = re.search(r"\$([\d,]+(?:\.\d{2})?)", prompt)
        if match:
            budget = float(match.group(1).replace(",", ""))
    if budget > 500:
        return "ceo", "approval", budget
    if any(w in p for w in ["campaign", "copy", "sms", "email", "list", "lead program", "roi", "offer"]):
        return "cmo", "campaign", budget
    if any(w in p for w in ["server", "deploy", "api", "health", "bug", "integration", "database", "stripe"]):
        return "cto", "tech", budget
    if any(w in p for w in ["onboard", "client", "setup", "rep", "weekly report", "fulfillment", "guarantee"]):
        return "coo", "ops", budget
    if any(w in p for w in ["sales", "qualify", "reply", "book", "appointment", "objection", "call"]):
        return "sales_director", "sales", budget
    return "ceo", "general", budget


STATE = DialDeskState(DB_PATH)


async def route_to_agent(prompt: str, from_user: str = "CEO", client_id: str = DEFAULT_CLIENT_ID) -> dict[str, Any]:
    agent_id, action, budget = route_agent(prompt)
    metadata = {"from": from_user, "action": action, "budget": budget}
    if action == "approval":
        approval = STATE.create_approval(
            client_id=client_id,
            approval_type="spend",
            title=f"Spend approval needed: ${budget:,.2f}",
            risk="high",
            payload={"prompt": prompt, "budget": budget},
        )
        return {
            "response": f"[CEO] Approval queued for ${budget:,.2f}.",
            "routed_to": "ceo",
            "agent_name": "CEO",
            "approval_id": approval["id"],
            "action": action,
            "budget_approved": False,
        }
    task = STATE.create_task(
        title=f"{AGENTS[agent_id].name}: {action}",
        agent_id=agent_id,
        client_id=client_id,
        prompt=prompt,
        priority=2,
        metadata=metadata,
        status="pending",
    )
    STATE.run_worker_once()
    latest = STATE.get_one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    result = latest["result"] if latest else ""
    return {
        "response": result or f"[{AGENTS[agent_id].name}] Task queued.",
        "routed_to": agent_id,
        "agent_name": AGENTS[agent_id].name,
        "task_id": task["id"],
        "action": action,
        "budget_approved": budget <= 500,
    }


async def scheduler_loop() -> None:
    async def run_component(component: str, action: Any) -> Any:
        if not STATE.runtime_component_enabled(component):
            return STATE.record_runtime_heartbeat(component, "paused", result={"skipped": True})
        started = now_iso()
        try:
            result = action()
            if inspect.isawaitable(result):
                result = await result
            STATE.record_runtime_heartbeat(component, "ok", started_at=started, result={"result": result})
            return result
        except Exception as exc:
            logger.exception("scheduler component failed: %s", component)
            STATE.record_runtime_heartbeat(component, "failed", started_at=started, error=str(exc))
            return None

    while True:
        try:
            if STATE.runtime_component_enabled("scheduler"):
                scheduler_started = now_iso()
                cycle = STATE.tick_operating_cycle("scheduler")
                await run_component("ceo_decision_review", lambda: STATE.run_ceo_decision_review(CeoDecisionReviewIn(source="scheduler")))
                await run_component("outbox", STATE.process_outbox_once)
                await run_component("outreach", STATE.execute_outreach_queue_once)
                await run_component("integration_sync", STATE.execute_integration_sync_queue_once)
                await run_component("crm_sync", STATE.execute_crm_sync_queue_once)
                await run_component("browser_recovery", STATE.recover_expired_browser_jobs)
                await run_component("workflow", STATE.execute_due_workflow_steps)
                await run_component("worker", STATE.run_worker_once)
                await run_component("scheduled_ops", STATE.maybe_run_scheduled_ops)
                await run_component("self_audit", STATE.maybe_run_self_audit)
                STATE.record_runtime_heartbeat("scheduler", "ok", started_at=scheduler_started, result={"cycle_id": cycle.get("id") if isinstance(cycle, dict) else ""})
                await run_component("watchdog", lambda: STATE.run_watchdog(WatchdogRunIn(source="scheduler")))
            else:
                STATE.record_runtime_heartbeat("scheduler", "paused", result={"skipped": True})
        except Exception:
            logger.exception("scheduler loop failed")
            STATE.record_runtime_heartbeat("scheduler", "failed", error="scheduler loop failed")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("DialDesk Coordinator starting...")
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    title="DialDesk Autonomous Appointment Setting OS",
    version="3.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def operator_auth_and_request_log(request: Request, call_next):
    started = utcnow()
    path = request.url.path
    status_code = 500
    authorized = not path_requires_auth(path)
    token = request_token(request)
    if path_requires_auth(path):
        authorized = token_is_valid(token)
    actor = request_actor(request, authorized)
    auth_mode = "token" if APP_TOKEN else "open"
    try:
        if not authorized:
            response = JSONResponse(
                status_code=401,
                content={
                    "detail": "Operator token required",
                    "hint": "Send Authorization: Bearer <APP_TOKEN> or X-API-Token.",
                },
            )
        else:
            response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        if not path.startswith("/static/"):
            duration_ms = int((utcnow() - started).total_seconds() * 1000)
            client_host = request.client.host if request.client else ""
            try:
                STATE.log_request(
                    request.method,
                    path,
                    status_code,
                    duration_ms,
                    client_host,
                    actor,
                    auth_mode,
                    {"query": str(request.url.query), "authorized": authorized},
                )
            except Exception:
                logger.exception("request log failed")

STATIC_DIR = pathlib.Path(__file__).parent.parent.parent / "web"
LANDING_DIR = STATIC_DIR / "landing-page"
DASHBOARD_DIR = STATIC_DIR / "dashboard"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def landing_page() -> HTMLResponse:
    f = LANDING_DIR / "index.html"
    if f.exists():
        return HTMLResponse(f.read_text())
    return HTMLResponse(
        """
        <html><head><title>AI Integraterz - DialDesk</title></head>
        <body style="font-family:Arial;background:#0A0A0F;color:white;padding:48px">
          <h1>Unlimited SMS Campaigns. Unlimited Email Campaigns. Deals That Close.</h1>
          <p>DialDesk is the managed appointment-setting operation for MCA and funding companies.</p>
          <p>AI callers, human SDRs, lead programs, qualification, calendar booking, and weekly reporting.</p>
          <a style="color:#FF6B35" href="/dashboard">Open CEO Dashboard</a>
        </body></html>
        """
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    f = DASHBOARD_DIR / "index.html"
    if f.exists():
        return HTMLResponse(f.read_text())
    return HTMLResponse(
        """
        <html>
        <head>
          <title>DialDesk CEO Dashboard</title>
          <style>
            :root { color-scheme: dark; font-family: Inter, Arial, sans-serif; }
            body { margin:0; background:#101114; color:#f6f3ed; }
            header { padding:28px 32px 18px; border-bottom:1px solid #313238; }
            h1 { margin:0; font-size:30px; letter-spacing:0; }
            p { color:#b7b5ad; margin:8px 0 0; max-width:900px; }
            main { padding:24px 32px 40px; display:grid; gap:22px; }
            .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }
            .tile { border:1px solid #313238; border-radius:8px; padding:16px; background:#17191d; min-height:80px; }
            .label { color:#aaa69b; font-size:12px; text-transform:uppercase; }
            .value { font-size:28px; margin-top:8px; font-weight:700; }
            section { display:grid; gap:10px; }
            h2 { margin:0; font-size:18px; }
            table { width:100%; border-collapse:collapse; background:#17191d; border:1px solid #313238; border-radius:8px; overflow:hidden; }
            th, td { text-align:left; padding:10px 12px; border-bottom:1px solid #2a2b30; vertical-align:top; }
            th { color:#aaa69b; font-size:12px; text-transform:uppercase; }
            td { color:#f6f3ed; font-size:14px; word-break:break-word; }
            .muted { color:#aaa69b; }
            .pill { display:inline-block; padding:3px 8px; border-radius:999px; background:#283238; color:#aee2c2; font-size:12px; }
            pre { white-space:pre-wrap; background:#17191d; border:1px solid #313238; border-radius:8px; padding:14px; overflow:auto; }
            .controls { display:grid; gap:12px; border:1px solid #313238; border-radius:8px; padding:14px; background:#17191d; }
            .control-row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
            button, select, input { min-height:36px; border:1px solid #3b3d43; border-radius:6px; background:#202228; color:#f6f3ed; padding:0 10px; font:inherit; }
            button { cursor:pointer; background:#2a332f; color:#ccefd9; }
            button.warn { background:#3b3028; color:#ffd4b8; }
            button.stop { background:#402c2d; color:#ffd1d1; }
            button:disabled { opacity:.55; cursor:not-allowed; }
            select, input { min-width:190px; }
            input { flex:1 1 260px; }
          </style>
        </head>
        <body>
          <header>
            <h1>DialDesk CEO Dashboard</h1>
            <p>Autonomous appointment-setting OS for MCA/funding companies. DialDesk sells outreach and booked calls, not loans.</p>
          </header>
          <main>
            <div class="grid" id="metrics"></div>
            <section>
              <h2>Control Room</h2>
              <table><thead><tr><th>Status</th><th>Area</th><th>Action</th></tr></thead><tbody id="controlRoom"></tbody></table>
            </section>
            <section>
              <h2>Operator Controls</h2>
              <div class="controls">
                <div class="control-row">
                  <button onclick="runControlAction('run_health')">Health</button>
                  <button onclick="runControlAction('run_watchdog', {force: true})">Watchdog</button>
                  <button onclick="runControlAction('run_self_audit', {force: true})">Self-Audit</button>
                  <button onclick="runControlAction('run_autonomy_drill', {force: true})">Autonomy Proof</button>
                  <button onclick="runControlAction('create_operator_briefing', {force: true})">Briefing</button>
                  <button onclick="runControlAction('create_backup')">Backup</button>
                  <button onclick="runControlAction('process_outbox')">Outbox</button>
                  <button onclick="runControlAction('recover_browser_jobs')">Browser Recovery</button>
                  <button onclick="runControlAction('tick_cycle')">CEO Tick</button>
                </div>
                <div class="control-row">
                  <select id="readinessProfile">
                    <option value="production">production</option>
                    <option value="live_outreach">live_outreach</option>
                    <option value="local">local</option>
                  </select>
                  <button onclick="runControlAction('run_readiness', {profile: document.getElementById('readinessProfile').value})">Readiness</button>
                  <select id="launchProfile">
                    <option value="vps">vps</option>
                    <option value="railway">railway</option>
                    <option value="local">local</option>
                  </select>
                  <button onclick="runControlAction('run_launch_gate', {profile: document.getElementById('launchProfile').value})">Launch Gate</button>
                </div>
                <div class="control-row">
                  <select id="runtimeComponent"></select>
                  <input id="controlReason" placeholder="Reason" />
                  <button class="warn" onclick="runControlAction('pause_runtime', controlPayload())">Pause</button>
                  <button onclick="runControlAction('resume_runtime', controlPayload())">Resume</button>
                  <button class="stop" onclick="runControlAction('kill_switch_on', killSwitchPayload())">Kill Switch On</button>
                  <button onclick="runControlAction('kill_switch_off', killSwitchPayload())">Kill Switch Off</button>
                </div>
                <pre id="controlActionResult">Ready.</pre>
              </div>
            </section>
            <section>
              <h2>Under The Hood</h2>
              <table><thead><tr><th>Layer</th><th>What It Does</th><th>Evidence</th></tr></thead><tbody id="underHood"></tbody></table>
            </section>
            <section>
              <h2>Proof Pack</h2>
              <table><thead><tr><th>Status</th><th>Proof</th><th>Command</th></tr></thead><tbody id="proofPack"></tbody></table>
            </section>
            <section>
              <h2>Proof Runs</h2>
              <table><thead><tr><th>Status</th><th>Type</th><th>Summary</th></tr></thead><tbody id="proofRuns"></tbody></table>
            </section>
            <section>
              <h2>Approvals</h2>
              <table><thead><tr><th>Risk</th><th>Title</th><th>Requested</th></tr></thead><tbody id="approvals"></tbody></table>
            </section>
            <section>
              <h2>Activity</h2>
              <table><thead><tr><th>Source</th><th>Status</th><th>Title</th></tr></thead><tbody id="activity"></tbody></table>
            </section>
            <section>
              <h2>Operator Briefings</h2>
              <table><thead><tr><th>Status</th><th>Title</th><th>Created</th></tr></thead><tbody id="operatorBriefings"></tbody></table>
            </section>
            <section>
              <h2>CEO Decisions</h2>
              <table><thead><tr><th>Status</th><th>Action</th><th>Summary</th></tr></thead><tbody id="ceoDecisions"></tbody></table>
            </section>
            <section>
              <h2>Open Tasks</h2>
              <table><thead><tr><th>Priority</th><th>Agent</th><th>Task</th></tr></thead><tbody id="tasks"></tbody></table>
            </section>
            <section>
              <h2>Agent Comms</h2>
              <table><thead><tr><th>From</th><th>To</th><th>Subject</th></tr></thead><tbody id="agentMessages"></tbody></table>
            </section>
            <section>
              <h2>Coordination Room</h2>
              <table><thead><tr><th>Unread</th><th>Participants</th><th>Latest</th></tr></thead><tbody id="agentThreads"></tbody></table>
            </section>
            <section>
              <h2>Operating Cycle</h2>
              <table><thead><tr><th>Status</th><th>Day</th><th>Summary</th></tr></thead><tbody id="operatingCycles"></tbody></table>
            </section>
            <section>
              <h2>Production Ops</h2>
              <table><thead><tr><th>Status</th><th>Area</th><th>Summary</th></tr></thead><tbody id="opsHealth"></tbody></table>
            </section>
            <section>
              <h2>Readiness</h2>
              <table><thead><tr><th>Status</th><th>Profile</th><th>Summary</th></tr></thead><tbody id="readiness"></tbody></table>
            </section>
            <section>
              <h2>Watchdog</h2>
              <table><thead><tr><th>Status</th><th>Severity</th><th>Summary</th></tr></thead><tbody id="watchdog"></tbody></table>
            </section>
            <section>
              <h2>Autonomy Proof</h2>
              <table><thead><tr><th>Status</th><th>Score</th><th>Summary</th></tr></thead><tbody id="autonomyDrills"></tbody></table>
            </section>
            <section>
              <h2>Launch Gate</h2>
              <table><thead><tr><th>Status</th><th>Score</th><th>Summary</th></tr></thead><tbody id="launchGate"></tbody></table>
            </section>
            <section>
              <h2>Runtime Controls</h2>
              <table><thead><tr><th>Status</th><th>Component</th><th>Reason</th></tr></thead><tbody id="runtimeControls"></tbody></table>
            </section>
            <section>
              <h2>Runtime Heartbeats</h2>
              <table><thead><tr><th>Status</th><th>Component</th><th>Last Finished</th></tr></thead><tbody id="runtimeHeartbeats"></tbody></table>
            </section>
            <section>
              <h2>Browser Operators</h2>
              <table><thead><tr><th>Status</th><th>Operator</th><th>Last Seen</th></tr></thead><tbody id="browserOperators"></tbody></table>
            </section>
            <section>
              <h2>Revenue Loop</h2>
              <table><thead><tr><th>Status</th><th>Company</th><th>Package</th></tr></thead><tbody id="opportunities"></tbody></table>
            </section>
            <section>
              <h2>Workflows</h2>
              <table><thead><tr><th>Status</th><th>Client</th><th>Name</th></tr></thead><tbody id="workflows"></tbody></table>
            </section>
            <section>
              <h2>Memory</h2>
              <table><thead><tr><th>Kind</th><th>Title</th><th>Notion</th></tr></thead><tbody id="memory"></tbody></table>
            </section>
            <section>
              <h2>Outbox</h2>
              <table><thead><tr><th>Status</th><th>Destination</th><th>Subject</th></tr></thead><tbody id="outbox"></tbody></table>
            </section>
            <section>
              <h2>Outreach</h2>
              <table><thead><tr><th>Status</th><th>Channel</th><th>Provider</th></tr></thead><tbody id="outreach"></tbody></table>
            </section>
            <section>
              <h2>Integration Sync</h2>
              <table><thead><tr><th>Status</th><th>Provider</th><th>Type</th></tr></thead><tbody id="syncs"></tbody></table>
            </section>
            <section>
              <h2>Governance Goals</h2>
              <table><thead><tr><th>Level</th><th>Owner</th><th>Goal</th></tr></thead><tbody id="goals"></tbody></table>
            </section>
            <section>
              <h2>Agent Budgets</h2>
              <table><thead><tr><th>Agent</th><th>Status</th><th>Remaining</th></tr></thead><tbody id="budgets"></tbody></table>
            </section>
            <section>
              <h2>Under The Hood</h2>
              <pre id="overview">Loading...</pre>
            </section>
          </main>
          <script>
            const text = value => String(value ?? '').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
            const empty = '<tr><td colspan="3" class="muted">Nothing waiting.</td></tr>';
            function row(cells) { return '<tr>' + cells.map(c => '<td>' + text(c) + '</td>').join('') + '</tr>'; }
            function renderTable(id, rows, map) {
              document.getElementById(id).innerHTML = rows.length ? rows.slice(0, 12).map(map).join('') : empty;
            }
            const authHeaders = () => {
              const token = localStorage.getItem('dialdeskOperatorToken') || '';
              return token ? {'Authorization': 'Bearer ' + token, 'X-Operator': 'dashboard'} : {};
            };
            async function api(path) {
              let res = await fetch(path, {headers: authHeaders()});
              if (res.status === 401) {
                const token = prompt('Operator token');
                if (token) {
                  localStorage.setItem('dialdeskOperatorToken', token);
                  res = await fetch(path, {headers: authHeaders()});
                }
              }
              if (!res.ok) throw new Error(path + ' failed: ' + res.status);
              return res.json();
            }
            async function apiPost(path, body) {
              let res = await fetch(path, {
                method: 'POST',
                headers: {...authHeaders(), 'Content-Type': 'application/json'},
                body: JSON.stringify(body || {})
              });
              if (res.status === 401) {
                const token = prompt('Operator token');
                if (token) {
                  localStorage.setItem('dialdeskOperatorToken', token);
                  res = await fetch(path, {
                    method: 'POST',
                    headers: {...authHeaders(), 'Content-Type': 'application/json'},
                    body: JSON.stringify(body || {})
                  });
                }
              }
              if (!res.ok) throw new Error(path + ' failed: ' + res.status + ' ' + await res.text());
              return res.json();
            }
            function controlPayload() {
              return {
                component: document.getElementById('runtimeComponent').value,
                reason: document.getElementById('controlReason').value || 'Dashboard operator control'
              };
            }
            function killSwitchPayload() {
              return {reason: document.getElementById('controlReason').value || 'Dashboard operator control'};
            }
            async function runControlAction(action, payload) {
              const output = document.getElementById('controlActionResult');
              output.textContent = 'Running ' + action + '...';
              document.querySelectorAll('button').forEach(button => button.disabled = true);
              try {
                const result = await apiPost('/api/ops/control-room/action', {action, actor: 'Justin', ...(payload || {})});
                output.textContent = JSON.stringify({action: result.action, result: result.result}, null, 2);
                await loadDashboard();
              } catch (err) {
                output.textContent = String(err);
              } finally {
                document.querySelectorAll('button').forEach(button => button.disabled = false);
              }
            }
            function populateRuntimeComponents(status) {
              const select = document.getElementById('runtimeComponent');
              const current = select.value;
              const components = status.runtime_controls.map(c => c.component).sort();
              select.innerHTML = components.map(c => '<option value="' + text(c) + '">' + text(c) + '</option>').join('');
              if (components.includes(current)) select.value = current;
            }
            async function loadDashboard() {
              const [status, overview, controlRoom] = await Promise.all([api('/api/status'), api('/api/ops/overview'), api('/api/ops/control-room')]);
              const m = status.metrics;
              const tiles = [
                ['Control Room', controlRoom.status],
                ['Active Clients', m.active_clients],
                ['Replies Today', m.replies_today],
                ['Booked Today', m.appointments_booked],
                ['Pending Approvals', status.approvals.length],
                ['Open Tasks', status.tasks.length],
                ['CEO Cycle', overview.operating_cycle.current[0]?.status ?? 'Not started'],
                ['Activity Items', status.activity.length],
                ['Briefings', status.reports.filter(r => r.type === 'operator_briefing').length],
                ['CEO Decisions', overview.governance.decisions_today],
                ['Decisions Waiting', overview.governance.decisions_waiting],
                ['Open Threads', overview.governance.open_threads],
                ['Justin Inbox', overview.governance.justin_unread],
                ['CEO Inbox', overview.governance.ceo_unread],
                ['Memory Entries', overview.memory.entries],
                ['Notion Queued', overview.memory.health.notion.queued],
                ['Readiness Score', overview.production_ops.latest_readiness_check[0]?.score ?? 'Run check'],
                ['Watchdog', overview.production_ops.latest_watchdog_run[0]?.status ?? 'Not run'],
                ['Self-Audit', overview.production_ops.latest_self_audit[0]?.status ?? 'Not run'],
                ['Autonomy Proof', overview.production_ops.latest_autonomy_drill[0]?.status ?? 'Not run'],
                ['Launch Gate', overview.production_ops.latest_launch_gate[0]?.status ?? 'Not run'],
                ['Proof Signals', overview.proof_pack.signals.length],
                ['Latest Proof Run', status.proof_runs[0]?.status ?? 'Not recorded'],
                ['Runtime Paused', status.runtime_controls.filter(c => !c.enabled).length],
                ['Runtime Failed', overview.production_ops.runtime_failed_components],
                ['Browser Operators', overview.browser_automation.online_operators],
                ['Queued Outbox', overview.visibility.queued_outbox],
                ['Outreach Queued', status.metrics.outreach_queued],
                ['Sync Queued', status.metrics.sync_queued],
                ['Open Opportunities', status.metrics.opportunities_open],
                ['CRM Sync Queued', status.metrics.crm_sync_queued],
                ['Unread Messages', status.metrics.agent_unread_messages],
                ['Open Incidents', status.metrics.open_incidents],
                ['Browser Claimed', status.metrics.browser_jobs_claimed],
                ['Workflow Due', status.metrics.workflow_steps_due],
                ['Paused Agents', overview.governance.paused_agents],
              ];
              document.getElementById('metrics').innerHTML = tiles.map(([label, value]) =>
                '<div class="tile"><div class="label">' + text(label) + '</div><div class="value">' + text(value) + '</div></div>'
              ).join('');
              populateRuntimeComponents(status);
              renderTable('approvals', status.approvals, a => row([a.risk, a.title, a.requested_by]));
              renderTable('controlRoom', controlRoom.attention, a => row([a.severity, a.title, a.action]));
              renderTable('underHood', controlRoom.how_it_works, l => row([l.layer, l.what_it_does, l.evidence.join(', ')]));
              renderTable('proofPack', overview.proof_pack.signals, p => row([p.status, p.proof, p.command]));
              renderTable('proofRuns', status.proof_runs, p => row([p.status, p.proof_type, p.summary]));
              renderTable('activity', status.activity, a => row([a.source, a.status, a.title]));
              renderTable('operatorBriefings', status.reports.filter(r => r.type === 'operator_briefing'), r => row([r.status, r.title, r.created_at]));
              renderTable('ceoDecisions', status.ceo_decisions, d => row([d.status, d.action, d.summary]));
              renderTable('tasks', status.tasks, t => row([t.priority, t.agent_id, t.title]));
              renderTable('agentMessages', status.agent_messages, m => row([m.from_agent_id, m.to_agent_id, m.subject]));
              renderTable('agentThreads', status.coordination_room.threads, t => row([t.unread_count, t.participants.join(', '), t.last_message?.subject ?? t.thread_id]));
              renderTable('operatingCycles', status.operating_cycles, c => row([c.status, c.day_key, c.summary]));
              renderTable('opsHealth', status.health_checks, h => row([h.status, h.severity, h.summary]));
              renderTable('readiness', status.readiness_checks, r => row([r.status + ' (' + r.score + ')', r.profile, r.summary]));
              renderTable('watchdog', status.watchdog_runs, w => row([w.status, w.severity, w.summary]));
              renderTable('autonomyDrills', status.autonomy_drills, d => row([d.status, d.score, d.summary]));
              renderTable('launchGate', status.launch_gate_runs, g => row([g.status, g.score, g.summary]));
              renderTable('runtimeControls', status.runtime_controls, c => row([c.enabled ? 'enabled' : 'paused', c.component, c.reason]));
              renderTable('runtimeHeartbeats', status.runtime_heartbeats, h => row([h.status, h.component, h.last_finished_at || 'waiting']));
              renderTable('browserOperators', status.browser_operators, o => row([o.alive ? o.status : 'stale', o.operator_id, o.last_seen_at]));
              renderTable('opportunities', status.opportunities, o => row([o.status, o.company_name, o.package_interest]));
              renderTable('workflows', status.workflow_runs, w => row([w.status, w.client_id, w.name]));
              renderTable('memory', status.memory, n => row([n.kind, n.title, n.notion_status]));
              renderTable('outbox', status.outbox, o => row([o.status, o.destination, o.subject]));
              renderTable('outreach', status.outreach_jobs, o => row([o.status, o.channel, o.provider]));
              renderTable('syncs', status.integration_syncs, s => row([s.status, s.provider, s.sync_type]));
              renderTable('goals', status.governance_goals, g => row([g.level, g.owner_agent_id, g.title]));
              renderTable('budgets', status.agent_budgets, b => row([b.agent_id, b.status, '$' + (Number(b.monthly_budget || 0) - Number(b.spent || 0)).toFixed(2)]));
              document.getElementById('overview').textContent = JSON.stringify(overview, null, 2);
            }
            loadDashboard().catch(err => {
              document.getElementById('overview').textContent = String(err);
              document.getElementById('controlActionResult').textContent = String(err);
            });
          </script>
        </body>
        </html>
        """
    )


@app.get("/dashboard.html", response_class=HTMLResponse)
def dashboard_html() -> HTMLResponse:
    return dashboard()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "up",
        "service": "DialDesk",
        "version": "3.0.0",
        "database": STATE.db_path,
        "time": now_iso(),
    }


@app.get("/agentCard")
async def agent_card() -> dict[str, Any]:
    return {
        "name": "DialDesk Autonomous Appointment Setting OS",
        "description": PRODUCT["truth"] + " " + PRODUCT["offer"],
        "url": PUBLIC_URL,
        "version": "3.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "appointment_setting_ops",
                "name": "Appointment Setting Operations",
                "description": "Run SMS, email, AI SDR, human SDR, qualification, booking, and reporting for MCA/funding companies.",
                "tags": ["mca", "funding", "sms", "email", "ai-sdr", "human-sdr", "appointments"],
            },
            {
                "id": "ceo_operating_loop",
                "name": "CEO Operating Loop",
                "description": "Daily CEO review, delegation, approvals, event wakeups, and escalation handling.",
                "tags": ["ceo", "autonomy", "tasks", "events", "approvals"],
            },
            {
                "id": "business_memory",
                "name": "Business Memory",
                "description": "Persist decisions, task results, briefs, appointments, and operating notes to SQLite with Obsidian and Notion sync paths.",
                "tags": ["memory", "obsidian", "notion", "audit"],
            },
            {
                "id": "visibility_and_browser_ops",
                "name": "Visibility and Browser Operations",
                "description": "Queue Slack alerts, inspect outbox state, and hand browser automation jobs to the operator/browser layer.",
                "tags": ["slack", "dashboard", "browser", "ops"],
            },
            {
                "id": "provider_sync",
                "name": "Provider Sync",
                "description": "Poll Smartlead replies/stats and Sendivo logs/billing into conversations, memory, events, and dashboards.",
                "tags": ["smartlead", "sendivo", "replies", "logs", "sync"],
            },
            {
                "id": "governance_controls",
                "name": "Governance Controls",
                "description": "Manage mission/project/agent goals, budgets, heartbeats, paused agents, and Slack-style operator commands.",
                "tags": ["governance", "budgets", "heartbeats", "slack", "controls"],
            },
        ],
    }


@app.post("/messages")
async def submit(req: MessageRequest) -> MessageResponse:
    prompt = "\n".join(p.text for p in req.parts if p.kind == "text" and p.text)
    if not prompt:
        raise HTTPException(status_code=400, detail="No text part in message")
    result = await route_to_agent(prompt, req.metadata.get("from_user", "A2A"), req.metadata.get("client_id", DEFAULT_CLIENT_ID))
    return MessageResponse(taskId=result.get("task_id") or result.get("approval_id"), state="submitted")


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    row = STATE.get_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "taskId": row["id"],
        "status": {"state": row["status"]},
        "artifacts": [{"kind": "text", "text": row["result"]}] if row["result"] else [],
        "result": {
            "agent": row["agent_id"],
            "title": row["title"],
            "metadata": json_loads(row["metadata"], {}),
        },
    }


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return {
        "product": PRODUCT,
        "agents": {k: asdict(v) for k, v in STATE.agents.items()},
        "clients": STATE.rows("clients ORDER BY created_at DESC LIMIT 25"),
        "campaigns": STATE.rows("campaigns ORDER BY created_at DESC LIMIT 25"),
        "metrics": STATE.metrics(),
        "approvals": STATE.rows("approvals", "status='pending' ORDER BY created_at DESC"),
        "tasks": STATE.rows("tasks", "status IN ('pending','working') ORDER BY priority ASC, created_at ASC LIMIT 25"),
        "events": STATE.rows("events ORDER BY created_at DESC LIMIT 25"),
        "reports": STATE.rows("reports ORDER BY created_at DESC LIMIT 20"),
        "memory": STATE.rows("memory_entries ORDER BY created_at DESC LIMIT 10"),
        "outbox": STATE.rows("outbox ORDER BY created_at DESC LIMIT 20"),
        "browser_jobs": STATE.rows("browser_jobs ORDER BY created_at DESC LIMIT 20"),
        "browser_operators": STATE.browser_operators(),
        "outreach_jobs": STATE.rows("outreach_jobs ORDER BY created_at DESC LIMIT 20"),
        "integration_syncs": STATE.rows("integration_syncs ORDER BY created_at DESC LIMIT 20"),
        "opportunities": STATE.rows("opportunities ORDER BY created_at DESC LIMIT 20"),
        "sales_briefs": STATE.rows("sales_briefs ORDER BY created_at DESC LIMIT 20"),
        "payment_links": STATE.rows("payment_links ORDER BY created_at DESC LIMIT 20"),
        "crm_sync_jobs": STATE.rows("crm_sync_jobs ORDER BY priority ASC, created_at ASC LIMIT 20"),
        "agent_messages": STATE.rows("agent_messages ORDER BY created_at DESC LIMIT 30"),
        "ceo_decisions": STATE.rows("ceo_decisions ORDER BY created_at DESC LIMIT 30"),
        "health_checks": STATE.rows("health_checks ORDER BY checked_at DESC LIMIT 20"),
        "readiness_checks": STATE.rows("readiness_checks ORDER BY checked_at DESC LIMIT 20"),
        "watchdog_runs": STATE.rows("watchdog_runs ORDER BY checked_at DESC LIMIT 20"),
        "autonomy_drills": STATE.rows("autonomy_drills ORDER BY completed_at DESC LIMIT 20"),
        "launch_gate_runs": STATE.rows("launch_gate_runs ORDER BY checked_at DESC LIMIT 20"),
        "proof_runs": STATE.rows("proof_runs ORDER BY completed_at DESC LIMIT 20"),
        "operating_cycles": STATE.rows("operating_cycles ORDER BY started_at DESC LIMIT 20"),
        "incidents": STATE.rows("incidents ORDER BY created_at DESC LIMIT 20"),
        "backups": STATE.rows("backups ORDER BY created_at DESC LIMIT 20"),
        "request_log": STATE.rows("request_log ORDER BY created_at DESC LIMIT 30"),
        "workflow_runs": STATE.rows("workflow_runs ORDER BY created_at DESC LIMIT 20"),
        "workflow_steps": STATE.rows("workflow_steps ORDER BY due_at ASC, step_order ASC LIMIT 50"),
        "runtime_controls": STATE.runtime_controls(),
        "runtime_heartbeats": STATE.runtime_heartbeats(),
        "governance_goals": STATE.rows("governance_goals ORDER BY priority ASC, created_at DESC LIMIT 25"),
        "agent_budgets": STATE.rows("agent_budgets ORDER BY agent_id ASC"),
        "agent_heartbeats": STATE.rows("agent_heartbeats ORDER BY created_at DESC LIMIT 20"),
        "coordination_room": STATE.coordination_room(),
        "activity": STATE.activity_feed(limit=40),
        "log": [dict(r) for r in STATE.get_all("SELECT * FROM agent_log ORDER BY id DESC LIMIT 20")],
        "kill_switches": STATE.rows("kill_switches"),
    }


@app.get("/api/ops/overview")
def ops_overview() -> dict[str, Any]:
    return STATE.ops_overview()


@app.get("/api/ops/proof-pack")
def ops_proof_pack() -> dict[str, Any]:
    return STATE.ops_overview()["proof_pack"]


@app.get("/api/ops/proof-runs")
def list_proof_runs(proof_type: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if proof_type:
        clauses.append("proof_type=?")
        params.append(proof_type)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("proof_runs", f"{where} ORDER BY completed_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/ops/proof-runs")
def record_proof_run(req: ProofRunRecordIn) -> dict[str, Any]:
    return STATE.record_proof_run(req)


@app.get("/api/ops/control-room")
def ops_control_room() -> dict[str, Any]:
    return STATE.control_room()


@app.post("/api/ops/control-room/action")
def ops_control_room_action(req: ControlRoomActionIn) -> dict[str, Any]:
    return STATE.run_control_room_action(req)


@app.get("/api/activity")
def activity_feed(
    client_id: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return STATE.activity_feed(limit=limit, client_id=client_id, source=source)


@app.get("/api/ceo/decisions")
def list_ceo_decisions(
    client_id: str | None = None,
    status: str | None = None,
    decision_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    if decision_type:
        clauses.append("decision_type=?")
        params.append(decision_type)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("ceo_decisions", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/ceo/decisions/evaluate")
def evaluate_ceo_decisions(req: CeoDecisionReviewIn) -> dict[str, Any]:
    return STATE.run_ceo_decision_review(req)


@app.post("/api/ops/health-checks/run")
def run_health_check() -> dict[str, Any]:
    return STATE.run_health_check("api")


@app.get("/api/ops/health-checks")
def list_health_checks(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 250))
    if status:
        return STATE.rows("health_checks", f"status=? ORDER BY checked_at DESC LIMIT {capped}", (status,))
    return STATE.rows("health_checks", f"1=1 ORDER BY checked_at DESC LIMIT {capped}")


@app.get("/api/ops/runtime-controls")
def list_runtime_controls() -> list[dict[str, Any]]:
    return STATE.runtime_controls()


@app.post("/api/ops/runtime-controls")
def set_runtime_control(req: RuntimeControlIn) -> dict[str, Any]:
    return STATE.set_runtime_control(req)


@app.get("/api/ops/runtime-heartbeats")
def list_runtime_heartbeats(status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return STATE.rows("runtime_heartbeats", "status=? ORDER BY component ASC", (status,))
    return STATE.runtime_heartbeats()


@app.post("/api/ops/readiness/run")
def run_readiness_check(req: ReadinessCheckCreateIn) -> dict[str, Any]:
    return STATE.run_readiness_check(req)


@app.get("/api/ops/readiness")
def list_readiness_checks(
    profile: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if profile:
        clauses.append("profile=?")
        params.append(profile)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("readiness_checks", f"{where} ORDER BY checked_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/ops/watchdog/run")
def run_watchdog(req: WatchdogRunIn) -> dict[str, Any]:
    return STATE.run_watchdog(req)


@app.get("/api/ops/watchdog")
def list_watchdog_runs(status: str | None = None, severity: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if severity:
        clauses.append("severity=?")
        params.append(severity)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("watchdog_runs", f"{where} ORDER BY checked_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/ops/self-audit/run")
def run_self_audit(req: SelfAuditRunIn) -> dict[str, Any]:
    return STATE.run_self_audit(req)


@app.post("/api/ops/autonomy-drills/run")
def run_autonomy_drill(req: AutonomyDrillRunIn) -> dict[str, Any]:
    return STATE.run_autonomy_drill(req)


@app.get("/api/ops/autonomy-drills")
def list_autonomy_drills(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 250))
    if status:
        return STATE.rows("autonomy_drills", f"status=? ORDER BY completed_at DESC LIMIT {capped}", (status,))
    return STATE.rows("autonomy_drills", f"1=1 ORDER BY completed_at DESC LIMIT {capped}")


@app.post("/api/ops/launch-gate/run")
def run_launch_gate(req: LaunchGateRunIn) -> dict[str, Any]:
    return STATE.run_launch_gate(req)


@app.get("/api/ops/launch-gate")
def list_launch_gate_runs(profile: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if profile:
        clauses.append("profile=?")
        params.append(profile)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("launch_gate_runs", f"{where} ORDER BY checked_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/ops/incidents")
def create_incident(req: IncidentCreateIn) -> dict[str, Any]:
    return STATE.create_incident(req)


@app.get("/api/ops/incidents")
def list_incidents(status: str | None = None, severity: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if severity:
        clauses.append("severity=?")
        params.append(severity)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("incidents", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/ops/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str, req: IncidentResolveIn) -> dict[str, Any]:
    return STATE.resolve_incident(incident_id, req)


@app.post("/api/ops/backups/create")
def create_backup(req: BackupCreateIn) -> dict[str, Any]:
    return STATE.create_backup(req)


@app.get("/api/ops/backups")
def list_backups(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 250))
    if status:
        return STATE.rows("backups", f"status=? ORDER BY created_at DESC LIMIT {capped}", (status,))
    return STATE.rows("backups", f"1=1 ORDER BY created_at DESC LIMIT {capped}")


@app.get("/api/ops/request-log")
def list_request_log(
    status_code: int | None = None,
    path: str | None = None,
    actor: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status_code is not None:
        clauses.append("status_code=?")
        params.append(status_code)
    if path:
        clauses.append("path=?")
        params.append(path)
    if actor:
        clauses.append("actor=?")
        params.append(actor)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 500))
    return STATE.rows("request_log", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.get("/api/integrations/config")
def integrations_config() -> dict[str, Any]:
    return provider_config_status()


@app.post("/api/ceo/chat")
async def ceo_chat(req: ChatRequest) -> dict[str, Any]:
    return await route_to_agent(req.message, req.from_user, req.client_id)


@app.get("/api/agents/coordination")
def agent_coordination_room(client_id: str | None = None) -> dict[str, Any]:
    return STATE.coordination_room(client_id)


@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str) -> dict[str, Any]:
    if agent_id not in STATE.agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    return asdict(STATE.agents[agent_id])


@app.post("/api/clients")
def create_client(req: ClientCreateIn) -> dict[str, Any]:
    return STATE.create_client(req)


@app.post("/api/workflows/launch")
def launch_workflow(req: WorkflowLaunchIn) -> dict[str, Any]:
    return STATE.launch_workflow(req)


@app.get("/api/workflows/runs")
def list_workflow_runs(client_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("workflow_runs", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.get("/api/workflows/steps")
def list_workflow_steps(
    client_id: str | None = None,
    run_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if run_id:
        clauses.append("run_id=?")
        params.append(run_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 300))
    return STATE.rows("workflow_steps", f"{where} ORDER BY due_at ASC, step_order ASC LIMIT {capped}", tuple(params))


@app.post("/api/workflows/execute-due")
def execute_due_workflow_steps(client_id: str | None = None, limit: int = 25) -> dict[str, Any]:
    started = now_iso()
    queued = STATE.execute_due_workflow_steps(client_id, limit)
    STATE.record_runtime_heartbeat("workflow", "ok", started_at=started, result={"queued": queued, "source": "api"})
    return {"queued": queued}


@app.post("/api/workflows/steps/{step_id}/complete")
def complete_workflow_step(step_id: str, req: WorkflowStepCompleteIn) -> dict[str, Any]:
    return STATE.complete_workflow_step(step_id, req)


@app.post("/api/opportunities")
def create_opportunity(req: OpportunityCreateIn) -> dict[str, Any]:
    return STATE.create_opportunity(req)


@app.get("/api/opportunities")
def list_opportunities(
    client_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("opportunities", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/opportunities/{opportunity_id}/sales-brief")
def create_opportunity_sales_brief(opportunity_id: str, req: SalesBriefCreateIn | None = None) -> dict[str, Any]:
    body = req or SalesBriefCreateIn(opportunity_id=opportunity_id)
    if body.opportunity_id != opportunity_id:
        raise HTTPException(status_code=400, detail="opportunity_id mismatch")
    return STATE.create_sales_brief(body)


@app.post("/api/sales-briefs")
def create_sales_brief(req: SalesBriefCreateIn) -> dict[str, Any]:
    return STATE.create_sales_brief(req)


@app.get("/api/sales-briefs")
def list_sales_briefs(client_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("sales_briefs", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/payment-links")
def create_payment_link(req: PaymentLinkCreateIn) -> dict[str, Any]:
    return STATE.create_payment_link(req)


@app.get("/api/payment-links")
def list_payment_links(client_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("payment_links", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/crm/sync-jobs")
def queue_crm_sync(req: CrmSyncCreateIn) -> dict[str, Any]:
    return STATE.queue_crm_sync(req)


@app.get("/api/crm/sync-jobs")
def list_crm_sync_jobs(
    client_id: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    if provider:
        clauses.append("provider=?")
        params.append(provider)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("crm_sync_jobs", f"{where} ORDER BY priority ASC, created_at ASC LIMIT {capped}", tuple(params))


@app.post("/api/crm/sync-jobs/{job_id}/execute")
async def execute_crm_sync_job(job_id: str) -> dict[str, Any]:
    return await STATE.execute_crm_sync_job(job_id)


@app.post("/api/crm/sync-next")
async def execute_crm_sync_next(limit: int = 5) -> dict[str, Any]:
    started = now_iso()
    processed = await STATE.execute_crm_sync_queue_once(max(1, min(limit, 50)))
    STATE.record_runtime_heartbeat("crm_sync", "ok", started_at=started, result={"processed": processed, "source": "api", "sync_enabled": SYNC_CRM})
    return {"processed": processed, "sync_enabled": SYNC_CRM}


@app.post("/api/events")
def create_event(req: EventIn) -> dict[str, Any]:
    return STATE.create_event(req.type, req.client_id, req.source, req.payload, req.severity)


@app.post("/api/memory")
def create_memory(req: MemoryCreateIn) -> dict[str, Any]:
    return STATE.remember(
        title=req.title,
        body=req.body,
        kind=req.kind,
        actor=req.actor,
        client_id=req.client_id,
        tags=req.tags,
        source=req.source,
        metadata=req.metadata,
    )


@app.get("/api/memory/health")
def memory_health() -> dict[str, Any]:
    return STATE.memory_health()


@app.post("/api/memory/resync-notion")
def resync_memory_to_notion(req: MemoryResyncIn | None = None) -> dict[str, Any]:
    return STATE.queue_memory_notion_resync(req or MemoryResyncIn())


@app.post("/api/memory/{memory_id}/sync-notion")
def sync_memory_to_notion(memory_id: str, req: MemoryResyncIn | None = None) -> dict[str, Any]:
    body = req or MemoryResyncIn()
    return STATE.queue_memory_notion_sync(memory_id, body.requested_by, body.metadata)


@app.get("/api/memory")
def list_memory(client_id: str | None = None, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("memory_entries", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/slack/notify")
def slack_notify(req: SlackNotifyIn) -> dict[str, Any]:
    return STATE.queue_slack(req.text, req.severity, req.channel, req.client_id, req.metadata)


@app.post("/slack/command")
async def slack_slash_command(request: Request) -> JSONResponse:
    raw_body = await request.body()
    verify_slack_signature({k.lower(): v for k, v in request.headers.items()}, raw_body)
    command = slack_form_to_command(raw_body)
    try:
        response = STATE.handle_slack_command(command)
        return JSONResponse({"response_type": "ephemeral", "text": response["text"], "dialdesk": response})
    except HTTPException as exc:
        return JSONResponse(
            {"response_type": "ephemeral", "text": f"DialDesk error: {exc.detail}"},
            status_code=200,
        )


@app.post("/api/slack/command")
def slack_command(req: SlackCommandIn) -> dict[str, Any]:
    return STATE.handle_slack_command(req)


@app.post("/api/governance/goals")
def create_governance_goal(req: GoalCreateIn) -> dict[str, Any]:
    return STATE.create_goal(req)


@app.get("/api/governance/goals")
def list_governance_goals(
    client_id: str | None = None,
    level: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if level:
        clauses.append("level=?")
        params.append(level)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("governance_goals", f"{where} ORDER BY priority ASC, created_at ASC LIMIT {capped}", tuple(params))


@app.get("/api/governance/budgets")
def list_agent_budgets(client_id: str | None = None) -> list[dict[str, Any]]:
    if client_id:
        return STATE.rows("agent_budgets", "client_id=? ORDER BY agent_id ASC", (client_id,))
    return STATE.rows("agent_budgets ORDER BY client_id ASC, agent_id ASC")


@app.post("/api/governance/budgets")
def set_agent_budget(req: BudgetSetIn) -> dict[str, Any]:
    return STATE.set_budget(req)


@app.post("/api/governance/heartbeats")
def record_agent_heartbeat(req: HeartbeatIn) -> dict[str, Any]:
    return STATE.record_heartbeat(req)


@app.get("/api/governance/heartbeats")
def list_agent_heartbeats(agent_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 200))
    if agent_id:
        return STATE.rows("agent_heartbeats", f"agent_id=? ORDER BY created_at DESC LIMIT {capped}", (agent_id,))
    return STATE.rows("agent_heartbeats", f"1=1 ORDER BY created_at DESC LIMIT {capped}")


@app.get("/api/agent-threads")
def list_agent_threads(
    client_id: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return STATE.agent_threads(client_id=client_id, agent_id=agent_id, limit=limit)


@app.get("/api/agents/{agent_id}/inbox")
def list_agent_inbox(
    agent_id: str,
    client_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return STATE.agent_inbox(agent_id, client_id=client_id, status=status, limit=limit)


@app.post("/api/agent-messages")
def send_agent_message(req: AgentMessageCreateIn) -> dict[str, Any]:
    return STATE.send_agent_message(req)


@app.get("/api/agent-messages")
def list_agent_messages(
    client_id: str | None = None,
    to_agent_id: str | None = None,
    from_agent_id: str | None = None,
    status: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    if to_agent_id:
        clauses.append("to_agent_id=?")
        params.append(to_agent_id)
    if from_agent_id:
        clauses.append("from_agent_id=?")
        params.append(from_agent_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    if thread_id:
        clauses.append("thread_id=?")
        params.append(thread_id)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 250))
    return STATE.rows("agent_messages", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/agent-messages/{message_id}/read")
def read_agent_message(message_id: str, reader: str = "ceo") -> dict[str, Any]:
    return STATE.mark_agent_message_read(message_id, reader)


@app.post("/api/agent-messages/{message_id}/reply")
def reply_agent_message(message_id: str, req: AgentMessageReplyIn) -> dict[str, Any]:
    return STATE.reply_agent_message(message_id, req)


@app.post("/api/agents/standup")
def create_agent_standup(req: AgentStandupCreateIn) -> dict[str, Any]:
    return STATE.create_agent_standup(req)


@app.post("/api/agents/{agent_id}/pause")
def pause_agent(agent_id: str, reason: str = "") -> dict[str, Any]:
    return STATE.pause_agent(agent_id, reason or "Paused from API")


@app.post("/api/agents/{agent_id}/resume")
def resume_agent(agent_id: str, reason: str = "") -> dict[str, Any]:
    return STATE.resume_agent(agent_id, reason or "Resumed from API")


@app.get("/api/outbox")
def list_outbox(destination: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if destination:
        clauses.append("destination=?")
        params.append(destination)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("outbox", f"{where} ORDER BY created_at DESC LIMIT {capped}", tuple(params))


@app.post("/api/outbox/process")
def process_outbox(limit: int = 10) -> dict[str, Any]:
    started = now_iso()
    processed = STATE.process_outbox_once(max(1, min(limit, 50)))
    STATE.record_runtime_heartbeat("outbox", "ok", started_at=started, result={"processed": processed, "source": "api", "send_enabled": SEND_OUTBOX})
    return {"processed": processed, "send_enabled": SEND_OUTBOX}


@app.post("/api/browser/jobs")
def create_browser_job(req: BrowserJobCreateIn) -> dict[str, Any]:
    return STATE.create_browser_job(req)


@app.post("/api/browser/jobs/claim")
def claim_browser_job(req: BrowserJobClaimIn) -> dict[str, Any]:
    return STATE.claim_browser_job(req)


@app.get("/api/browser/operators")
def list_browser_operators() -> list[dict[str, Any]]:
    return STATE.browser_operators()


@app.post("/api/browser/operators/heartbeat")
def browser_operator_heartbeat(req: BrowserOperatorHeartbeatIn) -> dict[str, Any]:
    return STATE.record_browser_operator_heartbeat(req)


@app.get("/api/browser/jobs")
def list_browser_jobs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    where = "status=?" if status else "1=1"
    params: tuple[Any, ...] = (status,) if status else ()
    capped = max(1, min(limit, 200))
    return STATE.rows("browser_jobs", f"{where} ORDER BY priority ASC, created_at ASC LIMIT {capped}", params)


@app.post("/api/browser/jobs/recover-expired")
def recover_expired_browser_jobs() -> dict[str, Any]:
    started = now_iso()
    recovered = STATE.recover_expired_browser_jobs()
    STATE.record_runtime_heartbeat("browser_recovery", "ok", started_at=started, result={"recovered": recovered, "source": "api"})
    return {"recovered": recovered}


@app.post("/api/browser/jobs/{job_id}/heartbeat")
def heartbeat_browser_job(job_id: str, req: BrowserJobHeartbeatIn) -> dict[str, Any]:
    return STATE.heartbeat_browser_job(job_id, req)


@app.post("/api/browser/jobs/{job_id}/fail")
def fail_browser_job(job_id: str, req: BrowserJobCompleteIn) -> dict[str, Any]:
    return STATE.fail_browser_job(job_id, req)


@app.post("/api/browser/jobs/{job_id}/complete")
def complete_browser_job(job_id: str, req: BrowserJobCompleteIn) -> dict[str, Any]:
    return STATE.complete_browser_job(job_id, req)


@app.post("/api/outreach/jobs")
def queue_outreach(req: OutreachJobCreateIn) -> dict[str, Any]:
    return STATE.queue_outreach(req)


@app.get("/api/outreach/jobs")
def list_outreach_jobs(status: str | None = None, channel: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if channel:
        clauses.append("channel=?")
        params.append(channel)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("outreach_jobs", f"{where} ORDER BY priority ASC, created_at ASC LIMIT {capped}", tuple(params))


@app.post("/api/outreach/jobs/{job_id}/execute")
async def execute_outreach_job(job_id: str) -> dict[str, Any]:
    return await STATE.execute_outreach_job(job_id)


@app.post("/api/outreach/execute-next")
async def execute_outreach_next(limit: int = 5) -> dict[str, Any]:
    started = now_iso()
    processed = await STATE.execute_outreach_queue_once(max(1, min(limit, 50)))
    STATE.record_runtime_heartbeat("outreach", "ok", started_at=started, result={"processed": processed, "source": "api", "send_enabled": SEND_OUTREACH})
    return {"processed": processed, "send_enabled": SEND_OUTREACH}


@app.post("/api/integrations/syncs")
def queue_integration_sync(req: IntegrationSyncCreateIn) -> dict[str, Any]:
    return STATE.queue_integration_sync(req)


@app.get("/api/integrations/syncs")
def list_integration_syncs(
    status: str | None = None,
    provider: str | None = None,
    sync_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if provider:
        clauses.append("provider=?")
        params.append(provider)
    if sync_type:
        clauses.append("sync_type=?")
        params.append(sync_type)
    where = " AND ".join(clauses) or "1=1"
    capped = max(1, min(limit, 200))
    return STATE.rows("integration_syncs", f"{where} ORDER BY priority ASC, created_at ASC LIMIT {capped}", tuple(params))


@app.post("/api/integrations/syncs/{sync_id}/execute")
async def execute_integration_sync(sync_id: str) -> dict[str, Any]:
    return await STATE.execute_integration_sync(sync_id)


@app.post("/api/integrations/sync-next")
async def execute_integration_sync_next(limit: int = 5) -> dict[str, Any]:
    started = now_iso()
    processed = await STATE.execute_integration_sync_queue_once(max(1, min(limit, 50)))
    STATE.record_runtime_heartbeat("integration_sync", "ok", started_at=started, result={"processed": processed, "source": "api"})
    return {"processed": processed}


@app.get("/api/tasks")
def list_tasks(status: str | None = None, client_id: str | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if client_id:
        clauses.append("client_id=?")
        params.append(client_id)
    where = " AND ".join(clauses)
    return STATE.rows("tasks", f"{where} ORDER BY priority ASC, created_at ASC" if where else "1=1 ORDER BY priority ASC, created_at ASC", tuple(params))


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: str, req: CompleteTaskIn) -> dict[str, Any]:
    return STATE.complete_task(task_id, req.result, req.status, req.metadata)


@app.get("/api/approvals")
def list_approvals(status: str = "pending") -> list[dict[str, Any]]:
    return STATE.rows("approvals", "status=? ORDER BY created_at DESC", (status,))


@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: str, req: ApprovalDecisionIn) -> dict[str, Any]:
    return STATE.decide_approval(approval_id, "approved", req.decided_by, req.note)


@app.post("/api/approvals/{approval_id}/reject")
def reject(approval_id: str, req: ApprovalDecisionIn) -> dict[str, Any]:
    return STATE.decide_approval(approval_id, "rejected", req.decided_by, req.note)


@app.post("/api/leads/import")
def import_leads(req: LeadImportIn) -> dict[str, Any]:
    if STATE.is_kill_switch_active(req.client_id):
        raise HTTPException(status_code=423, detail="Kill switch active")
    return STATE.import_leads(req)


@app.get("/api/clients/{client_id}/dashboard")
def client_dashboard(client_id: str) -> dict[str, Any]:
    return STATE.client_dashboard(client_id)


@app.get("/api/ceo/daily-brief")
def daily_brief(force: bool = False) -> dict[str, Any]:
    return STATE.create_daily_brief(force=force)


@app.post("/api/ops/operator-briefing")
def create_operator_briefing(req: OperatorBriefingCreateIn) -> dict[str, Any]:
    return STATE.create_operator_briefing(req)


@app.get("/api/reports")
def list_reports(report_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 250))
    if report_type:
        return STATE.rows("reports", f"type=? ORDER BY created_at DESC LIMIT {capped}", (report_type,))
    return STATE.rows("reports", f"1=1 ORDER BY created_at DESC LIMIT {capped}")


@app.post("/api/ceo/operating-cycle/start")
def start_operating_cycle(req: OperatingCycleStartIn | None = None) -> dict[str, Any]:
    return STATE.start_operating_cycle(req or OperatingCycleStartIn())


@app.post("/api/ceo/operating-cycle/tick")
def tick_operating_cycle(source: str = "api") -> dict[str, Any]:
    return STATE.tick_operating_cycle(source)


@app.post("/api/ceo/operating-cycle/close")
def close_operating_cycle(req: OperatingCycleCloseIn | None = None) -> dict[str, Any]:
    return STATE.close_operating_cycle(req or OperatingCycleCloseIn())


@app.get("/api/ceo/operating-cycles")
def list_operating_cycles(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    capped = max(1, min(limit, 250))
    if status:
        return STATE.rows("operating_cycles", f"status=? ORDER BY started_at DESC LIMIT {capped}", (status,))
    return STATE.rows("operating_cycles", f"1=1 ORDER BY started_at DESC LIMIT {capped}")


@app.post("/api/kill-switch")
def kill_switch(req: KillSwitchIn) -> dict[str, Any]:
    return STATE.set_kill_switch(req)


@app.get("/api/campaigns")
def get_campaigns() -> list[dict[str, Any]]:
    return STATE.rows("campaigns ORDER BY created_at DESC")


@app.post("/api/campaigns")
def create_campaign(req: CampaignAction) -> dict[str, Any]:
    if STATE.is_kill_switch_active(req.client_id):
        raise HTTPException(status_code=423, detail="Kill switch active")
    if STATE.agents["cmo"].status == "paused":
        raise HTTPException(status_code=423, detail="CMO paused")
    if req.budget > 500:
        approval = STATE.create_approval(
            req.client_id,
            "campaign_budget",
            f"Approve campaign budget: ${req.budget:,.2f}",
            {"campaign": model_to_dict(req)},
            "high",
            "cmo",
        )
        return {"success": False, "approval_required": True, "approval": approval}
    cid = short_id()
    now = now_iso()
    STATE.db.execute(
        """
        INSERT INTO campaigns (
            id, client_id, name, status, type, source, leads_total, budget,
            created_at, updated_at, metadata
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cid,
            req.client_id,
            req.name or f"{req.campaign_type.upper()} Campaign {cid}",
            req.action,
            req.campaign_type,
            req.source,
            req.lead_count,
            req.budget,
            now,
            now,
            json_dumps({"created_by": "api"}),
        ),
    )
    STATE.db.commit()
    STATE.audit("cmo", "create_campaign", "campaign", cid, model_to_dict(req))
    STATE.record_budget_usage("cmo", req.budget, req.client_id, "campaign_budget", cid)
    STATE.create_event("new_campaign", req.client_id, "campaign_api", {"campaign_id": cid, "budget": req.budget})
    return {"success": True, "campaign_id": cid, "campaign": dict(STATE.get_one("SELECT * FROM campaigns WHERE id=?", (cid,)))}


@app.post("/api/campaigns/{campaign_id}/action")
def campaign_action(campaign_id: str, req: CampaignAction) -> dict[str, Any]:
    row = STATE.get_one("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if req.action in {"start", "launch"}:
        approval = STATE.create_approval(
            row["client_id"],
            "campaign_launch",
            f"Approve campaign launch: {row['name']}",
            {"campaign_id": campaign_id, "action": req.action},
            "medium",
            "cmo",
        )
        return {"success": False, "approval_required": True, "approval": approval}
    STATE.db.execute(
        "UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
        (req.action, now_iso(), campaign_id),
    )
    STATE.db.commit()
    STATE.audit("cmo", "campaign_action", "campaign", campaign_id, {"action": req.action})
    return {"success": True, "campaign": dict(STATE.get_one("SELECT * FROM campaigns WHERE id=?", (campaign_id,)))}


@app.post("/api/webhooks/sms")
async def sms_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    client_id = payload.get("client_id", DEFAULT_CLIENT_ID)
    if STATE.is_kill_switch_active(client_id):
        raise HTTPException(status_code=423, detail="Kill switch active")
    conv = STATE.record_conversation(
        "sms",
        payload.get("direction", "inbound"),
        payload.get("body") or payload.get("message") or "",
        client_id,
        payload.get("lead_id"),
        payload,
    )
    return {"success": True, "conversation": conv}


@app.post("/api/webhooks/email")
async def email_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    client_id = payload.get("client_id", DEFAULT_CLIENT_ID)
    if STATE.is_kill_switch_active(client_id):
        raise HTTPException(status_code=423, detail="Kill switch active")
    conv = STATE.record_conversation(
        "email",
        payload.get("direction", "inbound"),
        payload.get("body") or payload.get("text") or payload.get("subject") or "",
        client_id,
        payload.get("lead_id"),
        payload,
    )
    return {"success": True, "conversation": conv}


@app.post("/api/webhooks/calendar")
async def calendar_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    appt = STATE.create_appointment(
        payload.get("client_id", DEFAULT_CLIENT_ID),
        payload.get("lead_id"),
        payload.get("starts_at"),
        "calendar",
        payload.get("summary", "Booked appointment"),
        payload.get("owner", "Justin"),
        payload,
    )
    if payload.get("opportunity_id"):
        STATE.db.execute(
            "UPDATE opportunities SET status=?, updated_at=? WHERE id=? AND client_id=?",
            ("call_booked", now_iso(), payload["opportunity_id"], payload.get("client_id", DEFAULT_CLIENT_ID)),
        )
        STATE.db.commit()
        STATE.audit("calendar", "update_opportunity_status", "opportunity", payload["opportunity_id"], {"status": "call_booked"})
    return {"success": True, "appointment": appt}


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    payment_id = payload.get("id") or short_id()
    client_id = payload.get("client_id", DEFAULT_CLIENT_ID)
    status = payload.get("status", "unknown")
    amount = money(payload.get("amount") or payload.get("amount_paid"))
    now = now_iso()
    STATE.db.execute(
        """
        INSERT INTO payments (id, client_id, provider, status, amount, created_at, updated_at, metadata)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status, amount=excluded.amount,
            updated_at=excluded.updated_at, metadata=excluded.metadata
        """,
        (payment_id, client_id, "stripe", status, amount, now, now, json_dumps(payload)),
    )
    STATE.db.commit()
    opportunity_id = payload.get("opportunity_id")
    if opportunity_id:
        next_status = "closed_won" if status in {"paid", "succeeded"} else "payment_failed"
        STATE.db.execute(
            "UPDATE opportunities SET status=?, updated_at=? WHERE id=? AND client_id=?",
            (next_status, now_iso(), opportunity_id, client_id),
        )
        STATE.db.execute(
            "UPDATE payment_links SET status=?, updated_at=? WHERE opportunity_id=? AND client_id=?",
            ("paid" if status in {"paid", "succeeded"} else "payment_failed", now_iso(), opportunity_id, client_id),
        )
        STATE.db.commit()
        STATE.audit("stripe", "update_opportunity_status", "opportunity", opportunity_id, {"status": next_status, "payment_id": payment_id})
    STATE.create_event("payment_succeeded" if status in {"paid", "succeeded"} else "payment_failed", client_id, "stripe", payload)
    return {"success": True, "payment_id": payment_id}


@app.post("/api/webhooks/crm")
async def crm_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    event = STATE.create_event(payload.get("type", "crm_update"), payload.get("client_id", DEFAULT_CLIENT_ID), "crm", payload)
    return {"success": True, "event": event}


@app.post("/api/telegram/webhook")
async def telegram_webhook(req: ChatRequest) -> dict[str, Any]:
    return await route_to_agent(req.message, req.from_user, req.client_id)


def run() -> None:
    import uvicorn

    uvicorn.run("src.coordinator.main:app", host="0.0.0.0", port=PORT, http="h11", log_level="info")


if __name__ == "__main__":
    run()
