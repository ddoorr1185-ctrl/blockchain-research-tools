#!/usr/bin/env bash
# One-command local live validation. Run from inside crypto_shutdown_radar/:
#   bash run_live_validation.sh
#
# Creates/reuses a venv, installs the existing requirements (no changes to
# monitoring logic), and runs scripts/live_validation.py, which makes real
# network requests and writes LIVE_VALIDATION_REPORT.md in this directory.
#
# Optional: export CSR_TELEGRAM_BOT_TOKEN / CSR_TELEGRAM_CHAT_ID before
# running this script if you want STEP 8 to actually send a Telegram test
# message. If unset, STEP 8 is reported as NOT_CONFIGURED (no fabrication).
set -euo pipefail
cd "$(dirname "$0")"

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=9

version_ok() {
  # $1 = python executable. Prints "MAJOR.MINOR" and exits 0 if >= 3.9, else exits 1.
  "$1" - <<'PYEOF'
import sys
major, minor = sys.version_info.major, sys.version_info.minor
print(f"{major}.{minor}")
sys.exit(0 if (major, minor) >= (3, 9) else 1)
PYEOF
}

echo "Checking for a usable Python interpreter (3.9+ required; a newer one is preferred when available)..."
PYTHON_BIN=""
PYTHON_VERSION=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if v=$(version_ok "$candidate" 2>/dev/null); then
      PYTHON_BIN="$candidate"
      PYTHON_VERSION="$v"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  cat >&2 <<'EOF'

FAILED: no Python 3.9+ interpreter was found on this machine.

This project requires Python 3.9 or newer. Checked for: python3.13,
python3.12, python3.11, python3.10, python3.9, python3 -- none of those
that exist on this machine report version >= 3.9.

Fix this by installing a modern Python, then re-run this script. On macOS,
the easiest path is Homebrew:
    brew install python@3.11
Or download an installer directly from https://www.python.org/downloads/

No files were changed and no venv was created.
EOF
  exit 1
fi

echo "Using $PYTHON_BIN (Python $PYTHON_VERSION)."

# If a venv already exists but was built with an interpreter that no longer
# meets the version requirement (e.g. left over from a prior failed run on
# the system's old default python3), rebuild it rather than silently reusing
# a broken environment.
if [ -d ".venv" ]; then
  if ! version_ok ".venv/bin/python" >/dev/null 2>&1; then
    echo "Existing .venv does not meet the Python 3.9+ requirement -- recreating it."
    rm -rf .venv
  fi
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt httpx

echo
echo "Starting live validation. This takes roughly 40-90 minutes, mostly"
echo "spent on the rate-limited CoinGecko universe detail fetch. Progress"
echo "is printed below as each step completes."
echo

python -m scripts.live_validation

echo
echo "Done. See LIVE_VALIDATION_REPORT.md in this directory."
