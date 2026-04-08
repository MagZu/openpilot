"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.params import Params
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.vehicle.brands.base import BrandSettings
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.widgets.list_view import toggle_item_sp, multiple_button_item_sp, option_item_sp

COOP_STEERING_MIN_KMH = 23
OEM_STEERING_MIN_KMH = 48
KM_TO_MILE = 0.621371

FOLLOW_DIST_LABELS = ["1", "2", "3", "4", "5", "6", "7"]
PEDAL_BUS_LABELS = ["Bus 0", "Bus 2"]


class TeslaSettings(BrandSettings):
  def __init__(self):
    super().__init__()

    self._params = Params()
    self._is_preap = (ui_state.CP is not None and ui_state.CP.carFingerprint == "TESLA_MODEL_S_PREAP")

    # --- Standard Tesla settings ---
    self.coop_steering_toggle = toggle_item_sp(tr("Cooperative Steering (Beta)"), "", param="TeslaCoopSteering")

    # --- Pre-AP Tesla (NAP) settings ---
    if self._is_preap:
      self.pedal_toggle = toggle_item_sp(
        "Pedal Interceptor",
        "Enable Comma Pedal for direct throttle control. Requires reboot.",
        param="NAPPedalEnabled",
        enabled=ui_state.is_offroad,
      )

      self.radar_toggle = toggle_item_sp(
        "Radar",
        "Enable Bosch radar for lead car detection. Requires GTW emulation in panda safety.",
        param="NAPRadarEnabled",
        enabled=ui_state.is_offroad,
      )

      self.radar_nosecone_toggle = toggle_item_sp(
        "Radar Behind Nosecone",
        "Set radar position for GTW emulation. Enable if radar is mounted behind the nosecone.",
        param="NAPRadarBehindNosecone",
        enabled=ui_state.is_offroad,
      )

      self.follow_dist = option_item_sp(
        "Follow Distance",
        param="NAPFollowDistance",
        min_value=1,
        max_value=7,
        description="Follow distance (1=closest, 7=farthest). Can also be set via cruise stalk dial.",
      )

      pedal_bus_val = self._params.get("NAPPedalCanBus", return_default=True)
      self.pedal_bus = multiple_button_item_sp(
        "Pedal CAN Bus",
        "Select which CAN bus the Comma Pedal is connected to. Requires reboot.",
        buttons=PEDAL_BUS_LABELS,
        selected_index=0 if pedal_bus_val == 0 else 1,
        callback=self._on_pedal_bus,
      )

      self.force_preap_toggle = toggle_item_sp(
        "Force Pre-AP Mode",
        "Force fingerprint to TESLA_MODEL_S_PREAP. Disable only if running on a different Tesla.",
        param="NAPForcePreAP",
        enabled=ui_state.is_offroad,
      )

      self.items = [
        self.pedal_toggle,
        self.radar_toggle,
        self.radar_nosecone_toggle,
        self.follow_dist,
        self.pedal_bus,
        self.force_preap_toggle,
      ]
    else:
      self.items = [self.coop_steering_toggle]

  def _on_pedal_bus(self, index: int):
    self._params.put("NAPPedalCanBus", 0 if index == 0 else 2)

  def update_settings(self):
    if self._is_preap:
      # Pre-AP settings are param-backed, no dynamic updates needed
      return

    # Standard Tesla coop steering dynamic description
    is_metric = ui_state.is_metric
    unit = "km/h" if is_metric else "mph"

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

    if not ui_state.is_offroad():
      coop_steering_desc = f"<b>{coop_steering_disabled_msg}</b><br><br>{coop_steering_desc}"

    self.coop_steering_toggle.set_description(coop_steering_desc)
    self.coop_steering_toggle.action_item.set_enabled(ui_state.is_offroad())
