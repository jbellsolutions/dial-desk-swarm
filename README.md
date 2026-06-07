# Dial Desk — Biz Loans Autonomous Business

> **A production-grade, white-label multi-agent platform for autonomous business loan lead generation.** Deploy a C-suite AI agent swarm (CEO, CMO, CTO, COO, Sales Director) that runs SMS/email campaigns, qualifies leads, and books appointments 24/7. Duplicate for any offer in under 30 minutes.

---

## What This Is

Dial Desk is a **complete autonomous business-in-a-box** for lead generation. Not a script. Not a template. A fully deployed, API-connected system with:

- **5 C-suite AI agents** running on Railway 24/7
- **Smartlead + Sendivo integration** for live email/SMS sends
- **GoHighLevel CRM** pipeline + calendar booking
- **CEO Dashboard** for real-time monitoring
- **Landing page + pricing** ready for client signups
- **Telegram/Slack/Web control** — talk to your swarm from anywhere
- **macOS menu bar** — live status from your desktop

**Primary vertical: Business Loans (MCA, term loans, lines of credit)**
**Designed to white-label:** Swap 5 config values → new offer live in 30 min

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT / OWNER INTERFACE                        │
│  ┌──────────────┐ ┌──────────────┐ ┌─────────────────────────────┐ │
│  │  Landing Page│ │ CEO Dashboard│ │  Telegram / Slack / Web     │ │
│  │  (Lead Gen)  │ │ (Monitoring) │ │  (Agent Chat / Commands)   │ │
│  │              │ │              │ │                             │ │
│  │  $300 AI     │ │  5 Agents    │ │  "Launch SMS campaign       │ │
│  │  $750 Human  │ │  3 Campaigns │ │   to 1,000 leads"           │ │
│  │  Sign Up     │ │  Revenue     │ │  → routed to CMO           │ │
│  └──────┬───────┘ └──────┬───────┘ └─────────────┬───────────────┘ │
└─────────┼──────────────────┼─────────────────────┼───────────────┘
          │                  │                     │
          └──────────────────┼─────────────────────┘
                             │
                    ┌────────┴────────┐
                    │  Dial Desk API  │  ← FastAPI (A2A + Dial Desk)
                    │  (Gateway)      │
                    │  /api/* + A2A    │
                    │  Port 8000       │
                    └───────┬─────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
   ┌────┴────┐       ┌─────┴──────┐     ┌──────┴──────┐
   │  CEO    │       │   CMO      │     │ Sales Dir   │
   │ Router  │       │ Campaign   │     │ Outreach    │
   │ Budget  │       │ Manager    │     │ Sequences   │
   │ >$500   │       │ Smartlead  │     │ Sendivo     │
   └────┬────┘       └─────┬──────┘     └──────┬──────┘
        │                  │                   │
        └──────────────────┼───────────────────┘
                             │
                    ┌────────┴────────┐
                    │  Hermes Fabric  │  ← (optional future layer)
                    │  NATS JetStream │
                    │  Temporal       │
                    └─────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   ┌────┴─────┐       ┌─────┴──────┐      ┌─────┴──────┐
   │Smartlead │       │  Sendivo   │      │    GHL     │
   │ (Email)  │       │  (SMS)     │      │  (CRM)     │
   │ Sequences│       │  Personal  │      │ Pipeline   │
   │ Drip     │       │  15-touch  │      │ Calendar   │
   └────┬─────┘       └─────┬──────┘      └─────┬──────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                              │
                    ┌─────────┴──────────┐
                    │     LEAD SOURCES   │
                    │  TLW SMS Real-Time │
                    │  Leads Warehouse   │
                    │  Magellan Triggers │
                    │  UCC Filings       │
                    └────────────────────┘
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Gateway | FastAPI + Uvicorn | HTTP API + static file serving |
| Protocol | A2A (Agent-to-Agent) | Agent discovery + job submission |
| Database | SQLite | Campaigns, tasks, metrics, logs |
| Frontend | Vanilla HTML/CSS/JS | Landing page + dashboard |
| Email | Smartlead API | Drip sequences, inbox management |
| SMS | Sendivo CLI / API | Personalized SMS campaigns |
| CRM | GoHighLevel | Lead pipeline, calendar, nurture |
| Calendar | Cal.com | Booking page embed |
| Payments | Stripe | Subscription billing |
| Hosting | Railway | 24/7 container hosting |
| Local Dev | Docker | Replicate production locally |

---

## Features

### Agent Swarm (5 Agents)

| Agent | Role | Key Actions |
|-------|------|-------------|
| **CEO** | Chief Executive Officer | Budget approval >$500, daily briefs, escalation routing, strategic decisions |
| **CMO** | Chief Marketing Officer | Create campaigns, source leads, write copy, schedule blasts, report ROI |
| **CTO** | Chief Technology Officer | Monitor infrastructure, deploy updates, manage APIs, health checks |
| **COO** | Chief Operating Officer | Client onboarding, lead delivery, calendar bookings, vendor management |
| **Sales Director** | Head of Sales | Execute SMS/email sequences, qualify leads, handle objections, book appointments |

### Campaign Engine
- **15-touch cadence** over 21 days (SMS + Email + Call rotation)
- **Smartlead integration** — Create campaigns, upload leads, trigger sequences
- **Sendivo integration** — Personalized SMS with merge tokens
- **Lead qualification** — BANT + urgency scoring, hot/warm/cold routing
- **Auto-escalation** — Hot leads transferred to human SDR within 8 seconds

### CEO Dashboard (`/dashboard`)
- **Agent Status Cards** — Live idle/working/offline for all 5 agents
- **Campaign Cards** — Active/paused/completed with reply rates, costs, appointments
- **KPIs** — Leads sourced, SMS sent, emails sent, appointments, revenue, cost
- **Activity Log** — Timestamped agent actions
- **CEO Chat Box** — Direct command interface (e.g., "Launch MCA campaign to 1000 leads")

### Multi-Channel Control
- **Web** — Dashboard + landing page served from same FastAPI app
- **Telegram** — `@super_agent_1_bot` — status queries, campaign commands
- **Slack** — `/dialdesk status`, `/dialdesk launch`, `/dialdesk report`
- **macOS** — Menu bar app with live agent count + revenue

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/jbellsolutions/dial-desk-swarm.git
cd dial-desk-swarm
```

### 2. Install

```bash
pip install fastapi uvicorn pydantic python-multipart
```

Or:
```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your API keys
```

Minimum viable:
```
SMARTLEAD_API_KEY=your_key
SENDIVO_API_KEY=your_key
GHL_API_KEY=your_key
GHL_LOCATION_ID=your_id
TELEGRAM_BOT_TOKEN=your_token
STRIPE_SECRET_KEY=sk_test_...
```

### 4. Run Local

```bash
python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000 --http h11
```

- Landing: http://localhost:8000/
- Dashboard: http://localhost:8000/dashboard
- API: http://localhost:8000/api/status

### 5. Deploy

```bash
railway login
railway link
./scripts/deploy.sh
```

Done. Your autonomous business is live.

---

## API Reference

### A2A Protocol (Agent Discovery)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/agentCard` | Agent capabilities |
| POST | `/messages` | Submit job to swarm |
| GET | `/tasks/{task_id}` | Check task status |

### Dial Desk API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Full fleet snapshot |
| POST | `/api/ceo/chat` | CEO command/chat |
| GET | `/api/agents/{id}` | Single agent profile |
| GET | `/api/campaigns` | List all campaigns |
| POST | `/api/campaigns` | Create new campaign |
| POST | `/api/campaigns/{id}/action` | Start/pause/stop |
| POST | `/api/telegram/webhook` | Telegram webhook |

See `docs/api/README.md` for full schemas + curl examples.

---

## White-Label & Duplication

This platform is **config-driven**. To duplicate for a new offer (solar, insurance, real estate, etc.):

### Step-by-Step (30 Minutes)

1. **Fork repo** → `git clone`
2. **Update branding** in `web/landing-page/index.html`:
   - Search/replace: `Dial Desk` → `Your Brand`
   - Colors: `#FF6B35` (orange) → your primary
   - Colors: `#00E5FF` (cyan) → your accent
3. **Update agent goals** in `src/coordinator/main.py` lines 66-82:
   - Change "MCA funding" → "solar panels" or "insurance quotes"
   - Update skills array for each agent
4. **Swap campaign playbook** in `campaigns/playbooks/`:
   - Copy `MCA_Campaign_Playbook.md` → `Solar_Campaign_Playbook.md`
   - Rewrite sequences for new vertical
5. **Update pricing** in landing page pricing cards
6. **Update lead sources** in `CampaignAction` API (or keep same vendors)
7. **Deploy fresh** — `railway up` on **new Railway project**
8. **Custom domain** — `railway domain add your-brand.com`

See `docs/white-label/CUSTOMIZATION_SOP.md` for the complete SOP with checklists.

---

## Campaign Playbook (Business Loans)

See `campaigns/playbooks/Business_Loans_Playbook.md`

**21-day 15-touch sequence:**
- Day 1: SMS intro + pain point
- Day 2: Email value add
- Day 3: SMS social proof
- Day 4: Call qualification
- Day 5: SMS urgency
- ... through Day 21: Referral ask

**Lead sources:**
- TLW SMS Real-Time ($67/lead, call within 8 sec)
- Leads Warehouse ($45/lead, aged 3-30 days)
- Magellan Triggers ($55/lead, credit/UCC events)
- UCC Filings ($38/lead, public record data)

**KPI targets:**
- SMS delivery >95%
- Reply rate >15%
- Qualified rate >5%
- Appointment rate >3%
- Funded rate >0.5%
- Cost per deal <$500

---

## Pricing (What You Charge Clients)

| Tier | Monthly | Includes |
|------|---------|----------|
| **AI Caller** | $300/mo | 1,000 SMS + 5,000 emails + AI voice + dashboard |
| **Human SDR** | $750/mo | 3,000 SMS + 15,000 emails + 1-2 human SDR seats + calendar |
| **Enterprise** | $1,500/mo | Unlimited + dedicated agent + custom sequences + priority |

Pass-through: Lead lists ($38-134/lead), Sendivo credits, Smartlead inboxes.

---

## macOS Menu Bar

```bash
cd tools/macos-menu-bar
pip install rumps
python3 dial_desk_menubar.py
```

Shows: Agent count, leads today, SMS sent, revenue. Click opens dashboard.

---

## File Structure

```
dial-desk-swarm/
├── README.md                          # This file
├── .env.example                       # Required API keys
├── requirements.txt                   # Python deps
│
├── src/
│   └── coordinator/
│       └── main.py                    # FastAPI app (A2A + Dial Desk)
│
├── web/
│   ├── landing-page/
│   │   └── index.html                 # Client offer page
│   └── dashboard/
│       └── index.html                 # CEO dashboard (real-time)
│
├── campaigns/
│   └── playbooks/
│       └── Business_Loans_Playbook.md # Full 15-touch sequence
│
├── docs/
│   ├── api/
│   │   └── README.md                  # Full API docs with curl
│   ├── deployment/
│   │   └── README.md                  # Railway + Docker deploy
│   ├── setup/
│   │   └── README.md                  # Local setup step-by-step
│   ├── architecture/
│   │   └── README.md                  # System architecture deep dive
│   └── white-label/
│       └── CUSTOMIZATION_SOP.md       # 30-min duplication guide
│
├── deploy/
│   ├── docker/Dockerfile              # Production container
│   └── railway/railway.json           # Railway config
│
├── scripts/
│   └── deploy.sh                      # One-command deploy
│
├── tools/
│   └── macos-menu-bar/
│       └── dial_desk_menubar.py       # Desktop status app
│
├── .github/workflows/
│   └── deploy.yml                     # CI/CD auto-deploy
│
└── tests/
    ├── unit/                          # Unit tests
    └── integration/                   # Integration tests
```

---

## Contributing

Two rules:
1. **No vendor API edits** — Wrap, don't modify
2. **No new frameworks** — FastAPI + vanilla JS only

---

## License

MIT © 2026 Dial Desk
