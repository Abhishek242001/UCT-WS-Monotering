#!/usr/bin/env bash
# Starts the backend on port 8001. Run ./setup.sh first.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source .venv/bin/activate
[ -f .env ] && set -a && source .env && set +a
cd backend
echo "Starting backend on http://localhost:8001 (docs at /docs)"
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
