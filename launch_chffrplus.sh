#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"

source "$DIR/launch_env.sh"

function agnos_init {
  # TODO: move this to agnos
  sudo rm -f /data/etc/NetworkManager/system-connections/*.nmmeta
  rm -f /data/scons_cache/config.lock

  # set success flag for current boot slot
  sudo abctl --set_success

  # TODO: do this without udev in AGNOS
  # udev does this, but sometimes we startup faster
  sudo chgrp gpu /dev/adsprpc-smd /dev/ion /dev/kgsl-3d0
  sudo chmod 660 /dev/adsprpc-smd /dev/ion /dev/kgsl-3d0

  # Check if AGNOS update is required
  if [ $(< /VERSION) != "$AGNOS_VERSION" ]; then
    AGNOS_PY="$DIR/openpilot/common/hardware/tici/agnos.py"
    MANIFEST="$DIR/openpilot/system/hardware/tici/agnos.json"
    if $AGNOS_PY --verify $MANIFEST; then
      sudo reboot
    fi
    $DIR/openpilot/common/hardware/tici/updater $AGNOS_PY $MANIFEST
  fi
}

# C3_DEPS: runtime dependency root, outside the repo so the updater cannot wipe
# it. The build-only root (/data/c3_deps/build) is added by SConstruct instead,
# so it reaches scons without reaching build.py's spinner.
C3_DEPS_RUNTIME="/data/c3_deps/runtime"

function launch {
  # C3_SETUP: run first-time setup on a fresh install
  if [ ! -f /data/c3_first_run ]; then
    echo "C3: running first-time setup..."
    bash "${DIR}/setup_c3_preap.sh"
  fi

  # Remove orphaned git lock if it exists on boot
  [ -f "$DIR/.git/index.lock" ] && rm -f $DIR/.git/index.lock

  # Check to see if there's a valid overlay-based update available. Conditions
  # are as follows:
  #
  # 1. The DIR init file has to exist, with a newer modtime than anything in
  #    the DIR Git repo. This checks for local development work or the user
  #    switching branches/forks, which should not be overwritten.
  # 2. The FINALIZED consistent file has to exist, indicating there's an update
  #    that completed successfully and synced to disk.

  if [ -f "${DIR}/.overlay_init" ]; then
    find ${DIR}/.git -newer ${DIR}/.overlay_init | grep -q '.' 2> /dev/null
    if [ $? -eq 0 ]; then
      echo "${DIR} has been modified, skipping overlay update installation"
    else
      if [ -f "${STAGING_ROOT}/finalized/.overlay_consistent" ]; then
        if [ ! -d /data/safe_staging/old_openpilot ]; then
          echo "Valid overlay update found, installing"
          LAUNCHER_LOCATION="${BASH_SOURCE[0]}"

          mv $DIR /data/safe_staging/old_openpilot
          mv "${STAGING_ROOT}/finalized" $DIR
          cd $DIR

          echo "Restarting launch script ${LAUNCHER_LOCATION}"
          unset AGNOS_VERSION
          exec "${LAUNCHER_LOCATION}"
        else
          echo "openpilot backup found, not updating"
          # TODO: restore backup? This means the updater didn't start after swapping
        fi
      fi
    fi
  fi

  # handle pythonpath
  ln -sfn $(pwd) /data/pythonpath
  # C3_DEPS: comma-deps-* wheels AGNOS 12.6 does not ship (see setup_c3_preap.sh).
  # They live outside the repo on purpose: the updater swaps $DIR for a fresh
  # checkout, which would delete anything untracked inside it.
  export PYTHONPATH="$C3_DEPS_RUNTIME:$PWD"
  # C3_DEPS: and their tools ahead of AGNOS's own, so the capnp compiler matches
  # the capnp headers we build against (12.6 ships 1.0.2, the wheel is 1.0.1).
  for _bin in "$C3_DEPS_RUNTIME"/*/install/bin; do
    [ -d "$_bin" ] && export PATH="$_bin:$PATH"
  done

  # C3_DISPLAY: 0.11.2 expects magic, comma's DRM compositor. AGNOS 12.6 predates
  # it and runs Weston, so the UI renders as a Wayland client against AGNOS's own
  # raylib (see c3_build_deps/requirements-build.txt). Point it at Weston's socket.
  export XDG_RUNTIME_DIR="/var/tmp/weston"
  export WAYLAND_DISPLAY="wayland-0"

  # submodule package symlinks for PYTHONPATH imports on device.
  # on PC these come from editable installs via pyproject.toml / uv.
  ln -sfn msgq_repo/msgq msgq
  ln -sfn opendbc_repo/opendbc opendbc
  ln -sfn rednose_repo/rednose rednose
  ln -sfn teleoprtc_repo/teleoprtc teleoprtc
  ln -sfn tinygrad_repo/tinygrad tinygrad

  # hardware specific init
  if [ -f /AGNOS ]; then
    agnos_init
  fi

  # write tmux scrollback to a file
  tmux capture-pane -pq -S-1000 > /tmp/launch_log

  # start manager
  cd openpilot/system/manager
  if [ ! -f $DIR/prebuilt ]; then
    # C3_DEPS: build.py runs with the runtime PYTHONPATH on purpose, so its
    # spinner uses AGNOS's Wayland raylib. SConstruct adds the build-only
    # raylib 6.0 for scons itself.
    ./build.py
  fi
  ./manager.py

  # if broken, keep on screen error
  while true; do sleep 1; done
}

launch
