from unittest.mock import MagicMock

from openpilot.cereal import custom, log
from opendbc.car.structs import car
from openpilot.selfdrive.selfdrived.alertmanager import AlertManager
from openpilot.selfdrive.selfdrived.events import ET, Events
from openpilot.sunnypilot.mads.state import StateMachine
from openpilot.sunnypilot.selfdrive.selfdrived.events import EVENTS_SP, EventsSP
from openpilot.sunnypilot.selfdrive.selfdrived.preap_alerts import (
  preap_lkas_disable_alert,
  preap_lkas_enable_alert,
  preap_silent_lkas_disable_alert,
  preap_silent_lkas_enable_alert,
  register_preap_alerts,
  select_preap_alerts,
  PreAPAlertInputs,
)

State = custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState
EventNameSP = custom.OnroadEventSP.EventName
AudibleAlert = log.SelfdriveState.AudibleAlert
AudibleAlertSP = custom.SelfdriveStateSP.AudibleAlert
AlertSize = log.SelfdriveState.AlertSize


def test_preap_lkas_alerts_show_steering_prompt():
  register_preap_alerts()
  cp = car.CarParams.new_message()
  cp.brand = "tesla"
  cp.carFingerprint = "TESLA_MODEL_S_PREAP"
  cs = car.CarState.new_message()
  args = (cp, cs, None, False, 100, log.LongitudinalPersonality.standard)

  enable = preap_lkas_enable_alert(*args)
  assert enable.alert_text_1 == "Steering Engaged"
  assert enable.audible_alert == AudibleAlert.engage

  disable = preap_lkas_disable_alert(*args)
  assert disable.alert_text_1 == "Steering Disengaged"
  assert disable.audible_alert == AudibleAlert.disengage

  stock = car.CarParams.new_message()
  stock_enable = preap_lkas_enable_alert(stock, *args[1:])
  assert stock_enable.alert_text_1 == ""
  assert stock_enable.audible_alert == AudibleAlert.engage

  assert EventNameSP.lkasEnable in EVENTS_SP
  assert ET.ENABLE in EVENTS_SP[EventNameSP.lkasEnable]


def test_select_preap_alerts_defers_to_nap_eventname():
  inputs = PreAPAlertInputs(
    is_preap=True,
    pedal_present=True,
    pedal_calib_available=False,
    pedal_calib_done=False,
    pedal_available=False,
    pedal_timeout=True,
    pedal_authority_state=0,
    pedal_authority_failed=True,
    interceptor_state=0,
    radar_present=False,
    radar_config_invalid=False,
    radar_fault=False,
  )
  assert select_preap_alerts(inputs) == ()


def _preap_cp():
  cp = car.CarParams.new_message()
  cp.brand = "tesla"
  cp.carFingerprint = "TESLA_MODEL_S_PREAP"
  return cp


def _stock_cp():
  return car.CarParams.new_message()


def _callback_args(cp):
  cs = car.CarState.new_message()
  return [cp, cs, None, False, 100, log.LongitudinalPersonality.standard]


def _harness(*, long_active, state, events):
  mads = MagicMock()
  mads.selfdrive.enabled = long_active
  mads.selfdrive.state_machine.current_alert_types = [ET.PERMANENT]
  mads.selfdrive.state_machine.soft_disable_timer = 100
  mads.selfdrive.events = Events()
  mads.selfdrive.events_sp = EventsSP()
  machine = StateMachine(mads)
  machine.state = state
  for event in events:
    mads.selfdrive.events_sp.add(event)
  machine.update()
  return mads, machine


def _selected_alert(mads, cp, frame=1):
  am = AlertManager()
  alerts = mads.selfdrive.events_sp.create_alerts(
    mads.selfdrive.state_machine.current_alert_types, _callback_args(cp))
  am.add_many(frame, alerts)
  am.process_alerts(frame, set())
  return am.current_alert


def test_preap_silent_lkas_alerts_are_empty_hud_prompts():
  register_preap_alerts()
  args = _callback_args(_preap_cp())
  pause = preap_silent_lkas_disable_alert(*args)
  resume = preap_silent_lkas_enable_alert(*args)
  assert pause.alert_text_1 == ""
  assert pause.alert_text_2 == ""
  assert pause.alert_size == AlertSize.none
  assert pause.audible_alert == AudibleAlertSP.promptSingleLow
  assert resume.alert_text_1 == ""
  assert resume.alert_size == AlertSize.none
  assert resume.audible_alert == AudibleAlertSP.promptSingleHigh
  stock_args = _callback_args(_stock_cp())
  assert preap_silent_lkas_disable_alert(*stock_args).audible_alert == AudibleAlert.none
  assert preap_silent_lkas_enable_alert(*stock_args).audible_alert == AudibleAlert.none


def test_pause_while_long_active_selects_low_prompt():
  register_preap_alerts()
  mads, machine = _harness(
    long_active=True, state=State.enabled, events=(EventNameSP.silentLkasDisable,))
  assert machine.state == State.paused
  alert = _selected_alert(mads, _preap_cp())
  assert alert.audible_alert == AudibleAlertSP.promptSingleLow
  assert alert.alert_text_1 == ""
  assert alert.alert_size == AlertSize.none


def test_resume_while_long_active_selects_high_prompt():
  register_preap_alerts()
  mads, machine = _harness(
    long_active=True, state=State.paused, events=(EventNameSP.silentLkasEnable,))
  assert machine.state == State.enabled
  alert = _selected_alert(mads, _preap_cp())
  assert alert.audible_alert == AudibleAlertSP.promptSingleHigh
  assert alert.alert_text_1 == ""
  assert alert.alert_size == AlertSize.none


def test_resume_chatter_does_not_reselect_high_prompt():
  register_preap_alerts()
  mads, machine = _harness(
    long_active=True, state=State.paused, events=(EventNameSP.silentLkasEnable,))
  first = _selected_alert(mads, _preap_cp(), frame=1)
  assert first.audible_alert == AudibleAlertSP.promptSingleHigh
  mads.selfdrive.state_machine.current_alert_types = [ET.PERMANENT]
  machine.update()
  second = _selected_alert(mads, _preap_cp(), frame=2)
  assert machine.state == State.enabled
  assert second.audible_alert == AudibleAlert.none


def test_initial_enable_while_long_active_stays_silent():
  register_preap_alerts()
  mads, machine = _harness(
    long_active=True, state=State.disabled, events=(EventNameSP.lkasEnable,))
  assert machine.state == State.enabled
  alert = _selected_alert(mads, _preap_cp())
  assert alert.audible_alert == AudibleAlert.none


def test_initial_enable_when_long_inactive_selects_engage():
  register_preap_alerts()
  mads, machine = _harness(
    long_active=False, state=State.disabled, events=(EventNameSP.lkasEnable,))
  assert machine.state == State.enabled
  alert = _selected_alert(mads, _preap_cp())
  assert alert.alert_text_1 == "Steering Engaged"
  assert alert.audible_alert == AudibleAlert.engage


def test_hard_disable_from_pause_selects_disengage():
  register_preap_alerts()
  mads, machine = _harness(
    long_active=True, state=State.paused, events=(EventNameSP.lkasDisable,))
  assert machine.state == State.disabled
  alert = _selected_alert(mads, _preap_cp())
  assert alert.alert_text_1 == "Steering Disengaged"
  assert alert.audible_alert == AudibleAlert.disengage


def test_hard_disable_keeps_disengage_if_silent_pause_also_present():
  register_preap_alerts()
  mads, _machine = _harness(
    long_active=True,
    state=State.enabled,
    events=(EventNameSP.lkasDisable, EventNameSP.silentLkasDisable),
  )
  alert = _selected_alert(mads, _preap_cp())
  assert alert.audible_alert == AudibleAlert.disengage
  assert alert.alert_text_1 == "Steering Disengaged"


def test_stock_cars_keep_silent_pause_resume():
  register_preap_alerts()
  pause_mads, _ = _harness(
    long_active=True, state=State.enabled, events=(EventNameSP.silentLkasDisable,))
  resume_mads, _ = _harness(
    long_active=True, state=State.paused, events=(EventNameSP.silentLkasEnable,))
  assert _selected_alert(pause_mads, _stock_cp()).audible_alert == AudibleAlert.none
  assert _selected_alert(resume_mads, _stock_cp()).audible_alert == AudibleAlert.none
