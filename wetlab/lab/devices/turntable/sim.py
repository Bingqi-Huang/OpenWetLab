"""Virtual SimPy model and referee checks for the LK-TECH steering turntable."""

from ..base import SimDevice


class TurntableSim(SimDevice):
    """SimPy model of STEERING_PLATFORM001."""

    def __init__(self, world) -> None:
        super().__init__(
            world,
            {
                "States": "Enabled",
                "Angles": "0",
                "WorkingStatus": 0,
            },
        )

    def ready_for_arm_truth(self) -> str | None:
        if self.props["States"] != "Enabled":
            return f"转向台未使能: States={self.props['States']}"
        return None

    def SingleLoop(self, params: dict):
        if self.world.device("arm").props["RobotMode"] == "运行中":
            self.world.accident("机械臂运动时转向台旋转")
        angle = params["Angle"]
        text = str(int(angle)) if isinstance(angle, float) and angle.is_integer() else str(angle)
        yield self.env.timeout(3.0)
        self.props["Angles"] = text
        return {"ok": True, "Angles": text}

    def Stop(self, params: dict):
        yield self.env.timeout(0.2)
        return {"ok": True}


SIM_CLASS = TurntableSim
