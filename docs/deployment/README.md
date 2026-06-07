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
- **Docker image** built from `deploy/docker/Dockerfile`
- **Container** runs `uvicorn coordinator.main:app --host 0.0.0.0 --port $PORT --http h11`
- **Static files** served from `/app/landing-page/` and `/app/web/`
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
curl -s https://your-app.railway.app/agentCard
```

---

## Docker Local Deploy

```bash
# Build
docker build -f deploy/docker/Dockerfile -t dial-desk .

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
**Cause:** Dockerfile not copying landing-page/
**Fix:** Verify `COPY landing-page/ /app/landing-page/` in Dockerfile

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
**Cause:** SQLite at `/tmp/dialdesk.sqlite3` (ephemeral)
**Fix:** Change `DB_PATH` to persistent volume:
```python
DB_PATH = os.getenv("DIALDESK_DB", "/app/data/dialdesk.sqlite3")
```
Then mount volume in Railway dashboard.

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
```

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
