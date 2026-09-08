"""Shared instruction strings for Pre-AP production tools."""
from openpilot.system.ui.lib.multilang import tr_noop

CALIBRATE_PEDAL_INSTRUCTIONS = tr_noop("""Keep the car ON for calibration.

1. Stop the vehicle and disengage assistance.
2. Select Neutral and hold the brake.
3. Release the accelerator.
4. Check that the pedal connector is seated.
5. Select the pedal's CAN bus below.

Start pauses driving assistance. Cancel stops calibration.
Settings are saved only after calibration succeeds.

When finished, turn the car off to Exit,
or choose Restart device. No automatic restart.""")


CALIBRATE_RADAR_INSTRUCTIONS = tr_noop("""Radar Calibration

Displays filtered Bosch radar points to help align the sensor.

PRECONDITIONS:
  1. Device must be offroad
  2. Vehicle parked with a target 3-10m ahead

Press START to confirm and begin.""")

TEST_RADAR_INSTRUCTIONS = tr_noop("""Radar Test

Displays live Bosch radar tracks for installation checks.

Press START to begin.""")

DIAGNOSE_RADAR_INSTRUCTIONS = tr_noop("""Radar Diagnosis

Reads Bosch radar health without changing panda safety mode.

Press START to begin.""")

FLASH_EPAS_INSTRUCTIONS = tr_noop("""EPAS Firmware Flash

WARNING: This modifies steering-system firmware.

POWER THE CAR ON, stay in Park, do not drive.
Power loss during the flash can brick EPAS.

Press START only if you accept these risks.""")

BACKUP_EPAS_INSTRUCTIONS = tr_noop("""EPAS Firmware Backup

Extracts stock EPAS firmware. No flashing is performed.

POWER THE CAR ON before starting.

Press START to extract the backup.""")

RESTORE_EPAS_INSTRUCTIONS = tr_noop("""EPAS Firmware Restore

WARNING: This reflashes steering-system firmware from the stock image.

POWER THE CAR ON, stay in Park, do not drive.

Press START only if you accept these risks.""")
