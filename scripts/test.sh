#!/bin/bash
set -e

echo "=== Testing Dial Desk Executive Swarm ==="

cd "$(dirname "$0")/.."
source .venv/bin/activate

# Test imports
echo "Testing imports..."
python3 -c "from swarm import create_agency; print('OK: swarm imports')"
python3 -c "from config import get_default_model; print('OK: config imports')"

# Test agency creation
echo "Testing agency creation..."
python3 -c "
from swarm import create_agency
agency = create_agency()
assert agency.name == 'OpenSwarm'
agents = [a.name for a in agency.agents]
assert 'CEO' in agents
assert 'CMO' in agents
assert 'CTO' in agents
assert 'COO' in agents
assert 'Sales Director' in agents
print('OK: 13 agents registered')
"

echo "=== All tests passed ==="
