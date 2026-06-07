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
python server.py
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
sudo cp systemd/dial-desk-swarm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dial-desk-swarm
sudo systemctl start dial-desk-swarm

# 5. Check status
sudo systemctl status dial-desk-swarm
curl -s http://127.0.0.1:8080/open-swarm/get_metadata
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
