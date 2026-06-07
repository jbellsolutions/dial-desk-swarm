# Super Agent Integration Reference

This directory contains raw code copied from the **Super Agent Sales Director**
repository (`/tmp/super-agent-sales-director`). It is kept here as a
**reference library** — not wired into the running Dial Desk Swarm app.

## Philosophy

- **Dial Desk Swarm** (`~/dial-desk-swarm-local`) is the production, deployable,
  self-contained project for the "Your Phone Team" offering.
- **Super Agent** (`/tmp/super-agent-sales-director`) is a separate, deeper
  agent framework with NATS JetStream, Temporal workflows, Composio hub, etc.
- We keep Super Agent's **integration patterns** (how to call Smartlead, how to
  call Sendivo, how to serve A2A cards) nearby so any agent working on Dial Desk
  can learn from them without switching repos.

## What's Here

| Directory | Source | Purpose | Status |
|-----------|--------|---------|--------|
| `smartlead/` | `src/agent_os/runtimes/smartlead/` | Email sending via Smartlead API | Reference only — imports `agent_os` |
| `sendivo/` | `src/agent_os/runtimes/sendivo/` | SMS sending via Sendivo API | Reference only — imports `agent_os` |
| `a2a/` | `src/agent_os/a2a/` | A2A server + agent card spec | Reference only — imports `agent_os` |
| `coordinator/` | `src/agent_os/runtimes/coordinator/` | Real coordinator runtime | Reference only — imports `agent_os` |
| `prospect_intel/` | `src/agent_os/runtimes/prospect_intel/` | ICP scoring logic | Reference only — imports `agent_os` |

## What's Actually Wired

The **working code** that Dial Desk Swarm uses is in sibling files:

- `smartlead_api.py` — Standalone httpx wrapper (no `agent_os` imports)
- `sendivo_api.py` — Standalone httpx wrapper (no `agent_os` imports)
- `../../coordinator/main.py` — FastAPI app that imports `smartlead_api` and
  `sendivo_api` when API keys are present

## When to Use Reference vs. Wired

| Situation | Action |
|-----------|--------|
| "How does Smartlead handle replies?" | Read `smartlead/invoke.py` (reference) |
| "Send an email from Dial Desk" | Import `smartlead_api.send_to_sequence()` (wired) |
| "How does Super Agent do A2A?" | Read `a2a/server.py` (reference) |
| "Add A2A to Dial Desk" | Extend `coordinator/main.py` A2A endpoints (wired) |

## Adding a New Integration

1. Read the Super Agent reference version to understand the vendor's API shape.
2. Write a new `_api.py` file in this directory using **only** standard libs +
   `httpx` (no `agent_os` imports).
3. Import your new `_api.py` from `coordinator/main.py` and wire it into the
   route handler.
4. Add the env var to `.env.example`.
5. Commit.

## Do NOT

- Modify files in `smartlead/`, `sendivo/`, `a2a/`, `coordinator/`, or
  `prospect_intel/` directly — they are frozen reference copies.
- Import `agent_os.*` from `coordinator/main.py` — that couples Dial Desk to
  Super Agent's framework and breaks standalone deploy.
- Delete this directory — future agents need the reference.
