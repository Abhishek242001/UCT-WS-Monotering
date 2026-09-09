#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup.sh
#
# Provisions a consistent environment for running the pytest suite for the
# Employee Identity-Aware Workstation Monitoring & Attendance System.
#
# This sets up the MOCK/CONTRACT test layer only (pure business logic +
# in-memory FastAPI mock + SQLite constraint checks). It does NOT install
# the real computer-vision stack (insightface, onnxruntime, ultralytics) --
# that belongs to the production service itself and is only needed once the
# skipped hardware/integration tests are being implemented for real.
#
# Usage:
#   ./setup.sh              # install deps + run the full suite once
#   ./setup.sh --no-run      # install deps only, don't run tests
#   ./setup.sh --coverage    # install deps + run with coverage report
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_TESTS=1
WITH_COVERAGE=0

for arg in "$@"; do
  case "$arg" in
    --no-run) RUN_TESTS=0 ;;
    --coverage) WITH_COVERAGE=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

echo "== Employee Identity-Aware Workstation Monitoring: test suite setup =="

# --- 1. Python version check -------------------------------------------------
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: $PYTHON_BIN not found. Install Python 3.10+ and re-run." >&2
  exit 1
fi
PY_VERSION="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Using Python $PY_VERSION ($PYTHON_BIN)"

# --- 2. Virtual environment ---------------------------------------------------
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# --- 3. Dependencies -----------------------------------------------------------
echo "Installing test-suite dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

# --- 4. Environment variables --------------------------------------------------
# These mirror the environment-aware configuration discussed for the real
# service (local vs. Lightning.ai cloud); the mock test layer does not need
# real values, but downstream integration tests that exercise the actual
# service should set these appropriately before running.
export APP_ENV="${APP_ENV:-test}"
export BACKEND_PORT="${BACKEND_PORT:-8001}"
export FRONTEND_PORT="${FRONTEND_PORT:-8002}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///:memory:}"
export SESSION_COOKIE_SECURE="${SESSION_COOKIE_SECURE:-false}"   # true when served over HTTPS (cloud)
export LOCKOUT_THRESHOLD="${LOCKOUT_THRESHOLD:-5}"
export LOCKOUT_WINDOW_SECONDS="${LOCKOUT_WINDOW_SECONDS:-300}"
export ACCEPTANCE_TARGET_FPS="${ACCEPTANCE_TARGET_FPS:-20}"

echo "APP_ENV=$APP_ENV"
echo "DATABASE_URL=$DATABASE_URL"
echo "ACCEPTANCE_TARGET_FPS=$ACCEPTANCE_TARGET_FPS"

# --- 5. Run the suite -----------------------------------------------------------
if [ "$RUN_TESTS" -eq 1 ]; then
  if [ "$WITH_COVERAGE" -eq 1 ]; then
    echo "Running pytest with coverage..."
    pytest --cov=src --cov-report=term-missing --cov-report=html -v
    echo "HTML coverage report written to htmlcov/index.html"
  else
    echo "Running pytest..."
    pytest -v
  fi
else
  echo "Skipping test run (--no-run passed). Environment is ready; activate with:"
  echo "  source $VENV_DIR/bin/activate"
fi

echo "== Done =="
