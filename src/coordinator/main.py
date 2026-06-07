"""
Dial Desk — Unified Coordinator
FastAPI app serving A2A protocol + Dial Desk API + static files

Vertical: Business Loans (MCA, term loans, lines of credit)
Deploy: railway up
"""
from __future__ import annotations
import asyncio, json, logging, os, sqlite3, time, uuid, pathlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

# ─── CONFIG ───────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dialdesk")

DB_PATH = os.getenv("DIALDESK_DB", "/tmp/dialdesk.sqlite3")
PORT = int(os.getenv("PORT", "8000"))
_PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")

# ─── DATA MODELS ──────────────────────────────────────
@dataclass
class Agent:
    id: str
    name: str
    role: str
    goal: str
    status: str = "idle"
    last_seen: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    tasks_completed_today: int = 0
    current_task: str | None = None
    skills: list[str] = field(default_factory=list)

@dataclass
class Campaign:
    id: str
    name: str
    status: str = "draft"
    type: str = "sms"
    source: str = ""
    leads_total: int = 0
    leads_sent: int = 0
    replies: int = 0
    appointments: int = 0
    reply_rate: float = 0.0
    budget: float = 0.0
    cost_so_far: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

@dataclass
class Task:
    id: str
    prompt: str
    agent_id: str
    status: str = "submitted"
    result: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

# ─── A2A SCHEMAS ─────────────────────────────────────
class Part(BaseModel):
    kind: str = "text"
    text: str = ""

class MessageRequest(BaseModel):
    parts: list[Part] = Field(default_factory=list)
    taskId: str | None = None
    metadata: dict = Field(default_factory=dict)

class MessageResponse(BaseModel):
    taskId: str
    state: str = "submitted"

class TaskStatus(BaseModel):
    taskId: str
    state: str
    artifacts: list = Field(default_factory=list)
    result: dict | None = None

# ─── DIAL DESK SCHEMAS ───────────────────────────────
class ChatRequest(BaseModel):
    message: str
    from_user: str = "CEO"

class CampaignAction(BaseModel):
    action: str
    campaign_type: str = "sms"
    source: str = ""
    lead_count: int = 0
    budget: float = 0

# ─── AGENT DEFINITIONS (Business Loans Vertical) ──────
AGENTS = {
    "ceo": Agent(id="ceo", name="CEO", role="Chief Executive Officer",
        goal="Orchestrate the business loan lead gen fleet. Approve budgets > $500. Daily briefs. Escalation routing. Strategic allocation across lead sources.",
        skills=["strategy", "budget_approval", "delegation", "reporting", "escalation"]),
    "cmo": Agent(id="cmo", name="CMO", role="Chief Marketing Officer",
        goal="Optimize MCA and term loan campaigns. Source qualified business owner leads. Write funding copy. Schedule SMS/email blasts. Report campaign ROI.",
        skills=["campaign_mgmt", "lead_sourcing", "copywriting", "scheduling", "analytics"]),
    "cto": Agent(id="cto", name="CTO", role="Chief Technology Officer",
        goal="Monitor dial desk infrastructure. Deploy services. Manage Smartlead/Sendivo/GHL APIs. Health checks. Uptime. Security.",
        skills=["deployment", "api_mgmt", "monitoring", "security", "troubleshooting"]),
    "coo": Agent(id="coo", name="COO", role="Chief Operating Officer",
        goal="Client onboarding for business loan prospects. Lead delivery to GHL. Calendar bookings. Vendor management (TLW, Leads Warehouse, Magellan). Compliance.",
        skills=["onboarding", "delivery", "calendar", "vendor_mgmt", "compliance"]),
    "sales_director": Agent(id="sales_director", name="Sales Director", role="Sales Director",
        goal="Execute SMS and email sequences to business owners. Qualify MCA leads (BANT). Handle objections. Book appointments. Transfer hot leads to human SDR within 8 seconds.",
        skills=["sms_outreach", "email_outreach", "qualification", "objection_handling", "closing"]),
}

# ─── IN-MEMORY STATE ─────────────────────────────────
class DialDeskState:
    def __init__(self):
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._init_db()
        self.agents = dict(AGENTS)
        self.campaigns: dict[str, Campaign] = {}
        self.tasks: dict[str, Task] = {}
        self.log: list[dict] = []
        self.metrics = {
            "leads_sourced_today": 0,
            "sms_sent_today": 0,
            "emails_sent_today": 0,
            "appointments_booked": 0,
            "revenue_pipeline": 0.0,
            "cost_today": 0.0,
        }
        self._load_campaigns()
        self._load_metrics()
        self.log_event("ceo", "Dial Desk online. Business loan lead gen fleet ready.")

    def _init_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, name TEXT, status TEXT, type TEXT, source TEXT,
                leads_total INTEGER DEFAULT 0, leads_sent INTEGER DEFAULT 0, replies INTEGER DEFAULT 0,
                appointments INTEGER DEFAULT 0, reply_rate REAL DEFAULT 0, budget REAL DEFAULT 0,
                cost_so_far REAL DEFAULT 0, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, prompt TEXT, agent_id TEXT, status TEXT,
                result TEXT, created_at TEXT, completed_at TEXT, metadata TEXT);
            CREATE TABLE IF NOT EXISTS metrics (date TEXT PRIMARY KEY, leads_sourced INTEGER DEFAULT 0,
                sms_sent INTEGER DEFAULT 0, emails_sent INTEGER DEFAULT 0, appointments INTEGER DEFAULT 0,
                revenue_pipeline REAL DEFAULT 0, cost REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS agent_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT, agent TEXT, message TEXT);
        """)
        self.db.commit()

    def _load_campaigns(self):
        for r in self.db.execute("SELECT * FROM campaigns").fetchall():
            self.campaigns[r[0]] = Campaign(id=r[0], name=r[1], status=r[2], type=r[3], source=r[4] or "",
                leads_total=r[5], leads_sent=r[6], replies=r[7], appointments=r[8], reply_rate=r[9],
                budget=r[10], cost_so_far=r[11], created_at=r[12], updated_at=r[13])

    def _load_metrics(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        row = self.db.execute("SELECT leads_sourced, sms_sent, emails_sent, appointments, revenue_pipeline, cost FROM metrics WHERE date=?", (today,)).fetchone()
        if row:
            self.metrics.update({"leads_sourced_today": row[0], "sms_sent_today": row[1],
                "emails_sent_today": row[2], "appointments_booked": row[3], "revenue_pipeline": row[4], "cost_today": row[5]})

    def save_campaign(self, c: Campaign):
        self.db.execute("INSERT OR REPLACE INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.id, c.name, c.status, c.type, c.source, c.leads_total, c.leads_sent, c.replies,
             c.appointments, c.reply_rate, c.budget, c.cost_so_far, c.created_at, c.updated_at))
        self.db.commit(); self.campaigns[c.id] = c

    def save_task(self, t: Task):
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            (t.id, t.prompt, t.agent_id, t.status, t.result, t.created_at, t.completed_at, json.dumps(t.metadata)))
        self.db.commit(); self.tasks[t.id] = t

    def save_metrics(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        self.db.execute("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?)",
            (today, self.metrics["leads_sourced_today"], self.metrics["sms_sent_today"],
             self.metrics["emails_sent_today"], self.metrics["appointments_booked"],
             self.metrics["revenue_pipeline"], self.metrics["cost_today"]))
        self.db.commit()

    def log_event(self, agent: str, message: str):
        now = datetime.utcnow().strftime("%H:%M")
        self.db.execute("INSERT INTO agent_log (time, agent, message) VALUES (?,?,?)", (now, agent, message))
        self.db.commit(); self.log.append({"time": now, "agent": agent, "message": message})
        if len(self.log) > 100: self.log = self.log[-100:]

STATE = DialDeskState()

# ─── AGENT ROUTING ────────────────────────────────────
async def route_to_agent(prompt: str, from_user: str = "CEO") -> dict:
    p = prompt.lower()
    agent_id = "ceo"; action = "general"; budget = 0.0

    # Campaign / Marketing
    if any(w in p for w in ["campaign", "cmo", "blast", "sequence", "sms", "email", "copy", "creative", "roi", "analytics", "funding", "loan", "mca"]):
        agent_id = "cmo"; action = "campaign"
    # Tech / Infrastructure
    elif any(w in p for w in ["tech", "server", "deploy", "error", "api", "health", "uptime", "infrastructure", "security", "bug", "fix"]):
        agent_id = "cto"; action = "tech"
    # Operations / Vendor
    elif any(w in p for w in ["onboard", "client", "delivery", "calendar", "vendor", "tlw", "leads warehouse", "magellan", "booking", "appointment", "compliance"]):
        agent_id = "coo"; action = "ops"
    # Sales / Outreach
    elif any(w in p for w in ["sales", "pipeline", "qualify", "objection", "closing", "follow up", "follow-up", "lead", "reply", "appointment", "transfer", "business owner", "revenue"]):
        agent_id = "sales_director"; action = "sales"

    # Budget extraction
    if "$" in prompt:
        import re
        m = re.search(r'\$([\d,]+(?:\.\d{2})?)', prompt)
        if m: budget = float(m.group(1).replace(",", ""))

    # Budget approval gate
    if budget > 500 and agent_id != "ceo":
        agent_id = "ceo"; action = "budget_approval"

    agent = STATE.agents[agent_id]
    agent.status = "working"
    agent.current_task = prompt[:100]
    agent.last_seen = datetime.utcnow().isoformat()

    task_id = str(uuid.uuid4())[:8]
    task = Task(id=task_id, prompt=prompt, agent_id=agent_id, status="working",
                metadata={"from": from_user, "action": action, "budget": budget})
    STATE.save_task(task)
    STATE.log_event(agent_id, f"Task #{task_id}: {action} | {prompt[:80]}")

    # Simulated execution (replace with real vendor API calls when keys configured)
    await asyncio.sleep(0.3)
    result = f"[{agent.name}] {action} completed for business loan campaign. Task #{task_id} done."

    agent.status = "idle"
    agent.tasks_completed_today += 1
    agent.current_task = None
    agent.last_seen = datetime.utcnow().isoformat()

    task.status = "completed"
    task.result = result
    task.completed_at = datetime.utcnow().isoformat()
    STATE.save_task(task)
    STATE.log_event(agent_id, f"Completed #{task_id}: {result[:80]}")

    if action == "sales":
        STATE.metrics["sms_sent_today"] += 100
    if action == "campaign":
        STATE.metrics["emails_sent_today"] += 200
    STATE.save_metrics()

    return {
        "response": result,
        "routed_to": agent_id,
        "agent_name": agent.name,
        "task_id": task_id,
        "action": action,
        "budget_approved": budget <= 500 or agent_id == "ceo",
    }

# ─── FASTAPI APP ──────────────────────────────────────
app = FastAPI(title="Dial Desk — Biz Loans Autonomous Business", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Serve static files
STATIC_DIR = pathlib.Path(__file__).parent.parent.parent / "web"
LANDING_DIR = STATIC_DIR / "landing-page"
DASHBOARD_DIR = STATIC_DIR / "dashboard"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ═══════════════════════════════════════════════════════
# A2A PROTOCOL ROUTES
# ═══════════════════════════════════════════════════════

@app.get("/agentCard")
async def agent_card() -> dict:
    return {
        "name": "Dial Desk — Biz Loans",
        "description": "Autonomous business loan lead generation. C-suite AI agents (CEO, CMO, CTO, COO, Sales Director) orchestrating SMS/email campaigns via Smartlead/Sendivo. A2A-compliant gateway.",
        "url": _PUBLIC_URL,
        "version": "2.0.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": "campaign_mgmt",
                "name": "Campaign Management",
                "description": "Create, launch, and monitor SMS/email campaigns for business loan lead generation.",
                "tags": ["campaign", "sms", "email", "mca", "term-loan", "lead-gen"],
                "examples": [
                    "Launch SMS campaign to 1000 MCA leads in Texas",
                    "Check email campaign ROI for Q3",
                    "Pause all campaigns under $100 budget",
                ],
            },
            {
                "id": "lead_qualification",
                "name": "Lead Qualification",
                "description": "Qualify business loan prospects using BANT framework. Route hot leads to human SDR.",
                "tags": ["qualification", "bant", "routing", "hot-lead"],
                "examples": [
                    "Qualify these 50 new leads",
                    "Route hot leads to SDR team",
                ],
            }
        ],
    }

@app.post("/messages")
async def submit(req: MessageRequest) -> MessageResponse:
    prompt = "\n".join(p.text for p in req.parts if p.kind == "text" and p.text)
    if not prompt:
        raise HTTPException(status_code=400, detail="No text part in message")

    result = await route_to_agent(prompt, req.metadata.get("from_user", "A2A"))
    task_id = result["task_id"]
    return MessageResponse(taskId=task_id, state="submitted")

@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = STATE.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "taskId": task.id,
        "status": {"state": task.status},
        "artifacts": [{"kind": "text", "text": task.result}] if task.result else [],
        "result": {
            "agent": task.agent_id,
            "action": task.metadata.get("action", "unknown"),
            "budget": task.metadata.get("budget", 0),
        },
    }

# ═══════════════════════════════════════════════════════
# DIAL DESK ROUTES
# ═══════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def landing_page():
    f = LANDING_DIR / "index.html"
    if f.exists():
        return f.read_text()
    return HTMLResponse("<h1>Dial Desk</h1><p>Business Loan Lead Generation</p>")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    f = DASHBOARD_DIR / "index.html"
    if f.exists():
        return f.read_text()
    return HTMLResponse("<h1>CEO Dashboard</h1><p>Loading...</p>")

@app.get("/dashboard.html", response_class=HTMLResponse)
def dashboard_html():
    return dashboard()

@app.get("/health")
def health():
    return {"status": "up", "service": "DialDesk", "version": "2.0.0", "time": datetime.utcnow().isoformat()}

@app.get("/api/status")
def get_status():
    return {
        "agents": {k: asdict(v) for k, v in STATE.agents.items()},
        "campaigns": [asdict(c) for c in STATE.campaigns.values()],
        "metrics": STATE.metrics,
        "log": STATE.log[-20:],
        "tasks_today": len([t for t in STATE.tasks.values() if t.status == "completed" and t.created_at[:10] == datetime.utcnow().strftime("%Y-%m-%d")]),
    }

@app.post("/api/ceo/chat")
async def ceo_chat(req: ChatRequest):
    return await route_to_agent(req.message, req.from_user)

@app.get("/api/agents/{agent_id}")
def get_agent(agent_id: str):
    if agent_id not in STATE.agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    return asdict(STATE.agents[agent_id])

@app.get("/api/campaigns")
def get_campaigns():
    return [asdict(c) for c in STATE.campaigns.values()]

@app.post("/api/campaigns")
def create_campaign(req: CampaignAction):
    cid = str(uuid.uuid4())[:8]
    c = Campaign(id=cid, name=f"Campaign {cid}", status=req.action, type=req.campaign_type,
                 source=req.source, leads_total=req.lead_count, budget=req.budget)
    STATE.save_campaign(c)
    STATE.log_event("cmo", f"Created {cid}: {req.campaign_type} | {req.source} | {req.lead_count} leads | ${req.budget}")
    return {"success": True, "campaign_id": cid, "campaign": asdict(c)}

@app.post("/api/campaigns/{campaign_id}/action")
def campaign_action(campaign_id: str, req: CampaignAction):
    if campaign_id not in STATE.campaigns:
        raise HTTPException(status_code=404, detail="Campaign not found")
    c = STATE.campaigns[campaign_id]
    c.status = req.action
    c.updated_at = datetime.utcnow().isoformat()
    STATE.save_campaign(c)
    STATE.log_event("cmo", f"Campaign {campaign_id} {req.action}")
    return {"success": True, "campaign": asdict(c)}

@app.post("/api/telegram/webhook")
async def telegram_webhook(req: ChatRequest):
    return await route_to_agent(req.message, req.from_user)

# ─── CRON: CEO DAILY BRIEF ────────────────────────────
async def ceo_daily_brief():
    while True:
        now = datetime.utcnow()
        if now.hour == 8 and now.minute == 0:
            await route_to_agent(
                f"Daily brief: {STATE.metrics['leads_sourced_today']} leads sourced, "
                f"{STATE.metrics['appointments_booked']} appointments, "
                f"${STATE.metrics['revenue_pipeline']} pipeline. Review and allocate.", "system"
            )
            for a in STATE.agents.values():
                a.tasks_completed_today = 0
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup():
    logger.info("Dial Desk starting...")
    asyncio.create_task(ceo_daily_brief())
    STATE.log_event("ceo", "Dial Desk online. Business loan fleet ready.")

# ─── ENTRY POINT ──────────────────────────────────────
def run() -> None:
    import uvicorn
    uvicorn.run("coordinator.main:app", host="0.0.0.0", port=PORT, http="h11", log_level="info")

if __name__ == "__main__":
    run()
