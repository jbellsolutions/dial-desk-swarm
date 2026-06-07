# Deployment Guide — Dial Desk

## Railway Deploy (Production)

### Prerequisites
- Railway CLI installed: `npm install -g @railway/cli`
- Logged in: `railway login`
- Git repo initialized with commits

### Step-by-Step

```bash
# 1. Link to Railway project
railway link
# Select: jbellsolutions's Projects → dial-desk-api → production → dial-desk-api

# 2. Ensure Docker builder is used (not Nixpacks)
# Check railway.json exists:
cat deploy/railway/railway.json
# Should show: "builder": "DOCKERFILE"

# 3. Remove nixpacks.toml if present (CRITICAL)
rm -f nixpacks.toml

# 4. Deploy
./scripts/deploy.sh
# Or manually:
git add -A && git commit -m "deploy"
railway up --detach
```

### What Gets Deployed
- **Docker image** built from `Dockerfile`
- **Container** runs `uvicorn src.coordinator.main:app --host 0.0.0.0 --port $PORT --http h11`
- **Static files** served from `/app/web/` when present, with coordinator fallbacks for landing/dashboard pages
- **Health check** on `/health` every 30 seconds
- **Auto-restart** on failure (max 5 retries)

### Environment Variables
Set in Railway dashboard or via CLI:
```bash
railway variable set SMARTLEAD_API_KEY=your_key
railway variable set SENDIVO_API_KEY=your_key
railway variable set GHL_API_KEY=your_key
railway variable set TELEGRAM_BOT_TOKEN=your_token
railway variable set STRIPE_SECRET_KEY=your_key
railway variable set DIALDESK_DB=/app/data/dialdesk.sqlite3
railway variable set APP_TOKEN=$(openssl rand -hex 32)
railway variable set DIALDESK_BACKUP_DIR=/app/data/backups
railway variable set DIALDESK_HEALTH_CHECK_INTERVAL_SECONDS=300
railway variable set DIALDESK_BACKUP_INTERVAL_SECONDS=86400
railway variable set SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
railway variable set NOTION_API_KEY=secret_...
railway variable set NOTION_DATABASE_ID=your_notion_database_id
railway variable set DIALDESK_SEND_OUTBOX=0
railway variable set SENDIVO_BEARER_AUTH=your_sendivo_bearer
railway variable set SMARTLEAD_API_KEY=your_smartlead_key
railway variable set DIALDESK_SEND_OUTREACH=0
railway variable set STRIPE_PAYMENT_LINK_HUMAN_SDR=https://buy.stripe.com/...
railway variable set STRIPE_PAYMENT_LINK_AI_CALLER=https://buy.stripe.com/...
railway variable set GHL_CONTACT_UPSERT_URL=https://...
railway variable set DIALDESK_CREATE_PAYMENT_LINKS=0
railway variable set DIALDESK_SYNC_CRM=0
railway variable set DIALDESK_DEFAULT_AGENT_MONTHLY_BUDGET=500
```

### Domain Setup
```bash
# Get Railway domain
railway domain
# Returns: your-app.railway.app

# Or add custom domain
railway domain add your-domain.com
# Then add CNAME: your-domain.com → your-app.railway.app
```

### Verify Deploy
```bash
curl -s https://your-app.railway.app/health
curl -s https://your-app.railway.app/api/status
curl -s https://your-app.railway.app/api/ops/overview
curl -s https://your-app.railway.app/api/ops/control-room
curl -s https://your-app.railway.app/api/activity
curl -s https://your-app.railway.app/api/ceo/decisions
curl -s https://your-app.railway.app/api/ops/runtime-controls
curl -s https://your-app.railway.app/api/ops/runtime-heartbeats
curl -s -X POST https://your-app.railway.app/api/ops/launch-gate/run \
  -H 'Content-Type: application/json' \
  -d '{"profile":"railway","requested_by":"cto"}'
curl -s -X POST https://your-app.railway.app/api/ops/watchdog/run \
  -H 'Content-Type: application/json' \
  -d '{"source":"deploy_smoke","force":true}'
curl -s -X POST https://your-app.railway.app/api/ops/autonomy-drills/run \
  -H 'Content-Type: application/json' \
  -d '{"requested_by":"ceo"}'
curl -s https://your-app.railway.app/api/agents/coordination
curl -s https://your-app.railway.app/agentCard
```

---

## Docker Local Deploy

```bash
# Build
docker build -t dial-desk .

# Run
docker run -p 8000:8000 \
  -e SMARTLEAD_API_KEY=your_key \
  -e SENDIVO_API_KEY=your_key \
  -e PORT=8000 \
  dial-desk

# Test
curl http://localhost:8000/health
```

---

## Troubleshooting

### "404 Not Found" on /agentCard
**Cause:** Railway using Nixpacks instead of Docker
**Fix:**
```bash
rm nixpacks.toml
git add -A && git commit -m "remove nixpacks"
railway up --detach
```

### "405 Method Not Allowed"
**Cause:** Railway HTTP/2 proxy incompatibility
**Fix:** Already in Dockerfile: `--http h11` flag

### "Port already in use"
**Cause:** Hardcoded port 8000, Railway provides $PORT
**Fix:** Use `$PORT` env var in start command (already in railway.json)

### Static files 404
**Cause:** Optional `web/` assets are not present.
**Fix:** The coordinator serves fallback landing/dashboard pages. Add `web/landing-page/index.html` and `web/dashboard/index.html` only when custom static assets are ready.

### Deploy hangs / builds forever
**Cause:** Large repo, many files
**Fix:** Add `.dockerignore`:
```
.git
__pycache__
*.pyc
.env
.DS_Store
node_modules/
```

### Database lost on restart
**Cause:** `DIALDESK_DB` points at ephemeral storage.
**Fix:** Set `DIALDESK_DB=/app/data/dialdesk.sqlite3` and mount `/app/data` as a persistent volume in Railway.

---

## CI/CD (GitHub Actions)

On every push to `main`:
```yaml
# .github/workflows/deploy.yml
name: Deploy to Railway
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm install -g @railway/cli
      - run: railway up --detach
        env:
          RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN }}
```

Add `RAILWAY_TOKEN` to GitHub Secrets (get from Railway dashboard).

---

## Monitoring

### Railway Dashboard
- URL: https://railway.com/project/your-project-id
- View: Logs, metrics, deploy history, environment variables

### Health Check
```bash
# Automated
curl https://your-app.railway.app/health

# Full status
curl https://your-app.railway.app/api/status | jq .
curl https://your-app.railway.app/api/ops/overview | jq .
curl https://your-app.railway.app/api/ops/control-room | jq .
curl https://your-app.railway.app/api/activity | jq .
curl https://your-app.railway.app/api/ceo/decisions | jq .
curl https://your-app.railway.app/api/ops/runtime-controls | jq .
curl https://your-app.railway.app/api/ops/runtime-heartbeats | jq .
curl https://your-app.railway.app/api/ops/watchdog | jq .
curl https://your-app.railway.app/api/ops/autonomy-drills | jq .
curl -X POST https://your-app.railway.app/api/ceo/decisions/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"source":"operator","force":true}' | jq .
curl https://your-app.railway.app/api/agents/coordination | jq .
curl https://your-app.railway.app/api/agent-threads | jq .
curl https://your-app.railway.app/api/outbox | jq .
curl https://your-app.railway.app/api/integrations/config | jq .
curl https://your-app.railway.app/api/integrations/syncs | jq .
curl https://your-app.railway.app/api/opportunities | jq .
curl https://your-app.railway.app/api/payment-links | jq .
curl https://your-app.railway.app/api/crm/sync-jobs | jq .
curl https://your-app.railway.app/api/agent-messages | jq .
curl https://your-app.railway.app/api/ops/health-checks | jq .
curl https://your-app.railway.app/api/ops/incidents | jq .
curl https://your-app.railway.app/api/ops/backups | jq .
curl https://your-app.railway.app/api/ops/request-log | jq .
curl https://your-app.railway.app/api/workflows/runs | jq .
curl https://your-app.railway.app/api/workflows/steps | jq .
curl https://your-app.railway.app/api/governance/goals | jq .
curl https://your-app.railway.app/api/governance/budgets | jq .
```

### Memory And Slack

- SQLite is the durable source of truth.
- `OBSIDIAN_VAULT_PATH` mirrors memory into markdown files when the path is available on the host.
- `NOTION_API_KEY` plus `NOTION_DATABASE_ID` queues Notion page syncs.
- Inspect memory sync health with `GET /api/memory/health`.
- Prove the full SQLite-to-Obsidian-to-Notion-queue path with `BASE_URL=http://127.0.0.1:8080 bash scripts/memory_stack_probe.sh`.
- Retry one Notion memory sync with `POST /api/memory/{id}/sync-notion`.
- Batch retry Notion sync with `POST /api/memory/resync-notion`.
- Notion outbox delivery updates memory rows to `synced` or `failed`.
- `SLACK_WEBHOOK_URL` queues Slack alerts.
- `DIALDESK_SEND_OUTBOX=0` leaves Slack/Notion messages queued for review; set it to `1` to let the runtime deliver queued outbox items.
- Configure a Slack slash command at `https://your-app.railway.app/slack/command`.
- Set `SLACK_SIGNING_SECRET` so DialDesk verifies `X-Slack-Signature`.
- Prove alert queueing, JSON command handling, and slash-command ingress with `BASE_URL=http://127.0.0.1:8080 bash scripts/slack_stack_probe.sh`.
- Slash commands use `/dialdesk <command>`, for example `status`, `control-room`, `coordination-room`, `inbox justin`, `decisions`, `decision-review force`, `runtime-controls`, `runtime-heartbeats`, `watchdog force`, `autonomy-drill`, `launch-gate`, `browser-operators`, `pause-runtime outreach`, `resume-runtime outreach`, `memory-health`, `readiness production`, `browser-jobs`, `outbox`, `operating-cycle`, `standup`, and `ask-agent sales_director <question>`.

### Browser Automation

Browser work is a durable queue:

```bash
curl -s https://your-app.railway.app/api/browser/jobs | jq .
curl -s https://your-app.railway.app/api/browser/operators | jq .
```

Create jobs with `POST /api/browser/jobs`, then have the browser/super-browser operator complete them through `POST /api/browser/jobs/{id}/complete`. Completed results are written to memory.

For a production browser worker:

- Run `python -m src.browser_operator` as a separate long-running process.
- Use `DIALDESK_BROWSER_MODE=auto` to prefer Playwright and fall back to HTTP inspection.
- Install optional rendered-browser support with `pip install playwright && playwright install chromium`.
- Install `systemd/dial-desk-browser-operator.service` beside the API service on a VPS.
- On a VPS, install both the API and browser operator services with `APP_DIR=/opt/dial-desk-swarm SERVICE_USER=$(id -un) bash scripts/vps_bootstrap.sh`.
- Run the full deploy proof pack with `BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/business_stack_probe.sh`; it runs the operator doctor, memory proof, Slack proof, and browser proof, then records the run at `GET /api/ops/proof-runs`.
- Verify any local, Railway, or VPS runtime with `BASE_URL=http://127.0.0.1:8080 bash scripts/ops_doctor.sh`; it reads `APP_TOKEN` from `.env` and checks dashboard controls, auth, control room actions, readiness, launch gate, proof-pack visibility, memory, browser operators, coordination, and outbox visibility.
- Run or inspect runtime self-audits with `POST /api/ops/self-audit/run` and `GET /api/ops/proof-runs?proof_type=runtime_self_audit`; scheduled self-audits are controlled by `DIALDESK_SELF_AUDIT_INTERVAL_SECONDS`.
- Generate the operator briefing with `POST /api/ops/operator-briefing` or `/dialdesk briefing`; the CEO operating cycle creates one automatically each day and stores it in reports, memory, dashboard, and Slack outbox.
- Prove the full browser automation path with `BASE_URL=http://127.0.0.1:8080 PROBE_URL=http://127.0.0.1:8080/health bash scripts/browser_stack_probe.sh`; it queues a job, runs the browser operator once, verifies operator proof-of-life, and confirms memory writeback.
- Or run Docker with `docker compose --profile browser up -d`.
- The operator reports idle/working proof-of-life with `POST /api/browser/operators/heartbeat`.
- Inspect live/stale browser operators with `GET /api/browser/operators`.
- Claim work with `POST /api/browser/jobs/claim`.
- Send progress with `POST /api/browser/jobs/{id}/heartbeat`.
- Complete work with `POST /api/browser/jobs/{id}/complete`.
- Fail temporary browser errors with `POST /api/browser/jobs/{id}/fail`.
- Let the scheduler recover expired leases, or run `POST /api/browser/jobs/recover-expired`.

### Outreach Execution

SMS/email outreach is durable and guarded:

```bash
curl -s https://your-app.railway.app/api/outreach/jobs | jq .
```

- Queue sends with `POST /api/outreach/jobs`.
- Execute one send with `POST /api/outreach/jobs/{id}/execute`.
- Execute the queue with `POST /api/outreach/execute-next`.
- Keep `DIALDESK_SEND_OUTREACH=0` until production sending is approved; this records dry-run outbound conversations.
- Set `DIALDESK_SEND_OUTREACH=1` only when Sendivo/Smartlead credentials, suppression rules, approvals, quiet hours, and kill switches are verified.

### Provider Sync

Provider sync closes the reply/reporting loop:

```bash
curl -s https://your-app.railway.app/api/integrations/config | jq .
curl -s https://your-app.railway.app/api/integrations/syncs | jq .
```

- Smartlead replies: `POST /api/integrations/syncs` with `provider=smartlead`, `sync_type=replies`, and `provider_campaign_id`.
- Smartlead stats: `provider=smartlead`, `sync_type=stats`.
- Sendivo logs: `provider=sendivo`, `sync_type=logs`.
- Sendivo billing: `provider=sendivo`, `sync_type=billing`.
- Execute one sync with `POST /api/integrations/syncs/{id}/execute`.
- Execute queued syncs with `POST /api/integrations/sync-next`.
- Imported replies/logs become conversations, memory, events, tasks, and dashboard state.

### Revenue Loop

The sales pipeline is durable and guarded:

```bash
curl -s https://your-app.railway.app/api/opportunities | jq .
curl -s https://your-app.railway.app/api/payment-links | jq .
curl -s https://your-app.railway.app/api/crm/sync-jobs | jq .
```

- Create opportunities with `POST /api/opportunities`.
- Create sales-call briefs with `POST /api/sales-briefs`.
- Create payment handoff records with `POST /api/payment-links`.
- Keep `DIALDESK_CREATE_PAYMENT_LINKS=0` until package payment links are configured.
- Queue CRM writes with `POST /api/crm/sync-jobs`.
- Keep `DIALDESK_SYNC_CRM=0` until `GHL_CONTACT_UPSERT_URL` and CRM auth are configured.
- Stripe webhooks can mark opportunities `closed_won` when the payload includes `opportunity_id` and a paid/succeeded status.

### Agent Coordination

Agents coordinate through durable messages:

```bash
curl -s https://your-app.railway.app/api/agent-messages | jq .
```

- Send messages with `POST /api/agent-messages`.
- Requests/questions/handoffs create response tasks for the target agent.
- Inspect the full room with `GET /api/agents/coordination`, durable threads with `GET /api/agent-threads`, and one inbox with `GET /api/agents/{id}/inbox`.
- Mark messages read with `POST /api/agent-messages/{id}/read` and reply inside a thread with `POST /api/agent-messages/{id}/reply`.
- Run a full team standup with `POST /api/agents/standup`.
- Slack command support includes `standup`, `coordination-room`, `inbox [agent]`, `send-agent <agent> <message>`, and `ask-agent <agent> <question>`.

### Production Operations

DialDesk keeps an inspectable operations history:

```bash
curl -s https://your-app.railway.app/api/ceo/operating-cycles | jq .
curl -s https://your-app.railway.app/api/ops/health-checks | jq .
curl -s https://your-app.railway.app/api/ops/readiness | jq .
curl -s https://your-app.railway.app/api/ops/incidents | jq .
curl -s https://your-app.railway.app/api/ops/backups | jq .
```

- Scheduler ticks the CEO operating cycle every loop, starts the daily brief/standup/plan, and closes the day after `CEO_EOD_HOUR_UTC`.
- Manually start, tick, or close with `/api/ceo/operating-cycle/start`, `/api/ceo/operating-cycle/tick`, and `/api/ceo/operating-cycle/close`.
- Scheduler also runs CEO decision reviews; inspect rationale with `GET /api/ceo/decisions` and force a review with `POST /api/ceo/decisions/evaluate`.
- The operator control room is available at `GET /api/ops/control-room`; safe actions run through `POST /api/ops/control-room/action`.
- The fallback dashboard at `/dashboard` exposes those same safe controls in the browser, including watchdog, autonomy proof, launch gate, readiness, backup, runtime pause/resume, browser recovery, outbox, CEO tick, and kill switch controls.
- The ops doctor uses that same control room path so deploy verification matches the dashboard and Slack operator model.
- Runtime controls are available at `GET /api/ops/runtime-controls`; pause/resume components with `POST /api/ops/runtime-controls`.
- Runtime proof-of-life is available at `GET /api/ops/runtime-heartbeats`.
- The autonomous watchdog runs from the scheduler or `POST /api/ops/watchdog/run`, tracks findings at `GET /api/ops/watchdog`, and opens deduped incidents for stale heartbeats, failed queues, or missing browser operators.
- The autonomy proof drill runs from `POST /api/ops/autonomy-drills/run`, tracks evidence at `GET /api/ops/autonomy-drills`, and verifies CEO cycle, memory, Slack, browser queue, agent coordination, watchdog, and readiness without live outreach.
- The launch gate runs from `POST /api/ops/launch-gate/run`, tracks blockers and commands at `GET /api/ops/launch-gate`, and verifies deploy manifests, persistence, token protection, memory, Slack, browser operator, backups, health, watchdog, autonomy proof, and safe live-outreach settings.
- Run checks with `POST /api/ops/health-checks/run`.
- Run readiness with `POST /api/ops/readiness/run` using `local`, `production`, or `live_outreach`.
- Production readiness requires a protected control plane, persistent database path, backups, health checks, scheduler config, and kill switch off.
- Live-outreach readiness also checks Sendivo, Smartlead, CRM sync, high-risk approvals, and `DIALDESK_SEND_OUTREACH=1`.
- Open incidents with `POST /api/ops/incidents`.
- Resolve incidents with `POST /api/ops/incidents/{id}/resolve`.
- Create SQLite backups with `POST /api/ops/backups/create`.
- Scheduler runs checks on `DIALDESK_HEALTH_CHECK_INTERVAL_SECONDS`.
- Scheduler writes backups to `DIALDESK_BACKUP_DIR` on `DIALDESK_BACKUP_INTERVAL_SECONDS`.
- Slack command support includes `health`, `backup-now`, and `incidents`.

### Security And Audit

Set `APP_TOKEN` before exposing the control plane:

```bash
curl -H "Authorization: Bearer $APP_TOKEN" https://your-app.railway.app/api/status | jq .
curl -H "Authorization: Bearer $APP_TOKEN" https://your-app.railway.app/api/ops/request-log | jq .
```

- Protected paths are `/api/*`, `/messages`, and `/tasks/*`.
- Public paths include `/`, `/dashboard`, `/health`, and `/agentCard`.
- The fallback dashboard prompts for the token when the API returns `401`.
- Every non-static request is written to `request_log` with status, actor, path, and duration.

### Workflows

Client onboarding runs as durable workflow state:

```bash
curl -s https://your-app.railway.app/api/workflows/runs | jq .
curl -s https://your-app.railway.app/api/workflows/steps | jq .
```

- Client signup launches the DialDesk Day 0-14 onboarding workflow.
- Day 0 steps queue immediately; later steps become due by day offset.
- Queue due steps with `POST /api/workflows/execute-due`.
- Complete or skip a step with `POST /api/workflows/steps/{id}/complete`.
- Workflow progress appears in `/api/clients/{id}/dashboard` and `/api/ops/overview`.

### Governance Controls

DialDesk carries lightweight governance without adding a separate platform:

```bash
curl -s https://your-app.railway.app/api/governance/goals | jq .
curl -s https://your-app.railway.app/api/governance/budgets | jq .
curl -s https://your-app.railway.app/api/governance/heartbeats | jq .
```

- Goals follow mission -> project -> agent -> task.
- Budgets are per agent and can auto-pause agents when spent.
- Heartbeats show that each operator is alive.
- `POST /api/slack/command` supports `status`, `budget-remaining <agent>`, `pause-agent <agent>`, `resume-agent <agent>`, `approve-action <id>`, `reject-action <id>`, and `kill-switch on|off`.

### Alerts
Set up Railway alerts for:
- CPU >80%
- Memory >80%
- Disk >80%
- Deploy failures

---

## Rollback

```bash
# List deploys
railway deploys

# Rollback to previous
railway deploys rollback

# Or via dashboard: Services → Deploys → Revert
```
