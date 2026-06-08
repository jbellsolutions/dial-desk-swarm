#!/bin/bash
set -e

echo "=== DialDesk Autonomous Business Runtime Installer ==="

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required but not installed."
    exit 1
fi

# Create venv
echo "Creating virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

# Install deps
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Creating persistent runtime directories..."
mkdir -p data/backups data/browser-artifacts uploads

# Create .env if missing
if [ ! -f .env ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo "Please edit .env with your API keys."
fi

echo "Installation complete. To start:"
echo "  source .venv/bin/activate"
echo "  python -m uvicorn src.coordinator.main:app --host 0.0.0.0 --port 8080 --http h11"
echo ""
echo "For a VPS 24/7 install of both services:"
echo "  APP_DIR=/opt/dial-desk-swarm SERVICE_USER=\$(id -un) bash scripts/vps_bootstrap.sh"
echo ""
echo "To run the browser/super-browser operator in another shell:"
echo "  python -m src.browser_operator"
echo ""
echo "Optional rendered browser support:"
echo "  pip install playwright && playwright install chromium"
