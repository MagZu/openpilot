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

echo "[1/3] Initialising submodules..."
git submodule update --init --depth 1

echo "[2/3] Creating submodule package symlinks..."
# Same links launch_chffrplus.sh makes; created here too so a manual scons run
# works before the first launch.
ln -sfn msgq_repo/msgq msgq
ln -sfn opendbc_repo/opendbc opendbc
ln -sfn rednose_repo/rednose rednose
ln -sfn teleoprtc_repo/teleoprtc teleoprtc
ln -sfn tinygrad_repo/tinygrad tinygrad

echo "[3/3] Installing native build dependencies into c3_third_party/..."
# /tmp is a 150MB tmpfs on AGNOS, too small to unpack these wheels.
export TMPDIR="/data/tmp"
mkdir -p "$TMPDIR"
# --no-deps on purpose: we want only the native payloads. c3_third_party is first
# on PYTHONPATH, so letting pip pull pure-python deps here (numpy, via acados)
# would shadow the device venv's own copies.
/usr/local/venv/bin/python -m pip install --target "$DIR/c3_third_party" --upgrade --no-deps \
  -r "$DIR/c3_third_party/requirements-c3.txt"

touch "$MARKER"
echo ""
echo "Setup complete."
