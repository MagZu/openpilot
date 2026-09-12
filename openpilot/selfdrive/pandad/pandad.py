#!/usr/bin/env python3
# simple pandad wrapper that updates the panda first
import os
import json
import usb1
import time
import signal
import subprocess

from panda import Panda, PandaDFU, PandaProtocolMismatch, McuType, FW_PATH
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params
from openpilot.common.hardware import HARDWARE
from openpilot.common.swaglog import cloudlog

from openpilot.sunnypilot.selfdrive.pandad.rivian_long_flasher import flash_rivian_long


# C3_F4_PANDA: the comma 3 internal panda is an F4/DOS, whose firmware is built
# on-device from panda/F4/ rather than shipped prebuilt.
_F4_BUILD_SH = os.path.join(BASEDIR, "panda", "F4", "build.sh")
_F4_CONFIG = os.path.join(BASEDIR, "panda", "F4", "config.json")
_F4_FW_PATH = os.path.join(BASEDIR, "panda", "board", "obj", "panda.bin.signed")


def f4_auto_compile_if_needed() -> None:
  try:
    with open(_F4_CONFIG) as f:
      cfg = json.load(f)
    if not cfg.get("auto_compile", False):
      return
    if not os.path.exists(_F4_FW_PATH):
      cloudlog.info("F4 firmware missing, auto-compiling via panda/F4/build.sh...")
      result = subprocess.run(["bash", _F4_BUILD_SH], capture_output=True, text=True)
      if result.returncode != 0:
        cloudlog.error(f"F4 auto-compile failed:\n{result.stderr}")
      else:
        cloudlog.info("F4 auto-compile complete")
  except Exception:
    cloudlog.exception("f4_auto_compile_if_needed failed")


def get_expected_signature(panda=None) -> bytes:
  # C3_F4_PANDA: F4/DOS panda has its own firmware file, H7 otherwise
  if panda is not None and panda.get_type() in Panda.DEPRECATED_DEVICES:
    fn = os.path.join(FW_PATH, McuType.F4.config.app_fn)
  else:
    fn = os.path.join(FW_PATH, McuType.H7.config.app_fn)
  return Panda.get_signature_from_firmware(fn)

def flash_panda(panda_serial: str):
  panda = Panda(panda_serial)

  # skip flashing if the detected panda is not supported
  # C3_F4_PANDA: F4/DOS is in DEPRECATED_DEVICES, not SUPPORTED_DEVICES
  if panda.get_type() not in Panda.SUPPORTED_DEVICES + Panda.DEPRECATED_DEVICES:
    cloudlog.warning(f"Panda {panda_serial} is not supported (hw_type: {panda.get_type()}), skipping flash...")
    panda.close()
    return

  fw_signature = get_expected_signature(panda)  # C3_F4_PANDA
  internal_panda = panda.is_internal()

  panda_version = "bootstub" if panda.bootstub else panda.get_version()
  panda_signature = b"" if panda.bootstub else panda.get_signature()
  cloudlog.warning(f"Panda {panda_serial} connected, version: {panda_version}, signature {panda_signature.hex()[:16]}, expected {fw_signature.hex()[:16]}")

  if panda.bootstub or panda_signature != fw_signature:
    cloudlog.info("Panda firmware out of date, update required")
    panda.flash()
    cloudlog.info("Done flashing")

  if panda.bootstub:
    bootstub_version = panda.get_version()
    cloudlog.info(f"Flashed firmware not booting, flashing development bootloader. {bootstub_version=}, {internal_panda=}")
    if internal_panda:
      HARDWARE.recover_internal_panda()
    panda.recover(reset=(not internal_panda))
    cloudlog.info("Done flashing bootstub")

  if panda.bootstub:
    cloudlog.info("Panda still not booting, exiting")
    raise AssertionError

  panda_signature = panda.get_signature()
  if panda_signature != fw_signature:
    cloudlog.info("Version mismatch after flashing, exiting")
    raise AssertionError

  panda.close()


def check_panda_support(panda_serials: list[str]) -> list[str]:
  spi_serials = set(Panda.spi_list())
  for serial in panda_serials:
    if serial in spi_serials:
      return [serial]

  for serial in panda_serials:
    panda = Panda(serial)
    is_internal = panda.is_internal()
    panda.close()
    if is_internal:
      return [serial]

  return []


def main() -> None:
  # signal pandad to close the relay and exit
  def signal_handler(signum, frame):
    cloudlog.info(f"Caught signal {signum}, exiting")
    nonlocal do_exit
    do_exit = True
    if process is not None:
      process.send_signal(signal.SIGINT)

  process = None
  do_exit = False
  signal.signal(signal.SIGINT, signal_handler)

  # C3_F4_PANDA: build F4 firmware on first boot if auto_compile is set in panda/F4/config.json
  f4_auto_compile_if_needed()

  # check health for lost heartbeat
  try:
    for s in Panda.list():
      with Panda(s) as p:
        health = p.health()
        if p.is_internal() and health["heartbeat_lost"]:
          Params().put_bool("PandaHeartbeatLost", True, block=True)
          cloudlog.event("heartbeat lost", deviceState=health)
  except Exception:
    cloudlog.exception("pandad.uncaught_exception")

  count = 0
  no_panda_count = 0  # C3_F4_PANDA
  while not do_exit:
    try:
      cloudlog.event("pandad.flash_and_connect", count=count)
      # C3_F4_PANDA: the comma 3 internal panda is an F4 on USB and takes a few
      # seconds to re-enumerate after a reset. Upstream resets (and every other
      # pass forces DFU) on every iteration with no settle time, which an H7 on
      # SPI wins and an F4 never does, so the panda is never seen. Only reset
      # after an attempt that found nothing, wait for USB to come back, and
      # escalate to DFU recovery only after repeated failures.
      if no_panda_count > 0:
        if no_panda_count % 3 == 0:
          cloudlog.info("No pandas found, putting internal panda into DFU")
          HARDWARE.recover_internal_panda()
        else:
          cloudlog.info("No pandas found, resetting internal panda")
          HARDWARE.reset_internal_panda()
        time.sleep(3)  # wait to come back up
      count += 1

      # Flash all Pandas in DFU mode
      for serial in PandaDFU.list():
        cloudlog.info(f"Panda in DFU mode found, flashing recovery {serial}")
        PandaDFU(serial).recover()
        time.sleep(1)

      panda_serials = Panda.list()
      if not len(panda_serials):
        no_panda_count += 1  # C3_F4_PANDA
      else:
        no_panda_count = 0  # C3_F4_PANDA
        # custom flasher for xnor's Rivian Longitudinal Upgrade Kit
        flash_rivian_long(panda_serials)
        # find the internal supported panda (e.g. skip external Black Panda)
        panda_serials = check_panda_support(panda_serials)

        assert len(panda_serials) == 1
        cloudlog.info(f"{len(panda_serials)} panda found, connecting - {panda_serials}")
        flash_panda(panda_serials[0])

        # run real pandad
        os.environ['MANAGER_DAEMON'] = 'pandad'
        process = subprocess.Popen(["./pandad"], cwd=os.path.join(BASEDIR, "openpilot/selfdrive/pandad"))
        process.wait()
    # TODO: wrap all panda exceptions in a base panda exception
    except (usb1.USBErrorNoDevice, usb1.USBErrorPipe):
      # a panda was disconnected while setting everything up. let's try again
      cloudlog.exception("Panda USB exception while setting up")
    except PandaProtocolMismatch:
      cloudlog.exception("pandad.protocol_mismatch")
    except Exception:
      cloudlog.exception("pandad.uncaught_exception")


if __name__ == "__main__":
  main()
