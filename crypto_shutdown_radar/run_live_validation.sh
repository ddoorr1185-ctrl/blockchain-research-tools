#!/usr/bin/env bash
# One-command local live validation. Run from inside crypto_shutdown_radar/:
#   bash run_live_validation.sh
#
# Creates/reuses a venv, installs the existing requirements (no changes),
# and runs scripts/live_validation.py, which makes real network requests
# and writes LIVE_VALIDATION_REPORT.md in this directory.
#
# Optional: export CSR_TELEGRAM_BOT_TOKEN / CSR_TELEGRAM_CHAT_ID before
# running this script if you want STEP 8 to actually send a Telegram test
# message. If unset, STEP 8 is reported as NOT_CONFIGURED (no fabrication).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt httpx

echo "Starting live validation. This takes roughly 40-90 minutes, mostly"
echo "spent on the rate-limited CoinGecko universe detail fetch. Progress"
echo "is printed below as each step completes."
echo

python -m scripts.live_validation

echo
echo "Done. See LIVE_VALIDATION_REPORT.md in this directory."
