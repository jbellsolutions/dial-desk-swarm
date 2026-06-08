# DialDesk Autonomous Appointment-Setting OS

DialDesk is a 24/7 managed appointment-setting operation for MCA and funding companies. It runs outreach, qualification, booking, reporting, approvals, and fulfillment operations from one durable FastAPI business runtime.

DialDesk does not offer business loans. The first ICP is MCA/funding companies that want more qualified booked calls through unlimited SMS, unlimited email, AI SDR, human SDR, lead programs, follow-up, calendar booking, and weekly reporting.

## What Runs

- FastAPI production surface for Railway, VPS, or local Docker.
- Durable SQLite business database at `DIALDESK_DB`, defaulting to `/app/data/dialdesk.sqlite3`.
- CEO operating loop for morning review, daily plan, delegation, approvals, event handling, and end-of-day style reporting.
- Worker loop that executes safe internal tasks and leaves risky decisions in approvals.
- Event system for webhooks, signups, campaign changes, booking, failures, and compliance risks.
- Guardrails for approvals, opt-outs, suppression, budget approvals, audit logs, global kill switch, and per-client kill switches.
- Dashboard endpoints for Justin, the CEO view, client status, campaigns, conversations, appointments, reports, and tasks.
- Business memory in SQLite, with optional Obsidian markdown export and Notion page sync through the outbox.
- Slack visibility through a durable outbox, with optional webhook delivery when explicitly enabled.
- Browser automation job queue with claim leases, heartbeats, retry/recovery, completion artifacts, and memory writeback.
- Durable outreach execution queue for SMS and email, with dry-run default and real provider sends only when `DIALDESK_SEND_OUTREACH=1`.
- Durable provider sync queue for Smartlead replies/stats and Sendivo logs/billing, so inbound provider data becomes conversations, events, memory, and dashboard state.
- Revenue-loop objects for opportunities, sales-call briefs, payment-link handoff, CRM sync jobs, and Stripe payment webhooks.
- Agent coordination layer with durable inter-agent messages, request-to-task handoffs, standups, heartbeats, Slack commands, and dashboard visibility.
- Production operations layer with durable health checks, incident tracking, scheduled SQLite backups, Slack commands, and dashboard/API visibility.
- Deployment readiness checks for local, production, and live-outreach profiles, with scored blockers before the system is trusted to operate.
- Durable onboarding workflow engine for the DialDesk Day 0-14 launch playbook.
- Optional `APP_TOKEN` protection for the operator control plane, with durable request logging.
- Paperclip-style governance primitives inside the current runtime: mission/project/agent goals, per-agent budgets, auto-pause, heartbeats, and Slack-style operator commands.

## Operating Model

The CEO is the daily operator. Every day it reads the business state, creates the brief, delegates work to the CMO, COO, CTO, Sales Director, and Builder, and only escalates the things Justin should actually touch: approvals, sales calls, broken systems, risky compliance items, and unusual client decisions.

The team works from durable tasks and events rather than one-off chat messages:

- CMO owns MCA/funding positioning, list strategy, SMS/email campaigns, offer testing, copy, and ROI.
- COO owns onboarding, launch checklists, rep operations, access reminders, weekly debriefs, and client health.
- CTO owns production health, integrations, infrastructure, deployment, monitoring, and restart safety.
- Sales Director owns qualification, hot replies, AI/human SDR routing, sales-call prep, and bookings.
- Builder owns approved system improvements.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8080 --http h11
```

Open:

- Landing page: `http://localhost:8080/`
- CEO dashboard: `http://localhost:8080/dashboard`
- API status: `http://localhost:8080/api/status`
- Daily CEO brief: `http://localhost:8080/api/ceo/daily-brief`

Run the operator doctor against any live runtime:

```bash
BASE_URL=http://127.0.0.1:8080 bash scripts/ops_doctor.sh
```

If `APP_TOKEN` is set in `.env`, the doctor reads it automatically. It checks health, dashboard controls, API auth, control room actions, production readiness, launch gate, proof-pack visibility, memory, browser operator visibility, agent coordination, and outbox visibility.

Run the full business-stack proof pack:

```bash
BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/business_stack_probe.sh
```

That runs the deploy doctor, memory proof, Slack proof, and browser proof as one go/no-go command, then records the result at `/api/ops/proof-runs` for dashboard, API, activity-feed, and Slack visibility.

Prove the memory stack against any live runtime:

```bash
BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh
```

That writes a memory note, verifies it is queryable from SQLite, checks the Obsidian markdown file when `OBSIDIAN_VAULT_PATH` is configured, and confirms Notion outbox queueing when `NOTION_DATABASE_ID` is configured.

Prove the Slack stack against any live runtime:

```bash
BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh
```

That queues a Slack-visible alert, verifies durable outbox visibility, proves JSON Slack-style commands, and exercises the `/slack/command` slash-command ingress.

## Configuration

```bash
cp .env.example .env
```

Core environment variables:

```env
DIALDESK_DB=/app/data/dialdesk.sqlite3
PUBLIC_URL=https://dial-desk-api-production.up.railway.app
APP_TOKEN=
PORT=8080
CEO_DAILY_HOUR_UTC=8
CEO_EOD_HOUR_UTC=22
SCHEDULER_INTERVAL_SECONDS=60
DIALDESK_HEALTH_CHECK_INTERVAL_SECONDS=300
DIALDESK_BACKUP_INTERVAL_SECONDS=86400
DIALDESK_SELF_AUDIT_INTERVAL_SECONDS=3600
DIALDESK_BACKUP_DIR=/app/data/backups
DIALDESK_DEFAULT_AGENT_MONTHLY_BUDGET=500

SENDIVO_API_KEY=
SENDIVO_BEARER_AUTH=
SENDIVO_BASE_URL=https://app.sendivo.io/api/v1
SMARTLEAD_API_KEY=
SMARTLEAD_BASE_URL=https://server.smartlead.ai/api/v1
GHL_API_KEY=
GHL_LOCATION_ID=
GHL_CONTACT_UPSERT_URL=
STRIPE_SECRET_KEY=
STRIPE_PAYMENT_LINK_AI_CALLER=
STRIPE_PAYMENT_LINK_HUMAN_SDR=
SLACK_BOT_TOKEN=
SLACK_WEBHOOK_URL=
SLACK_SIGNING_SECRET=
NOTION_API_KEY=
NOTION_DATABASE_ID=
OBSIDIAN_VAULT_PATH=
DIALDESK_SEND_OUTBOX=0
DIALDESK_SEND_OUTREACH=0
DIALDESK_CREATE_PAYMENT_LINKS=0
DIALDESK_SYNC_CRM=0
DIALDESK_API_BASE_URL=http://127.0.0.1:8080
DIALDESK_BROWSER_OPERATOR_ID=super-browser
DIALDESK_BROWSER_MODE=auto
DIALDESK_BROWSER_ARTIFACT_DIR=/app/data/browser-artifacts
DIALDESK_BROWSER_POLL_SECONDS=15
```

`DIALDESK_SEND_OUTBOX=0` keeps Slack and Notion messages queued for review. Set it to `1` only when the webhook/API credentials are ready and you want the runtime to deliver queued messages.

`DIALDESK_SEND_OUTREACH=0` keeps SMS/email execution in dry-run mode. Set it to `1` only after provider credentials, suppression rules, approval gates, and quiet-hour policy are ready for production sends.

`DIALDESK_CREATE_PAYMENT_LINKS=0` keeps payment-link handoff in dry-run mode. Set it to `1` only after `STRIPE_PAYMENT_LINK_AI_CALLER` and/or `STRIPE_PAYMENT_LINK_HUMAN_SDR` are configured.

`DIALDESK_SYNC_CRM=0` keeps CRM writes in dry-run mode. Set it to `1` only after `GHL_CONTACT_UPSERT_URL` and `GHL_API_KEY` are configured for the exact CRM write endpoint you want DialDesk to use.

Set `APP_TOKEN` in production to protect `/api/*`, `/messages`, and `/tasks/*`. Send `Authorization: Bearer <APP_TOKEN>` or `X-API-Token: <APP_TOKEN>`. The fallback dashboard prompts for the token and stores it in browser local storage.

Browser/super-browser operator:

```bash
python -m src.browser_operator --once --mode http
python -m src.browser_operator --mode auto
```

`auto` uses Playwright when it is installed and falls back to HTTP inspection. For rendered browser automation on a VPS, install the optional browser runtime:

```bash
pip install playwright
playwright install chromium
```

Prove the browser stack against a live runtime:

```bash
BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/browser_stack_probe.sh
```

That queues a browser job, runs `src.browser_operator` once, verifies the operator heartbeat, and confirms the browser result was written into memory.

## API Surface

Core operations:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Runtime health check |
| `GET` | `/agentCard` | A2A card and business capabilities |
| `POST` | `/messages` | Submit a natural-language job to the operating team |
| `GET` | `/tasks/{id}` | A2A task status |
| `GET` | `/api/status` | Full CEO dashboard snapshot |
| `GET` | `/api/ops/overview` | Under-the-hood map of runtime, memory, visibility, browser jobs, and guardrails |
| `GET` | `/api/ops/control-room` | Unified operator control room with live state, attention items, controls, and under-the-hood explanation |
| `POST` | `/api/ops/control-room/action` | Run safe operator actions such as health, readiness, watchdog, backup, runtime pause/resume, and kill switch |
| `GET` | `/api/activity` | Unified timeline across tasks, events, memory, approvals, browser jobs, outbox, incidents, and audit records |
| `POST` | `/api/clients` | Create a client and launch onboarding |
| `GET` | `/api/clients/{id}/dashboard` | Client-facing setup and performance data |
| `GET` | `/api/ceo/daily-brief` | CEO morning review and delegated plan |
| `POST` | `/api/ceo/operating-cycle/start` | Start or inspect today's CEO operating cycle |
| `POST` | `/api/ceo/operating-cycle/tick` | Update today's cycle metrics and close at EOD when due |
| `POST` | `/api/ceo/operating-cycle/close` | Close today's CEO cycle with an end-of-day report |
| `GET` | `/api/ceo/operating-cycles` | Inspect CEO operating-cycle history |
| `GET` | `/api/ceo/decisions` | Inspect the CEO decision log and rationale trail |
| `POST` | `/api/ceo/decisions/evaluate` | Make the CEO review current state and create needed escalations/tasks |
| `POST` | `/api/opportunities` | Create a DialDesk sales opportunity |
| `GET` | `/api/opportunities` | Inspect the sales pipeline |
| `POST` | `/api/sales-briefs` | Create a sales-call brief |
| `GET` | `/api/sales-briefs` | Inspect sales-call briefs |
| `POST` | `/api/payment-links` | Create a Stripe payment handoff or dry-run link |
| `GET` | `/api/payment-links` | Inspect payment-link handoffs |
| `POST` | `/api/crm/sync-jobs` | Queue CRM write work |
| `GET` | `/api/crm/sync-jobs` | Inspect CRM write queue |
| `POST` | `/api/crm/sync-jobs/{id}/execute` | Execute one CRM sync job |
| `POST` | `/api/crm/sync-next` | Execute queued CRM sync jobs |
| `POST` | `/api/agent-messages` | Send a durable message between Justin and/or agents |
| `GET` | `/api/agent-messages` | Inspect agent inboxes and coordination threads |
| `POST` | `/api/agent-messages/{id}/read` | Mark an agent message read |
| `POST` | `/api/agent-messages/{id}/reply` | Reply inside an existing agent thread |
| `GET` | `/api/agent-threads` | Inspect durable coordination threads |
| `GET` | `/api/agents/{id}/inbox` | Inspect one operator or agent inbox |
| `GET` | `/api/agents/coordination` | Full coordination-room snapshot for dashboard/Slack operators |
| `POST` | `/api/agents/standup` | Run a durable agent standup and heartbeat sweep |
| `POST` | `/api/ops/health-checks/run` | Run a durable production health check |
| `GET` | `/api/ops/health-checks` | Inspect health-check history |
| `GET` | `/api/ops/runtime-controls` | Inspect which autonomous subsystems are enabled or paused |
| `POST` | `/api/ops/runtime-controls` | Enable/pause scheduler, worker, outreach, browser recovery, syncs, outbox, workflow, scheduled ops, or watchdog |
| `GET` | `/api/ops/runtime-heartbeats` | Inspect runtime proof-of-life for autonomous subsystems |
| `POST` | `/api/ops/readiness/run` | Run a deployment or live-outreach readiness check |
| `GET` | `/api/ops/readiness` | Inspect readiness history and blockers |
| `POST` | `/api/ops/watchdog/run` | Run the autonomous watchdog against heartbeats, queues, browser operators, and incidents |
| `GET` | `/api/ops/watchdog` | Inspect watchdog run history and findings |
| `POST` | `/api/ops/self-audit/run` | Run an internal runtime self-audit and record it as a proof run |
| `POST` | `/api/ops/autonomy-drills/run` | Run a safe 24/7 autonomy proof across CEO cycle, memory, Slack, browser queue, agent coordination, watchdog, and readiness |
| `GET` | `/api/ops/autonomy-drills` | Inspect autonomy proof history and evidence |
| `POST` | `/api/ops/launch-gate/run` | Run the VPS/Railway/local production launch gate with deploy evidence and commands |
| `GET` | `/api/ops/launch-gate` | Inspect launch-gate history, blockers, warnings, and run commands |
| `GET` | `/api/ops/proof-pack` | Inspect the full deploy proof pack, latest proof runs, and exact prove-it commands |
| `GET` | `/api/ops/proof-runs` | Inspect recorded business-stack proof history |
| `POST` | `/api/ops/proof-runs` | Record an external proof script result |
| `POST` | `/api/ops/operator-briefing` | Generate an operator briefing that explains business state, under-the-hood layers, proof status, and next actions |
| `GET` | `/api/reports` | Inspect CEO daily briefs, operator briefings, close reports, and client reports |
| `POST` | `/api/ops/incidents` | Open a production incident |
| `GET` | `/api/ops/incidents` | Inspect incidents |
| `POST` | `/api/ops/incidents/{id}/resolve` | Resolve an incident |
| `POST` | `/api/ops/backups/create` | Create a SQLite backup |
| `GET` | `/api/ops/backups` | Inspect backup history |
| `GET` | `/api/ops/request-log` | Inspect request/auth audit history |
| `POST` | `/api/workflows/launch` | Launch a client workflow/playbook |
| `GET` | `/api/workflows/runs` | Inspect workflow runs |
| `GET` | `/api/workflows/steps` | Inspect workflow steps |
| `POST` | `/api/workflows/execute-due` | Queue due workflow steps as tasks |
| `POST` | `/api/workflows/steps/{id}/complete` | Complete or skip a workflow step |

The fallback CEO dashboard at `/dashboard` includes operator controls wired to `/api/ops/control-room/action`: health, watchdog, autonomy proof, launch gate, readiness, backup, outbox processing, browser recovery, CEO cycle tick, runtime pause/resume, and the global kill switch. With `APP_TOKEN` set, the dashboard prompts once and sends the same bearer token used by the API.

Plan interfaces:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/events` | Ingest a business event and let the CEO route it |
| `POST` | `/api/memory` | Save an operating memory note and mirror to Obsidian/Notion when configured |
| `GET` | `/api/memory` | Inspect durable business memory |
| `GET` | `/api/memory/health` | Inspect SQLite, Obsidian, Notion, and outbox memory sync health |
| `POST` | `/api/memory/{id}/sync-notion` | Queue one memory entry for Notion sync |
| `POST` | `/api/memory/resync-notion` | Batch queue memory entries for Notion resync |
| `POST` | `/api/slack/notify` | Queue a Slack-visible operating alert |
| `POST` | `/slack/command` | Slack slash-command ingress for `/dialdesk ...` |
| `GET` | `/api/outbox` | Inspect queued Slack/Notion deliveries |
| `POST` | `/api/outbox/process` | Process queued deliveries when sending is enabled |
| `POST` | `/api/browser/jobs` | Queue a browser/super-browser automation job |
| `GET` | `/api/browser/jobs` | Inspect browser automation jobs |
| `POST` | `/api/browser/jobs/claim` | Claim the next queued browser job with a lease |
| `POST` | `/api/browser/jobs/{id}/heartbeat` | Extend a claimed browser job lease |
| `POST` | `/api/browser/jobs/{id}/fail` | Fail/requeue a browser job |
| `POST` | `/api/browser/jobs/{id}/complete` | Save browser job results and memory |
| `POST` | `/api/browser/jobs/recover-expired` | Requeue expired browser leases |
| `GET` | `/api/browser/operators` | Inspect browser/super-browser operator proof-of-life |
| `POST` | `/api/browser/operators/heartbeat` | Let an external browser operator report online/idle/working status |
| `POST` | `/api/outreach/jobs` | Queue SMS or email outreach for a lead |
| `GET` | `/api/outreach/jobs` | Inspect outreach execution queue |
| `POST` | `/api/outreach/jobs/{id}/execute` | Execute one outreach job |
| `POST` | `/api/outreach/execute-next` | Execute the next queued outreach jobs |
| `GET` | `/api/integrations/config` | Show which provider credentials/features are configured |
| `POST` | `/api/integrations/syncs` | Queue Smartlead or Sendivo sync work |
| `GET` | `/api/integrations/syncs` | Inspect provider sync queue |
| `POST` | `/api/integrations/syncs/{id}/execute` | Execute one provider sync |
| `POST` | `/api/integrations/sync-next` | Execute the next queued provider syncs |
| `POST` | `/api/governance/goals` | Create a mission/project/agent/task goal |
| `GET` | `/api/governance/goals` | Inspect goal hierarchy |
| `GET` | `/api/governance/budgets` | Inspect per-agent budgets and pauses |
| `POST` | `/api/governance/budgets` | Set a monthly budget/auto-pause policy |
| `POST` | `/api/governance/heartbeats` | Record an agent heartbeat |
| `GET` | `/api/governance/heartbeats` | Inspect recent heartbeats |
| `POST` | `/api/slack/command` | Process Slack-style operator commands as JSON |
| `POST` | `/api/agents/{id}/pause` | Pause an agent |
| `POST` | `/api/agents/{id}/resume` | Resume an agent |
| `GET` | `/api/tasks` | List durable tasks |
| `POST` | `/api/tasks/{id}/complete` | Complete a task |
| `GET` | `/api/approvals` | List approval queue |
| `POST` | `/api/approvals/{id}/approve` | Approve an item |
| `POST` | `/api/approvals/{id}/reject` | Reject an item |
| `POST` | `/api/leads/import` | Import, dedupe, score, and suppress leads |
| `POST` | `/api/kill-switch` | Set global or client send/call lock |
| `POST` | `/api/webhooks/sms` | Record SMS replies/sends |
| `POST` | `/api/webhooks/email` | Record email replies/sends |
| `POST` | `/api/webhooks/calendar` | Record booked calls |
| `POST` | `/api/webhooks/stripe` | Record payment events |
| `POST` | `/api/webhooks/crm` | Record CRM events |

## Test

```bash
bash scripts/test.sh
```

The smoke test verifies:

- CEO daily workflow creates delegated tasks.
- Event wakeups create CEO decision records with rationale, then create the right tasks and approvals.
- Lead import dedupes and respects suppression.
- SMS replies become conversations and route hot replies.
- Booking/client dashboard paths work.
- Approval gates can be approved.
- Kill switch blocks lead import and send paths.
- The runtime uses a real database file instead of in-memory state.
- Memory writes to SQLite and Obsidian when `OBSIDIAN_VAULT_PATH` is configured, tracks Notion `queued`/`synced`/`failed` state, and can be resynced.
- Slack alerts queue to the durable outbox.
- Browser jobs can be queued, claimed by `python -m src.browser_operator`, heartbeated, retried, recovered from expired leases, completed, and written back to memory. Browser operators also report idle/working proof-of-life.
- `/api/ops/overview` explains how the runtime is wired underneath.
- `/api/ops/control-room` is the operator cockpit: current status, attention queue, safe controls, deployment evidence, and the plain-English map of how the business works.
- `/api/activity` shows one transparent feed across autonomous decisions, tasks, memory, browser work, alerts, incidents, and audit records.
- `/api/ceo/decisions` shows the CEO's decision trail: trigger, rationale, action, risk, task, approval, and waiting status.
- SMS/email outreach jobs execute in dry-run mode, record outbound conversations, and respect the kill switch.
- Smartlead reply sync and Sendivo log sync import inbound conversations and update sync dashboard state.
- Opportunities, sales briefs, payment links, CRM sync jobs, and Stripe webhook close-out work through durable revenue-loop tables.
- Agent messages persist, questions/requests can become tasks, replies stay in durable threads, inboxes expose who is blocked, and standups create messages, heartbeats, memory, and Slack-visible summaries.
- CEO operating cycles start the day, run the plan, tick through scheduler loops, and close with an end-of-day report.
- CEO decision reviews run from the scheduler or Slack/API, escalate high-risk approvals to Justin, and delegate broken systems back to operators.
- Watchdog runs from the scheduler or Slack/API, dedupes production incidents, and alerts when heartbeats, browser operators, or queues go unhealthy.
- Autonomy drills prove the system can execute a safe operating cycle, write memory, queue Slack/browser work, coordinate agents, and produce deployment evidence.
- Launch gates turn VPS/Railway deployment into a recorded go/no-go check with service manifests, persistent paths, browser operator, memory, Slack, backup, watchdog, autonomy proof, and exact launch commands.
- Health checks, incident creation/resolution, backup creation, and Slack ops commands are durable and visible.
- Readiness checks score local, production, and live-outreach safety before exposing the app or arming provider sends.
- Runtime controls let Justin pause/resume autonomous subsystems without killing the server or losing state.
- Runtime heartbeats show when each autonomous subsystem last ran, what it processed, and whether it is paused or failing.
- Secured-mode requests require `APP_TOKEN`, unauthorized attempts return `401`, and request history is logged.
- Client signup launches the DialDesk Day 0-14 onboarding workflow, due steps become tasks, and workflow progress appears on dashboards.
- Governance seeds mission/project/agent goals, tracks budgets, records heartbeats, and supports Slack-style status/pause/resume/budget commands.

## Deploy

Docker runs the coordinator directly:

```bash
docker build -t dialdesk-autonomous .
docker run -p 8080:8080 -e DIALDESK_DB=/app/data/dialdesk.sqlite3 dialdesk-autonomous
```

Railway and VPS deployments should run:

```bash
python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port ${PORT:-8080} --http h11
BASE_URL=http://127.0.0.1:${PORT:-8080} PROBE_URL=http://127.0.0.1:${PORT:-8080}/health bash scripts/business_stack_probe.sh
```

For a full VPS install of the API plus browser/super-browser operator:

```bash
APP_DIR=/opt/dial-desk-swarm SERVICE_USER=$(id -un) bash scripts/vps_bootstrap.sh
```

The VPS bootstrap installs dependencies, syncs the repo, writes both systemd services, enables the API and browser operator, waits for `/health`, and runs `scripts/business_stack_probe.sh` against the deployed runtime. The proof pack records readiness and launch-gate evidence through the same authenticated control-room path used by the dashboard and Slack, then proves memory, Slack, and browser paths.

The quickest full proof after deploy is `BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/business_stack_probe.sh`.

After bootstrap, run `BASE_URL=http://127.0.0.1:8080 bash scripts/browser_stack_probe.sh` to prove browser automation can move from queued job to completed memory entry through the same API and token protection used in production.

Also run `BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh` to prove SQLite, Obsidian, and Notion-queue memory behavior from the deployed control plane.

Run `BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh` to prove Slack-visible alerts, outbox inspection, and slash-command control are working from the deployed control plane.

## Template Later

Do not extract templates yet. First prove DialDesk end to end for the MCA/funding appointment-setting offer. After it works without prompting, extract `offer.yaml` with ICP, offer, channels, pricing, playbooks, scoring, dashboards, approvals, and integrations.
