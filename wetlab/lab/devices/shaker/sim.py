"""Virtual SimPy model and referee checks for the NAYO MCHS thermo-shaker."""

from ..base import SimDevice


class ShakerSim(SimDevice):
    """SimPy model of T002001 calibrated against GW_001 captures."""

    def __init__(self, world) -> None:
        super().__init__(
            world,
            {
                "ElmState": 2,
                "ShakeState": 1,
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
        self._started_at: float = 0.0
        self._timed_ends_at: float = 0.0

    def refresh(self) -> None:
        if self.props["ShakeState"] == 0 and self._timed_ends_at > self.env.now:
            self.props["ShakeRemainSeconds"] = max(0, round(self._timed_ends_at - self.env.now))
        else:
            self.props["ShakeRemainSeconds"] = 0

    def ready_for_arm_truth(self) -> str | None:
        if (
            self.props["ActualSpeed"] > 0
            or self.props["ShakeState"] != 1
            or self.props["ElmState"] != 2
        ):
            return (
                f"振荡模组未处于可交接状态: ActualSpeed={self.props['ActualSpeed']}, "
                f"ShakeState={self.props['ShakeState']}, ElmState={self.props['ElmState']}"
            )
        return None

    def SetClamp(self, params: dict):
        clamp = bool(params["clamp"])
        if not clamp and (self.props["ActualSpeed"] > 0 or self.props["ShakeState"] == 0):
            self.world.accident("振荡模组在振荡中松夹")
        yield self.env.timeout(0.6)
        self.props["ElmState"] = 1 if clamp else 2
        return {"ok": True, "clamp": clamp, "elmState": self.props["ElmState"]}

    def SetTargetSpeed(self, params: dict):
        rpm = int(params["rpm"])
        yield self.env.timeout(0.2)
        self.props["TargetSpeed"] = rpm
        return {"ok": True, "speed": rpm}

    def SetDirection(self, params: dict):
        yield self.env.timeout(0.2)
        return {"ok": True, "direction": int(params["direction"])}

    def _spin_up(self, timed_seconds: int = 0) -> None:
        if self.world.plate_at("shaker") is not None and self.props["ElmState"] != 1:
            self.world.accident("振荡模组上有板但没夹紧就开始振荡")
        self.props.update(
            ShakeState=0,
            ActualSpeed=self.props["TargetSpeed"],
            ShakeRemainSeconds=timed_seconds,
        )
        self._started_at = float(self.env.now)
        self._timed_ends_at = self._started_at + timed_seconds if timed_seconds > 0 else 0.0

    def _spin_down(self) -> None:
        rpm = self.props["ActualSpeed"]
        elapsed = round(float(self.env.now) - self._started_at)
        self._timed_ends_at = 0.0
        self.props.update(ActualSpeed=0, ShakeState=1, ElmState=2, ShakeRemainSeconds=0)
        self.world.record("shaker", "shaken", rpm=rpm, seconds=elapsed)

    def StartShake(self, params: dict):
        self._spin_up()
        yield self.env.timeout(2.0)
        return {"ok": True, "shakeState": 0}

    def StartShakeTimed(self, params: dict):
        seconds = int(params["seconds"])
        self._spin_up(timed_seconds=seconds)
        yield self.env.timeout(seconds)
        self._spin_down()
        yield self.env.timeout(3.6)
        return {
            "ok": True,
            "finished": True,
            "shakeState": 1,
            "elmState": 2,
        }

    def StopShake(self, params: dict):
        if self.props["ActualSpeed"] > 0 or self.props["ShakeState"] == 0:
            self._spin_down()
        else:
            self.props.update(ActualSpeed=0, ShakeState=1, ElmState=2, ShakeRemainSeconds=0)
        yield self.env.timeout(4.0)
        return {"ok": True, "shakeState": 1, "elmState": 2}

    def Home(self, params: dict):
        yield self.env.timeout(1.5)
        self.props["ShakeState"] = 1
        return {"ok": True}

    def SetTargetTemperature(self, params: dict):
        c = float(params["celsius"])
        yield self.env.timeout(0.2)
        self.props["TargetTemperature"] = c
        if self.props["TempControllerState"] != 0:
            self.props["BlockTemperature"] = c
        return {"ok": True, "targetTemperature": c}

    def SetHoldTime(self, params: dict):
        yield self.env.timeout(0.2)
        return {"ok": True, "seconds": int(params["seconds"])}

    def SetTemperatureControl(self, params: dict):
        on = bool(params["on"])
        yield self.env.timeout(0.2)
        if on:
            self.props["TempControllerState"] = 2
            self.props["TempControllerStateText"] = "制热状态"
            self.props["BlockTemperature"] = self.props["TargetTemperature"]
        else:
            self.props["TempControllerState"] = 0
            self.props["TempControllerStateText"] = "关闭"
        return {"ok": True, "on": on}

    def EmergencyStop(self, params: dict):
        if self.props["ActualSpeed"] > 0 or self.props["ShakeState"] == 0:
            self._spin_down()
        self.props.update(TempControllerState=0, TempControllerStateText="关闭")
        yield self.env.timeout(0.2)
        return {"ok": True}

    def ClearError(self, params: dict):
        yield self.env.timeout(0.2)
        self.props["ErrorCode"] = 0
        return {"ok": True}


SIM_CLASS = ShakerSim
