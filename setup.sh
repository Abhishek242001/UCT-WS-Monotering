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

# --- venv creation, with a fallback for managed platforms (e.g. Lightning.ai
# Studios) that allow only one environment per Studio and block
# `python -m venv` / `conda create` on top of it. If venv creation fails,
# install directly into whatever Python environment is already active
# instead of aborting. ---
VENV_DIR="$SCRIPT_DIR/.venv"
USE_VENV=1

if [ -d "$VENV_DIR" ]; then
  echo "Using existing virtual environment at $VENV_DIR"
elif "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/tmp/uct_venv_error.log; then
  echo "Created virtual environment at $VENV_DIR"
else
  echo "Could not create a virtual environment (expected on some managed platforms,"
  echo "e.g. Lightning.ai Studios, which allow only one environment per Studio):"
  sed 's/^/    /' /tmp/uct_venv_error.log
  echo "Falling back to installing directly into the current Python environment."
  rm -rf "$VENV_DIR"
  USE_VENV=0
fi

if [ "$USE_VENV" -eq 1 ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
fi

# --- pip install helper: falls back to --break-system-packages if the
# environment is PEP 668 "externally managed" (common on bare Debian/Ubuntu
# Python when not using a venv). Conda environments (Lightning.ai's default)
# don't normally need this, but it's a safe, harmless fallback either way. ---
pip_install() {
  if pip install "$@" -q 2>/tmp/uct_pip_error.log; then
    return 0
  fi
  if grep -qi "externally-managed-environment" /tmp/uct_pip_error.log; then
    pip install "$@" --break-system-packages -q
  else
    cat /tmp/uct_pip_error.log >&2
    return 1
  fi
}

echo "Installing backend dependencies (this includes ultralytics/torch -- can take a few minutes)..."
pip_install --upgrade pip || echo "(Could not upgrade pip in place -- harmless, continuing with the existing version.)"
pip_install -r backend/requirements.txt

if [ "$WITH_FACE_RECOGNITION" -eq 1 ]; then
  echo "Installing real face-recognition dependencies (insightface + onnxruntime)..."
  echo "(This combination was verified working during this project's own build: real faces"
  echo " correctly discriminated with cosine similarity ~1.0 for the same person and ~0.0"
  echo " for different people. Model download is ~124MB, one-time, needs internet access.)"
  pip_install -r backend/requirements-face-recognition.txt
  echo "Real InsightFace will activate automatically on next backend start."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example -- edit it to change default admin credentials."
fi

if [ "$RUN_TESTS" -eq 1 ]; then
  echo ""
  echo "== Running design-acceptance suite (algorithm + API-contract tests) =="
  (cd tests/design-acceptance-suite && pip_install -r requirements.txt && pytest -q)

  echo ""
  echo "== Running real-app smoke tests (against the actual backend) =="
  (cd tests/real-app-smoke && pytest -q)
fi

echo ""
echo "== Setup complete =="
if [ "$USE_VENV" -eq 0 ]; then
  echo "(Installed directly into the current environment -- no .venv was created.)"
fi
echo "Next steps:"
echo "  1. Start the backend:   ./run_backend.sh"
echo "  2. Start the frontend:  ./run_frontend.sh   (in a second terminal)"
echo "  3. Open http://localhost:8002 -- default login is printed by the backend on first start."
