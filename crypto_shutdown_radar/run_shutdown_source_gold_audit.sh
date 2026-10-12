#!/usr/bin/env bash
# GOLD layer audit: re-verifies the STRICT layer's A/B results under a
# genuine-publication rule and runs a targeted recovery pass over raw-C
# projects. Run from inside crypto_shutdown_radar/:
#   bash run_shutdown_source_gold_audit.sh
#
# Requires the same cached universe AND persisted 'v1' validation_sample
# that prior runs already used. Reuses that exact 100-project sample and
# the official_sources / shutdown_source_audit rows already recorded --
# does NOT rebuild the universe, the sample, or re-run raw/STRICT discovery.
#
# Targets <=25 minutes. Writes SHUTDOWN_SOURCE_GOLD_AUDIT.md in this directory.
set -euo pipefail
cd "$(dirname "$0")"

version_ok() {
  "$1" - <<'PYEOF'
import sys
major, minor = sys.version_info.major, sys.version_info.minor
sys.exit(0 if (major, minor) >= (3, 9) else 1)
PYEOF
}

if [ -d ".venv" ] && version_ok ".venv/bin/python" >/dev/null 2>&1; then
  echo "Reusing existing .venv."
else
  echo "FAILED: no usable .venv found. Run bash run_source_coverage_validation.sh "
  echo "(or run_live_validation.sh) first to set one up." >&2
  exit 1
fi

source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f "data/radar.db" ]; then
  echo "FAILED: data/radar.db not found in this directory. This script will not" >&2
  echo "rebuild it. Copy your prior database here (or set CSR_DB_PATH) first." >&2
  exit 1
fi

echo "Starting GOLD shutdown-source audit (target: <=25 minutes)..."
echo

python -m scripts.shutdown_source_gold_audit

echo
echo "Done. See SHUTDOWN_SOURCE_GOLD_AUDIT.md in this directory."
