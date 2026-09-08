import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock
from typing import cast

import pytest
from opendbc.car import structs

from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import epas_integrity
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import (
  PedalCalibrationError,
  build_pedal_command,
  parse_configured_pedal_bus,
)
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.epas_integrity import (
  BOOTLOADER_SIZE,
  FW_MD5SUM,
  FW_SHA256,
  FW_SIZE,
  BootloaderIntegrityError,
  FirmwareIntegrityError,
  load_stock_firmware,
  verify_bootloader,
  verify_firmware,
)
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner import (
  APPROVED_TOOLS, RUN_SCRIPT_MODULE, approved_module, launch_on_device_runner, start_tool,
)
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import (
  ToolSafetyError,
  require_confirmation,
  require_offroad,
  require_preap_tool_start,
)
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.transport import (
  CAN_RECV_RETRIES,
  CAN_RECV_TIMEOUT_MS,
  HEALTH_TIMEOUT_MS,
  HEARTBEAT_TIMEOUT_MS,
  PREAP_FLAG_ENABLE_PEDAL,
  PREAP_FLAG_PEDAL_BUS_ZERO,
  PREAP_FLAG_PEDAL_CALIBRATION,
  SAFETY_ALLOUTPUT,
  SAFETY_ELM327,
  SAFETY_SILENT,
  SAFETY_TESLA_PREAP,
  DiagnosticTransport,
  TransportError,
)


class FakeParams:
  def __init__(self, offroad=True):
    self.store = {"IsOffroad": offroad}

  def get_bool(self, key):
    return bool(self.store.get(key, False))

  def put_bool(self, key, value, block=True):
    self.store[key] = bool(value)

  def get(self, key, return_default=False):
    return self.store.get(key)

  def put(self, key, value, block=True):
    self.store[key] = value


def _health_bytes(*, safety_mode=0, safety_param=0, heartbeat_lost=0,
                  ignition_line=0, ignition_can=0):
  from panda import Panda
  n = len(Panda.HEALTH_STRUCT.unpack(bytes(Panda.HEALTH_STRUCT.size)))
  vals = [0] * n
  vals[8] = ignition_line
  vals[9] = ignition_can
  vals[12] = safety_mode
  vals[13] = safety_param
  vals[16] = heartbeat_lost
  return Panda.HEALTH_STRUCT.pack(*vals)


class UsbHandle:
  def __init__(self, *, health=b"", can=b"", fail=None):
    self.health = health
    self.can = can
    self.fail = fail
    self.writes = []
    self.reads = []
    self.bulk = []

  def controlWrite(self, request_type, request, value, index, data, timeout=0, **kwargs):
    self.writes.append((request, value, index, timeout))
    if self.fail == "write":
      raise RuntimeError("usb timeout")

  def controlRead(self, request_type, request, value, index, length, timeout=0):
    self.reads.append((request, length, timeout))
    if self.fail == "read":
      raise RuntimeError("usb timeout")
    return self.health

  def bulkRead(self, endpoint, length, timeout=0):
    self.bulk.append((endpoint, length, timeout))
    if self.fail == "bulk":
      raise RuntimeError("usb timeout")
    return self.can


def _usb_panda(handle=None, **handle_kw):
  from panda import Panda

  class UsbPanda:
    HEALTH_STRUCT = Panda.HEALTH_STRUCT

    def __init__(self, usb_handle):
      self._handle = usb_handle
      self.can_rx_overflow_buffer = bytearray()
      self.set_safety_mode = MagicMock()
      self.can_send = MagicMock()

  return UsbPanda(handle or UsbHandle(**handle_kw))




def test_offroad_and_confirmation_gates():
  require_offroad(FakeParams(offroad=True))
  with pytest.raises(ToolSafetyError):
    require_offroad(FakeParams(offroad=False))
  require_confirmation(True, tool="flash_epas")
  with pytest.raises(ToolSafetyError):
    require_confirmation(False, tool="flash_epas")
  require_confirmation(False, tool="diagnose_radar")
  require_preap_tool_start(FakeParams(offroad=False), tool="calibrate_pedal", confirmed=True)
  with pytest.raises(ToolSafetyError):
    require_preap_tool_start(FakeParams(offroad=False), tool="flash_epas", confirmed=True)
  with pytest.raises(ToolSafetyError):
    require_preap_tool_start(FakeParams(offroad=True), tool="calibrate_pedal", confirmed=False)



def test_transport_rejects_alloutput():
  panda = MagicMock()
  transport = DiagnosticTransport(panda=panda)
  with pytest.raises(TransportError, match="bypass"):
    transport._set_safety_mode(SAFETY_ALLOUTPUT)
  panda.set_safety_mode.assert_not_called()

def test_transport_selects_cereal_elm327_mode():
  panda = MagicMock()
  transport = DiagnosticTransport(panda=panda)
  transport.set_diagnostic_session()
  assert SAFETY_ELM327 == int(structs.CarParams.SafetyModel.elm327)
  panda.set_safety_mode.assert_called_once_with(SAFETY_ELM327, 0)


def test_unapproved_tool_rejected():
  with pytest.raises(ValueError, match="unapproved"):
    approved_module("radar_replay")
  with pytest.raises(ValueError, match="unapproved"):
    start_tool("lead_source_analysis", confirmed=True, params=FakeParams())
  with pytest.raises(ValueError, match="unapproved"):
    approved_module("vision_radar_delta")
  assert "vision_radar_delta" not in APPROVED_TOOLS
  assert "radar_replay" not in APPROVED_TOOLS


def test_start_tool_clears_script_running_on_spawn_fail(monkeypatch):
  params = FakeParams()

  def boom(*_args, **_kwargs):
    raise OSError("spawn failed")

  monkeypatch.setattr("openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.subprocess.Popen", boom)
  with pytest.raises(OSError):
    start_tool("diagnose_radar", confirmed=True, params=params)
  assert params.get_bool("NAPScriptRunning") is False
  assert params.get_bool("NAPEpasRiskAccepted") is False


def test_bootloader_integrity_before_connect(monkeypatch):
  payload = b"x" * BOOTLOADER_SIZE
  md5 = hashlib.md5(payload).hexdigest()
  sha = hashlib.sha256(payload).hexdigest()
  monkeypatch.setattr(epas_integrity, "BOOTLOADER_MD5SUM", md5)
  monkeypatch.setattr(epas_integrity, "BOOTLOADER_SHA256", sha)
  with TemporaryDirectory() as tmp:
    path = Path(tmp) / "bl.bin"
    path.write_bytes(payload)
    assert verify_bootloader(path) == payload
    path.write_bytes(b"y" * BOOTLOADER_SIZE)
    with pytest.raises(BootloaderIntegrityError):
      verify_bootloader(path)


def test_flash_and_restore_verify_bootloader_before_connect(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import flash_epas

  called = []

  def boom(_path):
    called.append("verify")
    raise BootloaderIntegrityError("bad bootloader")

  class BoomTransport:
    def __init__(self, *args, **kwargs):
      called.append("transport_init")

    def connect(self):
      called.append("connect")
      raise AssertionError("must not connect after integrity failure")

  monkeypatch.setattr(flash_epas, "verify_bootloader", boom)
  monkeypatch.setattr(flash_epas, "require_preap_tool_start", lambda **_k: None)
  monkeypatch.setattr(flash_epas, "_consume_ui_risk_ack", lambda: True)
  monkeypatch.setattr(flash_epas, "DiagnosticTransport", BoomTransport)
  monkeypatch.setattr(flash_epas, "bootloader_path", lambda _name: Path("/tmp/missing.bin"))

  assert flash_epas.main(["--accept-risk"]) == 1
  assert called == ["verify"]
  called.clear()
  assert flash_epas.main(["--restore", "--accept-risk"]) == 1
  assert called == ["verify"]


def test_pedal_command_rejects_invalid_bus():
  with pytest.raises(PedalCalibrationError, match="invalid pedal bus"):
    build_pedal_command(0.0, enable=0, bus=1)
  addr, dat, bus = build_pedal_command(0.0, enable=0, bus=2)
  assert bus == 2
  assert len(dat) == 6
  assert addr == 0x551


def test_start_tool_flash_sets_risk_ack(monkeypatch):
  params = FakeParams()
  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.subprocess.Popen",
    lambda *_args, **_kwargs: MagicMock(),
  )
  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.threading.Thread",
    lambda **_kwargs: MagicMock(),
  )
  start_tool("flash_epas", confirmed=True, params=params)
  assert params.get_bool("NAPScriptRunning") is True
  assert params.get_bool("NAPEpasRiskAccepted") is True

  params = FakeParams()
  start_tool("diagnose_radar", confirmed=True, params=params)
  assert params.get_bool("NAPScriptRunning") is True
  assert params.get_bool("NAPEpasRiskAccepted") is False


def test_flash_requires_risk_ack_before_connect(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import flash_epas

  called = []

  class BoomTransport:
    def __init__(self, *args, **kwargs):
      called.append("transport_init")

    def connect(self):
      called.append("connect")
      raise AssertionError("must not connect without risk ack")

  monkeypatch.setattr(flash_epas, "_consume_ui_risk_ack", lambda: False)
  monkeypatch.setattr(flash_epas, "DiagnosticTransport", BoomTransport)
  monkeypatch.setattr(flash_epas, "verify_bootloader", lambda *_a, **_k: called.append("verify") or b"x")
  assert flash_epas.main([]) == 1
  assert called == []
  assert flash_epas.main(["--restore"]) == 1
  assert called == []


def test_diagnose_radar_does_not_change_safety():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.diagnose_radar import diagnose_panda

  panda = MagicMock()
  panda.can_recv.return_value = []
  transport = DiagnosticTransport(panda=panda)
  diagnose_panda(transport, sniff_s=0.0)
  panda.set_safety_mode.assert_not_called()
  transport.close()
  panda.set_safety_mode.assert_not_called()


def test_manager_stops_daemons_while_script_running():
  src = Path(__file__).resolve().parents[7] / "system" / "manager" / "manager.py"
  text = src.read_text()
  assert 'params.get_bool("NAPScriptRunning")' in text
  assert 'nap_ignore = ["pandad", "card", "controlsd", "selfdrived", "plannerd", "radard",' in text
  assert '"calibrationd", "torqued", "locationd", "modeld", "dmonitoringmodeld"]' in text
  assert "not_run=ignore + nap_ignore" in text


def test_approved_tools_include_diagnose_radar():
  assert "diagnose_radar" in APPROVED_TOOLS


def test_native_panel_keyboard_and_diagnose():
  tesla = Path(__file__).resolve().parents[7] / "selfdrive" / "ui" / "sunnypilot" / "layouts" / "settings" / "vehicle" / "brands" / "tesla.py"
  nap = Path(__file__).resolve().parents[7] / "selfdrive" / "ui" / "sunnypilot" / "layouts" / "settings" / "nap.py"
  tesla_text = tesla.read_text()
  nap_text = nap.read_text()
  assert "Diagnose Radar" not in tesla_text
  assert "Emergency Disable" not in tesla_text
  assert "coop_steering_toggle.set_visible(not is_preap)" in tesla_text
  assert "Diagnose Radar" in nap_text
  assert "Emergency Disable" in nap_text
  assert "NAPBrakeFactor" in nap_text or "BRAKE_FACTOR" in nap_text
  assert "launch_on_device_runner" in nap_text
  assert "RadarMonitorDialog" in nap_text


def test_mads_parent_reachable_while_forced():
  src = Path(__file__).resolve().parents[7] / "selfdrive" / "ui" / "sunnypilot" / "layouts" / "settings" / "steering.py"
  text = src.read_text()
  assert "ui_state.is_offroad() and self._mads_toggle.action_item.get_state()" not in text
  assert "mads_required" in text
  assert "self._mads_toggle.set_visible(True)" in text
  assert "set_visible(not is_preap)" not in text
  assert "mads_required or is_preap" in text

def test_negative_response_maps_to_transport_error():
  from opendbc.car.uds import NegativeResponseError
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.transport import fail_closed_negative_response

  with pytest.raises(TransportError, match="negative response"):
    fail_closed_negative_response(NegativeResponseError("NRC 0x22", 0x10, 0x22))


def test_flash_negative_response_fails_closed_before_write(monkeypatch):
  from opendbc.car.uds import NegativeResponseError
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import flash_epas
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.epas_integrity import FirmwareIntegrityError

  called = []

  class FakeTransport:
    def __init__(self, *args, **kwargs):
      called.append("transport_init")

    def connect(self, panda_factory=None):
      called.append("connect")
      panda = MagicMock()
      panda.can_recv.return_value = []
      return panda

    def set_diagnostic_session(self):
      called.append("diag")

    def close(self):
      called.append("close")

  def boom(*_args, **_kwargs):
    called.append("extract")
    raise NegativeResponseError("NRC 0x7F service not supported", 0x10, 0x7F)

  def missing_packaged():
    raise FirmwareIntegrityError("packaged firmware missing")

  monkeypatch.setattr(flash_epas, "verify_bootloader", lambda *_a, **_k: called.append("verify") or b"x")
  monkeypatch.setattr(flash_epas, "require_preap_tool_start", lambda **_k: None)
  monkeypatch.setattr(flash_epas, "_consume_ui_risk_ack", lambda: True)
  monkeypatch.setattr(flash_epas, "DiagnosticTransport", FakeTransport)
  monkeypatch.setattr(flash_epas, "extract_firmware", boom)
  monkeypatch.setattr(flash_epas, "flash_bootloader", lambda *_a, **_k: called.append("flash_bl"))
  monkeypatch.setattr(flash_epas, "flash_firmware", lambda *_a, **_k: called.append("flash_fw"))
  monkeypatch.setattr(flash_epas, "load_stock_firmware", missing_packaged)
  monkeypatch.setattr(flash_epas.os.path, "exists", lambda *_a, **_k: False)

  assert flash_epas.main(["--accept-risk"]) == 1
  assert "verify" in called
  assert "connect" in called
  assert "extract" in called
  assert "flash_bl" not in called
  assert "flash_fw" not in called
  assert "close" in called




def test_runtime_path_requires_basedir(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import require_runtime_path, ToolSafetyError
  require_runtime_path()
  monkeypatch.setattr("openpilot.common.basedir.BASEDIR", "/tmp/not-openpilot")
  with pytest.raises(ToolSafetyError, match="BASEDIR"):
    require_runtime_path()


def test_runtime_path_rejects_copied_module(tmp_path):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import require_runtime_path, ToolSafetyError
  copied = tmp_path / "flash_epas.py"
  copied.write_text("# copied")
  with pytest.raises(ToolSafetyError, match="production path"):
    require_runtime_path(copied)


def test_run_script_rejects_unapproved_and_requires_offroad():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script import prepare_run, APPROVED_MODULES
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner import RUN_SCRIPT_MODULE, APPROVED_TOOLS
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import ToolSafetyError
  assert RUN_SCRIPT_MODULE.endswith("run_script")
  assert "run_script" not in APPROVED_TOOLS
  with pytest.raises(ValueError, match="unapproved"):
    prepare_run("scripts.nap.radar_replay", FakeParams(offroad=True))
  with pytest.raises(ToolSafetyError, match="offroad"):
    prepare_run(APPROVED_TOOLS["diagnose_radar"], FakeParams(offroad=False))
  assert prepare_run(APPROVED_TOOLS["calibrate_pedal"], FakeParams(offroad=False)) in APPROVED_MODULES
  assert prepare_run(APPROVED_TOOLS["diagnose_radar"], FakeParams(offroad=True)) in APPROVED_MODULES



def test_negative_response_fail_closed():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.transport import (
    TransportError,
    fail_closed_negative_response,
    uds_fail_closed,
  )
  class NegativeResponseError(Exception):
    error_code = 0x7F
  with pytest.raises(TransportError, match="negative response"):
    fail_closed_negative_response(NegativeResponseError("0x22"))
  wrapped = uds_fail_closed(NegativeResponseError("0x22"))
  assert isinstance(wrapped, TransportError)
  assert "fail closed" in str(wrapped)

def test_follow_scroll_offset_pins_overflow_to_bottom():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script import follow_scroll_offset
  assert follow_scroll_offset(2, 45, 200) == 0.0
  assert follow_scroll_offset(10, 45, 200) == -(10 * 45 - 200)



def test_run_script_not_in_yaml():
  src = Path(__file__).resolve().parents[6] / "sunnylink" / "settings_ui_src" / "pages" / "vehicle.yaml"
  text = src.read_text()
  assert "run_script" not in text
  assert "NAPRadarOffset" not in text


def test_direct_destructive_main_requires_confirm(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import calibrate_pedal, calibrate_radar
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import parse_explicit_confirmation

  assert parse_explicit_confirmation([]) is False
  assert parse_explicit_confirmation(None) is False
  assert parse_explicit_confirmation(["--confirm"]) is True
  assert parse_explicit_confirmation(["--other"]) is False

  seen = {}

  def fake_run(*, confirmed, **kwargs):
    seen["confirmed"] = confirmed
    return 0

  monkeypatch.setattr(calibrate_pedal, "run", fake_run)
  assert calibrate_pedal.main([]) == 0
  assert seen["confirmed"] is False
  assert calibrate_pedal.main(["--confirm"]) == 0
  assert seen["confirmed"] is True

  seen.clear()
  monkeypatch.setattr(calibrate_radar, "run", fake_run)
  assert calibrate_radar.main([]) == 0
  assert seen["confirmed"] is False
  assert calibrate_radar.main(["--confirm"]) is not None
  assert seen["confirmed"] is True


def test_start_tool_passes_confirm_flag_for_destructive(monkeypatch):
  captured = {}

  def fake_popen(cmd, **kwargs):
    captured["cmd"] = cmd
    return MagicMock()

  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.subprocess.Popen",
    fake_popen,
  )
  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.threading.Thread",
    lambda **_kwargs: MagicMock(),
  )
  start_tool("calibrate_pedal", confirmed=True, params=FakeParams())
  assert "--confirm" in captured["cmd"]
  start_tool("diagnose_radar", confirmed=True, params=FakeParams())
  assert "--confirm" not in captured["cmd"]


def test_start_tool_clears_runtime_flags_when_child_exits(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import runner

  params = FakeParams()
  process = MagicMock()
  captured = {}

  class FakeThread:
    def __init__(self, *, target, args, daemon):
      captured.update(target=target, args=args, daemon=daemon)

    def start(self):
      captured["started"] = True

  monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
  monkeypatch.setattr(runner.threading, "Thread", FakeThread)

  assert runner.start_tool("flash_epas", confirmed=True, params=params) is process
  assert params.get_bool("NAPScriptRunning") is True
  assert params.get_bool("NAPEpasRiskAccepted") is True
  assert captured["started"] is True
  assert captured["daemon"] is True

  captured["target"](*captured["args"])
  process.wait.assert_called_once_with()
  assert params.get_bool("NAPScriptRunning") is False
  assert params.get_bool("NAPEpasRiskAccepted") is False


def test_start_tool_allows_second_launch_without_exclusive_lock(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import runner

  params = FakeParams()
  params.put_bool("NAPScriptRunning", True)
  monkeypatch.setattr(runner.subprocess, "Popen", MagicMock(return_value=MagicMock()))
  monkeypatch.setattr(runner.threading, "Thread", lambda **_kwargs: MagicMock())

  assert runner.start_tool("diagnose_radar", confirmed=True, params=params) is not None
  runner.subprocess.Popen.assert_called_once()


def test_stop_tool_fail_closed_leaves_script_running(monkeypatch):
  import subprocess
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import runner

  params = FakeParams()
  params.put_bool("NAPScriptRunning", True)
  process = MagicMock()
  process.poll.return_value = None
  process.wait.side_effect = subprocess.TimeoutExpired(cmd="tool", timeout=1)
  runner.stop_tool(process, params)
  assert params.get_bool("NAPScriptRunning") is True


def test_epas_firmware_image_present():
  data = load_stock_firmware()
  assert len(data) == FW_SIZE == 258048
  assert hashlib.md5(data).hexdigest() == FW_MD5SUM
  assert hashlib.sha256(data).hexdigest() == FW_SHA256


def test_epas_firmware_rejects_wrong_payload():
  with pytest.raises(FirmwareIntegrityError):
    verify_firmware(b"not-the-epas-image")


def test_start_tool_constructs_default_params_for_native_ui(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import runner

  params = FakeParams()
  process = MagicMock()
  monkeypatch.setattr(runner, "Params", lambda: params)
  monkeypatch.setattr(runner, "require_preap_tool_start", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: process)
  monkeypatch.setattr(runner.threading, "Thread", lambda **_kwargs: MagicMock())

  assert runner.start_tool("diagnose_radar", confirmed=True) is process
  assert params.get_bool("NAPScriptRunning") is True


def test_launch_on_device_runner_spawns_run_script_not_the_tool(monkeypatch):
  captured = {}

  def fake_popen(cmd, **kwargs):
    captured["cmd"] = cmd
    return MagicMock()

  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.subprocess.Popen",
    fake_popen,
  )
  launch_on_device_runner("Pedal Calibration", "calibrate_pedal", "hold brake", params=FakeParams())
  assert captured["cmd"][1:3] == ["-m", RUN_SCRIPT_MODULE]
  assert captured["cmd"][3] == "Pedal Calibration"
  assert captured["cmd"][4] == APPROVED_TOOLS["calibrate_pedal"]
  assert "calibrate_pedal --confirm" not in " ".join(captured["cmd"])


def test_spawn_approved_module_confirms_destructive_and_sets_script_lock(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script import spawn_approved_module

  captured = {}

  def fake_popen(cmd, **kwargs):
    captured["cmd"] = cmd
    return MagicMock()

  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script.subprocess.Popen",
    fake_popen,
  )
  params = FakeParams()
  spawn_approved_module(APPROVED_TOOLS["calibrate_pedal"], params)
  assert "--confirm" in captured["cmd"]
  assert params.get_bool("NAPScriptRunning") is True


def test_flash_parser_accepts_runner_confirmation_flag():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import flash_epas

  assert flash_epas.parse_args(["--confirm"]).accept_risk is True


def test_napbrakefactor_is_registered_for_grayed_ui():
  src = Path(__file__).resolve().parents[7] / "common" / "params_keys.h"
  assert '"NAPBrakeFactor"' in src.read_text()



class PedalCalibParams:
  def __init__(self, offroad=True, enabled=True, bus=2):
    self.store = {"IsOffroad": offroad, "NAPPedalEnabled": enabled, "NAPPedalCanBus": bus}

  def get_bool(self, key):
    return bool(self.store.get(key, False))

  def put_bool(self, key, value, block=True):
    self.store[key] = bool(value)

  def get(self, key):
    return self.store.get(key)

  def put(self, key, value, block=True):
    self.store[key] = value



def test_parse_configured_pedal_bus_preserves_zero_and_defaults_empty():
  assert parse_configured_pedal_bus(0) == 0
  assert parse_configured_pedal_bus("0") == 0
  assert parse_configured_pedal_bus(b"0") == 0
  assert parse_configured_pedal_bus(None) == 2
  assert parse_configured_pedal_bus("") == 2
  assert parse_configured_pedal_bus(b"") == 2
  assert parse_configured_pedal_bus(2) == 2


def test_transport_pedal_calibration_programs_legal_params():
  handle = UsbHandle()
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  transport.set_pedal_calibration_session(2)
  panda.set_safety_mode.assert_any_call(SAFETY_SILENT, 0)
  panda.set_safety_mode.assert_any_call(SAFETY_TESLA_PREAP, PREAP_FLAG_PEDAL_CALIBRATION)
  assert handle.writes[-1][0] == 0xf3
  assert handle.writes[-1][1:] == (0, 0, HEARTBEAT_TIMEOUT_MS)

  transport.set_pedal_calibration_session(0)
  panda.set_safety_mode.assert_called_with(
    SAFETY_TESLA_PREAP, PREAP_FLAG_PEDAL_CALIBRATION | PREAP_FLAG_PEDAL_BUS_ZERO,
  )
  with pytest.raises(TransportError, match="invalid pedal bus"):
    transport.set_pedal_calibration_session(1)


def test_transport_rejects_elm327_alloutput_and_mixed_preap():
  panda = MagicMock()
  transport = DiagnosticTransport(panda=panda)
  transport.set_diagnostic_session()
  with pytest.raises(TransportError, match="teslaPreap"):
    transport.can_send(0x551, bytes(6), 2)
  panda.can_send.assert_not_called()
  with pytest.raises(TransportError, match="bypass"):
    transport._set_safety_mode(SAFETY_ALLOUTPUT)
  transport._set_safety_mode(SAFETY_TESLA_PREAP, PREAP_FLAG_PEDAL_CALIBRATION)
  with pytest.raises(TransportError, match="longitudinal"):
    transport._set_safety_mode(SAFETY_TESLA_PREAP, PREAP_FLAG_PEDAL_CALIBRATION | PREAP_FLAG_ENABLE_PEDAL)


def test_parse_pedal_calibration_args_bus_optional():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import (
    parse_pedal_calibration_args,
  )
  confirmed, bus = parse_pedal_calibration_args(["--confirm"])
  assert confirmed is True
  assert bus is None
  confirmed, bus = parse_pedal_calibration_args(["--confirm", "--bus", "0"])
  assert confirmed is True
  assert bus == 0
  confirmed, bus = parse_pedal_calibration_args(["--bus", "2"])
  assert confirmed is False
  assert bus == 2


class RecordingParams(FakeParams):
  def __init__(self, offroad=True):
    super().__init__(offroad=offroad)
    self.puts = []

  def put(self, key, value, block=True):
    self.puts.append(key)
    self.store[key] = value

  def put_bool(self, key, value, block=True):
    self.puts.append(key)
    self.store[key] = bool(value)


class FakeSubMaster:
  def __init__(self, *, started=False, engaged=False, v_ego=0.0, car_fresh=True,
               processes=None, device_seen=True, device_fresh=None,
               selfdrive_fresh=True, mads=None, mads_fresh=True,
               can_msgs=None, can_fresh=False):
    if device_fresh is None:
      device_fresh = device_seen
    self.seen = {
      "deviceState": device_seen,
      "selfdriveState": True,
      "carState": car_fresh,
      "managerState": processes is not None,
    }
    self.alive = {
      "deviceState": bool(device_seen and device_fresh),
      "selfdriveState": selfdrive_fresh,
      "carState": car_fresh,
      "managerState": processes is not None,
    }
    self.valid = dict(self.alive)
    self.data = {
      "deviceState": type("DS", (), {"started": started})(),
      "selfdriveState": type("SS", (), {"enabled": engaged})(),
      "carState": type("CS", (), {"vEgo": v_ego})(),
      "managerState": type("MS", (), {"processes": processes or []})(),
    }
    if mads is not None:
      self.data["selfdriveStateSP"] = type("SP", (), {
        "mads": type("M", (), {"enabled": mads})(),
      })()
      self.seen["selfdriveStateSP"] = True
      self.alive["selfdriveStateSP"] = mads_fresh
      self.valid["selfdriveStateSP"] = mads_fresh
    if can_msgs is not None:
      self.data["can"] = can_msgs
      self.seen["can"] = True
      self.alive["can"] = can_fresh
      self.valid["can"] = can_fresh

  def __getitem__(self, name):
    return self.data[name]

  def update(self, timeout=0):
    return None


def test_pedal_entry_offroad_without_carstate_waits():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import (
    pedal_calibration_entry_reason,
  )
  assert pedal_calibration_entry_reason(
    offroad=True, engaged=False, car_state_fresh=False, v_ego=20.0,
  ) is not None


def test_pedal_entry_rejects_moving_and_engaged_and_unknown():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import (
    pedal_calibration_entry_reason,
  )
  assert pedal_calibration_entry_reason(
    offroad=False, engaged=False, car_state_fresh=True, v_ego=5.0,
  ) is not None
  assert pedal_calibration_entry_reason(
    offroad=False, engaged=True, car_state_fresh=True, v_ego=0.0,
  ) is not None
  assert pedal_calibration_entry_reason(
    offroad=False, engaged=False, car_state_fresh=False, v_ego=0.0,
  ) is not None
  assert pedal_calibration_entry_reason(
    offroad=False, engaged=False, car_state_fresh=True, v_ego=0.0,
  ) is None
  assert pedal_calibration_entry_reason(
    offroad=True, engaged=False, car_state_fresh=True, v_ego=0.0,
  ) is None


def test_pedal_entry_rejects_invalid_speed_even_when_fresh():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import pedal_calibration_entry_reason

  for speed in (float("nan"), float("inf"), -1.0):
    assert pedal_calibration_entry_reason(
      offroad=True, engaged=False, car_state_fresh=True, v_ego=speed,
    ) is not None


def test_admission_from_submaster_missing_and_stale_fail_closed():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import admission_from_submaster
  assert admission_from_submaster(None) is not None
  assert admission_from_submaster(FakeSubMaster(device_seen=False, car_fresh=True)) is not None
  assert admission_from_submaster(
    FakeSubMaster(device_seen=True, device_fresh=False, started=False, car_fresh=True),
  ) is not None
  assert admission_from_submaster(FakeSubMaster(started=False, car_fresh=False)) is not None
  assert admission_from_submaster(FakeSubMaster(started=True, car_fresh=False)) is not None
  assert admission_from_submaster(
    FakeSubMaster(started=True, selfdrive_fresh=False, car_fresh=True, v_ego=0.0),
  ) is not None
  assert admission_from_submaster(
    FakeSubMaster(started=True, engaged=True, car_fresh=True, v_ego=0.0),
  ) is not None
  assert admission_from_submaster(
    FakeSubMaster(started=True, engaged=False, mads=True, car_fresh=True, v_ego=0.0),
  ) is not None
  assert admission_from_submaster(
    FakeSubMaster(started=True, mads=False, mads_fresh=False, car_fresh=True, v_ego=0.0),
  ) is not None
  assert admission_from_submaster(
    FakeSubMaster(started=True, v_ego=0.0, car_fresh=True),
  ) is None
  assert admission_from_submaster(
    FakeSubMaster(started=False, v_ego=0.0, car_fresh=True),
  ) is None


def test_admission_uses_fresh_bus0_esp_b_when_carstate_missing():
  from opendbc.can import CANPacker
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import admission_from_submaster

  addr, dat, src = CANPacker("tesla_preap").make_can_msg("ESP_B", 0, {"ESP_vehicleSpeed": 0.0})
  msg = type("Can", (), {"address": addr, "dat": dat, "src": src})()
  assert admission_from_submaster(FakeSubMaster(
    started=False, car_fresh=False, can_msgs=[msg], can_fresh=True,
  )) is None
  moving, dat_m, src_m = CANPacker("tesla_preap").make_can_msg("ESP_B", 0, {"ESP_vehicleSpeed": 5.0})
  moving_msg = type("Can", (), {"address": moving, "dat": dat_m, "src": src_m})()
  assert admission_from_submaster(FakeSubMaster(
    started=False, car_fresh=False, can_msgs=[moving_msg], can_fresh=True,
  )) is not None
  wrong_bus = type("Can", (), {"address": addr, "dat": dat, "src": 2})()
  assert admission_from_submaster(FakeSubMaster(
    started=False, car_fresh=False, can_msgs=[wrong_bus], can_fresh=True,
  )) is not None

def test_esp_b_zero_speed_is_standstill():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import esp_b_speed_ms
  assert esp_b_speed_ms(bytes(8)) == 0.0
  assert esp_b_speed_ms(bytes(6)) is None
  assert esp_b_speed_ms(b"\x00") is None


def test_esp_b_speed_matches_dbc_packer_not_byte4():
  from opendbc.can import CANPacker
  from opendbc.car.common.conversions import Conversions as CV
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import esp_b_speed_ms

  addr, dat, _bus = CANPacker("tesla_preap").make_can_msg("ESP_B", 0, {"ESP_vehicleSpeed": 1.0})
  assert addr == 0x155
  assert len(dat) >= 7
  assert dat[5] == 0
  assert dat[6] == 100
  wrong = ((dat[5] << 8) | dat[4]) * 0.01
  assert wrong == 0.0
  speed = esp_b_speed_ms(dat)
  assert speed is not None
  assert abs(speed - 1.0 * CV.KPH_TO_MS) < 1e-6



def test_persist_calibration_invalidates_done_then_commits(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import persist_calibration
  params = RecordingParams()
  persist_calibration(params, min_v=10.0, max_v=90.0, factor=1.25, zero=12.0, bus=0)
  assert params.puts[0] == "NAPPedalCalibDone"
  assert params.store["NAPPedalCalibDone"] is True
  assert params.puts[-1] == "NAPPedalCalibDone"
  assert "NAPPedalCalibMin" in params.puts
  assert params.puts.index("NAPPedalCanBus") < params.puts.index("NAPPedalEnabled")
  assert params.puts.index("NAPPedalEnabled") < len(params.puts) - 1
  assert params.get_bool("NAPPedalEnabled") is True
  assert params.store["NAPPedalCanBus"] == 0


def test_persist_calibration_failure_does_not_leave_done_true():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import persist_calibration

  class BoomParams(RecordingParams):
    def put(self, key, value, block=True):
      super().put(key, value, block=block)
      if key == "NAPPedalCalibFactor":
        raise RuntimeError("disk full")

  params = BoomParams()
  params.put_bool("NAPPedalCalibDone", True)
  params.put("NAPPedalCanBus", 2)
  params.puts.clear()
  with pytest.raises(RuntimeError, match="disk full"):
    persist_calibration(params, min_v=10.0, max_v=90.0, factor=1.25, zero=12.0, bus=0)
  assert params.store.get("NAPPedalCalibMin") == 10.0
  assert params.store.get("NAPPedalCanBus") == 2
  assert params.get_bool("NAPPedalCalibDone") is False


def test_persist_without_bus_does_not_write_can_bus():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import persist_calibration
  params = RecordingParams()
  persist_calibration(params, min_v=10.0, max_v=90.0, factor=1.25, zero=12.0)
  assert "NAPPedalCanBus" not in params.puts



def test_require_calibration_health_aborts_silent_without_rearm():
  handle = UsbHandle(health=_health_bytes(safety_mode=SAFETY_SILENT))
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  with pytest.raises(TransportError, match="SILENT"):
    transport.require_calibration_health(SAFETY_TESLA_PREAP, PREAP_FLAG_PEDAL_CALIBRATION)
  panda.set_safety_mode.assert_not_called()
  assert handle.reads[-1] == (0xd2, type(panda).HEALTH_STRUCT.size, HEALTH_TIMEOUT_MS)


def test_require_calibration_health_aborts_watchdog_loss_without_rearm():
  handle = UsbHandle(health=_health_bytes(
    safety_mode=SAFETY_TESLA_PREAP,
    safety_param=PREAP_FLAG_PEDAL_CALIBRATION,
    heartbeat_lost=1,
  ))
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  with pytest.raises(TransportError, match="watchdog"):
    transport.require_calibration_health(SAFETY_TESLA_PREAP, PREAP_FLAG_PEDAL_CALIBRATION)
  panda.set_safety_mode.assert_not_called()


def test_heartbeat_defaults_false_false():
  handle = UsbHandle()
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  transport.send_heartbeat()
  assert handle.writes == [(0xf3, 0, 0, HEARTBEAT_TIMEOUT_MS)]


def test_usb_io_fails_closed_without_handle():
  panda = MagicMock()
  transport = DiagnosticTransport(panda=panda)
  with pytest.raises(TransportError, match="usb handle"):
    transport.send_heartbeat()
  with pytest.raises(TransportError, match="usb handle"):
    transport.read_health()
  with pytest.raises(TransportError, match="usb handle"):
    transport.can_recv()
  panda.send_heartbeat.assert_not_called()
  panda.health.assert_not_called()
  panda.can_recv.assert_not_called()


def test_processes_released_requires_named_daemons_stopped():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner import processes_released

  class Proc:
    def __init__(self, name, running):
      self.name = name
      self.running = running

  state = type("MS", (), {"processes": [
    Proc("pandad", False), Proc("card", False), Proc("controlsd", False), Proc("ui", True),
  ]})()
  assert processes_released(state) is True
  state.processes[0].running = True
  assert processes_released(state) is False


def test_wait_for_manager_release_times_out():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner import wait_for_manager_release
  sm = FakeSubMaster(processes=[type("P", (), {"name": "pandad", "running": True})()])
  with pytest.raises(ToolSafetyError, match="timed out"):
    wait_for_manager_release(sm, timeout_s=0.0, sleep=lambda _s: None, now=lambda: 1.0)


def test_reap_leaves_script_running_while_started():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import runner
  params = FakeParams(offroad=False)
  params.put_bool("NAPScriptRunning", True)
  process = MagicMock()
  process.wait.return_value = 0
  runner._reap_tool(process, params)
  assert params.get_bool("NAPScriptRunning") is True

  params = FakeParams(offroad=True)
  params.put_bool("NAPScriptRunning", True)
  runner._reap_tool(process, params)
  assert params.get_bool("NAPScriptRunning") is False


def test_early_handoff_failure_clears_flag(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import calibrate_pedal
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import ToolSafetyError

  params = PedalCalibParams(offroad=False, enabled=False)
  params.put_bool("NAPScriptRunning", True)

  def boom():
    raise ToolSafetyError("timed out waiting for driving processes to stop")

  monkeypatch.setattr(calibrate_pedal, "wait_for_manager_release", boom)
  with pytest.raises(ToolSafetyError):
    calibrate_pedal.run(confirmed=True, params=params)
  assert params.get_bool("NAPScriptRunning") is False


def test_usb_claimed_failure_does_not_clear_flag(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import calibrate_pedal

  params = PedalCalibParams(offroad=False, enabled=False)
  params.put_bool("NAPScriptRunning", True)

  class ClaimedTransport:
    def __init__(self, panda=None):
      self.panda = panda

    def connect(self, panda_factory=None):
      return self.panda

    def set_pedal_calibration_session(self, bus=2):
      raise TransportError("unexpected SILENT safety mode")

    def set_silent(self):
      return None

    def can_send(self, *args, **kwargs):
      return None

    def close(self):
      return None

  monkeypatch.setattr(calibrate_pedal, "wait_for_manager_release", lambda *a, **k: None)
  with pytest.raises(PedalCalibrationError, match="SILENT"):
    calibrate_pedal.run(
      confirmed=True,
      params=params,
      transport=cast(calibrate_pedal.DiagnosticTransport, ClaimedTransport()),
    )
  assert params.get_bool("NAPScriptRunning") is True


def _packed(name, bus, values):
  from opendbc.can import CANPacker
  return CANPacker("tesla_preap").make_can_msg(name, bus, values)


def _stationary_vehicle_frames(pedal_bus=2):
  frames = [
    _packed("ESP_B", 0, {"ESP_vehicleSpeed": 0.0}),
    _packed("GTW_status", 0, {"GTW_driveRailReq": 1}),
    _packed("BrakeMessage", 0, {"driverBrakeStatus": 2}),
    _packed("DI_torque2", 0, {"DI_gear": 3}),
    _packed("DI_torque1", 0, {"DI_pedalPos": 0}),
  ]
  frames.append(_packed("GAS_SENSOR", pedal_bus, {"STATE": 0, "INTERCEPTOR_GAS": 0, "INTERCEPTOR_GAS2": 0}))
  return frames


def _make_calibrator(frames, bus=2, monkeypatch=None, t_ms=10_000):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import PedalCalibrator
  if monkeypatch is not None:
    monkeypatch.setattr(
      "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal.current_time_ms",
      lambda: t_ms,
    )
  transport = MagicMock()
  transport.can_recv.return_value = frames
  return PedalCalibrator(PedalCalibParams(), transport, bus)


def test_process_can_ignores_wrong_bus_and_requires_fresh_sources(monkeypatch):

  cal = _make_calibrator([_packed("ESP_B", 2, {"ESP_vehicleSpeed": 0.0})], bus=2, monkeypatch=monkeypatch)
  cal.process_can()
  assert cal.speed_seen_ms == 0
  assert cal.check_safety() is False

  cal = _make_calibrator(_stationary_vehicle_frames(pedal_bus=2), bus=2, monkeypatch=monkeypatch)
  cal.process_can()
  assert cal.check_safety() is True
  assert cal.car_on is True
  assert cal.brake_pressed is True
  assert cal.gear_neutral is True
  assert cal.last_pedal_seen_ms > 0

  cal = _make_calibrator(_stationary_vehicle_frames(pedal_bus=2), bus=0, monkeypatch=monkeypatch)
  cal.process_can()
  assert cal.last_pedal_seen_ms == 0


def test_check_safety_rejects_low_speed_invalid_brake_and_stale_ignition(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import (
    PedalCalibrationError, SOURCE_TIMEOUT_MS,
  )

  moving = _stationary_vehicle_frames()
  moving[0] = _packed("ESP_B", 0, {"ESP_vehicleSpeed": 5.0})
  cal = _make_calibrator(moving, monkeypatch=monkeypatch)
  cal.process_can()
  assert cal.check_safety() is False
  cal.pedal_enabled = 1
  with pytest.raises(PedalCalibrationError, match="moving"):
    cal.check_safety()

  invalid = _stationary_vehicle_frames()
  invalid[2] = _packed("BrakeMessage", 0, {"driverBrakeStatus": 0})
  cal = _make_calibrator(invalid, monkeypatch=monkeypatch)
  cal.process_can()
  assert cal.brake_valid is False
  assert cal.check_safety() is False
  cal.pedal_enabled = 1
  with pytest.raises(PedalCalibrationError, match="invalid brake"):
    cal.check_safety()

  released = _stationary_vehicle_frames()
  released[2] = _packed("BrakeMessage", 0, {"driverBrakeStatus": 1})
  cal = _make_calibrator(released, monkeypatch=monkeypatch)
  cal.process_can()
  assert cal.brake_valid is True
  assert cal.brake_pressed is False
  assert cal.check_safety() is False

  t = [10_000]
  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal.current_time_ms",
    lambda: t[0],
  )
  cal = _make_calibrator(_stationary_vehicle_frames())
  cal.process_can()
  assert cal.check_safety() is True
  t[0] = 10_000 + SOURCE_TIMEOUT_MS + 1
  cal.transport.can_recv.return_value = [
    _packed("ESP_B", 0, {"ESP_vehicleSpeed": 0.0}),
    _packed("BrakeMessage", 0, {"driverBrakeStatus": 2}),
    _packed("DI_torque2", 0, {"DI_gear": 3}),
    _packed("DI_torque1", 0, {"DI_pedalPos": 0}),
  ]
  cal.process_can()
  cal.pedal_enabled = 1
  with pytest.raises(PedalCalibrationError, match="ignition lost"):
    cal.check_safety()


def test_stale_accelerator_blocks_enable_ramp(monkeypatch):
  frames = [
    _packed("ESP_B", 0, {"ESP_vehicleSpeed": 0.0}),
    _packed("GTW_status", 0, {"GTW_driveRailReq": 1}),
    _packed("BrakeMessage", 0, {"driverBrakeStatus": 2}),
    _packed("DI_torque2", 0, {"DI_gear": 3}),
  ]
  cal = _make_calibrator(frames, monkeypatch=monkeypatch)
  cal.process_can()
  cal.status = 2
  assert cal.accel_seen_ms == 0
  assert cal.check_safety() is False


def test_heartbeat_stays_false_false_past_watchdog():
  handle = UsbHandle(health=_health_bytes(
    safety_mode=SAFETY_TESLA_PREAP,
    safety_param=PREAP_FLAG_PEDAL_CALIBRATION,
  ))
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  transport._mode = SAFETY_TESLA_PREAP
  transport._param = PREAP_FLAG_PEDAL_CALIBRATION
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import PedalCalibrator
  cal = PedalCalibrator(PedalCalibParams(), transport, 2)
  for _ in range(600):
    cal._tick_watchdog()
  heartbeats = [w for w in handle.writes if w[0] == 0xf3]
  assert len(heartbeats) == 600
  assert all(w[1:] == (0, 0, HEARTBEAT_TIMEOUT_MS) for w in heartbeats)
  panda.set_safety_mode.assert_not_called()


def test_heartbeat_clamps_true_true_in_tesla_preap():
  handle = UsbHandle()
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  transport._mode = SAFETY_TESLA_PREAP
  transport.send_heartbeat(True, True)
  assert handle.writes == [(0xf3, 0, 0, HEARTBEAT_TIMEOUT_MS)]


def test_can_recv_retries_are_bounded():
  handle = UsbHandle(fail="bulk")
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  with pytest.raises(TransportError, match="can_recv"):
    transport.can_recv()
  assert len(handle.bulk) == CAN_RECV_RETRIES
  assert all(b == (1, 16384, CAN_RECV_TIMEOUT_MS) for b in handle.bulk)


def test_can_recv_unpacks_serialized_frames():
  from panda.python import pack_can_buffer
  packed = bytes(pack_can_buffer([(0x155, bytes(8), 0)])[0])
  handle = UsbHandle(can=packed)
  panda = _usb_panda(handle)
  msgs = DiagnosticTransport(panda=panda).can_recv()
  assert msgs[0][0] == 0x155
  assert msgs[0][2] == 0
  assert handle.bulk == [(1, 16384, CAN_RECV_TIMEOUT_MS)]


def test_heartbeat_usb_failure_is_bounded():
  handle = UsbHandle(fail="write")
  panda = _usb_panda(handle)
  transport = DiagnosticTransport(panda=panda)
  with pytest.raises(TransportError, match="heartbeat"):
    transport.send_heartbeat()
  assert handle.writes == [(0xf3, 0, 0, HEARTBEAT_TIMEOUT_MS)]


def test_connect_failure_after_handoff_leaves_flag(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import calibrate_pedal

  params = PedalCalibParams(offroad=False, enabled=False)
  params.put_bool("NAPScriptRunning", True)

  class BoomConnect:
    def connect(self, *args, **kwargs):
      raise TransportError("panda busy")

    def set_silent(self):
      return None

    def can_send(self, *args, **kwargs):
      return None

    def close(self):
      return None

  monkeypatch.setattr(calibrate_pedal, "wait_for_manager_release", lambda *a, **k: None)
  with pytest.raises(PedalCalibrationError, match="panda busy"):
    calibrate_pedal.run(
      confirmed=True,
      params=params,
      transport=cast(calibrate_pedal.DiagnosticTransport, BoomConnect()),
    )
  assert params.get_bool("NAPScriptRunning") is True
  assert params.store.get("NAPPedalCanBus") == 2
  assert params.get_bool("NAPPedalEnabled") is False


def test_run_explicit_bus_does_not_persist_before_success(monkeypatch):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import calibrate_pedal

  recorded = {}

  class FakeTransport:
    def connect(self, *args, **kwargs):
      return None

    def set_pedal_calibration_session(self, bus=2):
      recorded["session_bus"] = bus

    def set_silent(self):
      return None

    def close(self):
      return None

  class FakeCalibrator:
    def __init__(self, params, transport, bus):
      recorded["cal_bus"] = bus

    def run(self):
      return 0

    def cleanup(self):
      return None

  params = PedalCalibParams(bus=2)
  monkeypatch.setattr(calibrate_pedal, "wait_for_manager_release", lambda *a, **k: None)
  monkeypatch.setattr(calibrate_pedal, "PedalCalibrator", FakeCalibrator)
  assert calibrate_pedal.run(
      confirmed=True,
      params=params,
      transport=cast(calibrate_pedal.DiagnosticTransport, FakeTransport()),
      bus=0,
  ) == 0
  assert recorded["session_bus"] == 0
  assert recorded["cal_bus"] == 0
  assert params.store["NAPPedalCanBus"] == 2


def test_processes_released_rejects_missing_names():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner import processes_released
  empty = type("MS", (), {"processes": []})()
  assert processes_released(empty) is False



def test_pedal_ready_lines_include_bus_setup():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script import pedal_ready_lines
  params = PedalCalibParams(bus=0)
  sm = FakeSubMaster(started=False)
  lines = pedal_ready_lines(sm, params)
  assert any("bus: 0" in line.lower() or "bus: 0" in line for line in lines)
  assert any("interceptor toggle" in line.lower() for line in lines)


def test_launch_on_device_runner_allows_pedal_ignition_on(monkeypatch):
  captured = {}

  def fake_popen(cmd, **kwargs):
    captured["cmd"] = cmd
    return MagicMock()

  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.subprocess.Popen",
    fake_popen,
  )
  launch_on_device_runner(
    "Pedal Calibration", "calibrate_pedal", "hold brake",
    params=FakeParams(offroad=False),
  )
  assert captured["cmd"][2].endswith("run_script")


def test_start_tool_flash_still_requires_offroad(monkeypatch):
  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner.subprocess.Popen",
    lambda *_a, **_k: MagicMock(),
  )
  with pytest.raises(ToolSafetyError, match="offroad"):
    start_tool("flash_epas", confirmed=True, params=FakeParams(offroad=False))


def test_pedal_safe_exit_requires_restart_unless_ignition_off():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script import pedal_safe_exit_decision
  assert pedal_safe_exit_decision(False) == "clear"
  assert pedal_safe_exit_decision(True) == "restart"
  assert pedal_safe_exit_decision(None) == "restart"


def test_ignition_from_health_unknown_without_keys():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.transport import ignition_from_health
  assert ignition_from_health({}) is None
  assert ignition_from_health({"ignition_line": 0, "ignition_can": 0}) is False
  assert ignition_from_health({"ignition_line": 1, "ignition_can": 0}) is True


def test_sample_panda_ignition_always_closes():
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.transport import sample_panda_ignition
  closed = []

  class Probe:
    def connect(self, retries=3, delay=0.2):
      return None

    def read_health(self):
      return {"ignition_line": 0, "ignition_can": 0}

    def close(self):
      closed.append(True)

  assert sample_panda_ignition(transport_factory=Probe) is False
  assert closed == [True]

  closed.clear()

  class BoomProbe:
    def connect(self, retries=3, delay=0.2):
      raise RuntimeError("busy")

    def close(self):
      closed.append(True)

  assert sample_panda_ignition(transport_factory=BoomProbe) is None
  assert closed == [True]


def test_reap_prints_error_when_flag_held(capsys):
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import runner
  params = FakeParams(offroad=False)
  params.put_bool("NAPScriptRunning", True)
  process = MagicMock()
  process.wait.return_value = 0
  runner._reap_tool(process, params)
  assert params.get_bool("NAPScriptRunning") is True
  assert "NAPScriptRunning left set" in capsys.readouterr().out


