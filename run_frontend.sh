#!/usr/bin/env bash
# Serves the static frontend on port 8002. Run this in a second terminal
# alongside run_backend.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/frontend"
echo "Serving frontend on http://localhost:8002"
python3 -m http.server 8002
