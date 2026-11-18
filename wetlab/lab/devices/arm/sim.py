"""Virtual SimPy model and referee checks for the Huayan 6-axis robot arm."""

from ...errors import DeviceFailed
from ..base import SimDevice


class ArmSim(SimDevice):
    """SimPy model of TYPE_ROBOT_ARM001 calibrated against GW_001 captures."""

    def __init__(self, world) -> None:
        super().__init__(
            world,
            {
                "RobotMode": "使能空闲",
                "SafetyStatus": "NORMAL",
                "GripperCommStatus": "CONNECTED",
                "GripperPosition": 350,
                "WorkingStatus": 0,
            },
        )

    def MovMulti(self, params: dict):
        tasks = params.get("Tasks") or []
        if not tasks:
            raise DeviceFailed("MovMulti 缺少 Tasks")
        self.props["RobotMode"] = "运行中"
        try:
            for task in tasks:
                point = str(task["PositionId"])
                pos = self.world.position_of_point(point)
                if pos is None:
                    raise DeviceFailed(f"未知点位: {point}")
                if pos == "arm:home":
                    yield self.env.timeout(20.0)
                    continue
                yield self.env.timeout(20.0)
                owner_name = self.world.owner_of_position(pos)
                unsafe = self.world.device(owner_name).ready_for_arm_truth()
                if unsafe:
                    self.world.accident(unsafe)
                claw = task["ClawSwitch"]
                if claw == 0:
                    plate = self.world.plate_at(pos)
                    if plate is None:
                        self.world.accident(f"夹爪在空位置 {pos} 上夹取")
                    else:
                        self.world.move_plate(plate, "arm")
                    self.props["GripperPosition"] = 75
                elif claw == 1:
                    held = self.world.plate_at("arm")
                    if self.world.plate_at(pos) is not None:
                        self.world.accident(f"往已有器皿的位置 {pos} 放置")
                    if held is not None:
                        self.world.move_plate(held, pos, record_move=True)
                    self.props["GripperPosition"] = 350
            yield self.env.timeout(1.0)
        finally:
            self.props["RobotMode"] = "使能空闲"
        return {"code": "0"}

    def Pause(self, params: dict):
        yield self.env.timeout(0.2)
        return {"success": True}

    def Continue(self, params: dict):
        yield self.env.timeout(0.2)
        return {"success": True}

    def Stop(self, params: dict):
        self.props["RobotMode"] = "使能空闲"
        yield self.env.timeout(0.2)
        return {"success": True}

    def EmergencyStop(self, params: dict):
        self.props["RobotMode"] = "使能空闲"
        yield self.env.timeout(0.2)
        return {"success": True}


SIM_CLASS = ArmSim
