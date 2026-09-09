#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup.sh -- one-command setup for the whole project.
#
# Usage:
#   ./setup.sh                 install everything + run both test suites
#   ./setup.sh --no-tests       install only, skip running tests
#   ./setup.sh --with-face-recognition   also install real InsightFace
#                                        (downloads a large model on first
#                                        use -- see backend/app/vision/
#                                        face_embedder.py)
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_TESTS=1
WITH_FACE_RECOGNITION=0
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    --with-face-recognition) WITH_FACE_RECOGNITION=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

echo "== UCT Workstation Monitor: setup =="

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found. Install Python 3.10+ and re-run." >&2
  exit 1
fi
echo "Using $($PYTHON_BIN --version)"

VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "Installing backend dependencies (this includes ultralytics/torch -- can take a few minutes)..."
pip install --upgrade pip -q
pip install -r backend/requirements.txt -q

if [ "$WITH_FACE_RECOGNITION" -eq 1 ]; then
  echo "Installing real face-recognition dependencies (insightface + onnxruntime)..."
  echo "(This combination was verified working during this project's own build: real faces"
  echo " correctly discriminated with cosine similarity ~1.0 for the same person and ~0.0"
  echo " for different people. Model download is ~124MB, one-time, needs internet access.)"
  pip install -r backend/requirements-face-recognition.txt -q
  echo "Real InsightFace will activate automatically on next backend start."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example -- edit it to change default admin credentials."
fi

if [ "$RUN_TESTS" -eq 1 ]; then
  echo ""
  echo "== Running design-acceptance suite (algorithm + API-contract tests) =="
  (cd tests/design-acceptance-suite && pip install -r requirements.txt -q && pytest -q)

  echo ""
  echo "== Running real-app smoke tests (against the actual backend) =="
  (cd tests/real-app-smoke && pytest -q)
fi

echo ""
echo "== Setup complete =="
echo "Next steps:"
echo "  1. Start the backend:   ./run_backend.sh"
echo "  2. Start the frontend:  ./run_frontend.sh   (in a second terminal)"
echo "  3. Open http://localhost:8002 -- default login is printed by the backend on first start."
