"""Approved Pre-AP diagnostic transport. Never grants ALLOUTPUT or driving authority."""
from __future__ import annotations

import time
from opendbc.car import structs
from opendbc.car.tesla.preap.teslacan import GAS_COMMAND_ID

# Matches tesla_preap.h PREAP_FLAG_ENABLE_PEDAL / calibration island.
PREAP_FLAG_ENABLE_PEDAL = 1
PREAP_MODE_INVALID = 3
PREAP_FLAG_PEDAL_BUS_ZERO = 1 << 5
PREAP_FLAG_PEDAL_CALIBRATION = 1 << 6

SAFETY_SILENT = int(structs.CarParams.SafetyModel.silent)
SAFETY_ELM327 = int(structs.CarParams.SafetyModel.elm327)
SAFETY_ALLOUTPUT = int(structs.CarParams.SafetyModel.allOutput)
SAFETY_TESLA_PREAP = int(structs.CarParams.SafetyModel.teslaPreap)

ALLOWED_DIAGNOSTIC_MODES = frozenset({SAFETY_SILENT, SAFETY_ELM327})
ALLOWED_SAFETY_MODES = ALLOWED_DIAGNOSTIC_MODES | frozenset({SAFETY_TESLA_PREAP})
PANDA_CONNECT_RETRIES = 5
PANDA_CONNECT_DELAY = 2.0
PEDAL_CONNECT_RETRIES = 8
PEDAL_CONNECT_DELAY = 0.25
# Ignition-on teslaPreap falls to SILENT in 5s without 0xf3. Keep USB I/O
# well under that. Default panda bulkRead timeout is 15s with unbounded retry.
CAN_RECV_TIMEOUT_MS = 100
CAN_RECV_RETRIES = 3
HEALTH_TIMEOUT_MS = 200
HEARTBEAT_TIMEOUT_MS = 200


class TransportError(Exception):
  """Fail-closed transport or negative-response failure."""


def uds_fail_closed(exc: BaseException) -> TransportError:
  """Wrap UDS negative responses so callers fail closed."""
  name = type(exc).__name__
  if name == "NegativeResponseError" or "negative response" in str(exc).lower():
    return TransportError(f"UDS negative response (fail closed): {exc}")
  return TransportError(str(exc))


def fail_closed_negative_response(exc: BaseException) -> None:
  """Convert a UDS negative response into TransportError. Never continue the tool."""
  name = type(exc).__name__
  message = str(exc)
  if name == "NegativeResponseError" or "negative response" in message.lower() or getattr(exc, "error_code", None) is not None:
    raise TransportError(f"UDS negative response (fail closed): {exc}") from exc
  raise TransportError(f"UDS request failed closed: {exc}") from exc


class DiagnosticTransport:
  """Panda access that cannot bypass TX restrictions."""

  def __init__(self, panda=None):
    self.panda = panda
    self._mode = SAFETY_SILENT
    self._param = 0
    self._mode_set = False

  def connect(self, panda_factory=None, retries=PANDA_CONNECT_RETRIES, delay=PANDA_CONNECT_DELAY):
    if self.panda is not None:
      return self.panda
    factory = panda_factory
    if factory is None:
      from panda import Panda
      factory = Panda
    last_exc: Exception | None = None
    for attempt in range(retries):
      try:
        self.panda = factory()
        return self.panda
      except Exception as exc:
        last_exc = exc
        if attempt < retries - 1:
          time.sleep(delay)
    raise TransportError(f"panda connect failed: {last_exc}") from last_exc

  def set_diagnostic_session(self) -> None:
    self._set_safety_mode(SAFETY_ELM327)

  def set_silent(self) -> None:
    self._set_safety_mode(SAFETY_SILENT)

  def set_pedal_calibration_session(self, bus: int = 2) -> None:
    """teslaPreap calibration island. Never ELM327/allOutput/ENABLE_PEDAL.

    Legal safetyParam is 64 (bus 2) or 96 (bus 0). Production 0x551 gates
    are unchanged; this mode only admits 0x551 on the selected bus.
    teslaPreap is a car safety mode: heartbeat checks cannot stay disabled.
    """
    if bus not in (0, 2):
      raise TransportError("invalid pedal bus")
    param = PREAP_FLAG_PEDAL_CALIBRATION
    if bus == 0:
      param |= PREAP_FLAG_PEDAL_BUS_ZERO
    self.set_silent()
    self._set_safety_mode(SAFETY_TESLA_PREAP, param=param)
    self.send_heartbeat(False, False)

  def _usb_handle(self):
    if self.panda is None:
      raise TransportError("panda not connected")
    handle = getattr(self.panda, "_handle", None)
    buf = getattr(self.panda, "can_rx_overflow_buffer", None)
    has_usb = (
      handle is not None
      and callable(getattr(handle, "controlWrite", None))
      and callable(getattr(handle, "controlRead", None))
      and callable(getattr(handle, "bulkRead", None))
      and isinstance(buf, (bytes, bytearray))
    )
    if not has_usb:
      raise TransportError("panda usb handle missing")
    return handle

  def send_heartbeat(self, engaged: bool = False, engaged_mads: bool = False) -> None:
    """Keep the panda watchdog alive without driving engagement bits.

    Panda.send_heartbeat defaults to True, True. Calibration must pass
    false, false explicitly. teslaPreap calibration clamps both to false.
    """
    handle = self._usb_handle()
    if self._mode == SAFETY_TESLA_PREAP:
      engaged = False
      engaged_mads = False
    try:
      from panda import Panda
      handle.controlWrite(
        Panda.REQUEST_OUT, 0xf3, int(engaged), int(engaged_mads), b"",
        timeout=HEARTBEAT_TIMEOUT_MS,
      )
    except TransportError:
      raise
    except Exception as exc:
      raise TransportError(f"heartbeat failed: {exc}") from exc

  def read_health(self) -> dict:
    handle = self._usb_handle()
    health_struct = getattr(type(self.panda), "HEALTH_STRUCT", None)
    if health_struct is None or not hasattr(health_struct, "size") or not hasattr(health_struct, "unpack"):
      raise TransportError("panda health failed: missing HEALTH_STRUCT")
    try:
      from panda import Panda
      dat = handle.controlRead(
        Panda.REQUEST_IN, 0xd2, 0, 0, health_struct.size, timeout=HEALTH_TIMEOUT_MS,
      )
      a = health_struct.unpack(dat)
      return {
        "safety_mode": a[12],
        "safety_param": a[13],
        "heartbeat_lost": a[16],
        "ignition_line": a[8],
        "ignition_can": a[9],
      }
    except TransportError:
      raise
    except Exception as exc:
      raise TransportError(f"panda health failed: {exc}") from exc

  def require_calibration_health(self, expected_mode: int, expected_param: int) -> None:
    """Abort on SILENT, watchdog loss, or mode/param mismatch. Never rearm."""
    health = self.read_health()
    if health.get("heartbeat_lost"):
      raise TransportError("watchdog heartbeat lost")
    mode = int(health.get("safety_mode", -1))
    param = int(health.get("safety_param", -1))
    if mode == SAFETY_SILENT:
      raise TransportError("unexpected SILENT safety mode")
    if mode != int(expected_mode) or param != int(expected_param):
      raise TransportError("unexpected safety mode")

  def _set_safety_mode(self, mode: int, param: int = 0) -> None:
    if mode == SAFETY_ALLOUTPUT or mode not in ALLOWED_SAFETY_MODES:
      raise TransportError("panda TX bypass is not permitted")
    if mode == SAFETY_TESLA_PREAP:
      if int(param) & PREAP_FLAG_ENABLE_PEDAL:
        raise TransportError("calibration must not grant longitudinal")
      if (int(param) & PREAP_FLAG_PEDAL_CALIBRATION) == 0:
        raise TransportError("teslaPreap transport requires calibration flag")
      extra = int(param) & ~(PREAP_FLAG_PEDAL_CALIBRATION | PREAP_FLAG_PEDAL_BUS_ZERO)
      if extra:
        raise TransportError("calibration safetyParam must be exactly 64 or 96")
      if int(param) not in (PREAP_FLAG_PEDAL_CALIBRATION,
                            PREAP_FLAG_PEDAL_CALIBRATION | PREAP_FLAG_PEDAL_BUS_ZERO):
        raise TransportError("calibration safetyParam must be exactly 64 or 96")
    elif int(param) != 0:
      raise TransportError("diagnostic modes cannot carry safetyParam bits")
    if self.panda is None:
      raise TransportError("panda not connected")
    self.panda.set_safety_mode(mode, param)
    self._mode = mode
    self._param = int(param)
    self._mode_set = True

  def can_recv(self):
    handle = self._usb_handle()
    panda = self.panda
    if panda is None:
      raise TransportError("panda not connected")
    last_exc: Exception | None = None
    for _attempt in range(CAN_RECV_RETRIES):
      try:
        from panda.python import unpack_can_buffer
        dat = handle.bulkRead(1, 16384, timeout=CAN_RECV_TIMEOUT_MS)
        overflow = bytearray(panda.can_rx_overflow_buffer)
        msgs, overflow = unpack_can_buffer(overflow + dat)
        panda.can_rx_overflow_buffer = overflow
        return msgs
      except TransportError:
        raise
      except Exception as exc:
        last_exc = exc
    raise TransportError(f"can_recv failed: {last_exc}") from last_exc

  def can_send(self, addr: int, dat: bytes, bus: int) -> None:
    if self.panda is None:
      raise TransportError("panda not connected")
    if self._mode == SAFETY_ALLOUTPUT:
      raise TransportError("panda TX bypass is not permitted")
    if int(addr) == GAS_COMMAND_ID:
      if self._mode in (SAFETY_ELM327, SAFETY_ALLOUTPUT) or self._mode != SAFETY_TESLA_PREAP:
        raise TransportError("pedal frames require teslaPreap calibration safety")
    try:
      self.panda.can_send(addr, dat, bus)
    except Exception as exc:
      raise TransportError(f"can_send failed: {exc}") from exc

  def close(self) -> None:
    if self.panda is None:
      return
    if self._mode_set:
      try:
        self.set_silent()
      except Exception:
        pass
    try:
      self.panda.close()
    except Exception:
      pass
    self.panda = None
    self._mode_set = False
    self._param = 0


def ignition_from_health(health: dict) -> bool | None:
  line = health.get("ignition_line")
  can = health.get("ignition_can")
  if line is None and can is None:
    return None
  return bool(line or can)


def sample_panda_ignition(transport_factory=None) -> bool | None:
  """Fresh ignition after the calibrator has released USB. None if unknown.

  Always closes the probe. Does not program teslaPreap.
  """
  factory = transport_factory or DiagnosticTransport
  transport = factory()
  try:
    transport.connect(retries=3, delay=0.2)
    return ignition_from_health(transport.read_health())
  except Exception:
    return None
  finally:
    try:
      transport.close()
    except Exception:
      pass

