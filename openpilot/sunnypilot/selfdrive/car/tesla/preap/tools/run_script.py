#!/usr/bin/env python3
"""Production Pre-AP script runner.

Non-pedal tools take over the Comma screen from a standalone process.
Pedal calibration is hosted in the existing UI as a Widget: no second DRM
client, no tmux kill. Start admits on a live SubMaster before setting
NAPScriptRunning. Safe Exit pops the widget; Restart device is explicit.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from openpilot.common.basedir import BASEDIR
from openpilot.common.hardware import HARDWARE, PC
from openpilot.common.params import Params
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.calibrate_pedal import parse_configured_pedal_bus
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.runner import (
  APPROVED_TOOLS,
  clear_tool_flags,
  mark_script_running,
  stop_child,
)
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.safety import (
  DESTRUCTIVE_TOOLS,
  PEDAL_IGNITION_ON_TOOLS,
  ToolSafetyError,
  admission_from_submaster,
  require_offroad,
  require_runtime_path,
)
from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools.transport import sample_panda_ignition


APPROVED_MODULES = frozenset(APPROVED_TOOLS.values())
WAITING_SIGNALS = "waiting for vehicle signals"


def waiting_status_text(reason: str) -> str:
  text = (reason or "").strip()
  if not text:
    return ""
  if text.lower().startswith("waiting"):
    return text[0].upper() + text[1:]
  return f"Waiting: {text}"


def _pedal_submaster():
  from openpilot.cereal import messaging
  services = ["deviceState", "pandaStates", "carState", "selfdriveState", "managerState", "can"]
  try:
    from openpilot.cereal.services import SERVICE_LIST
    if "selfdriveStateSP" in SERVICE_LIST:
      services.append("selfdriveStateSP")
  except Exception:
    pass
  return messaging.SubMaster(services)


def pedal_ready_lines(sm, params: Params, selected_bus: int | None = None) -> list[str]:
  sm.update(0)
  bus = parse_configured_pedal_bus(selected_bus if selected_bus is not None else params.get("NAPPedalCanBus"))
  lines = [
    f"Pedal CAN bus: {bus}.",
    "Confirm the comma pedal connector is seated. This step talks to the pedal on the selected bus.",
    "First calibration does not require the interceptor toggle.",
  ]
  reason = admission_from_submaster(sm)
  if reason:
    lines.append(waiting_status_text(reason))
  elif params.get_bool("IsOffroad"):
    lines.append("Ready: parked. Ignition on, Neutral, hold brake, then Start.")
  else:
    lines.append("Ready: stationary and disengaged. Start begins calibration.")
  return lines


def pedal_safe_exit_decision(ignition: bool | None) -> str:
  """After the child has released USB: clear only on observed ignition off."""
  if ignition is False:
    return "clear"
  return "restart"


def follow_scroll_offset(line_count: int, line_height: float, bounds_height: float) -> float:
  overflow = line_count * line_height - bounds_height
  return -overflow if overflow > 0 else 0.0


class ScriptState:
  READY = 0
  RUNNING = 1
  COMPLETED = 2
  ERROR = 3


def tool_name_for_module(module: str) -> str:
  return next(name for name, path in APPROVED_TOOLS.items() if path == module)


def prepare_run(module: str, params: Params | None = None) -> str:
  """Validate module and runtime path. Offroad except pedal calibration."""
  require_runtime_path()
  params = params or Params()
  if module not in APPROVED_MODULES:
    raise ValueError(f"unapproved tool: {module}")
  if tool_name_for_module(module) not in PEDAL_IGNITION_ON_TOOLS:
    require_offroad(params)
  return module


def spawn_approved_module(module: str, params: Params | None = None, *, bus: int | None = None) -> subprocess.Popen:
  module = prepare_run(module, params)
  params = params or Params()
  tool = next(name for name, path in APPROVED_TOOLS.items() if path == module)
  if tool in ("flash_epas", "restore_epas"):
    params.put_bool("NAPEpasRiskAccepted", True, block=True)
  env = os.environ.copy()
  env["PYTHONPATH"] = env.get("PYTHONPATH", BASEDIR)
  cmd = [sys.executable, "-m", module]
  if tool in DESTRUCTIVE_TOOLS:
    cmd.append("--confirm")
  if tool in PEDAL_IGNITION_ON_TOOLS and bus is not None:
    if bus not in (0, 2):
      raise ValueError("invalid pedal bus")
    cmd.extend(["--bus", str(int(bus))])
  mark_script_running(params)
  try:
    return subprocess.Popen(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      cwd=str(Path(BASEDIR)),
      start_new_session=True,
      env=env,
      text=True,
      bufsize=1,
    )
  except Exception:
    if tool not in PEDAL_IGNITION_ON_TOOLS:
      clear_tool_flags(params)
    raise


def _default_reboot() -> None:
  if not PC:
    HARDWARE.reboot()


class ScriptRunnerController:
  """Start / Cancel / Exit / Restart for an approved tool.

  Pedal Start fail-closes without a live SubMaster. Admission runs before
  NAPScriptRunning. After that flag is set, child failure does not clear it.
  USB ignition probe runs only after the child is confirmed gone.
  """

  def __init__(
    self,
    *,
    title: str,
    module: str,
    instructions: str,
    params: Params | None = None,
    hosted: bool = False,
    admission_fn=admission_from_submaster,
    spawn_fn=spawn_approved_module,
    stop_child_fn=stop_child,
    sample_ignition_fn=sample_panda_ignition,
    mark_fn=mark_script_running,
    clear_fn=clear_tool_flags,
    submaster_factory=_pedal_submaster,
    pop_fn=None,
    close_fn=None,
    reboot_fn=None,
    start_thread=None,
  ):
    self.title = title
    self._module = module
    self.instructions = instructions
    self._params = params or Params()
    self.hosted = hosted
    self._is_pedal = tool_name_for_module(module) in PEDAL_IGNITION_ON_TOOLS
    self._admission = admission_fn
    self._spawn = spawn_fn
    self._stop_child = stop_child_fn
    self._sample_ignition = sample_ignition_fn
    self._mark = mark_fn
    self._clear = clear_fn
    self._pop_fn = pop_fn
    self._close_fn = close_fn
    self._reboot_fn = reboot_fn or _default_reboot
    self._start_thread = start_thread or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    self.state = ScriptState.READY
    self.output_lines: list[str] = []
    self._events: queue.Queue = queue.Queue()
    self._process = None
    self.selected_bus = parse_configured_pedal_bus(self._params.get("NAPPedalCanBus"))
    if self.selected_bus not in (0, 2):
      self.selected_bus = 2
    self.restart_required = False
    self.probe_inflight = False
    self.probe_done = False
    self._probe_started = False
    self.handoff_requested = False
    self._ignore_done = False
    self._last_ignition: bool | None = None
    self._sm = None
    if self._is_pedal:
      try:
        self._sm = submaster_factory()
      except Exception:
        self._sm = None

  @property
  def is_pedal(self) -> bool:
    return self._is_pedal

  @property
  def start_block_reason(self) -> str | None:
    if self.state != ScriptState.READY or not self._is_pedal:
      return None
    if self._sm is None:
      return WAITING_SIGNALS
    try:
      self._sm.update(0)
      return self._admission(self._sm)
    except Exception:
      return WAITING_SIGNALS

  @property
  def start_enabled(self) -> bool:
    return self.state == ScriptState.READY and self.start_block_reason is None

  @property
  def ready_status_banner(self) -> str:
    """Fixed READY status. Admission reason is not the last scroll line."""
    if self.state != ScriptState.READY or not self._is_pedal:
      return ""
    reason = self.start_block_reason
    if reason:
      return waiting_status_text(reason)
    if self._params.get_bool("IsOffroad"):
      return "Ready: parked. Ignition on, Neutral, hold brake, then Start."
    return "Ready: stationary and disengaged. Start begins calibration."

  @property
  def status_banner(self) -> str:
    if self.state == ScriptState.READY:
      return self.ready_status_banner
    if self.probe_inflight:
      return "Checking ignition..."
    if self.restart_visible:
      return "Turn the car off, then Check ignition / Exit."
    return ""

  @property
  def action_label(self) -> str:
    if self.probe_inflight:
      return "Exit"
    if self.state == ScriptState.RUNNING and self._is_pedal:
      return "Cancel"
    if self._is_pedal and self.state in (ScriptState.COMPLETED, ScriptState.ERROR):
      return "Check ignition / Exit"
    return "Exit"

  @property
  def action_enabled(self) -> bool:
    if self.probe_inflight:
      return False
    if self.state == ScriptState.RUNNING and not self._is_pedal:
      return False
    return True

  @property
  def restart_visible(self) -> bool:
    if not self._is_pedal or not self.handoff_requested:
      return False
    if self.state not in (ScriptState.COMPLETED, ScriptState.ERROR):
      return False
    if self._process is not None and self._process.poll() is None:
      return self.restart_required
    return True

  @property
  def restart_enabled(self) -> bool:
    return self.restart_visible and not self.probe_inflight

  def ready_lines(self) -> list[str]:
    lines = self.instructions.split("\n")
    if not self._is_pedal:
      return lines
    lines.append("")
    if self._sm is None:
      lines.extend([
        f"Pedal CAN bus: {self.selected_bus}.",
        "Confirm the comma pedal connector is seated. This step talks to the pedal on the selected bus.",
        "First calibration does not require the interceptor toggle.",
        waiting_status_text(WAITING_SIGNALS),
      ])
      return lines
    lines.extend(pedal_ready_lines(self._sm, self._params, selected_bus=self.selected_bus))
    return lines

  def display_lines(self) -> list[str]:
    if self.state == ScriptState.READY:
      return self.ready_lines()
    return list(self.output_lines)

  def select_bus(self, bus: int) -> None:
    if self.state != ScriptState.READY or not self._is_pedal:
      return
    if bus not in (0, 2):
      return
    self.selected_bus = bus

  def start(self) -> str | None:
    if self.state != ScriptState.READY:
      return "not ready"
    if self._is_pedal:
      if self._sm is None:
        return WAITING_SIGNALS
      try:
        self._sm.update(0)
        reason = self._admission(self._sm)
      except Exception:
        return WAITING_SIGNALS
      if reason:
        return reason
    self.state = ScriptState.RUNNING
    self.output_lines = ["Starting script...", ""]
    self.handoff_requested = True
    self._ignore_done = False
    bus = self.selected_bus if self._is_pedal else None
    try:
      self._process = self._spawn(self._module, self._params, bus=bus)
      self._start_thread(self._read_output)
    except Exception as exc:
      if not self._is_pedal:
        self._clear(self._params)
      self.output_lines.append(f"Error starting script: {exc}")
      self.state = ScriptState.ERROR
      if self._is_pedal:
        self.restart_required = True
    return None

  def cancel(self) -> None:
    if self.state != ScriptState.RUNNING or not self._is_pedal:
      return
    if self._process is not None and self._process.poll() is None:
      if not self._stop_child(self._process):
        self.output_lines.append("[ERROR] script did not exit; manager will stay paused")
        self.output_lines.append("Use Restart device to recover.")
        self.state = ScriptState.ERROR
        self.restart_required = True
        return
    self._ignore_done = True
    self.state = ScriptState.ERROR
    self.output_lines.append("[Cancelled]")
    self.output_lines.append("Turn the car off, then Check ignition / Exit. Restart device is optional.")

  def request_exit(self) -> str:
    if self.probe_inflight:
      return "blocked"
    if self.state == ScriptState.RUNNING:
      if self._is_pedal:
        self.cancel()
        return "cancel"
      return "locked"
    if self.state == ScriptState.READY:
      self._leave_ready()
      return "exited"
    if self._is_pedal:
      if self._process is not None and self._process.poll() is None:
        return "blocked"
      self._begin_probe()
      return "probing"
    self._clear(self._params)
    self._close()
    self._reboot_fn()
    return "exited"

  def restart_device(self) -> str:
    if self.probe_inflight or not self.restart_visible:
      return "blocked"
    self._reboot_fn()
    return "reboot"

  def activate_action(self) -> str:
    return self.request_exit()

  def pump(self) -> bool:
    got_new = False
    while True:
      try:
        kind, payload = self._events.get_nowait()
      except queue.Empty:
        break
      got_new = True
      if kind == "line":
        self.output_lines.append(payload)
      elif kind == "done":
        if self._ignore_done:
          continue
        code = payload
        if code == 0:
          self.output_lines.append("[Script completed successfully]")
          self.state = ScriptState.COMPLETED
        else:
          self.output_lines.append(f"[Script exited with code {code}]")
          self.state = ScriptState.ERROR
        if self._is_pedal:
          self.output_lines.append("Turn the car off, then Check ignition / Exit. Restart device is optional.")
      elif kind == "probe":
        self.probe_inflight = False
        self.probe_done = True
        self._last_ignition = payload
        if self._is_pedal and pedal_safe_exit_decision(payload) == "clear":
          self._clear(self._params)
          self._pop_hosted_or_close()
        elif self._is_pedal:
          self.restart_required = True
          self.output_lines.append("Ignition still on. Turn the car off and Check ignition / Exit, or Restart device.")
    return got_new

  def _begin_probe(self) -> None:
    if not self._is_pedal or self.probe_inflight:
      return
    if self._process is not None and self._process.poll() is None:
      return
    self.probe_inflight = True

    def run():
      ignition = None
      try:
        ignition = self._sample_ignition()
      except Exception:
        ignition = None
      self._events.put(("probe", ignition))

    self._start_thread(run)


  def _read_output(self) -> None:
    try:
      if self._process is not None and self._process.stdout:
        for line in iter(self._process.stdout.readline, ""):
          if line:
            self._events.put(("line", line.rstrip()))
      code = 1
      if self._process is not None:
        code = self._process.wait()
      self._events.put(("done", code))
    except Exception as exc:
      self._events.put(("line", f"[Error reading output: {exc}]"))
      self._events.put(("done", 1))

  def _leave_ready(self) -> None:
    if self.hosted:
      self._pop()
      return
    self._clear(self._params)
    self._close()
    if not self._is_pedal:
      self._reboot_fn()

  def _pop_hosted_or_close(self) -> None:
    if self.hosted:
      self._pop()
    else:
      self._close()

  def _pop(self) -> None:
    if self._pop_fn is not None:
      self._pop_fn()
      return
    from openpilot.system.ui.lib.application import gui_app
    gui_app.pop_widget()

  def _close(self) -> None:
    if self._close_fn is not None:
      self._close_fn()
      return
    from openpilot.system.ui.lib.application import gui_app
    gui_app.request_close()


def make_script_runner_widget(title: str, module: str, instructions: str, *,
                              hosted: bool = False, controller: ScriptRunnerController | None = None,
                              **ctrl_kwargs):
  """Widget wrapping ScriptRunnerController. Imports pyray only when constructed."""
  import pyray as rl
  from openpilot.system.ui.lib.application import gui_app, FontWeight
  from openpilot.system.ui.lib.scroll_panel import GuiScrollPanel
  from openpilot.system.ui.widgets import Widget
  from openpilot.system.ui.widgets.button import Button, ButtonStyle

  ctrl = controller or ScriptRunnerController(
    title=title, module=module, instructions=instructions, hosted=hosted, **ctrl_kwargs,
  )

  class ScriptRunnerWidget(Widget):
    def __init__(self):
      super().__init__()
      self.controller = ctrl
      self._scroll_panel = GuiScrollPanel()
      self._font = None
      self._title_font = None
      self._start_button = self._child(Button("Start", click_callback=self._on_start,
                                              button_style=ButtonStyle.PRIMARY))
      self._action_button = self._child(Button("Exit", click_callback=self._on_action,
                                               button_style=ButtonStyle.TRANSPARENT_WHITE_BORDER))
      self._bus0_button = self._child(Button("Bus 0", click_callback=lambda: self.controller.select_bus(0)))
      self._bus2_button = self._child(Button("Bus 2", click_callback=lambda: self.controller.select_bus(2)))

    def _on_start(self):
      if self.controller.restart_visible:
        self.controller.restart_device()
      else:
        self.controller.start()

    def _on_action(self):
      self.controller.request_exit()

    def _tune(self, compact: bool) -> None:
      font = 16 if compact else 45
      pad = 4 if compact else 16
      radius = 8 if compact else 10
      for btn in (self._start_button, self._action_button, self._bus0_button, self._bus2_button):
        btn._label.set_font_size(font)
        btn._label._text_padding = pad
        btn._border_radius = radius

    def _render(self, rect):
      compact = rect.width < 800 and rect.height < 500
      self._tune(compact)
      got_new = self.controller.pump()
      if self._font is None:
        self._font = gui_app.font(FontWeight.NORMAL)
        self._title_font = gui_app.font(FontWeight.BOLD)

      rl.draw_rectangle_rec(rect, rl.Color(20, 20, 20, 255))
      margin = 8 if compact else 50
      title_font_size = 18 if compact else 70
      text_font_size = 14 if compact else 45
      output_font_size = 12 if compact else 35
      line_height = 16 if compact else 45
      button_width = 128 if compact else 350
      button_height = 44 if compact else 110
      button_spacing = 8 if compact else 30
      bus_width = 72 if compact else 160

      content_x = rect.x + margin
      current_y = rect.y + (3 if compact else margin)
      rl.draw_text_ex(self._title_font, self.controller.title, rl.Vector2(content_x, current_y),
                      title_font_size, 0, rl.WHITE)
      current_y += title_font_size + (8 if compact else margin)
      button_y = rect.y + rect.height - margin - button_height
      banner = self.controller.status_banner
      banner_h = (line_height + 8) if banner else 0
      body_bottom = button_y - margin - banner_h
      body_rect = rl.Rectangle(content_x, current_y, rect.width - margin * 2,
                               max(0.0, body_bottom - current_y))
      ready = self.controller.state == ScriptState.READY
      lines = self.controller.display_lines()
      font_size = text_font_size if ready else output_font_size
      color = rl.Color(200, 200, 200, 255) if ready else rl.WHITE
      content_rect = rl.Rectangle(0, 0, body_rect.width, len(lines) * line_height)
      if got_new and not ready:
        self._scroll_panel.set_offset(follow_scroll_offset(len(lines), line_height, body_rect.height))
      scroll = self._scroll_panel.update(body_rect, content_rect)
      rl.begin_scissor_mode(int(body_rect.x), int(body_rect.y), int(body_rect.width), int(body_rect.height))
      for i, line in enumerate(lines):
        line_y = body_rect.y + scroll + i * line_height
        if line_y + line_height < body_rect.y or line_y > body_rect.y + body_rect.height:
          continue
        rl.draw_text_ex(self._font, line, rl.Vector2(body_rect.x, line_y), font_size, 0, color)
      rl.end_scissor_mode()

      if banner:
        waiting = bool(self.controller.start_block_reason) or self.controller.probe_inflight or self.controller.restart_visible
        banner_color = rl.Color(255, 180, 80, 255) if waiting else rl.Color(128, 216, 166, 255)
        rl.draw_text_ex(self._font, banner, rl.Vector2(content_x, button_y - banner_h),
                        text_font_size, 0, banner_color)

      x_right = rect.x + rect.width - margin - button_width
      self._action_button.set_text(self.controller.action_label)
      self._action_button.set_button_style(ButtonStyle.TRANSPARENT_WHITE_BORDER)
      self._action_button.set_enabled(self.controller.action_enabled)
      self._action_button.render(rl.Rectangle(x_right, button_y, button_width, button_height))

      show_start = ready
      show_restart = self.controller.restart_visible
      if show_start or show_restart:
        if show_restart:
          self._start_button.set_text("Restart device")
          self._start_button.set_button_style(ButtonStyle.DANGER)
          self._start_button.set_enabled(self.controller.restart_enabled)
        else:
          self._start_button.set_text("Start")
          self._start_button.set_button_style(ButtonStyle.PRIMARY)
          self._start_button.set_enabled(self.controller.start_enabled)
        start_x = x_right - button_spacing - button_width
        self._start_button.render(rl.Rectangle(start_x, button_y, button_width, button_height))
      else:
        start_x = x_right

      show_bus = ready and self.controller.is_pedal
      if show_bus:
        bus2_x = start_x - button_spacing - bus_width
        bus0_x = bus2_x - button_spacing - bus_width
        self._bus0_button.set_button_style(
          ButtonStyle.PRIMARY if self.controller.selected_bus == 0 else ButtonStyle.NORMAL)
        self._bus2_button.set_button_style(
          ButtonStyle.PRIMARY if self.controller.selected_bus == 2 else ButtonStyle.NORMAL)
        self._bus0_button.render(rl.Rectangle(bus0_x, button_y, bus_width, button_height))
        self._bus2_button.render(rl.Rectangle(bus2_x, button_y, bus_width, button_height))

  return ScriptRunnerWidget()


def open_pedal_calibration(instructions: str | None = None) -> None:
  """Push the in-process pedal wizard onto the existing UI stack."""
  from openpilot.system.ui.lib.application import gui_app
  from openpilot.sunnypilot.selfdrive.car.tesla.preap.tools import instructions as preap_instructions
  text = instructions or preap_instructions.CALIBRATE_PEDAL_INSTRUCTIONS
  gui_app.push_widget(make_script_runner_widget(
    "Pedal Calibration",
    APPROVED_TOOLS["calibrate_pedal"],
    text,
    hosted=True,
  ))


def main(argv: list[str] | None = None) -> int:
  argv = list(sys.argv if argv is None else argv)
  if len(argv) < 4:
    print("Usage: run_script.py <title> <module> <instructions>")
    return 1

  title = argv[1]
  module = argv[2]
  instructions = argv[3]
  try:
    prepare_run(module)
  except (ToolSafetyError, ValueError) as exc:
    print(f"ERROR: {exc}")
    return 1

  if tool_name_for_module(module) in PEDAL_IGNITION_ON_TOOLS:
    print("ERROR: pedal calibration must run in the existing UI, not a second window")
    return 1

  import pyray as rl
  from openpilot.system.ui.lib.application import gui_app

  # tmux isn't installed on dev hosts, so swallow the FileNotFoundError.
  try:
    subprocess.run(["tmux", "kill-session", "-t", "comma"], capture_output=True)
  except FileNotFoundError:
    pass
  gui_app.init_window("Pre-AP Script Runner")
  widget = make_script_runner_widget(title, module, instructions, hosted=False)
  for _ in gui_app.render():
    widget.render(rl.Rectangle(0, 0, gui_app.width, gui_app.height))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
