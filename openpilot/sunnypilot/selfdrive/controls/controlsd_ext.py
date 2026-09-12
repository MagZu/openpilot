"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import time

import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log, custom

from opendbc.car import structs
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.livedelay.helpers import get_lat_delay
from openpilot.sunnypilot.modeld_v2.modeld_base import ModelStateBase
from openpilot.sunnypilot.selfdrive.controls.lib.blinker_pause_lateral import BlinkerPauseLateral
from openpilot.sunnypilot.selfdrive.controls.lib.latcontrol_torque_v0 import LatControlTorque as LatControlTorqueV0


class ControlsExt(ModelStateBase):
  def __init__(self, CP: structs.CarParams, params: Params):
    ModelStateBase.__init__(self)
    self.CP = CP
    self.params = params
    self._param_update_time: float = 0.0
    self.blinker_pause_lateral = BlinkerPauseLateral()

    cloudlog.info("controlsd_ext is waiting for CarParamsSP")
    self.CP_SP = messaging.log_from_bytes(params.get("CarParamsSP", block=True), custom.CarParamsSP)
    cloudlog.info("controlsd_ext got CarParamsSP")

    self.sm_services_ext = ['radarState', 'selfdriveStateSP', 'longitudinalPlanSP']
    self.pm_services_ext = ['carControlSP']

  def initialize_lateral_control(self, lac, CI, dt):
    enforce_torque_control = self.params.get_bool("EnforceTorqueControl")
    torque_versions = self.params.get("TorqueControlTune")
    if not enforce_torque_control:
      if self.CP.lateralTuning.which() == 'torque':
        return LatControlTorqueV0(self.CP, self.CP_SP, CI, dt)  # FIXME-SP: revert when upstream fixes tuning issues with v1
      return lac

    if torque_versions == 0.0:  # v0
      return LatControlTorqueV0(self.CP, self.CP_SP, CI, dt)
    else:
      return lac

  def get_params_sp(self, sm: messaging.SubMaster) -> None:
    if time.monotonic() - self._param_update_time > PARAMS_UPDATE_PERIOD:
      self.blinker_pause_lateral.get_params()

      if self.CP.lateralTuning.which() == 'torque':
        self.lat_delay = get_lat_delay(self.params, sm["liveDelay"].lateralDelay)

      self._param_update_time = time.monotonic()

  def get_lat_active(self, sm: messaging.SubMaster) -> bool:
    if self.blinker_pause_lateral.update(sm['carState']):
      return False

    ss_sp = sm['selfdriveStateSP']
    if ss_sp.mads.available:
      return bool(ss_sp.mads.active)

    # MADS not available, use stock state to engage
    return bool(sm['selfdriveState'].active)

  @staticmethod
  def get_lead_data(_lead, src: log.RadarState.LeadData) -> None:
    _lead.dRel = src.dRel
    _lead.yRel = src.yRel
    _lead.vRel = src.vRel
    _lead.aRel = src.deprecated.aRel
    _lead.vLead = src.vLead
    _lead.dPath = src.deprecated.dPath
    _lead.vLat = src.deprecated.vLat
    _lead.vLeadK = src.vLeadK
    _lead.aLeadK = src.aLeadK
    _lead.fcw = src.deprecated.fcw
    _lead.status = src.present
    _lead.aLeadTau = src.aLeadTau
    _lead.modelProb = src.modelProb
    _lead.radar = src.radar
    _lead.radarTrackId = src.radarTrackId

  # NAP Buddy IC integration. The instrument cluster draws the path from a cubic
  # in car coordinates, so fit modelV2's predicted path here where the model is
  # available and pass only the coefficients to the car layer. The IC renders at
  # 2x scale, hence IC_LANE_SCALE.
  NAP_BUDDY_IC_LANE_SCALE = 0.5
  NAP_BUDDY_PATH_LENGTH_M = 100.0

  @staticmethod
  def get_nap_buddy_lanes(lanes, md) -> None:
    """Fill CarControlSP.napBuddyLanes from modelV2. Leaves valid False if the
    model has not produced a usable path yet, in which case the cluster keeps
    drawing a flat one rather than something wrong."""
    probs = md.laneLineProbs
    x = np.asarray(md.position.x, dtype=float)
    y = np.asarray(md.position.y, dtype=float)
    if len(probs) < 4 or len(x) < 4 or len(x) != len(y):
      return

    # Only fit the portion of the path the cluster shows.
    n = int(np.count_nonzero(x < ControlsExt.NAP_BUDDY_PATH_LENGTH_M))
    if n < 4:
      return

    try:
      coefs = np.polyfit(x[:n], y[:n], 3)
    except (np.linalg.LinAlgError, ValueError):
      return
    if not np.all(np.isfinite(coefs)):
      return

    f = 1.0 / ControlsExt.NAP_BUDDY_IC_LANE_SCALE
    lanes.valid = True
    lanes.laneWidth = 4.0
    lanes.leftLaneProb = float(probs[1])
    lanes.rightLaneProb = float(probs[2])
    lanes.leftEdgeProb = float(probs[0])
    lanes.rightEdgeProb = float(probs[3])
    # c1 is suppressed: the cluster derives heading from the path itself, and
    # feeding it here double-counts and skews the drawn lane.
    lanes.c0 = float(coefs[3])
    lanes.c1 = 0.0
    lanes.c2 = float(coefs[1]) * f * f
    lanes.c3 = float(coefs[0]) * f * f * f

  def state_control_ext(self, sm: messaging.SubMaster) -> custom.CarControlSP:
    CC_SP = custom.CarControlSP.new_message()

    self.get_lead_data(CC_SP.leadOne, sm['radarState'].leadOne)
    self.get_lead_data(CC_SP.leadTwo, sm['radarState'].leadTwo)

    # NAP Buddy IC lane geometry (display-only; the car layer gates on its own toggle)
    if sm.valid.get('modelV2', False):
      self.get_nap_buddy_lanes(CC_SP.napBuddyLanes, sm['modelV2'])

    # NAP Buddy road-sign widget. The posted limit, not the offset-adjusted
    # target: the cluster draws a speed-limit sign, not openpilot's set point.
    resolver = sm['longitudinalPlanSP'].speedLimit.resolver
    CC_SP.napBuddySpeedLimit = float(resolver.speedLimit) if resolver.speedLimitValid else 0.0

    # NAP Buddy grey steering wheel. selfdriveState.engageable is the real
    # "openpilot could engage now" signal; the car layer has no access to it.
    CC_SP.napBuddyEngageable = bool(sm['selfdriveState'].engageable)

    # MADS state
    mads_src = sm['selfdriveStateSP'].mads
    CC_SP.mads.state = mads_src.state
    CC_SP.mads.enabled = mads_src.enabled
    CC_SP.mads.active = mads_src.active
    CC_SP.mads.available = mads_src.available

    # ICBM state
    icbm_src = sm['selfdriveStateSP'].intelligentCruiseButtonManagement
    CC_SP.intelligentCruiseButtonManagement.state = icbm_src.state
    CC_SP.intelligentCruiseButtonManagement.sendButton = icbm_src.sendButton
    CC_SP.intelligentCruiseButtonManagement.vTarget = icbm_src.vTarget

    return CC_SP

  @staticmethod
  def publish_ext(CC_SP: custom.CarControlSP, sm: messaging.SubMaster, pm: messaging.PubMaster) -> None:
    cc_sp_send = messaging.new_message('carControlSP')
    cc_sp_send.valid = sm['carState'].canValid
    cc_sp_send.carControlSP = CC_SP

    pm.send('carControlSP', cc_sp_send)

  def run_ext(self, sm: messaging.SubMaster, pm: messaging.PubMaster) -> None:
    CC_SP = self.state_control_ext(sm)
    self.publish_ext(CC_SP, sm, pm)
