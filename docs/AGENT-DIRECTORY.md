# Agent Directory

Full catalog of all 13 agents in the Dial Desk Executive Swarm.

---

## C-Suite

### CEO
**Role:** Strategic leader
**Files:** `agents/ceo/`
**Purpose:** Makes decisions, monitors KPIs, delegates to CMO/CTO/COO/Sales Director, approves spend under $500.

**When to use:**
- Strategic direction, budgets, approvals
- Complex decisions requiring cross-functional coordination
- Commissioning multi-agent projects

**Can commission:**
- Any production specialist

---

### CMO
**Role:** Chief Marketing Officer
**Files:** `agents/cmo/`
**Purpose:** Campaigns, landing pages, ad management, copy, lead programs, brand positioning.

**When to use:**
- Marketing campaigns
- Landing page copy
- Lead generation strategies
- Brand positioning

**Can commission:**
- Slides Agent (pitch decks)
- Docs Agent (landing page copy, proposals)
- Image Agent (ad creative)
- Video Agent (promo videos)

---

### CTO
**Role:** Chief Technology Officer
**Files:** `agents/cto/`
**Purpose:** Infrastructure, API integrations (GHL, Composio), deployments, tech stack decisions.

**When to use:**
- System architecture
- Integration planning
- Deployment strategy
- Tech stack evaluation

**Can commission:**
- General Agent (Composio tool execution)
- Docs Agent (architecture docs)

---

### COO
**Role:** Chief Operating Officer
**Files:** `agents/coo/`
**Purpose:** Client onboarding, vendor management, delivery tracking, ops, SOPs.

**When to use:**
- Client onboarding design
- Vendor SLA evaluation
- SOP creation
- Operational workflow optimization

**Can commission:**
- Data Analyst (ops dashboards)
- Docs Agent (SOPs)

---

### Sales Director
**Role:** Sales Director
**Files:** `agents/sales_director/`
**Purpose:** SMS/email outreach, lead qualification, pipeline management, deal closing.

**When to use:**
- Outreach strategy
- Pipeline analysis
- Deal closing tactics
- Lead qualification rubric

**Can commission:**
- Deep Research (market intel)
- Data Analyst (pipeline metrics)
- Docs Agent (proposals)

---

## Production Specialists

### Orchestrator
**Role:** Router
**Files:** `agents/orchestrator/`
**Purpose:** Routes user requests to the correct agent. Does not execute tasks.

**When to use:**
- Automatic (default entry point)
- When you don't know which agent to ask

---

### General Agent
**Role:** Virtual assistant + integrations
**Files:** `agents/virtual_assistant/`
**Purpose:** 10,000+ external integrations via Composio (Gmail, Slack, GHL, Stripe, etc.)

**When to use:**
- Send emails
- Schedule meetings
- Check integrations
- File operations

---

### Deep Research Agent
**Role:** Researcher
**Files:** `agents/deep_research/`
**Purpose:** Evidence-based research with source-backed analysis. Scholar search enabled.

**When to use:**
- Market research
- Competitor analysis
- Academic/scholarly topics
- Fact-checking

---

### Data Analyst
**Role:** Analyst
**Files:** `agents/data_analyst/`
**Purpose:** Data analysis, KPIs, charts, statistical modeling.

**When to use:**
- Metrics dashboards
- Sales pipeline analysis
- Campaign performance
- Health scores

---

### Slides Agent
**Role:** Presentation engineer
**Files:** `agents/slides_agent/`
**Purpose:** PowerPoint creation, editing, .pptx export.

**When to use:**
- Investor pitches
- Sales decks
- Proposals
- Training materials

---

### Docs Agent
**Role:** Document engineer
**Files:** `agents/docs_agent/`
**Purpose:** Document creation, editing, conversion (PDF, DOCX, Markdown, TXT).

**When to use:**
- Contracts
- SOPs
- Proposals
- Blog posts
- Landing page copy

---

### Video Agent
**Role:** Video specialist
**Files:** `agents/video_generation_agent/`
**Purpose:** Video generation, editing, assembly.

**When to use:**
- Product demos
- Social media content
- Training videos
- Promo reels

---

### Image Agent
**Role:** Image specialist
**Files:** `agents/image_generation_agent/`
**Purpose:** Image generation, editing, composition.

**When to use:**
- Ad creative
- Social media graphics
- Logo concepts
- Presentation visuals

---

## Communication Matrix

```
Orchestrator → all agents (routing)
CEO → all specialists (commissioning)
CMO → Slides, Docs, Image, Video
CTO → General Agent
COO → Data Analyst, Docs
Sales Director → Deep Research, Data Analyst, Docs
Any agent → Any other agent (handoff)
```
