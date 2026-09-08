"""Offroad, confirmation, and runtime-path gates for Pre-AP production tools."""
from __future__ import annotations

from pathlib import Path

from openpilot.common.params import Params

DESTRUCTIVE_TOOLS = frozenset({
  "calibrate_pedal",
  "calibrate_radar",
  "flash_epas",
  "restore_epas",
})

# Pedal calibration may start with ignition on. Other tools stay offroad-only.
PEDAL_IGNITION_ON_TOOLS = frozenset({"calibrate_pedal"})

# Match a parked car. Unknown / missing speed is not treated as standstill.
STANDSTILL_SPEED_MS = 0.5

TOOLS_DIR = Path(__file__).resolve().parent


class ToolSafetyError(Exception):
  """Tool refused to run because a safety gate failed."""


def require_runtime_path(module_file: str | Path | None = None) -> Path:
  """Production tools must execute from this package under BASEDIR."""
  from openpilot.common.basedir import BASEDIR
  tools_dir = TOOLS_DIR
  base = Path(BASEDIR).resolve()
  if base not in tools_dir.parents:
    raise ToolSafetyError(f"tools must run from BASEDIR ({base}), not {tools_dir}")
  if module_file is not None:
    path = Path(module_file).resolve()
    if path.parent != tools_dir and tools_dir not in path.parents:
      raise ToolSafetyError(f"tool must run from production path {tools_dir}")
    return path
  return tools_dir


def require_offroad(params: Params | None = None) -> None:
  params = params or Params()
  if not params.get_bool("IsOffroad"):
    raise ToolSafetyError("tool requires offroad")


def require_confirmation(confirmed: bool, *, tool: str) -> None:
  if tool in DESTRUCTIVE_TOOLS and not confirmed:
    raise ToolSafetyError("destructive tool requires explicit confirmation")


def require_preap_tool_start(params: Params | None = None, *, tool: str, confirmed: bool) -> None:
  require_runtime_path()
  if tool not in PEDAL_IGNITION_ON_TOOLS:
    require_offroad(params)
  require_confirmation(confirmed, tool=tool)


def require_pedal_calibration_start(params: Params | None = None, *, confirmed: bool) -> None:
  """Path + explicit confirm. Does not require IsOffroad."""
  require_preap_tool_start(params, tool="calibrate_pedal", confirmed=confirmed)


def pedal_calibration_entry_reason(*, offroad: bool, engaged: bool,
                                   car_state_fresh: bool, v_ego: float) -> str | None:
  """None if wizard entry / Start is allowed.

  Unknown motion is never permission to take over driving processes.
  Offroad is not a bypass: still need fresh stationary evidence.
  """
  del offroad
  if engaged:
    return "disengage before calibrating the pedal"
  if not car_state_fresh:
    return "waiting for stationary vehicle state"
  if not 0.0 <= v_ego <= STANDSTILL_SPEED_MS:
    return "vehicle must be stationary"
  return None


def allow_pedal_calibration_entry(*, offroad: bool, engaged: bool,
                                  car_state_fresh: bool, v_ego: float) -> bool:
  return pedal_calibration_entry_reason(
    offroad=offroad, engaged=engaged, car_state_fresh=car_state_fresh, v_ego=v_ego,
  ) is None


def _service_fresh(sm, name: str) -> bool:
  seen = getattr(sm, "seen", {}).get(name, False)
  alive = getattr(sm, "alive", {}).get(name, False)
  valid = getattr(sm, "valid", {}).get(name, False)
  return bool(seen and alive and valid)


def _has_service(sm, name: str) -> bool:
  data = getattr(sm, "data", None)
  if data is not None and name in data:
    return True
  return name in getattr(sm, "seen", {})


def _esp_b_speed_ms(dat: bytes) -> float | None:
  if len(dat) < 7:
    return None
  return ((dat[5] << 8) | dat[6]) * 0.01 / 3.6


def _v_ego_from_can(sm) -> float | None:
  if not _service_fresh(sm, "can"):
    return None
  try:
    messages = sm["can"]
  except Exception:
    return None
  speed = None
  for msg in messages:
    addr = int(getattr(msg, "address", getattr(msg, "addr", 0)))
    src = int(getattr(msg, "src", 0)) & 0x7F
    dat = bytes(getattr(msg, "dat", b""))
    if addr == 0x155 and src == 0:
      decoded = _esp_b_speed_ms(dat)
      if decoded is not None:
        speed = decoded
  return speed


def admission_from_submaster(sm) -> str | None:
  """Return a block reason from cereal, or None if Start may proceed.

  Missing SubMaster, unseen/stale deviceState, and started+stale
  selfdriveState/MADS fail closed. Offroad still needs fresh carState or
  fresh bus-0 ESP_B; unknown movement is not admission.
  """
  if sm is None:
    return "waiting for vehicle state"
  if not _service_fresh(sm, "deviceState"):
    return "waiting for device state"

  started = bool(sm["deviceState"].started)
  engaged = False
  if started:
    if not _service_fresh(sm, "selfdriveState"):
      return "waiting for disengage state"
    engaged = bool(sm["selfdriveState"].enabled)
    if _has_service(sm, "selfdriveStateSP"):
      if not _service_fresh(sm, "selfdriveStateSP"):
        return "waiting for disengage state"
      mads = getattr(sm["selfdriveStateSP"], "mads", None)
      engaged = engaged or bool(getattr(mads, "enabled", False))

  car_fresh = _service_fresh(sm, "carState")
  if car_fresh:
    v_ego = float(sm["carState"].vEgo)
  else:
    v_ego_can = _v_ego_from_can(sm)
    car_fresh = v_ego_can is not None
    v_ego = 0.0 if v_ego_can is None else v_ego_can

  return pedal_calibration_entry_reason(
    offroad=not started, engaged=engaged, car_state_fresh=car_fresh, v_ego=v_ego,
  )


def parse_explicit_confirmation(argv=None) -> bool:
  """Return True only when this invocation includes an explicit --confirm flag.

  Absence, empty argv, and default argparse False cannot silently satisfy
  destructive tools. Callers must pass the flag after a real user acknowledgment.
  """
  import argparse
  parser = argparse.ArgumentParser(add_help=False)
  parser.add_argument("--confirm", action="store_true", default=False)
  args, _unknown = parser.parse_known_args(argv)
  return bool(args.confirm)
