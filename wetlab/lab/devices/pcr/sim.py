"""Virtual SimPy model and referee checks for the Arhat-auto 96 PCR cycler."""

import json

import simpy

from ...errors import DeviceFailed
from ..base import SimDevice

AMBIENT_TEMP_C = 25.0
LID_AMBIENT_C = 27.0
NOMINAL_RAMP_C_PER_S = 3.0
LID_RAMP_C_PER_S = 1.5


def program_seconds(steps: list[dict]) -> int:
    """Compute total PCR program run time in seconds, including heating/cooling ramps."""
    total = 0.0
    temp = AMBIENT_TEMP_C
    i = 0
    loop_iter: dict[int, int] = {}
    enclosing: dict[int, int] = {}
    for end_idx, st in enumerate(steps):
        nxt = int(st.get("NextStep", 0))
        cycles = int(st.get("Cycles", 1))
        if nxt and cycles > 1:
            for idx in range(nxt - 1, end_idx + 1):
                enclosing[idx] = end_idx

    while i < len(steps):
        st = steps[i]
        end_idx = enclosing.get(i)
        k = loop_iter.get(end_idx, 0) if end_idx is not None else 0
        target = float(st["ZoneATarget"]) + float(st.get("RampPerCycleCelsius", 0)) * k
        hold = int(st["HoldSeconds"]) + int(st.get("RampPerCycleSeconds", 0)) * k
        req_rate = float(st.get("RampRate", 0))
        rate = min(NOMINAL_RAMP_C_PER_S, req_rate) if req_rate > 0 else NOMINAL_RAMP_C_PER_S
        total += abs(target - temp) / rate + hold
        temp = target

        nxt = int(st.get("NextStep", 0))
        cycles = int(st.get("Cycles", 1))
        if nxt and cycles > 1:
            if k + 1 < cycles:
                loop_iter[i] = k + 1
                i = nxt - 1
                continue
            loop_iter.pop(i, None)
        elif cycles > 1:
            for rep in range(1, cycles):
                t_rep = float(st["ZoneATarget"]) + float(st.get("RampPerCycleCelsius", 0)) * rep
                h_rep = int(st["HoldSeconds"]) + int(st.get("RampPerCycleSeconds", 0)) * rep
                total += abs(t_rep - temp) / rate + h_rep
                temp = t_rep
        i += 1
    return max(1, round(total))


class PcrSim(SimDevice):
    """SimPy model of PCR002 calibrated against GW_001 captures."""

    FILES = ["Iscript", "ITAQFST", "NEST2", "NEST3", "RD2", "RD3", "TAQ", "TCHDOWN"]
    FILE_SECONDS = 5400

    def __init__(self, world) -> None:
        super().__init__(
            world,
            {
                "DoorState": "Closed",
                "LidMotorState": "Lifted",
                "PcrState": "Idle",
                "FaultText": "无故障",
                "FaultCode": 0,
                "InterfaceLock": 0,
                "PcrTotalSeconds": 0,
                "PcrRemainingSeconds": 0,
                "LidTemperature": LID_AMBIENT_C,
                "HeaterLidEnabled": False,
                "HeaterLidTarget": 105.0,
                "TubeMode": "Block",
                "PlateCapacity": 0,
                "WorkingStatus": 0,
            },
        )
        self._run_proc: simpy.Process | None = None
        self._ends_at: float = 0.0
        self._paused_remaining: int = 0
        self._paused_what: dict = {}
        self._lid_updated_at: float = 0.0

    def start_from_props(self) -> None:
        self._lid_updated_at = float(self.env.now)
        rem = int(self.props.get("PcrRemainingSeconds", 0))
        if self.props.get("PcrState") == "Running" and rem > 0:
            self._run_proc = self.env.process(
                self._program(rem, {"resumed": True}, check_start_safety=False)
            )

    def refresh(self) -> None:
        now = float(self.env.now)
        dt = now - self._lid_updated_at
        if dt > 0:
            cur = float(self.props["LidTemperature"])
            lid_on = bool(self.props["HeaterLidEnabled"])
            goal = float(self.props["HeaterLidTarget"]) if lid_on else LID_AMBIENT_C
            step = LID_RAMP_C_PER_S * dt
            if abs(goal - cur) <= step:
                self.props["LidTemperature"] = round(goal, 2)
            else:
                delta = step if goal > cur else -step
                self.props["LidTemperature"] = round(cur + delta, 2)
            self._lid_updated_at = now
        if self.props["PcrState"] == "Running":
            self.props["PcrRemainingSeconds"] = max(0, round(self._ends_at - now))

    def ready_for_arm_truth(self) -> str | None:
        if (
            self.props["DoorState"] != "Open"
            or self.props["LidMotorState"] != "Lifted"
            or self.props["PcrState"] != "Idle"
        ):
            return (
                f"PCR 未处于可交接状态: DoorState={self.props['DoorState']}, "
                f"LidMotorState={self.props['LidMotorState']}, PcrState={self.props['PcrState']}"
            )
        return None

    def OpenDoor(self, params: dict):
        _ = params
        if self.props["LidMotorState"] != "Lifted":
            self.world.accident("PCR 热盖没抬起时开门")
        if self.props["PcrState"] == "Running":
            self.world.accident("PCR 程序运行中开门")
        self.props["DoorState"] = "Moving"
        yield self.env.timeout(4.1)
        self.props["DoorState"] = "Open"
        return {"success": True, "doorState": "Open"}

    def CloseDoor(self, params: dict):
        wait_lid = params.get("WaitLid", True)
        self.props["DoorState"] = "Moving"
        yield self.env.timeout(4.1)
        self.props["DoorState"] = "Closed"
        should_press = (
            self.world.plate_at("pcr") is not None and self.props["LidMotorState"] != "Pressed"
        )
        pressing = self.env.process(self._auto_press()) if should_press else None
        if wait_lid:
            if pressing is not None:
                yield pressing
            elif self.props["LidMotorState"] != "Pressed":
                yield self.env.timeout(20.0)
                raise DeviceFailed("关门后等待热盖下压超时(20000ms)")
        return {"success": True, "doorState": "Closed"}

    def _auto_press(self):
        self.props["LidMotorState"] = "Moving"
        yield self.env.timeout(6.0)
        self.props["LidMotorState"] = "Pressed"

    def SetLidMotor(self, params: dict):
        act = params.get("Action", "Seal")
        if act not in ("Lift", "Press", "Seal"):
            raise DeviceFailed(f"未知热盖动作: {act}")
        if act == "Lift" and self.props["PcrState"] == "Running":
            self.world.accident("PCR 程序运行中抬热盖")
        if act in ("Press", "Seal") and self.props["DoorState"] != "Closed":
            self.world.accident("PCR 门开着压热盖")
        self.props["LidMotorState"] = "Sealing" if act == "Seal" else "Moving"
        yield self.env.timeout(8.0)
        if act == "Seal":
            self.world.record("pcr", "sealed")
        self.props["LidMotorState"] = "Pressed" if act == "Press" else "Lifted"
        return {"success": True, "action": act, "lidMotorState": self.props["LidMotorState"]}

    def SetHeater(self, params: dict):
        self.refresh()
        zone = params.get("Zone", "Lid")
        enabled = bool(params.get("Enabled", True))
        target = params.get("TargetCelsius")
        if enabled and (
            self.props["DoorState"] != "Closed" or self.props["LidMotorState"] != "Pressed"
        ):
            self.world.accident("PCR 加热开始时门没关或热盖没压下")
        yield self.env.timeout(0.2)
        if zone == "Lid":
            self.props["HeaterLidEnabled"] = enabled
            if target is not None:
                self.props["HeaterLidTarget"] = float(target)
        self._lid_updated_at = float(self.env.now)
        return {"success": True, "zone": zone, "enabled": enabled}

    def SetPlateCapacity(self, params: dict):
        ul = int(params.get("Microliters", 100))
        yield self.env.timeout(0.2)
        self.props["PlateCapacity"] = ul
        return {"success": True, "microliters": ul}

    def SetTubeMode(self, params: dict):
        mode = params.get("Mode", "Block")
        if mode not in ("Block", "Tube"):
            raise DeviceFailed(f"未知模式: {mode}")
        yield self.env.timeout(0.2)
        self.props["TubeMode"] = mode
        return {"success": True, "mode": mode}

    def ListFiles(self, params: dict):
        _ = params
        yield self.env.timeout(0.5)
        files = [
            {"index": idx, "folder": "Monad", "file": name, "fullName": f"Monad/{name}"}
            for idx, name in enumerate(self.FILES)
        ]
        return {"success": True, "totalFiles": len(files), "returned": len(files), "files": files}

    def _interrupt_active(self, cause: str) -> None:
        if self._run_proc is not None and self._run_proc.is_alive:
            self._run_proc.interrupt(cause)

    def RunProgram(self, params: dict):
        raw = params["Steps"]
        steps = json.loads(raw) if isinstance(raw, str) else raw
        self._interrupt_active("replaced")
        seconds = program_seconds(steps)
        self._run_proc = self.env.process(self._program(seconds, {"steps": steps}))
        yield self.env.timeout(1.0)
        self.refresh()
        return {"success": True, "totalSeconds": seconds}

    def RunFile(self, params: dict):
        folder = params.get("Folder", "Monad")
        file_name = params.get("File", "")
        if folder != "Monad" or file_name not in self.FILES:
            raise DeviceFailed(f"文件不存在: {folder}/{file_name}")
        self._interrupt_active("replaced")
        self._run_proc = self.env.process(
            self._program(self.FILE_SECONDS, {"file": f"{folder}/{file_name}"})
        )
        yield self._run_proc
        return {"success": True, "file": f"{folder}/{file_name}"}

    def PauseRun(self, params: dict):
        _ = params
        if self.props["PcrState"] != "Running":
            raise DeviceFailed("空闲态没有运行中的程序,无法暂停")
        self.refresh()
        self._paused_remaining = max(1, int(self.props["PcrRemainingSeconds"]))
        self._interrupt_active("paused")
        yield self.env.timeout(0.5)
        return {"success": True, "pcrState": "Paused"}

    def StartRun(self, params: dict):
        _ = params
        if self.props["PcrState"] != "Paused" or self._paused_remaining <= 0:
            raise DeviceFailed("空闲态没有可继续的程序")
        rem = self._paused_remaining
        what = self._paused_what
        self._paused_remaining = 0
        self._run_proc = self.env.process(self._program(rem, what))
        yield self.env.timeout(0.5)
        self.refresh()
        return {"success": True, "pcrState": "Running"}

    def StopRun(self, params: dict):
        _ = params
        self._interrupt_active("stopped")
        self._paused_remaining = 0
        self.props.update(PcrState="Idle", PcrRemainingSeconds=0)
        yield self.env.timeout(1.0)
        return {"success": True, "pcrState": "Idle"}

    def _program(self, seconds: int, what: dict, *, check_start_safety: bool = True):
        if check_start_safety and (
            self.props["DoorState"] != "Closed" or self.props["LidMotorState"] != "Pressed"
        ):
            self.world.accident("PCR 加热开始时门没关或热盖没压下")
        self._paused_what = what
        self.props.update(
            PcrState="Running",
            PcrTotalSeconds=seconds,
            PcrRemainingSeconds=seconds,
        )
        started = float(self.env.now)
        self._ends_at = started + seconds
        try:
            yield self.env.timeout(seconds)
            outcome = "completed"
        except simpy.Interrupt as intr:
            outcome = str(intr.cause)
            if outcome == "paused":
                self.props["PcrState"] = "Paused"
                return
        self.props.update(PcrState="Idle", PcrRemainingSeconds=0)
        elapsed_s = round(float(self.env.now) - started)
        self.world.record("pcr", "pcr_program", outcome=outcome, seconds=elapsed_s, **what)


SIM_CLASS = PcrSim
