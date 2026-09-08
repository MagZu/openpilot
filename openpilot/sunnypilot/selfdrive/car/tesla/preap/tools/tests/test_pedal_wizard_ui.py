"""Pedal wizard controller: Start, Cancel, and Exit through injected seams."""
from __future__ import annotations

import io
import subprocess

from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner import APPROVED_TOOLS
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script import (
  ScriptRunnerController,
  ScriptState,
  WAITING_SIGNALS,
  main,
  pedal_safe_exit_decision,
  spawn_approved_module,
)


PEDAL_MODULE = APPROVED_TOOLS["calibrate_pedal"]
FLASH_MODULE = APPROVED_TOOLS["flash_epas"]


class FakeParams:
  def __init__(self, offroad=True, bus=2):
    self.store = {"IsOffroad": offroad, "NAPPedalCanBus": bus, "NAPScriptRunning": False}

  def get_bool(self, key):
    return bool(self.store.get(key, False))

  def put_bool(self, key, value, block=True):
    self.store[key] = bool(value)

  def get(self, key):
    return self.store.get(key)

  def put(self, key, value, block=True):
    self.store[key] = value


class FakeSubMaster:
  def __init__(self, *, block_reason=None, explode=False):
    self.block_reason = block_reason
    self.explode = explode
    self.updates = 0

  def update(self, timeout=0):
    self.updates += 1
    if self.explode:
      raise RuntimeError("submaster unavailable")


class FakeProcess:
  def __init__(self, running=True):
    self._code = None if running else 0
    self.stdout = io.StringIO("")

  def poll(self):
    return self._code

  def wait(self, timeout=None):
    if self._code is None:
      if timeout is None:
        raise RuntimeError("FakeProcess does not support unbounded wait while running")
      raise subprocess.TimeoutExpired("fake", timeout)
    return self._code

  def finish(self, code=0):
    self._code = code


def _controller(*, sm=None, spawn=None, stop=None, ignition=True, params=None,
                hosted=True, admission=None, module=PEDAL_MODULE, threads=None):
  popped = []
  closed = []
  rebooted = []
  pending = threads if threads is not None else []
  params = params or FakeParams(offroad=False, bus=2)

  def fake_admission(submaster):
    if submaster is None:
      raise AssertionError("admission must not run without a SubMaster")
    return submaster.block_reason

  def fake_spawn(module_name, params_obj=None, *, bus=None):
    params_obj.put_bool("NAPScriptRunning", True, block=True)
    proc = FakeProcess()
    spawn_calls.append({"module": module_name, "bus": bus, "proc": proc})
    return proc

  def fake_stop(proc):
    proc.finish(1)
    return True

  spawn_calls = []

  ctrl = ScriptRunnerController(
    title="Pedal Calibration",
    module=module,
    instructions="hold brake",
    params=params,
    hosted=hosted,
    admission_fn=admission or fake_admission,
    spawn_fn=spawn or fake_spawn,
    stop_child_fn=stop or fake_stop,
    sample_ignition_fn=(ignition if callable(ignition) else lambda: ignition),
    clear_fn=lambda p: p.put_bool("NAPScriptRunning", False, block=True),
    submaster_factory=lambda: sm,
    pop_fn=lambda: popped.append("pop"),
    close_fn=lambda: closed.append("close"),
    reboot_fn=lambda: rebooted.append("reboot"),
    start_thread=lambda fn: pending.append(fn),
  )
  ctrl._spawn_calls = spawn_calls
  ctrl._popped = popped
  ctrl._closed = closed
  ctrl._rebooted = rebooted
  ctrl._pending = pending
  return ctrl


def test_start_without_submaster_refuses_and_does_not_mark():
  params = FakeParams(offroad=False)
  ctrl = _controller(sm=None, params=params)
  ctrl._sm = None
  assert ctrl.start_enabled is False
  assert ctrl.start_block_reason == WAITING_SIGNALS
  assert ctrl.start() == WAITING_SIGNALS
  assert ctrl.state == ScriptState.READY
  assert params.get_bool("NAPScriptRunning") is False
  assert ctrl._spawn_calls == []
  assert ctrl.handoff_requested is False


def test_ready_status_banner_shows_admission_not_instruction_tail():
  params = FakeParams(offroad=False)
  sm = FakeSubMaster(block_reason="waiting for stationary vehicle state")
  ctrl = _controller(sm=sm, params=params)
  ctrl.instructions = "\n".join(["hold brake"] * 80)
  assert ctrl.ready_status_banner == "Waiting for stationary vehicle state"
  assert ctrl.start_enabled is False
  sm.block_reason = None
  assert ctrl.ready_status_banner.startswith("Ready:")
  assert ctrl.start_enabled is True


def test_start_admission_reason_does_not_mark_or_spawn():
  params = FakeParams(offroad=False)
  sm = FakeSubMaster(block_reason="vehicle must be stationary")
  ctrl = _controller(sm=sm, params=params)
  assert ctrl.start() == "vehicle must be stationary"
  assert ctrl.state == ScriptState.READY
  assert params.get_bool("NAPScriptRunning") is False
  assert ctrl._spawn_calls == []


def test_start_admits_then_spawns_with_local_bus_before_flag_side_effects():
  params = FakeParams(offroad=False, bus=2)
  order = []
  sm = FakeSubMaster(block_reason=None)

  def admission(submaster):
    order.append("admission")
    assert params.get_bool("NAPScriptRunning") is False
    return None

  def spawn(module_name, params_obj=None, *, bus=None):
    order.append(("spawn", bus))
    params_obj.put_bool("NAPScriptRunning", True, block=True)
    return FakeProcess()

  ctrl = _controller(sm=sm, params=params, admission=admission, spawn=spawn)
  ctrl.select_bus(0)
  assert ctrl.start() is None
  assert ctrl.state == ScriptState.RUNNING
  assert order == ["admission", ("spawn", 0)]
  assert params.get_bool("NAPScriptRunning") is True
  assert ctrl.action_label == "Cancel"
  assert ctrl.action_enabled is True


def test_ready_exit_hosted_pops_without_reboot_or_clear():
  params = FakeParams(offroad=False)
  ctrl = _controller(sm=FakeSubMaster(), params=params, hosted=True)
  assert ctrl.request_exit() == "exited"
  assert ctrl._popped == ["pop"]
  assert ctrl._closed == []
  assert ctrl._rebooted == []
  assert params.get_bool("NAPScriptRunning") is False


def test_cancel_stops_child_and_does_not_clear_after_handoff():
  params = FakeParams(offroad=False)
  ctrl = _controller(sm=FakeSubMaster(), params=params, ignition=True)
  assert ctrl.start() is None
  assert params.get_bool("NAPScriptRunning") is True
  ctrl.cancel()
  proc = ctrl._spawn_calls[0]["proc"]
  proc.finish(1)
  ctrl._pending[0]()
  ctrl.pump()
  assert params.get_bool("NAPScriptRunning") is True
  assert ctrl.state == ScriptState.ERROR
  assert ctrl.probe_inflight is False
  assert ctrl.action_label == "Check ignition / Exit"
  assert ctrl.restart_visible is True
  assert ctrl.output_lines.count("[Cancelled]") == 1
  assert sum("Turn the car off" in line for line in ctrl.output_lines) == 1
  assert not any("exited with code" in line for line in ctrl.output_lines)
  assert ctrl._popped == []


def test_exit_blocked_while_probe_inflight():
  params = FakeParams(offroad=False)
  ctrl = _controller(sm=FakeSubMaster(), params=params, ignition=False)
  ctrl.start()
  proc = ctrl._spawn_calls[0]["proc"]
  proc.finish(0)
  ctrl._pending[0]()
  ctrl.pump()
  assert ctrl.request_exit() == "probing"
  assert ctrl.probe_inflight is True
  assert ctrl.action_enabled is False
  assert ctrl.restart_enabled is False
  assert ctrl.request_exit() == "blocked"
  assert params.get_bool("NAPScriptRunning") is True
  assert ctrl._popped == []


def test_safe_exit_after_ignition_off_clears_and_pops():
  params = FakeParams(offroad=False)
  ctrl = _controller(sm=FakeSubMaster(), params=params, ignition=False, hosted=True)
  ctrl.start()
  proc = ctrl._spawn_calls[0]["proc"]
  proc.finish(0)
  ctrl._pending[0]()
  ctrl.pump()
  assert ctrl.request_exit() == "probing"
  ctrl._pending[-1]()
  ctrl.pump()
  assert pedal_safe_exit_decision(False) == "clear"
  assert params.get_bool("NAPScriptRunning") is False
  assert ctrl._popped == ["pop"]
  assert ctrl._rebooted == []


def test_restart_device_is_explicit_and_never_automatic():
  params = FakeParams(offroad=False)
  ctrl = _controller(sm=FakeSubMaster(), params=params, ignition=True, hosted=True)
  ctrl.start()
  proc = ctrl._spawn_calls[0]["proc"]
  proc.finish(0)
  ctrl._pending[0]()
  ctrl.pump()
  assert ctrl._rebooted == []
  assert ctrl.restart_visible is True
  assert ctrl.activate_action() == "probing"
  assert ctrl._rebooted == []
  assert ctrl.restart_device() == "blocked"
  ctrl._pending[-1]()
  ctrl.pump()
  assert ctrl.restart_required is True
  assert ctrl.restart_device() == "reboot"
  assert ctrl._rebooted == ["reboot"]
  assert params.get_bool("NAPScriptRunning") is True
  assert ctrl._popped == []


def test_exit_after_car_off_following_on_probe_does_not_reboot():
  params = FakeParams(offroad=False)
  ignition = {"on": True}
  ctrl = _controller(sm=FakeSubMaster(), params=params, ignition=lambda: ignition["on"], hosted=True)
  ctrl.start()
  proc = ctrl._spawn_calls[0]["proc"]
  proc.finish(0)
  ctrl._pending[0]()
  ctrl.pump()
  assert ctrl.action_label == "Check ignition / Exit"
  assert ctrl.restart_visible is True
  assert ctrl.request_exit() == "probing"
  ctrl._pending[-1]()
  ctrl.pump()
  assert params.get_bool("NAPScriptRunning") is True
  assert ctrl._popped == []
  assert ctrl.restart_required is True
  assert ctrl.action_label == "Check ignition / Exit"
  assert ctrl.restart_visible is True
  ignition["on"] = False
  assert ctrl.request_exit() == "probing"
  ctrl._pending[-1]()
  ctrl.pump()
  assert params.get_bool("NAPScriptRunning") is False
  assert ctrl._popped == ["pop"]
  assert ctrl._rebooted == []

def test_main_refuses_pedal_without_tmux_or_window(monkeypatch):
  ran = []
  monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
  code = main(["run_script.py", "Pedal Calibration", PEDAL_MODULE, "hold brake"])
  assert code == 1
  assert ran == []


def test_spawn_passes_bus_flag(monkeypatch):
  captured = {}

  def fake_popen(cmd, **kwargs):
    captured["cmd"] = cmd
    return FakeProcess()

  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script.subprocess.Popen",
    fake_popen,
  )
  monkeypatch.setattr(
    "openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.run_script.require_runtime_path",
    lambda: None,
  )
  params = FakeParams(offroad=True)
  spawn_approved_module(PEDAL_MODULE, params, bus=0)
  assert captured["cmd"][-2:] == ["--bus", "0"]
  assert "--confirm" in captured["cmd"]
  assert params.get_bool("NAPScriptRunning") is True


def test_child_fail_after_handoff_does_not_clear():
  params = FakeParams(offroad=False)

  def boom(module_name, params_obj=None, *, bus=None):
    params_obj.put_bool("NAPScriptRunning", True, block=True)
    raise OSError("exec failed")

  ctrl = _controller(sm=FakeSubMaster(), params=params, spawn=boom)
  ctrl.start()
  assert ctrl.state == ScriptState.ERROR
  assert ctrl.restart_required is True
  assert params.get_bool("NAPScriptRunning") is True
  assert ctrl._popped == []
