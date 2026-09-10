import time
import pyray as rl

from opendbc.car import structs
from openpilot.common.params import Params


def test_tesla_settings_level_callback_and_next_drive_enablement(monkeypatch):
  monkeypatch.setenv("SCALE", "1")
  from openpilot.system.ui.lib.application import gui_app
  from openpilot.selfdrive.ui.sunnypilot.layouts.settings.vehicle.brands.tesla import TeslaSettings
  from openpilot.selfdrive.ui.ui_state import ui_state

  # Callback tests need font handles, not a window or rendering resources.
  monkeypatch.setattr(gui_app, "font", lambda *_: rl.Font())
  params = Params()
  params.put_bool("TeslaPreapHandsOnPause", True, block=True)
  params.put("TeslaPreapHandsOnLevel", 2, block=True)
  monkeypatch.setattr(ui_state, "params", params)
  monkeypatch.setattr(ui_state, "started", False)
  monkeypatch.setattr(ui_state, "CP", structs.CarParams(carFingerprint="TESLA_MODEL_S_PREAP"))
  monkeypatch.setattr(ui_state, "CP_SP", structs.CarParamsSP(madsHandsOnPauseAvailable=True))

  settings = TeslaSettings()
  settings.update_settings()
  assert settings.hands_on_level.is_visible
  assert settings.hands_on_level.action_item.enabled

  for index in (2, 0, 1):
    params.remove("TeslaPreapHandsOnLevel")
    settings.hands_on_level.action_item.callback(index)
    # Wait for the real asynchronous Params write, never the previous selection.
    deadline = time.monotonic() + 5
    while (value := params.get("TeslaPreapHandsOnLevel")) is None and time.monotonic() < deadline:
      time.sleep(0.01)
    assert value == index + 1

  monkeypatch.setattr(ui_state, "started", True)
  settings.update_settings()
  assert settings.hands_on_level.is_visible
  assert not settings.hands_on_level.action_item.enabled

  monkeypatch.setattr(ui_state, "started", False)
  settings.hands_on_pause_toggle.action_item.set_state(False)
  settings.update_settings()
  assert settings.hands_on_level.is_visible
  assert not settings.hands_on_level.action_item.enabled
