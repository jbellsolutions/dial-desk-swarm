#!/bin/bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dial-desk-swarm}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"
PORT="${PORT:-8080}"

if ! command -v sudo >/dev/null 2>&1; then
  echo "sudo is required for service installation."
  exit 1
fi

echo "=== DialDesk VPS Bootstrap ==="
echo "App directory: $APP_DIR"
echo "Service user: $SERVICE_USER:$SERVICE_GROUP"
echo "Port: $PORT"

mkdir -p data/backups data/browser-artifacts uploads

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit it before live outreach."
fi

bash scripts/install.sh

sudo mkdir -p "$APP_DIR"
sudo rsync -a --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  ./ "$APP_DIR/"
sudo mkdir -p "$APP_DIR/data/backups" "$APP_DIR/data/browser-artifacts" "$APP_DIR/uploads"
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"

tmp_api="$(mktemp)"
tmp_browser="$(mktemp)"
sed \
  -e "s|^User=.*|User=$SERVICE_USER|" \
  -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
  -e "s|/opt/dial-desk-swarm|$APP_DIR|g" \
  -e "s|--port 8080|--port $PORT|g" \
  systemd/dial-desk-swarm.service > "$tmp_api"
sed \
  -e "s|^User=.*|User=$SERVICE_USER|" \
  -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
  -e "s|/opt/dial-desk-swarm|$APP_DIR|g" \
  -e "s|http://127.0.0.1:8080|http://127.0.0.1:$PORT|g" \
  systemd/dial-desk-browser-operator.service > "$tmp_browser"

sudo install -m 0644 "$tmp_api" /etc/systemd/system/dial-desk-swarm.service
sudo install -m 0644 "$tmp_browser" /etc/systemd/system/dial-desk-browser-operator.service
rm -f "$tmp_api" "$tmp_browser"

sudo systemctl daemon-reload
sudo systemctl enable --now dial-desk-swarm
sudo systemctl enable --now dial-desk-browser-operator

echo "Waiting for API health..."
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null; then
    break
  fi
  sleep 1
done

curl -fsS "http://127.0.0.1:$PORT/health"
echo
echo "Running deploy doctor..."
(
  cd "$APP_DIR"
  BASE_URL="http://127.0.0.1:$PORT" \
    DIALDESK_DOCTOR_PROFILE=production \
    DIALDESK_LAUNCH_PROFILE=vps \
    PROBE_URL="http://127.0.0.1:$PORT/health" \
    bash scripts/business_stack_probe.sh
)
echo "DialDesk VPS bootstrap complete."
