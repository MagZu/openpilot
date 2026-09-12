#!/usr/bin/env bash
# First-time setup for naponsp-C3-alpha on a comma 3 (tici).
#
# openpilot 0.11.2 dropped comma 3 support and moved its native build
# dependencies to comma-deps-* wheels that AGNOS 12.6 does not ship. This
# installs them repo-locally (c3_third_party/, first on PYTHONPATH) and
# initialises the submodules.
#
# Run once after cloning onto the device:
#   bash /data/openpilot/setup_c3_preap.sh

set -e

MARKER="/data/c3_first_run"
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"

if [ ! -f /AGNOS ]; then
  echo "ERROR: not running on AGNOS — this script is for the comma 3 only"
  exit 1
fi

echo "=== comma 3 first-time setup ==="
cd "$DIR"

echo "[1/2] Initialising submodules..."
git submodule update --init --depth 1 panda opendbc_repo msgq_repo rednose_repo teleoprtc_repo tinygrad_repo

echo "[2/2] Installing native build dependencies into c3_third_party/..."
/usr/local/venv/bin/python -m pip install --target "$DIR/c3_third_party" --upgrade \
  -r "$DIR/c3_third_party/requirements-c3.txt"

touch "$MARKER"
echo ""
echo "Setup complete."
