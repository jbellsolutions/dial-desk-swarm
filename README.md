# Dial Desk Executive Swarm

**The AI-Powered Executive Team That Scales Your Business**

A production-grade multi-agent system built on [Agency Swarm](https://github.com/VRSEN/agency-swarm) that deploys a full C-suite of AI agents to run and scale your business operations.

Originally built for **AI Integraterz / Dial Desk** (MCA lead generation & sales operations), this system is **business-model-agnostic** and can be forked for any agency, consultancy, or service business.

---

## What You Get

### The C-Suite
| Agent | Role | What They Own |
|---|---|---|
| **CEO** | Strategic leader | KPIs, delegation, budget approval, decisions |
| **CMO** | Marketing | Campaigns, ads, landing pages, copy, lead programs |
| **CTO** | Technology | Infrastructure, API integrations, deployments, stack |
| **COO** | Operations | Client onboarding, vendors, delivery, SOPs |
| **Sales Director** | Sales | Outreach, qualification, pipeline, deals, dialer |

### Production Specialists
| Agent | Role | What They Own |
|---|---|---|
| **Orchestrator** | Router | Routes requests to the right executive |
| **Deep Research** | Researcher | Evidence-based research, source-backed analysis |
| **Data Analyst** | Analyst | KPIs, charts, data analysis, insights |
| **Slides Agent** | Presentations | PowerPoint creation, .pptx export |
| **Docs Agent** | Documents | PDF, DOCX, Markdown, TXT creation |
| **Video Agent** | Video | Video generation, editing, assembly |
| **Image Agent** | Images | Image generation, editing, composition |
| **General Agent** | Integrations | 10,000+ Composio integrations (Gmail, Slack, CRM, etc.) |

### Included Systems
- **Client Intake & Onboarding**: Automated qualifying, scoring, welcome sequences
- **Client Success Dashboard**: Health scores, automated nudges, escalation rules
- **100-Client Management**: Manage 100+ clients in under 1 hour/day combined
- **Budget Guardrails**: $25/day agency spend limit with CEO approval flow

---

## Quick Start

### Prerequisites
- Python 3.10+
- OpenAI API key (or compatible LiteLLM provider)
- 2GB RAM minimum

### Install

```bash
git clone https://github.com/jbellsolutions/dial-desk-swarm.git
cd dial-desk-swarm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your keys
```

### Run

```bash
python server.py
```

The API will be available at `http://127.0.0.1:8080`

### Test

```bash
python scripts/test.py
```

---

## API Reference

### Single Message
```bash
curl -X POST http://127.0.0.1:8080/open-swarm/get_response \
  -H "Content-Type: application/json" \
  -d '{"message":"I need a go-to-market strategy for my new offer","agent":"CEO"}'
```

### With Streaming
```bash
curl -X POST http://127.0.0.1:8080/open-swarm/get_response_stream \
  -H "Content-Type: application/json" \
  -d '{"message":"Write me a landing page for a plumbing SaaS","agent":"CMO"}'
```

### Get Metadata
```bash
curl http://127.0.0.1:8080/open-swarm/get_metadata
```

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full system design.

```
User Request
    |
    v
Orchestrator (routes to CEO or specialist)
    |
    +---> CEO (delegates to CMO, CTO, COO, Sales Director)
    |         |
    |         +---> CMO (commissions Slides, Docs, Image, Video)
    |         +---> CTO (uses General Agent for integrations)
    |         +---> COO (uses Data Analyst for ops dashboards)
    |         +---> Sales Director (uses Deep Research for market intel)
    |
    +---> Specialists (Deep Research, Data Analyst, Slides, etc.)
```

---

## Deployment

### Docker

```bash
docker-compose up -d
```

### Systemd (Linux VPS)

```bash
sudo cp systemd/dial-desk-swarm.service /etc/systemd/system/
sudo systemctl enable dial-desk-swarm
sudo systemctl start dial-desk-swarm
```

### Railway

```bash
railway login
railway link
railway up
```

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for full deployment guide.

---

## Customizing For Your Business

This system is designed to be **forked and reshaped** for any vertical:

1. **Fork the repo**
2. **Rewrite agent instructions** in `agents/*/instructions.md`
3. **Update `shared_instructions.md`** with your company context
4. **Rename agents** or add new ones in `swarm.py`
5. **Deploy** and start talking to your new C-suite

### Customization Guide

See [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md) for step-by-step fork instructions.

---

## Commercial License

This project is licensed under the **Commercial License**.

- **Personal / Internal Use**: Free
- **Agency / Client Deployment**: $2,500 one-time license
- **White-Label / Resell Rights**: $5,000 one-time license

See [LICENSE.md](LICENSE.md) for full terms.

**Interested in licensing?**
Email: justin@usingaitoscale.com

---

## Screenshots

### Web Chat Interface
![Web Chat](docs/screenshots/web-chat.png)

### Agent Network Visualization
![Agent Network](docs/screenshots/agent-network.png)

---

## Roadmap

- [x] C-suite agent definitions
- [x] Production specialist agents
- [x] Client intake & onboarding system
- [x] Client success dashboard spec
- [ ] Stripe billing integration
- [ ] GHL CRM deep integration
- [ ] Slack alert system
- [ ] Vapi voice agent integration
- [ ] Multi-tenant support
- [ ] White-label dashboard

---

## Support

- **Email**: justin@usingaitoscale.com
- **GitHub Issues**: [jbellsolutions/dial-desk-swarm/issues](https://github.com/jbellsolutions/dial-desk-swarm/issues)

---

## Built By

**Justin Bellware** — AI Integraterz / Dial Desk
Tampa, FL

---

*Ready to clone your C-suite?*
