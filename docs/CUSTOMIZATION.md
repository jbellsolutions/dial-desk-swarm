# Customization Guide

This system is designed to be **forked and reshaped** for any business vertical.

## Forking for a New Business

### Step 1: Fork the Repo

```bash
git clone https://github.com/jbellsolutions/dial-desk-swarm.git
mv dial-desk-swarm acme-agency-swarm
cd acme-agency-swarm
rm -rf .git
git init
git add .
git commit -m "Initial commit from Dial Desk template"
```

### Step 2: Update Company Context

Edit `shared_instructions.md`:

```markdown
# Shared Runtime Instructions (All Agents)

## 0) Company Context

- **Business:** [Your business description]
- **Entity:** [Your LLC name, EIN, location]
- **Primary contact:** [Your name, email, phone]
- **Tech stack:** [Your tools]
- **Budget guard:** [Your budget]
```

### Step 3: Rename or Replace Agents

Edit `swarm.py` to add/remove agents:

```python
# Remove what you don't need
def create_agency(load_threads_callback=None):
    from agency_swarm import Agency
    from agency_swarm.tools import Handoff, SendMessage

    from orchestrator import create_orchestrator
    from ceo import create_ceo
    from cmo import create_cmo
    # ... keep only what you need

    return Agency(
        *agents,
        communication_flows=flows,
        name="ACME Agency Swarm",
        shared_instructions="shared_instructions.md",
    )
```

### Step 4: Rewrite Agent Instructions

Edit `agents/ceo/instructions.md`:

```markdown
# CEO Instructions

You are the CEO of [Your Company Name].

## Authority
- Strategic decisions
- Budget approval up to $[X]
- Delegation to your team
```

Do this for every agent you keep.

### Step 5: Update Entry Points

In `server.py`:

```python
run_fastapi(
    agencies={
        "acme-agency": create_agency,
    },
    port=8080,
)
```

### Step 6: Deploy

Follow [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Adding Custom Tools

Create a new folder `agents/<name>/tools/` and add Python tool classes:

```python
# agents/ceo/tools/MyCustomTool.py
from agency_swarm.tools import BaseTool

class MyCustomTool(BaseTool):
    """
    Description of what this tool does.
    """

    def run(self):
        # Implementation
        return "Result"
```

Register in `agents/<name>/<name>.py`:

```python
tools_folder="./tools",
```

---

## Changing the Model

Edit `.env`:

```bash
DEFAULT_MODEL=anthropic/claude-sonnet-4-5
```

Or use OpenRouter for cost optimization:

```bash
DEFAULT_MODEL=openrouter/openai/gpt-5.2
OPENAI_API_KEY=sk-or-v1-...
```

---

## Multi-Tenant Setup

For multiple clients on one swarm, add client ID to all requests:

```bash
curl -X POST http://localhost:8080/open-swarm/get_response \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What's my pipeline?",
    "agent": "Sales Director",
    "thread_id": "client_acme_001"
  }'
```

Implement thread isolation in `server.py`.

---

## White-Labeling

For a completely branded experience:

1. Replace all mentions of "Dial Desk" and "AI Integraterz" with your brand
2. Update agent names if desired (e.g., rename "CEO" to "Principal")
3. Edit `shared_instructions.md` with your tone and context
4. Replace the web chat HTML with your branded version
5. Update the logo and colors in the docs

---

## Examples of Vertical Forks

| Vertical | CEO | CMO | CTO | COO | Sales Director |
|---|---|---|---|---|---|
| Plumbing/HVAC | Owner | Marketing Manager | IT/Systems | Dispatch Manager | Lead Setter |
| SaaS | CEO | VP Marketing | VP Engineering | VP Ops | VP Sales |
| E-commerce | Founder | Growth Lead | Dev Lead | Fulfillment Lead | Store Manager |
| Consulting | Principal | BD Lead | Data Lead | Delivery Lead | Account Manager |
| Legal | Managing Partner | Marketing Dir | IT Dir | Paralegal Lead | Intake Coordinator |

---

## Agent Templates

Each agent folder follows this template:

```
agents/<name>/
  __init__.py          # exports create_<name>()
  <name>.py            # Agent class definition
  instructions.md      # System prompt / role definition
  files/               # (optional) File assets
  tools/               # (optional) Custom tools
```

The `instructions.md` is the most important file — it defines the agent's personality, knowledge, and constraints.

---

## Testing Your Customization

```bash
python -c "
from swarm import create_agency
agency = create_agency()
print('Agency:', agency.name)
print('Agents:', [a.name for a in agency.agents])
"
```

Then test each agent:

```bash
curl -X POST http://localhost:8080/open-swarm/get_response \
  -d '{"message":"test","agent":"CEO"}'
```
