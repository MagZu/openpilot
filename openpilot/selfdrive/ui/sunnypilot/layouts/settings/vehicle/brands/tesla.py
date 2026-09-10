"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from opendbc.sunnypilot.car.tesla.values import TeslaFlagsSP
from opendbc.car.tesla.preap.constants import parse_hands_on_level_param
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.vehicle.brands.base import BrandSettings
from openpilot.selfdrive.ui.ui_state import ui_state
from opendbc.car.tesla.preap.sp.platform import is_preap_ui_platform
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.widgets.list_view import multiple_button_item_sp, toggle_item_sp

COOP_STEERING_MIN_KMH = 23
OEM_STEERING_MIN_KMH = 48
KM_TO_MILE = 0.621371


def _bundle_platform() -> str:
  bundle = ui_state.params.get("CarPlatformBundle")
  if isinstance(bundle, dict):
    return str(bundle.get("platform", "") or "")
  return ""


def is_tesla_preap_ui() -> bool:
  return is_preap_ui_platform(_bundle_platform(), ui_state.CP)


class TeslaSettings(BrandSettings):
  def __init__(self):
    super().__init__()
    self.coop_steering_toggle = toggle_item_sp(tr("Cooperative Steering (Beta)"), "", param="TeslaCoopSteering")
    self.hands_on_pause_toggle = toggle_item_sp(
      tr("Hands-On Pause"),
      tr(
        "Off: takeover disengages. On: pause steering only; resume 1 second after release. Canceled cruise stays off. Applies next drive.",
      ),
      param="TeslaPreapHandsOnPause",
      callback=self._on_pause_changed,
    )
    level = parse_hands_on_level_param(ui_state.params.get("TeslaPreapHandsOnLevel", return_default=True))
    self.hands_on_level = multiple_button_item_sp(
      title=lambda: tr("Hands-On Level"),
      description=tr(
        "Which EPAS hands-on level pauses steering. 1 is lightest, 3 is heaviest. Default 2. Applies on the next drive.",
      ),
      buttons=["1", "2", "3"],
      selected_index=level - 1,
      callback=self._on_hands_on_level,
      inline=False,
    )
    self.mads_screen_button = multiple_button_item_sp(
      title=lambda: tr("MADS Screen Activation"),
      description="",
      buttons=[lambda: tr("Off"), lambda: tr("3-Finger"), lambda: tr("4-Finger"), lambda: tr("5-Finger")],
      param="TeslaMadsScreenButton",
      inline=False,
    )
    self.items = [self.coop_steering_toggle, self.hands_on_pause_toggle, self.hands_on_level, self.mads_screen_button]

  def _on_pause_changed(self, _state):
    self.update_settings()

  def _on_hands_on_level(self, index: int) -> None:
    if 0 <= index <= 2:
      ui_state.params.put("TeslaPreapHandsOnLevel", index + 1)

  def update_settings(self):
    is_metric = ui_state.is_metric
    unit = "km/h" if is_metric else "mph"
    offroad = ui_state.is_offroad()
    is_preap = is_tesla_preap_ui()

    display_value_coop = COOP_STEERING_MIN_KMH if is_metric else round(COOP_STEERING_MIN_KMH * KM_TO_MILE)
    display_value_oem = OEM_STEERING_MIN_KMH if is_metric else round(OEM_STEERING_MIN_KMH * KM_TO_MILE)

    coop_steering_disabled_msg = tr("Enable \"Always Offroad\" in Device panel, or turn vehicle off to toggle.")
    coop_steering_warning = tr(f"Warning: May experience steering oscillations below {display_value_oem} {unit} during turns, " +
                               "recommend disabling this feature if you experience these.")
    coop_steering_desc = (
      f"<b>{coop_steering_warning}</b><br><br>" +
      f"{tr('Allows the driver to provide limited steering input while openpilot is engaged.')}<br>" +
      f"{tr(f'Only works above {display_value_coop} {unit}.')}"
    )
    if not offroad:
      coop_steering_desc = f"<b>{coop_steering_disabled_msg}</b><br><br>{coop_steering_desc}"

    self.coop_steering_toggle.set_description(coop_steering_desc)
    self.coop_steering_toggle.action_item.set_enabled(offroad)
    self.coop_steering_toggle.set_visible(not is_preap)
    pause_available = ui_state.CP_SP is not None and bool(getattr(ui_state.CP_SP, "madsHandsOnPauseAvailable", False))
    self.hands_on_pause_toggle.set_visible(is_preap and pause_available)
    self.hands_on_pause_toggle.action_item.set_enabled(offroad)

    pause_on = bool(self.hands_on_pause_toggle.action_item.get_state())
    level = parse_hands_on_level_param(ui_state.params.get("TeslaPreapHandsOnLevel", return_default=True))
    self.hands_on_level.action_item.set_selected_button(level - 1)
    level_visible = bool(is_preap and pause_available)
    self.hands_on_level.set_visible(level_visible)
    self.hands_on_level.action_item.set_enabled(level_visible and pause_on and offroad)
    level_desc = tr(
      "Which EPAS hands-on level pauses steering. 1 is lightest, 3 is heaviest. Default 2. Applies on the next drive.",
    )
    if not offroad:
      level_desc = (
        f"<b>{tr('Enable \"Always Offroad\" in Device panel, or turn vehicle off to change.')}</b><br><br>{level_desc}"
      )
    elif not pause_on:
      level_desc = f"<b>{tr('Enable Hands-On Pause to change the level.')}</b><br><br>{level_desc}"
    self.hands_on_level.set_description(level_desc)

    mads_screen_button_desc = (
      f"{tr('Use a multi-finger press on the infotainment screen to toggle MADS.')} " +
      f"{tr('This allows the use of full MADS functionality when enabled.')}<br><br>" +
      f"{tr('Selecting a higher finger count may reduce accidental activations.')}<br><br>" +
      f"<b>{tr('Note: Setting this to Off will reset your MADS settings to default.')}</b>"
    )
    if not offroad:
      mads_screen_button_disabled_msg = tr("Enable \"Always Offroad\" in Device panel, or turn vehicle off to change.")
      mads_screen_button_desc = f"<b>{mads_screen_button_disabled_msg}</b><br><br>{mads_screen_button_desc}"
    self.mads_screen_button.set_description(mads_screen_button_desc)
    has_vehicle_bus = ui_state.CP_SP is not None and bool(ui_state.CP_SP.flags & TeslaFlagsSP.HAS_VEHICLE_BUS)
    self.mads_screen_button.set_visible((not is_preap) and has_vehicle_bus)
    self.mads_screen_button.action_item.set_enabled(offroad)
