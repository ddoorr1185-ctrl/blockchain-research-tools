#!/usr/bin/env bash
# Fast, focused follow-up validation. Run from inside crypto_shutdown_radar/:
#   bash run_source_coverage_validation.sh
#
# Requires a cached universe to already exist (from a prior successful
# `scripts.refresh_universe` or `scripts.live_validation` run, i.e. an
# existing data/radar.db with ~1000 coins already in it). It will NOT
# trigger a fresh 1000-coin CoinGecko detail fetch -- if no cache is found
# it stops immediately and tells you so instead of silently doing that.
#
# Targets <=30 minutes total runtime. Writes SOURCE_COVERAGE_VALIDATION.md
# in this directory.
#
# Optional: export CSR_RSSBRIDGE_BASE_URL="http://host:port" before running
# if you have your own self-hosted RSS-Bridge instance to test as one of
# the Phase D X-fallback candidates. Not required.
set -euo pipefail
cd "$(dirname "$0")"

version_ok() {
  "$1" - <<'PYEOF'
import sys
major, minor = sys.version_info.major, sys.version_info.minor
print(f"{major}.{minor}")
sys.exit(0 if (major, minor) >= (3, 9) else 1)
PYEOF
}

if [ -d ".venv" ] && version_ok ".venv/bin/python" >/dev/null 2>&1; then
  echo "Reusing existing .venv."
else
  echo "Checking for a usable Python interpreter (3.9+ required)..."
  PYTHON_BIN=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && version_ok "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
  if [ -z "$PYTHON_BIN" ]; then
    echo "FAILED: no Python 3.9+ interpreter found. See run_live_validation.sh's error message for install options." >&2
    exit 1
  fi
  echo "Creating virtual environment with $PYTHON_BIN..."
  rm -rf .venv
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f "data/radar.db" ]; then
  echo
  echo "WARNING: data/radar.db not found in this directory. This script will"
  echo "check for a cached universe and, if none is found, stop immediately"
  echo "with instructions rather than re-running the ~hours-long full fetch."
  echo "If your prior successful run's database is elsewhere, copy it to"
  echo "data/radar.db first (or set CSR_DB_PATH to point at it)."
  echo
fi

echo "Starting source-coverage validation (target: <=30 minutes)..."
echo

python -m scripts.source_coverage_validation

echo
echo "Done. See SOURCE_COVERAGE_VALIDATION.md in this directory."
