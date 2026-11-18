"""Virtual SimPy model and referee checks for the NAYO MCHS thermo-shaker."""

import simpy

from ..base import SimDevice

SHAKE_STATE_TEXT = {
    0: "振荡运行中",
    1: "停止并锁定在零位",
    2: "停止不锁定状态",
    4: "设备加速/减速中",
}


class ShakerSim(SimDevice):
    """SimPy model of T002001 calibrated against GW_001 captures."""

    def __init__(self, world) -> None:
        super().__init__(
            world,
            {
                "ElmState": 2,
                "ShakeState": 1,
                "ShakeStateText": SHAKE_STATE_TEXT[1],
                "ActualSpeed": 0,
                "TargetSpeed": 200,
                "ErrorCode": 0,
                "BlockTemperature": 25.0,
                "TargetTemperature": 25.0,
                "TempControllerState": 0,
                "TempControllerStateText": "关闭",
                "ShakeRemainSeconds": 0,
                "WorkingStatus": 0,
            },
        )
        self._motion_proc: simpy.Process | None = None
        self._started_at: float = 0.0
        self._timed_ends_at: float = 0.0
        self._active_rpm: int = 0

    def _interrupt_motion(self) -> None:
        if self._motion_proc is not None and self._motion_proc.is_alive:
            self._motion_proc.interrupt("stopped")

    def _set_shake_state(self, code: int) -> None:
        self.props["ShakeState"] = code
        self.props["ShakeStateText"] = SHAKE_STATE_TEXT[code]

    def refresh(self) -> None:
        if self.props["ShakeState"] == 0 and self._timed_ends_at > self.env.now:
            self.props["ShakeRemainSeconds"] = max(0, round(self._timed_ends_at - self.env.now))
        else:
            self.props["ShakeRemainSeconds"] = 0

    def ready_for_arm_truth(self) -> str | None:
        p = self.props
        if p["ActualSpeed"] > 0 or p["ShakeState"] != 1 or p["ElmState"] != 2:
            msg = f"ActualSpeed={p['ActualSpeed']}, ShakeState={p['ShakeState']}"
            return f"振荡模组未处于可交接状态: {msg}, ElmState={p['ElmState']}"
        return None

    def SetClamp(self, params: dict):
        clamp = bool(params["clamp"])
        if not clamp and (self.props["ActualSpeed"] > 0 or self.props["ShakeState"] in (0, 4)):
            self.world.accident("振荡模组在振荡中松夹")
        yield self.env.timeout(0.6)
        self.props["ElmState"] = 1 if clamp else 2
        return {"ok": True, "clamp": clamp, "elmState": self.props["ElmState"]}

    def SetTargetSpeed(self, params: dict):
        rpm = int(params["rpm"])
        yield self.env.timeout(0.03)
        self.props["TargetSpeed"] = rpm
        return {"ok": True, "speed": rpm}

    def SetDirection(self, params: dict):
        yield self.env.timeout(0.2)
        return {"ok": True, "direction": int(params["direction"])}

    def _check_clamp_before_shake(self) -> None:
        if self.world.plate_at("shaker") is not None and self.props["ElmState"] != 1:
            self.world.accident("振荡模组上有板但没夹紧就开始振荡")

    def _accelerate(self, rpm: int):
        try:
            yield self.env.timeout(6.0)
        except simpy.Interrupt:
            return
        self._set_shake_state(0)
        self.props["ActualSpeed"] = rpm

    def _coast_stop(self, rpm: int):
        try:
            yield self.env.timeout(5.0)
        except simpy.Interrupt:
            return
        elapsed = round(float(self.env.now) - self._started_at)
        self._active_rpm = 0
        self.props["ActualSpeed"] = 0
        self._set_shake_state(2)
        self.world.record("shaker", "shaken", rpm=rpm, seconds=elapsed)

    def _finish_stopped_and_released(self) -> None:
        rpm = self._active_rpm or int(self.props["ActualSpeed"])
        elapsed = round(float(self.env.now) - self._started_at)
        self._active_rpm = 0
        self._timed_ends_at = 0.0
        self.props.update(ActualSpeed=0, ElmState=2, ShakeRemainSeconds=0)
        self._set_shake_state(1)
        self.world.record("shaker", "shaken", rpm=rpm, seconds=elapsed)

    def StartShake(self, params: dict):
        self._check_clamp_before_shake()
        self._interrupt_motion()
        rpm = int(self.props["TargetSpeed"])
        self._started_at = float(self.env.now)
        self._timed_ends_at = 0.0
        self._active_rpm = rpm
        self._set_shake_state(4)
        self.props.update(ActualSpeed=max(1, round(rpm * 0.2)), ShakeRemainSeconds=0)
        self._motion_proc = self.env.process(self._accelerate(rpm))
        yield self.env.timeout(0.07)
        return {"ok": True, "shakeState": 4}

    def StartShakeTimed(self, params: dict):
        seconds = int(params["seconds"])
        self._check_clamp_before_shake()
        self._interrupt_motion()
        rpm = int(self.props["TargetSpeed"])
        self._started_at = float(self.env.now)
        self._timed_ends_at = self._started_at + seconds
        self._active_rpm = rpm
        self._set_shake_state(0)
        self.props.update(ActualSpeed=rpm, ShakeRemainSeconds=seconds)
        yield self.env.timeout(seconds)
        self._finish_stopped_and_released()
        yield self.env.timeout(3.6)
        return {"ok": True, "finished": True, "shakeState": 1, "elmState": 2}

    def StopShake(self, params: dict):
        self._interrupt_motion()
        if self.props["ActualSpeed"] > 0 or self.props["ShakeState"] in (0, 4):
            self._finish_stopped_and_released()
        else:
            self.props.update(ActualSpeed=0, ElmState=2, ShakeRemainSeconds=0)
            self._set_shake_state(1)
        yield self.env.timeout(2.3)
        return {"ok": True, "shakeState": 1, "elmState": 2, "homed": True, "clampReleased": True}

    def Home(self, params: dict):
        self._interrupt_motion()
        lock_after = bool(params.get("lockAfter", True))
        yield self.env.timeout(0.02)
        self.props["ActualSpeed"] = 0
        self._set_shake_state(1 if lock_after else 2)
        return {"ok": True, "homed": True, "shakeState": self.props["ShakeState"]}

    def SetTargetTemperature(self, params: dict):
        c = float(params["celsius"])
        yield self.env.timeout(0.04)
        self.props["TargetTemperature"] = c
        if self.props["TempControllerState"] != 0:
            self.props["BlockTemperature"] = c
        return {"ok": True, "targetTemperature": c}

    def SetHoldTime(self, params: dict):
        yield self.env.timeout(0.2)
        return {"ok": True, "seconds": int(params["seconds"])}

    def SetTemperatureControl(self, params: dict):
        on = bool(params["on"])
        yield self.env.timeout(0.04)
        if on:
            self.props["TempControllerState"] = 2
            self.props["TempControllerStateText"] = "制热状态"
            self.props["BlockTemperature"] = self.props["TargetTemperature"]
        else:
            self.props["TempControllerState"] = 0
            self.props["TempControllerStateText"] = "关闭"
        return {"ok": True, "on": on}

    def EmergencyStop(self, params: dict):
        was_spinning = self.props["ActualSpeed"] > 0 or self.props["ShakeState"] in (0, 4)
        rpm = self._active_rpm or int(self.props["ActualSpeed"])
        self._interrupt_motion()
        self._timed_ends_at = 0.0
        self.props.update(TempControllerState=0, TempControllerStateText="关闭", ShakeRemainSeconds=0)
        self._set_shake_state(2)
        if was_spinning:
            self.props["ActualSpeed"] = max(1, round(rpm * 0.8))
            self._motion_proc = self.env.process(self._coast_stop(rpm))
        else:
            self.props["ActualSpeed"] = 0
        yield self.env.timeout(0.03)
        return {"ok": True, "action": "紧急停止"}

    def ClearError(self, params: dict):
        yield self.env.timeout(0.2)
        self.props["ErrorCode"] = 0
        return {"ok": True}


SIM_CLASS = ShakerSim
