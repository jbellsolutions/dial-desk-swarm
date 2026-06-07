# Architecture

## System Overview

Dial Desk Executive Swarm is a multi-agent system built on the Agency Swarm framework. It consists of 13 AI agents organized into a C-suite business layer and a production specialist layer.

```
User Request
    |
    v
Orchestrator (entry point & router)
    |
    +---> Business Strategy Layer (C-Suite)
    |         |
    |         +---> CEO (strategic decisions, KPI monitoring, delegation)
    |         |         +---> Can commission any production specialist
    |         |
    |         +---> CMO (campaigns, landing pages, copy, lead programs)
    |         |         +---> Commissions: Docs, Image, Video, Slides
    |         |
    |         +---> CTO (infrastructure, integrations, deployments)
    |         |         +---> Commissions: General Agent (Composio tools)
    |         |
    |         +---> COO (client onboarding, vendors, ops, SOPs)
    |         |         +---> Commissions: Data Analyst, Docs
    |         |
    |         +---> Sales Director (outreach, pipeline, deals)
    |                   +---> Commissions: Deep Research, Data Analyst
    |
    +---> Production Specialist Layer
              |
              +---> Deep Research Agent (research, analysis)
              +---> Data Analyst (KPIs, charts, data)
              +---> Slides Agent (presentations, .pptx)
              +---> Docs Agent (documents, conversions)
              +---> Video Agent (video generation)
              +---> Image Agent (image generation)
              +---> General Agent (10,000+ integrations)
```

## Communication Flow

All agents communicate through two mechanisms:

### 1. SendMessage (delegation)
Orchestrator or CEO can send a task to any specialist without transferring the conversation. Used when parallel work is needed.

### 2. Handoff (transfer)
Any agent can transfer a conversation to another agent. The receiving agent gets full conversation history and can iterate directly with the user.

## Runtime Environment

- **Host**: DigitalOcean VPS (NYC3) or equivalent
- **Python**: 3.10+
- **Framework**: Agency Swarm 1.9.9+
- **API Server**: FastAPI + Uvicorn
- **Port**: 8080 (default)
- **Authentication**: Optional API token (recommended for production)

## File Structure

```
dial-desk-swarm/
├── agents/                  # Agent definitions
│   ├── orchestrator/         # Entry-point router
│   ├── ceo/                 # Strategic leader
│   ├── cmo/                # Marketing
│   ├── cto/                # Technology
│   ├── coo/                # Operations
│   ├── sales_director/      # Sales
│   ├── virtual_assistant/  # Composio integrations
│   ├── deep_research/      # Research
│   ├── data_analyst/       # Data analysis
│   ├── slides_agent/       # Presentations
│   ├── docs_agent/         # Documents
│   ├── video_generation_agent/  # Video
│   └── image_generation_agent/  # Images
├── swarm.py                 # Agent wiring & communication flows
├── server.py                # FastAPI entry point
├── config.py                # Model configuration
├── shared_instructions.md   # Context shared across all agents
├── .env                     # API keys & secrets
├── .env.example            # Template for .env
├── requirements.txt         # Python dependencies
├── docker-compose.yml       # Docker orchestration
├── Dockerfile               # Container definition
├── systemd/                 # Linux service files
├── docs/                    # Documentation
│   ├── ARCHITECTURE.md
│   ├── DEPLOYMENT.md
│   ├── CUSTOMIZATION.md
│   ├── INTAKE-SUCCESS-SYSTEM.md
│   └── AGENT-DIRECTORY.md
└── scripts/                 # Helper scripts
    ├── install.sh
    ├── start.sh
    └── test.sh
```

## Model Configuration

Agents use a shared model configuration via `config.py`:

```python
# Default model (OpenAI)
DEFAULT_MODEL=gpt-5.2

# Alternative via LiteLLM
DEFAULT_MODEL=anthropic/claude-sonnet-4-5
```

The system auto-detects if the model is OpenAI (no slash) or LiteLLM-routed (contains slash) and configures accordingly.

## State Management

- **Conversations**: Stored via Agency Swarm's ThreadManager (configurable callback)
- **Files**: Agent-specific files stored in `agents/<name>/files/`
- **Logs**: Runtime logs to stdout + file
- **No database required** for basic operation. Optional: connect PostgreSQL for multi-tenancy.

## Security Considerations

- API key stored in `.env` (never commit)
- Optional app token for production API access
- File access restricted to `agents/<name>/files/` and `./uploads`
- No public internet exposure required (runs on localhost or VPN/VPS)
