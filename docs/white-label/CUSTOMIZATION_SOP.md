# White-Label Customization SOP

> **How to duplicate Dial Desk for any vertical in under 30 minutes.**
> Drop this repo link to any AI agent (Hermes, Claude, Codex, etc.) and it can execute this SOP autonomously.

---

## Purpose

This SOP enables **any developer or AI agent** to take the Dial Desk platform and create a customized version for a different vertical (solar, insurance, real estate, SaaS, healthcare, etc.) with minimal changes.

**Time required:** 30 minutes (human) or 15 minutes (AI agent with tool access)

**Prerequisites:** Git, Python 3.11+, Railway CLI, API keys for new vertical's vendors

---

## SOP: 10-Step Customization

### Step 1: Fork / Clone Repo
```bash
git clone https://github.com/jbellsolutions/dial-desk-swarm.git
cd dial-desk-swarm
```

### Step 2: Rename Brand (5 min)
**Files to edit:** `web/landing-page/index.html`

**Search and replace:**
- `Dial Desk` → `Your Brand Name`
- `Business Loans` → `Your Vertical` (e.g., "Solar Installation")
- `MCA` → `Your Product` (e.g., "Solar Panels")
- `AI Integraterz` → `Your Company`

**Colors to replace:**
- Primary orange: `#FF6B35` → your primary hex
- Accent cyan: `#00E5FF` → your accent hex
- Dark background: `#0A0A0A` → optional

**Update logo/favicon:**
- Replace `favicon.ico` in `web/landing-page/`
- Update any SVG or image references

### Step 3: Update Agent Goals (5 min)
**File:** `src/coordinator/main.py` (lines 66-82)

Change each agent's `goal` field to match new vertical:

```python
AGENTS = {
    "ceo": Agent(id="ceo", name="CEO", role="Chief Executive Officer",
        goal="Orchestrate the solar lead gen fleet. Approve budgets >$500. Daily briefs.",
        skills=["strategy", "budget_approval", "delegation", "reporting", "escalation"]),
    "cmo": Agent(id="cmo", name="CMO", role="Chief Marketing Officer",
        goal="Optimize solar campaigns. Source homeowner leads. Write copy. Schedule blasts.",
        skills=["campaign_mgmt", "lead_sourcing", "copywriting", "scheduling", "analytics"]),
    "sales_director": Agent(id="sales_director", name="Sales Director", role="Sales Director",
        goal="Execute SMS/email sequences for solar consultations. Qualify homeowners. Book appointments.",
        skills=["sms_outreach", "email_outreach", "qualification", "objection_handling", "closing"]),
}
```

**Rule:** Keep agent IDs (ceo, cmo, cto, coo, sales_director) exactly the same. Only change names, roles, goals, and skills.

### Step 4: Swap Campaign Playbook (5 min)
**Source:** `campaigns/playbooks/Business_Loans_Playbook.md`
**Create:** `campaigns/playbooks/YOUR_VERTICAL_Playbook.md`

Copy the structure but rewrite:
- **Lead sources** — e.g., "HomeAdvisor" instead of "TLW"
- **Pricing** — Your lead costs and customer pricing
- **Touch sequence** — Rewrite all 15 messages for new vertical
- **Qualification script** — BANT questions for your product
- **KPI targets** — Adjust based on your market benchmarks

### Step 5: Update Landing Page Pricing (2 min)
**File:** `web/landing-page/index.html`

Change pricing cards:
- AI Caller: `$300/mo` → your price
- Human SDR: `$750/mo` → your price
- Enterprise: `$1,500/mo` → your price

Update feature bullets in each card.

### Step 6: Update API Integration URLs (3 min)
**File:** `src/coordinator/main.py`

If using different vendors, update:
- Smartlead → your email vendor (e.g., Instantly, Mailgun)
- Sendivo → your SMS vendor (e.g., Twilio, TextMagic)
- GHL → your CRM (e.g., HubSpot, Salesforce)

**Pattern:** The `route_to_agent()` function handles vendor routing. Update action keywords:

```python
if any(w in p for w in ["campaign", "cmo", "blast", "sequence", "sms", "email"]):
    agent_id = "cmo"
elif any(w in p for w in ["solar", "panel", "installation", "quote", "roof"]):
    agent_id = "sales_director"  # Updated for solar
```

### Step 7: Update .env Variables (2 min)
**File:** `.env` (copy from `.env.example`)

Replace API keys with new vertical's vendors:
```
SMARTLEAD_API_KEY=your_new_email_vendor_key
SENDIVO_API_KEY=your_new_sms_vendor_key
GHL_API_KEY=your_crm_key
GHL_LOCATION_ID=your_crm_location
STRIPE_SECRET_KEY=your_stripe_key
```

### Step 8: Test Locally (5 min)
```bash
python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8000 --http h11
```

Verify:
- [ ] Landing page loads with new branding
- [ ] Dashboard shows 5 agents with new goals
- [ ] `/api/status` returns correct agent names
- [ ] `/api/ceo/chat` with "launch campaign" routes to CMO

### Step 9: Deploy (2 min)
```bash
railway login
railway link  # Create NEW project (don't overwrite existing)
./scripts/deploy.sh
```

Get new domain:
```bash
railway domain
```

### Step 10: Configure Telegram (1 min)
1. Create new bot with @BotFather
2. Set `TELEGRAM_BOT_TOKEN` in Railway env vars
3. Set webhook:
```bash
curl -X POST "https://api.telegram.org/bot{TOKEN}/setWebhook" \
  -d "url=https://your-new-domain.railway.app/api/telegram/webhook"
```

---

## Config-Only Changes (No Code Required)

These changes require **zero code edits** — just config files:

| Change | File | What to Edit |
|--------|------|--------------|
| Brand name | `web/landing-page/index.html` | Text search/replace |
| Colors | `web/landing-page/index.html` | CSS hex values |
| Pricing | `web/landing-page/index.html` | Price text |
| Agent names | `src/coordinator/main.py` | `name` fields |
| Agent goals | `src/coordinator/main.py` | `goal` fields |
| Campaign messages | `campaigns/playbooks/*.md` | Markdown text |
| Lead sources | `campaigns/playbooks/*.md` | Vendor names/prices |
| API keys | `.env` | Key values |
| Domain | Railway dashboard | Custom domain setting |

---

## AI Agent Instructions

If you are an AI agent receiving this repo link, follow this exact sequence:

1. **Clone repo**
2. **Read this SOP** (you are here)
3. **Read README.md** for architecture overview
4. **Read `src/coordinator/main.py`** lines 1-100 to understand agent definitions
5. **Read `campaigns/playbooks/Business_Loans_Playbook.md`** to understand campaign structure
6. **Execute Steps 2-7 above** (brand, agents, playbook, pricing, URLs, .env)
7. **Test locally** (Step 8)
8. **Deploy** (Step 9)
9. **Report back** with new domain + status

**Critical rules:**
- Do NOT add new frameworks (no React, no Django, no new dependencies)
- Do NOT modify vendor API internals (wrap only)
- Do NOT change agent IDs (ceo, cmo, cto, coo, sales_director)
- Do NOT remove the A2A protocol endpoints (`/agentCard`, `/messages`, `/tasks/*`)
- Do NOT remove the Dial Desk API endpoints (`/api/*`)

---

## Verification Checklist

After customization, verify:

- [ ] Landing page shows new brand name
- [ ] Landing page colors match new brand
- [ ] Pricing matches new vertical
- [ ] Dashboard loads at `/dashboard`
- [ ] `/api/status` shows 5 agents with updated goals
- [ ] `/health` returns `{"status":"up"}`
- [ ] `/agentCard` returns A2A metadata
- [ ] Telegram bot responds to "status"
- [ ] Campaign playbook is vertical-specific
- [ ] `.env` has all required keys
- [ ] Railway domain is live
- [ ] No 404 or 405 errors on any endpoint

---

## Examples: Vertical Adaptations

### Solar Installation
- **Lead sources:** HomeAdvisor, Angi, local home shows
- **Campaign:** "Free solar estimate" → consultation booking
- **Qualification:** Homeowner? Roof age? Electric bill? Shading?
- **Pricing:** AI Caller $400/mo, Human SDR $900/mo

### Insurance (Medicare)
- **Lead sources:** Turning 65 lists, quote request forms
- **Campaign:** "Medicare enrollment deadline" → appointment
- **Qualification:** Age? Current plan? Prescription needs?
- **Pricing:** AI Caller $350/mo, Human SDR $800/mo

### Real Estate (Investors)
- **Lead sources:** PropStream, ListSource, driving for dollars
- **Campaign:** "Cash offer for your house" → property evaluation
- **Qualification:** Property address? Motivation? Timeline?
- **Pricing:** AI Caller $500/mo, Human SDR $1,200/mo

### SaaS (B2B Sales)
- **Lead sources:** Apollo, LinkedIn Sales Nav, G2
- **Campaign:** "Demo request" → calendar booking
- **Qualification:** Company size? Current tool? Decision maker?
- **Pricing:** AI Caller $600/mo, Human SDR $1,500/mo

---

## Support

- **Docs:** `docs/` directory
- **API:** `docs/api/README.md`
- **Deploy:** `docs/deployment/README.md`
- **Setup:** `docs/setup/README.md`
- **Issues:** https://github.com/jbellsolutions/dial-desk-swarm/issues
