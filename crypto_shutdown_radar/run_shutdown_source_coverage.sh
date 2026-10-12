#!/usr/bin/env bash
# Shutdown-event source coverage measurement. Run from inside crypto_shutdown_radar/:
#   bash run_shutdown_source_coverage.sh
#
# Requires BOTH a cached universe (data/radar.db with ~1000 coins) AND the
# persisted 'v1' validation_sample that a prior
# `python -m scripts.source_coverage_validation` run already created. It
# reuses that exact 100-project sample and all already-cached metadata/
# discovery results -- it will NOT rebuild the universe, the sample, or
# re-fetch anything already successfully cached.
#
# Targets <=25 minutes. Writes SHUTDOWN_SOURCE_COVERAGE.md in this directory.
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

echo "Starting shutdown-source coverage measurement (target: <=25 minutes)..."
echo

python -m scripts.shutdown_source_coverage

echo
echo "Done. See SHUTDOWN_SOURCE_COVERAGE.md in this directory."
