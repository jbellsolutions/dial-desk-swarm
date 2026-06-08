# Deployment Guide

## Development (Local)

### Prerequisites
- Python 3.10+
- Git
- 2GB RAM
- OpenAI API key (or compatible provider)

### Steps

```bash
git clone https://github.com/jbellsolutions/dial-desk-swarm.git
cd dial-desk-swarm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8080 --http h11
```

API available at `http://127.0.0.1:8080`

---

## Production (VPS / Bare Metal)

### Prerequisites
- Ubuntu 22.04+ or Debian 12+
- Python 3.10+
- systemd
- nginx (optional, for reverse proxy + auth)

### Steps

```bash
# 1. Clone
git clone https://github.com/jbellsolutions/dial-desk-swarm.git /opt/dial-desk-swarm
cd /opt/dial-desk-swarm

# 2. Virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Environment
cp .env.example .env
nano .env  # Add your keys

# 4. Systemd service
APP_DIR=/opt/dial-desk-swarm SERVICE_USER=$(id -un) bash scripts/vps_bootstrap.sh

# 5. Check status
sudo systemctl status dial-desk-swarm
sudo systemctl status dial-desk-browser-operator
curl -s http://127.0.0.1:8080/health
BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/business_stack_probe.sh
BASE_URL=http://127.0.0.1:8080 bash scripts/ops_doctor.sh
BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh
BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh
BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/browser_stack_probe.sh
```

---

## Docker

### Prerequisites
- Docker 20.10+
- Docker Compose 2.0+

### Steps

```bash
cd dial-desk-swarm
cp .env.example .env
# Edit .env with your keys
docker-compose up -d
```

### Stop

```bash
docker-compose down
```

---

## Railway

### Prerequisites
- Railway CLI
- Railway account

### Steps

```bash
# 1. Login
railway login

# 2. Create project
railway init --name dial-desk-swarm

# 3. Add environment variables via dashboard or CLI
railway variables set OPENAI_API_KEY=sk-...
railway variables set DEFAULT_MODEL=gpt-5.2
railway variables set DIALDESK_DB=/app/data/dialdesk.sqlite3
railway variables set APP_TOKEN=$(openssl rand -hex 32)
railway variables set DIALDESK_BACKUP_DIR=/app/data/backups
railway variables set DIALDESK_HEALTH_CHECK_INTERVAL_SECONDS=300
railway variables set DIALDESK_BACKUP_INTERVAL_SECONDS=86400
railway variables set SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
railway variables set NOTION_API_KEY=secret_...
railway variables set NOTION_DATABASE_ID=...
railway variables set DIALDESK_SEND_OUTBOX=0
railway variables set SENDIVO_BEARER_AUTH=...
railway variables set SMARTLEAD_API_KEY=...
railway variables set DIALDESK_SEND_OUTREACH=0
railway variables set STRIPE_PAYMENT_LINK_HUMAN_SDR=https://buy.stripe.com/...
railway variables set STRIPE_PAYMENT_LINK_AI_CALLER=https://buy.stripe.com/...
railway variables set GHL_CONTACT_UPSERT_URL=https://...
railway variables set DIALDESK_CREATE_PAYMENT_LINKS=0
railway variables set DIALDESK_SYNC_CRM=0
railway variables set DIALDESK_DEFAULT_AGENT_MONTHLY_BUDGET=500

# 4. Deploy
railway up
```

---

## DigitalOcean App Platform

### Steps

1. Fork the repo to your GitHub
2. In DigitalOcean, go to **Apps** → **Create App**
3. Choose GitHub source, select your fork
4. Set environment variables (OPENAI_API_KEY, etc.)
5. Deploy

---

## Transparency And Memory

The runtime is inspectable from:

```bash
curl -s http://127.0.0.1:8080/api/status
curl -s http://127.0.0.1:8080/api/ops/overview
curl -s http://127.0.0.1:8080/api/ops/control-room
curl -s http://127.0.0.1:8080/api/activity
curl -s http://127.0.0.1:8080/api/ceo/decisions
curl -s http://127.0.0.1:8080/api/ops/runtime-controls
curl -s http://127.0.0.1:8080/api/ops/runtime-heartbeats
curl -s http://127.0.0.1:8080/api/ops/watchdog
curl -s http://127.0.0.1:8080/api/ops/autonomy-drills
curl -s -X POST http://127.0.0.1:8080/api/ceo/decisions/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"source":"operator","force":true}'
curl -s http://127.0.0.1:8080/api/agents/coordination
curl -s http://127.0.0.1:8080/api/agent-threads
curl -s http://127.0.0.1:8080/api/memory
curl -s http://127.0.0.1:8080/api/outbox
curl -s http://127.0.0.1:8080/api/browser/jobs
curl -s http://127.0.0.1:8080/api/browser/operators
curl -s http://127.0.0.1:8080/api/integrations/config
curl -s http://127.0.0.1:8080/api/integrations/syncs
curl -s http://127.0.0.1:8080/api/opportunities
curl -s http://127.0.0.1:8080/api/payment-links
curl -s http://127.0.0.1:8080/api/crm/sync-jobs
curl -s http://127.0.0.1:8080/api/agent-messages
curl -s http://127.0.0.1:8080/api/ops/health-checks
curl -s http://127.0.0.1:8080/api/ops/incidents
curl -s http://127.0.0.1:8080/api/ops/backups
curl -s http://127.0.0.1:8080/api/ops/request-log
curl -s http://127.0.0.1:8080/api/workflows/runs
curl -s http://127.0.0.1:8080/api/workflows/steps
curl -s http://127.0.0.1:8080/api/governance/goals
curl -s http://127.0.0.1:8080/api/governance/budgets
```

Memory behavior:

- SQLite is always the source of truth.
- Set `OBSIDIAN_VAULT_PATH=/path/to/vault` to mirror operating notes into markdown under `DialDesk/YYYY-MM-DD/`.
- Set `NOTION_API_KEY` and `NOTION_DATABASE_ID` to queue Notion page syncs.
- Set `DIALDESK_SEND_OUTBOX=1` only when Slack/Notion credentials are ready and you want delivery enabled.
- Inspect sync state with `GET /api/memory/health`.
- Prove the full SQLite-to-Obsidian-to-Notion-queue path with `BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh`.
- Queue one memory entry for Notion with `POST /api/memory/{id}/sync-notion`.
- Batch retry Notion sync with `POST /api/memory/resync-notion`.
- Delivered Notion outbox items update the memory row to `synced`; failed deliveries update it to `failed`.

Slack behavior:

- `SLACK_WEBHOOK_URL` enables delivery through the outbox processor.
- With `DIALDESK_SEND_OUTBOX=0`, alerts stay queued and visible in `/api/outbox`.
- Configure Slack slash commands to post to `https://your-app.example.com/slack/command`.
- Set `SLACK_SIGNING_SECRET` to verify Slack request signatures.
- Prove alert queueing, command handling, and slash-command ingress with `BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh`.
- `/dialdesk status`, `/dialdesk control-room`, `/dialdesk memory-health`, `/dialdesk readiness production`, `/dialdesk watchdog`, `/dialdesk autonomy-drill`, `/dialdesk browser-jobs`, `/dialdesk browser-operators`, `/dialdesk outbox`, and `/dialdesk operating-cycle` are supported.
- `POST /api/slack/command` remains available for JSON-based internal/operator calls.

Browser automation behavior:

- Queue browser work through `POST /api/browser/jobs`.
- Browser/super-browser claims work through `POST /api/browser/jobs/claim`.
- Browser/super-browser reports idle/working proof-of-life through `POST /api/browser/operators/heartbeat`.
- Inspect browser operators with `GET /api/browser/operators`.
- Claimed jobs should heartbeat through `POST /api/browser/jobs/{id}/heartbeat`.
- Complete jobs through `POST /api/browser/jobs/{id}/complete`.
- Fail temporary browser errors through `POST /api/browser/jobs/{id}/fail`; jobs retry until attempts are exhausted.
- Expired leases are recovered by the scheduler or manually through `POST /api/browser/jobs/recover-expired`.
- Completed browser results are saved back into memory.
- Run the durable browser operator with `python -m src.browser_operator`.
- Prove the full API-to-browser-to-memory path with `BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/browser_stack_probe.sh`.
- Use `--mode auto` to prefer Playwright and fall back to HTTP inspection.
- Install optional rendered-browser support with `pip install playwright && playwright install chromium`.
- VPS systemd unit: `systemd/dial-desk-browser-operator.service`.
- Docker Compose profile: `docker compose --profile browser up -d`.

Outreach execution behavior:

- Queue SMS/email work through `POST /api/outreach/jobs`.
- Inspect with `GET /api/outreach/jobs`.
- Execute with `POST /api/outreach/jobs/{id}/execute` or let the scheduler drain queued jobs.
- `DIALDESK_SEND_OUTREACH=0` records dry-run outbound conversations only.
- `DIALDESK_SEND_OUTREACH=1` allows provider calls through Sendivo and Smartlead, still behind kill switches and suppression checks.

Provider sync behavior:

- Queue Smartlead reply polling with `POST /api/integrations/syncs` using `{"provider":"smartlead","sync_type":"replies","provider_campaign_id":"..."}`.
- Queue Smartlead stats with `{"provider":"smartlead","sync_type":"stats","provider_campaign_id":"..."}`.
- Queue Sendivo logs with `{"provider":"sendivo","sync_type":"logs"}`.
- Queue Sendivo billing with `{"provider":"sendivo","sync_type":"billing"}`.
- Execute one sync with `POST /api/integrations/syncs/{id}/execute` or let the scheduler drain queued syncs.
- Imported replies/logs become conversations and can trigger hot-reply events, tasks, Slack outbox entries, and memory.

Revenue-loop behavior:

- Create sales opportunities with `POST /api/opportunities`.
- Create call prep with `POST /api/sales-briefs`.
- Create payment handoff records with `POST /api/payment-links`.
- Keep `DIALDESK_CREATE_PAYMENT_LINKS=0` until package payment links are configured.
- Queue CRM writes with `POST /api/crm/sync-jobs`.
- Keep `DIALDESK_SYNC_CRM=0` until the exact CRM write endpoint is configured with `GHL_CONTACT_UPSERT_URL`.
- Stripe webhooks can update opportunity status to `closed_won` when the payload includes `opportunity_id` and a paid/succeeded status.

Agent coordination behavior:

- Send durable messages with `POST /api/agent-messages`.
- Questions, requests, and handoffs sent to agents create response tasks.
- Inspect the full room with `GET /api/agents/coordination`, durable threads with `GET /api/agent-threads`, and one inbox with `GET /api/agents/{id}/inbox`.
- Mark messages read with `POST /api/agent-messages/{id}/read` and reply inside a thread with `POST /api/agent-messages/{id}/reply`.
- Run a heartbeat-backed standup with `POST /api/agents/standup`.
- Slack commands include `standup`, `coordination-room`, `inbox [agent]`, `send-agent <agent> <message>`, and `ask-agent <agent> <question>`.

CEO operating-cycle behavior:

- Scheduler calls `POST /api/ceo/operating-cycle/tick` behavior internally every loop.
- The cycle starts the daily CEO brief, opens an agent standup, records a structured plan, and updates metrics all day.
- `CEO_DAILY_HOUR_UTC` controls the intended morning planning hour.
- `CEO_EOD_HOUR_UTC` controls when the scheduler closes the day with an end-of-day report.
- Inspect cycle state with `GET /api/ceo/operating-cycles`.
- Manually start or close with `POST /api/ceo/operating-cycle/start` and `POST /api/ceo/operating-cycle/close`.
- Scheduler also runs CEO decision reviews so high-risk approvals, incidents, and failed system jobs become visible decisions instead of silent state.
- Inspect the rationale trail with `GET /api/ceo/decisions`; trigger a review with `POST /api/ceo/decisions/evaluate`.

Production operations behavior:

- Run the full deploy proof pack with `BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/business_stack_probe.sh`; it runs the operator doctor, memory proof, Slack proof, and browser proof, then records the run at `GET /api/ops/proof-runs`.
- Run the deploy/operator doctor with `BASE_URL=http://127.0.0.1:8080 bash scripts/ops_doctor.sh`; it reads `APP_TOKEN` from the environment or `.env` and checks dashboard controls, auth, control room actions, readiness, launch gate, proof-pack visibility, memory, browser operators, coordination, and outbox visibility.
- Run health checks with `POST /api/ops/health-checks/run`.
- Inspect the unified operator cockpit with `GET /api/ops/control-room`; run safe actions with `POST /api/ops/control-room/action`.
- Use `/dashboard` as the browser control surface for the same safe actions: health, watchdog, autonomy proof, launch gate, readiness, backups, outbox, browser recovery, CEO tick, runtime pause/resume, and the global kill switch.
- Inspect health history with `GET /api/ops/health-checks`.
- Run readiness checks with `POST /api/ops/readiness/run`.
- Inspect readiness history with `GET /api/ops/readiness`.
- Inspect runtime controls with `GET /api/ops/runtime-controls`; pause/resume scheduler, worker, outreach, browser recovery, syncs, outbox, workflow, scheduled ops, or watchdog with `POST /api/ops/runtime-controls`.
- Inspect subsystem proof-of-life with `GET /api/ops/runtime-heartbeats`.
- Run the autonomous watchdog with `POST /api/ops/watchdog/run`; inspect prior runs at `GET /api/ops/watchdog`.
- Run or inspect runtime self-audits with `POST /api/ops/self-audit/run` and `GET /api/ops/proof-runs?proof_type=runtime_self_audit`; scheduled self-audits are controlled by `DIALDESK_SELF_AUDIT_INTERVAL_SECONDS`.
- Inspect proof-pack commands and durable proof history with `GET /api/ops/proof-pack` and `GET /api/ops/proof-runs`.
- Generate the operator briefing with `POST /api/ops/operator-briefing` or `/dialdesk briefing`; the CEO operating cycle creates one automatically each day and stores it in reports, memory, dashboard, and Slack outbox.
- Watchdog findings dedupe open incidents by source/title so repeated scheduler runs alert without spamming duplicate incident records.
- Run a 24/7 autonomy proof with `POST /api/ops/autonomy-drills/run`; inspect evidence at `GET /api/ops/autonomy-drills`.
- The autonomy drill exercises CEO cycle, memory, Slack outbox, browser queue, agent coordination, health, watchdog, and production readiness without sending live outreach.
- Run the launch gate with `POST /api/ops/launch-gate/run`; inspect blockers, warnings, and run commands at `GET /api/ops/launch-gate`.
- The launch gate checks service manifests, persistent database/backup paths, token protection, memory, Slack, browser operator, backups, health, watchdog, autonomy proof, and safe live-outreach settings.
- Open and resolve incidents through `/api/ops/incidents`.
- Create SQLite backups with `POST /api/ops/backups/create`.
- Inspect backup history with `GET /api/ops/backups`.
- Scheduler runs health checks every `DIALDESK_HEALTH_CHECK_INTERVAL_SECONDS`.
- Scheduler creates backups every `DIALDESK_BACKUP_INTERVAL_SECONDS`.
- Slack commands include `health`, `backup-now`, and `incidents`.

Readiness profiles:

- `local` checks that the local runtime can work, while warning about missing outbound visibility or integrations.
- `production` requires `APP_TOKEN`, a persistent database path, backups, health checks, scheduler config, and the global kill switch off.
- `live_outreach` adds live-send requirements for Sendivo, Smartlead, CRM sync, high-risk approvals, and `DIALDESK_SEND_OUTREACH=1`.

Example:

```bash
curl -s -X POST https://your-app.railway.app/api/ops/readiness/run \
  -H "Authorization: Bearer $APP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"profile":"production","requested_by":"cto"}' | jq .
```

Security and request audit behavior:

- Set `APP_TOKEN` in production.
- Protected paths are `/api/*`, `/messages`, and `/tasks/*`.
- Send `Authorization: Bearer <APP_TOKEN>` or `X-API-Token: <APP_TOKEN>`.
- The fallback dashboard prompts for the token when API calls return `401`.
- Inspect request/auth history with `GET /api/ops/request-log`.

Workflow behavior:

- Client signup automatically launches the DialDesk Day 0-14 onboarding workflow.
- Inspect runs with `GET /api/workflows/runs`.
- Inspect steps with `GET /api/workflows/steps`.
- Due workflow steps queue COO/CMO/CTO/Sales Director/CEO tasks.
- Manually queue due work with `POST /api/workflows/execute-due`.
- Complete or skip a step with `POST /api/workflows/steps/{id}/complete`.

Governance behavior:

- Mission, project, and agent goals are seeded on first boot.
- Inspect goals with `GET /api/governance/goals`.
- Inspect and set budgets with `/api/governance/budgets`.
- Agent heartbeats write to `/api/governance/heartbeats`.
- Operator commands can be sent to `POST /api/slack/command`.
- Supported commands include `status`, `control-room`, `coordination-room`, `inbox [agent]`, `decisions`, `decision-review [force]`, `runtime-controls`, `runtime-heartbeats`, `watchdog [force]`, `autonomy-drill`, `launch-gate`, `browser-operators`, `pause-runtime <component>`, `resume-runtime <component>`, `budget-remaining <agent>`, `pause-agent <agent>`, `resume-agent <agent>`, `approve-action <id>`, `reject-action <id>`, and `kill-switch on|off`.
- Budget exhaustion can auto-pause an agent, and paused agents stop taking new tasks/outreach.

---

## Nginx Reverse Proxy (Recommended for Production)

```nginx
server {
    listen 80;
    server_name swarm.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8080/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}
```

---

## Web Chat

The repo includes a static web chat interface:

```bash
cp examples/web-chat/index.html /var/www/openswarm-chat/
```

Or serve directly:

```bash
cd examples/web-chat
python3 -m http.server 3000
```

Then configure nginx to proxy `/openswarm-chat/` to the static files and `/openswarm/` to the API.

---

## SSL / HTTPS

Use Certbot:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d swarm.yourdomain.com
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `DEFAULT_MODEL` | No | Default model (default: gpt-5.2) |
| `COMPOSIO_API_KEY` | No | For Composio integrations |
| `AGENTS_API_KEY` | No | For custom model provider |
| `APP_TOKEN` | No | API auth token (recommended for production) |

---

## Monitoring

### Logs

```bash
# systemd
sudo journalctl -u dial-desk-swarm -f

# Docker
docker-compose logs -f
```

### Health Check

```bash
curl -s http://localhost:8080/open-swarm/get_metadata
```

---

## Backup

### Conversations

Configure a database persistence layer in `server.py`:

```python
from some_db import load_threads, save_threads

run_fastapi(
    agencies={"dial-desk": create_agency},
    port=8080,
    load_threads_callback=load_threads,
    save_threads_callback=save_threads,
)
```

### Instructions

Back up the entire `agents/` directory and `.env`:

```bash
tar -czf dial-desk-backup-$(date +%Y%m%d).tar.gz agents/ .env shared_instructions.md
```
