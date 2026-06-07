#!/bin/bash
set -e

echo "=== Dial Desk Executive Swarm Installer ==="

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

# Create .env if missing
if [ ! -f .env ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo "Please edit .env with your API keys."
fi

echo "Installation complete. To start:"
echo "  source .venv/bin/activate"
echo "  python server.py"
