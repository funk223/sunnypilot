from cereal import car
from common.numpy_fast import clip, interp
from selfdrive.car import apply_toyota_steer_torque_limits, create_gas_interceptor_command, make_can_msg
from selfdrive.car.toyota.toyotacan import create_steer_command, create_ui_command, \
                                           create_accel_command, create_acc_cancel_command, \
                                           create_fcw_command, create_lta_steer_command, \
                                           create_ui_command_disable_startup_lkas
from selfdrive.car.toyota.values import CAR, STATIC_DSU_MSGS, NO_STOP_TIMER_CAR, TSS2_CAR, \
                                        MIN_ACC_SPEED, PEDAL_TRANSITION, CarControllerParams, FEATURES
from selfdrive.car.toyota.interface import CarInterface
from opendbc.can.packer import CANPacker
from common.realtime import DT_CTRL

VisualAlert = car.CarControl.HUDControl.VisualAlert

# constants for fault workaround
MAX_STEER_RATE = 100  # deg/s
MAX_STEER_RATE_FRAMES = 19


class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.torque_rate_limits = CarControllerParams(self.CP)
    self.frame = 0
    self.last_steer = 0
    self.alert_active = False
    self.last_standstill = False
    self.standstill_req = False
    self.steer_rate_limited = False

    self.steer_rate_counter = 0

    self.packer = CANPacker(dbc_name)
    self.gas = 0
    self.accel = 0

    self.signal_last = 0.
    self.lat_active = False
    self.has_set_lkas = False
    self.standstill_status = 0
    self.standstill_status_timer = 0

  def update(self, CC, CS):
    actuators = CC.actuators
    hud_control = CC.hudControl
    pcm_cancel_cmd = CC.cruiseControl.cancel

    # gas and brake
    if self.CP.enableGasInterceptor and CC.longActive:
      MAX_INTERCEPTOR_GAS = 0.5
      # RAV4 has very sensitive gas pedal
      if self.CP.carFingerprint in (CAR.RAV4, CAR.RAV4H, CAR.HIGHLANDER, CAR.HIGHLANDERH):
        PEDAL_SCALE = interp(CS.out.vEgo, [0.0, MIN_ACC_SPEED, MIN_ACC_SPEED + PEDAL_TRANSITION], [0.15, 0.3, 0.0])
      elif self.CP.carFingerprint in (CAR.COROLLA,):
        PEDAL_SCALE = interp(CS.out.vEgo, [0.0, MIN_ACC_SPEED, MIN_ACC_SPEED + PEDAL_TRANSITION], [0.3, 0.4, 0.0])
      else:
        PEDAL_SCALE = interp(CS.out.vEgo, [0.0, MIN_ACC_SPEED, MIN_ACC_SPEED + PEDAL_TRANSITION], [0.4, 0.5, 0.0])
      # offset for creep and windbrake
      pedal_offset = interp(CS.out.vEgo, [0.0, 2.3, MIN_ACC_SPEED + PEDAL_TRANSITION], [-.4, 0.0, 0.2])
      pedal_command = PEDAL_SCALE * (actuators.accel + pedal_offset)
      interceptor_gas_cmd = clip(pedal_command, 0., MAX_INTERCEPTOR_GAS)
    else:
      interceptor_gas_cmd = 0.
    pid_accel_limits = CarInterface.get_pid_accel_limits(self.CP, CS.out.vEgo, None)  # Need to get cruise speed from somewhere
    pcm_accel_cmd = 0 if not CC.longActive else clip(actuators.accel, pid_accel_limits[0], pid_accel_limits[1])

    # steer torque
    new_steer = int(round(actuators.steer * CarControllerParams.STEER_MAX))
    apply_steer = apply_toyota_steer_torque_limits(new_steer, self.last_steer, CS.out.steeringTorqueEps, self.torque_rate_limits)

    cur_time = self.frame * DT_CTRL
    if CS.leftBlinkerOn or CS.rightBlinkerOn:
      self.signal_last = cur_time

    # EPS_STATUS->LKA_STATE either goes to 21 or 25 on rising edge of a steering fault and
    # the value seems to describe how many frames the steering rate was above 100 deg/s, so
    # cut torque with some margin for the lower state
    if CC.latActive and abs(CS.out.steeringRateDeg) >= MAX_STEER_RATE:
      self.steer_rate_counter += 1
    else:
      self.steer_rate_counter = 0

    apply_steer_req = 1
    if CC.latActive:
      self.steer_rate_limited = new_steer != apply_steer

    if not CC.latActive:
      apply_steer = 0
      apply_steer_req = 0
    elif self.steer_rate_counter >= MAX_STEER_RATE_FRAMES:
      apply_steer_req = 0
      self.steer_rate_counter = 0

    # TODO: probably can delete this. CS.pcm_acc_status uses a different signal
    # than CS.cruiseState.enabled. confirm they're not meaningfully different
    #if not CC.enabled and CS.pcm_acc_status:
    #  pcm_cancel_cmd = 1

    # on entering standstill, send standstill request
    if CS.out.standstill and not self.last_standstill and self.CP.carFingerprint not in NO_STOP_TIMER_CAR:
      self.standstill_req = True
      self.standstill_status = 1
    if CS.pcm_acc_status != 8:
      # pcm entered standstill or it's disabled
      self.standstill_req = False

    self.last_steer = apply_steer
    self.last_standstill = CS.out.standstill

    if CS.out.brakeLights and CS.out.vEgo < 0.1:
      self.standstill_status = 1
      self.standstill_status_timer += 1
      if self.standstill_status_timer > 200:
        self.standstill_status = 1
        self.standstill_status_timer = 0
    if self.standstill_status == 1 and CS.out.vEgo > 1:
      self.standstill_status = 0

    can_sends = []

    # *** control msgs ***
    # print("steer {0} {1} {2} {3}".format(apply_steer, min_lim, max_lim, CS.steer_torque_motor)

    # toyota can trace shows this message at 42Hz, with counter adding alternatively 1 and 2;
    # sending it at 100Hz seem to allow a higher rate limit, as the rate limit seems imposed
    # on consecutive messages
    can_sends.append(create_steer_command(self.packer, apply_steer, apply_steer_req, self.frame))
    if self.frame % 2 == 0 and self.CP.carFingerprint in TSS2_CAR:
      can_sends.append(create_lta_steer_command(self.packer, 0, 0, self.frame // 2))

    # LTA mode. Set ret.steerControlType = car.CarParams.SteerControlType.angle and whitelist 0x191 in the panda
    # if self.frame % 2 == 0:
    #   can_sends.append(create_steer_command(self.packer, 0, 0, self.frame // 2))
    #   can_sends.append(create_lta_steer_command(self.packer, actuators.steeringAngleDeg, apply_steer_req, self.frame // 2))

    # we can spam can to cancel the system even if we are using lat only control
    if (self.frame % 3 == 0 and self.CP.openpilotLongitudinalControl) or pcm_cancel_cmd:
      lead = hud_control.leadVisible or CS.out.vEgo < 12.  # at low speed we always assume the lead is present so ACC can be engaged

      # Lexus IS uses a different cancellation message
      if pcm_cancel_cmd and self.CP.carFingerprint in (CAR.LEXUS_IS, CAR.LEXUS_RC):
        can_sends.append(create_acc_cancel_command(self.packer))
      elif self.CP.openpilotLongitudinalControl:
        can_sends.append(create_accel_command(self.packer, pcm_accel_cmd, pcm_cancel_cmd, self.standstill_req, lead, CS.acc_type, CS.gap_adjust_cruise_tr_line, CS.reverse_acc_change))
        self.accel = pcm_accel_cmd
      else:
        can_sends.append(create_accel_command(self.packer, 0, pcm_cancel_cmd, False, lead, CS.acc_type, CS.gap_adjust_cruise_tr_line, CS.reverse_acc_change))

    if self.frame % 2 == 0 and self.CP.enableGasInterceptor and self.CP.openpilotLongitudinalControl:
      # send exactly zero if gas cmd is zero. Interceptor will send the max between read value and gas cmd.
      # This prevents unexpected pedal range rescaling
      can_sends.append(create_gas_interceptor_command(self.packer, interceptor_gas_cmd, self.frame // 2))
      self.gas = interceptor_gas_cmd

    # ui mesg is at 1Hz but we send asap if:
    # - there is something to display
    # - there is something to stop displaying
    fcw_alert = hud_control.visualAlert == VisualAlert.fcw
    steer_alert = hud_control.visualAlert in (VisualAlert.steerRequired, VisualAlert.ldw)

    send_ui = False
    if ((fcw_alert or steer_alert) and not self.alert_active) or \
       (not (fcw_alert or steer_alert) and self.alert_active):
      send_ui = True
      self.alert_active = not self.alert_active
    elif pcm_cancel_cmd:
      # forcing the pcm to disengage causes a bad fault sound so play a good sound instead
      send_ui = True

    use_lta_msg = False
    if CarControllerParams.FEATURE_NO_LKAS_ICON:
      if CS.CP.carFingerprint in FEATURES["use_lta_msg"]:
        use_lta_msg = True
        if CS.persistLkasIconDisabled == 1:
          self.has_set_lkas = True
      else:
        use_lta_msg = False
        if CS.persistLkasIconDisabled == 0:
          self.has_set_lkas = True

      if self.frame % 100 == 0 or send_ui:
        if not self.has_set_lkas:
          can_sends.append(create_ui_command_disable_startup_lkas(self.packer, use_lta_msg))

    if self.frame % 100 == 0 or send_ui:
      can_sends.append(create_ui_command(self.packer, steer_alert, pcm_cancel_cmd, hud_control.leftLaneVisible,
                                         hud_control.rightLaneVisible, hud_control.leftLaneDepart,
                                         hud_control.rightLaneDepart, CC.latActive, CS.madsEnabled, use_lta_msg))

    self.lat_active = CC.latActive

    if self.frame % 100 == 0 and self.CP.enableDsu:
      can_sends.append(create_fcw_command(self.packer, fcw_alert))

    # *** static msgs ***
    for addr, cars, bus, fr_step, vl in STATIC_DSU_MSGS:
      if self.frame % fr_step == 0 and self.CP.enableDsu and self.CP.carFingerprint in cars:
        can_sends.append(make_can_msg(addr, vl, bus))

    new_actuators = actuators.copy()
    new_actuators.steer = apply_steer / CarControllerParams.STEER_MAX
    new_actuators.accel = self.accel
    new_actuators.gas = self.gas

    self.frame += 1
    return new_actuators, can_sends
