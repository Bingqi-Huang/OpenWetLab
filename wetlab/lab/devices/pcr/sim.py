"""Virtual SimPy model and referee checks for the Arhat-auto 96 PCR cycler."""

import json
import math

import simpy

from ...errors import DeviceFailed
from ..base import SimDevice

AMBIENT_TEMP_C = 25.0
LID_AMBIENT_C = 27.0
HEAT_RAMP_C_PER_S = 2.0
COOL_DECAY_PER_S = 0.029
PREP_SECONDS = 20.0
LID_RAMP_C_PER_S = 1.5
LID_MOVE_SECONDS = 5.0

_INIT_PROPS = dict(
    DoorState="Closed", LidMotorState="Lifted", PcrState="Idle", FaultText="无故障",
    FaultCode=0, InterfaceLock=0, PcrTotalSeconds=0, PcrRemainingSeconds=0,
    LidTemperature=LID_AMBIENT_C, HeaterLidEnabled=False, HeaterLidTarget=105.0,
    TubeMode="Block", PlateCapacity=0, WorkingStatus=0,
)


def _ramp_seconds(t_from: float, t_to: float, req_rate: float) -> float:
    if t_to > t_from:
        rate = min(HEAT_RAMP_C_PER_S, req_rate) if req_rate > 0 else HEAT_RAMP_C_PER_S
        return (t_to - t_from) / rate
    if t_to < t_from:
        cool_s = math.log(t_from / t_to) / COOL_DECAY_PER_S
        return max(cool_s, (t_from - t_to) / req_rate) if req_rate > 0 else cool_s
    return 0.0


def _build_phases(steps: list[dict], *, lid_on: bool) -> list[tuple[float, bool | None]]:
    init_lid = [(1.0, True), (10.0, False), (9.0, True)]
    phases: list[tuple[float, bool | None]] = init_lid if lid_on else [(PREP_SECONDS, False)]
    enclosing = {
        idx: end_idx
        for end_idx, st in enumerate(steps)
        if int(st.get("NextStep", 0)) and int(st.get("Cycles", 1)) > 1
        for idx in range(int(st["NextStep"]) - 1, end_idx + 1)
    }
    loop_iter: dict[int, int] = {}
    temp = AMBIENT_TEMP_C
    lid_active = lid_on
    i = 0
    while i < len(steps):
        st = steps[i]
        end_idx = enclosing.get(i)
        k = loop_iter.get(end_idx, 0) if end_idx is not None else 0
        req_rate = float(st.get("RampRate", 0))
        nxt = int(st.get("NextStep", 0))
        cycles = int(st.get("Cycles", 1))
        for rep in ([k] if (nxt and cycles > 1) else range(max(1, cycles))):
            target = float(st["ZoneATarget"]) + float(st.get("RampPerCycleCelsius", 0)) * rep
            hold = int(st["HoldSeconds"]) + int(st.get("RampPerCycleSeconds", 0)) * rep
            lid_active = False if target <= 4.0 else lid_active
            phases.append((_ramp_seconds(temp, target, req_rate) + hold, lid_active))
            temp = target
        if nxt and cycles > 1 and k + 1 < cycles:
            loop_iter[i] = k + 1
            i = nxt - 1
            continue
        loop_iter.pop(i, None)
        i += 1
    return phases


def program_seconds(steps: list[dict]) -> int:
    """Compute total PCR program run time in seconds (20s prep + ramps + holds)."""
    return max(1, round(sum(dur for dur, _ in _build_phases(steps, lid_on=False))))


class PcrSim(SimDevice):
    """SimPy model of PCR002 calibrated against GW_001 captures."""

    FILES = ["Iscript", "ITAQFST", "NEST2", "NEST3", "RD2", "RD3", "TAQ", "TCHDOWN"]
    FILE_SECONDS = 5400

    def __init__(self, world) -> None:
        super().__init__(world, dict(_INIT_PROPS))
        self._run_proc: simpy.Process | None = None
        self._lid_proc: simpy.Process | None = None
        self._ends_at: float = 0.0
        self._paused_phases: list[tuple[float, bool | None]] = []
        self._paused_what: dict = {}
        self._lid_updated_at: float = 0.0

    def start_from_props(self) -> None:
        self._lid_updated_at = float(self.env.now)
        self.props["PlateCapacity"] = 0
        rem = int(self.props.get("PcrRemainingSeconds", 0))
        if self.props.get("PcrState") == "Running" and rem > 0:
            self._start([(float(rem), None)], {"resumed": True}, check_safety=False)

    def refresh(self) -> None:
        self.props["PlateCapacity"] = 0
        now = float(self.env.now)
        dt = now - self._lid_updated_at
        if dt > 0:
            cur = float(self.props["LidTemperature"])
            goal = float(self.props["HeaterLidTarget"]) if self.props["HeaterLidEnabled"] else 27.0
            step = LID_RAMP_C_PER_S * dt
            self.props["LidTemperature"] = round(cur + max(-step, min(step, goal - cur)), 2)
            self._lid_updated_at = now
        if self.props["PcrState"] == "Running":
            self.props["PcrRemainingSeconds"] = max(0, round(self._ends_at - now))

    def ready_for_arm_truth(self) -> str | None:
        p = self.props
        if p["DoorState"] != "Open" or p["LidMotorState"] != "Lifted" or p["PcrState"] != "Idle":
            msg = f"DoorState={p['DoorState']}, LidMotorState={p['LidMotorState']}"
            return f"PCR 未处于可交接状态: {msg}, PcrState={p['PcrState']}"
        return None

    def _check_closed_pressed(self) -> None:
        if self.props["DoorState"] != "Closed" or self.props["LidMotorState"] != "Pressed":
            self.world.accident("PCR 加热开始时门没关或热盖没压下")

    def _interrupt_lid(self) -> None:
        if self._lid_proc is not None and self._lid_proc.is_alive:
            self._lid_proc.interrupt("replaced")

    def _move_lid(self, act: str, seconds: float):
        self.props["LidMotorState"] = "Sealing" if act == "Seal" else "Moving"
        try:
            yield self.env.timeout(seconds)
        except simpy.Interrupt:
            return
        if act == "Seal":
            self.world.record("pcr", "sealed")
        self.props["LidMotorState"] = "Pressed" if act == "Press" else "Lifted"

    def OpenDoor(self, params: dict):
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
        pressing = None
        if self.world.plate_at("pcr") is not None and self.props["LidMotorState"] != "Pressed":
            self._interrupt_lid()
            self.props["LidMotorState"] = "Moving"
            pressing = self.env.process(self._move_lid("Press", 6.0))
            self._lid_proc = pressing
        if wait_lid and pressing is not None:
            yield pressing
        elif wait_lid and self.props["LidMotorState"] != "Pressed":
            yield self.env.timeout(20.0)
            raise DeviceFailed("关门后等待热盖下压超时(20000ms)")
        return {"success": True, "doorState": "Closed"}

    def SetLidMotor(self, params: dict):
        act = params.get("Action", "Seal")
        if act not in ("Lift", "Press", "Seal"):
            raise DeviceFailed(f"未知热盖动作: {act}")
        if act == "Lift" and self.props["PcrState"] == "Running":
            self.world.accident("PCR 程序运行中抬热盖")
        if act in ("Press", "Seal") and self.props["DoorState"] != "Closed":
            self.world.accident("PCR 门开着压热盖")
        self._interrupt_lid()
        self.props["LidMotorState"] = "Sealing" if act == "Seal" else "Moving"
        self._lid_proc = self.env.process(self._move_lid(act, LID_MOVE_SECONDS))
        yield self.env.timeout(0.2)
        return {"success": True, "action": act, "lidMotorState": self.props["LidMotorState"]}

    def SetHeater(self, params: dict):
        self.refresh()
        zone = params.get("Zone", "Lid")
        enabled = bool(params.get("Enabled", True))
        if enabled:
            self._check_closed_pressed()
        yield self.env.timeout(0.2)
        if zone == "Lid":
            self.props["HeaterLidEnabled"] = enabled
            if params.get("TargetCelsius") is not None:
                self.props["HeaterLidTarget"] = float(params["TargetCelsius"])
        self._lid_updated_at = float(self.env.now)
        return {"success": True, "zone": zone, "enabled": enabled}

    def SetPlateCapacity(self, params: dict):
        ul = int(params.get("Microliters", 100))
        yield self.env.timeout(0.1)
        self.props["PlateCapacity"] = 0
        return {"success": True, "plateCapacity": ul, "microliters": ul}

    def SetTubeMode(self, params: dict):
        mode = params.get("Mode", "Block")
        if mode not in ("Block", "Tube"):
            raise DeviceFailed(f"未知模式: {mode}")
        yield self.env.timeout(0.1)
        self.props["TubeMode"] = mode
        return {"success": True, "mode": mode}

    def ListFiles(self, params: dict):
        yield self.env.timeout(0.5)
        files = [
            {"index": idx, "folder": "Monad", "file": name, "fullName": f"Monad/{name}"}
            for idx, name in enumerate(self.FILES)
        ]
        return {"success": True, "totalFiles": len(files), "returned": len(files), "files": files}

    def _interrupt_active(self, cause: str) -> None:
        if self._run_proc is not None and self._run_proc.is_alive:
            self._run_proc.interrupt(cause)

    def _start(self, phases: list[tuple[float, bool | None]], what: dict, *, check_safety=True):
        self._interrupt_active("replaced")
        self._run_proc = self.env.process(self._program(phases, what, check_safety=check_safety))

    def RunProgram(self, params: dict):
        raw = params["Steps"]
        steps = json.loads(raw) if isinstance(raw, str) else raw
        phases = _build_phases(steps, lid_on=bool(self.props["HeaterLidEnabled"]))
        seconds = max(1, round(sum(dur for dur, _ in phases)))
        self._start(phases, {"steps": steps})
        yield self.env.timeout(0.25)
        self.refresh()
        return {"success": True, "totalSeconds": seconds}

    def RunFile(self, params: dict):
        full = f"{params.get('Folder', 'Monad')}/{params.get('File', '')}"
        if params.get("Folder", "Monad") != "Monad" or params.get("File", "") not in self.FILES:
            raise DeviceFailed(f"文件不存在: {full}")
        self._start([(float(self.FILE_SECONDS), None)], {"file": full})
        assert self._run_proc is not None
        yield self._run_proc
        return {"success": True, "file": full}

    def PauseRun(self, params: dict):
        if self.props["PcrState"] != "Running":
            raise DeviceFailed("空闲态没有运行中的程序,无法暂停")
        self.refresh()
        self._interrupt_active("paused")
        yield self.env.timeout(0.37)
        return {"success": True, "pcrState": "Paused"}

    def StartRun(self, params: dict):
        if self.props["PcrState"] != "Paused" or not self._paused_phases:
            raise DeviceFailed("空闲态没有可继续的程序")
        phases = self._paused_phases
        self._paused_phases = []
        self._start(phases, self._paused_what, check_safety=False)
        yield self.env.timeout(0.36)
        self.refresh()
        return {"success": True, "pcrState": "Running"}

    def StopRun(self, params: dict):
        self._interrupt_active("stopped")
        self._paused_phases = []
        self.props.update(PcrState="Idle", PcrRemainingSeconds=0)
        yield self.env.timeout(0.53)
        return {"success": True, "pcrState": "Idle"}

    def _program(self, phases: list[tuple[float, bool | None]], what: dict, *, check_safety=True):
        if check_safety:
            self._check_closed_pressed()
        total_s = sum(dur for dur, _ in phases)
        tot = max(1, round(total_s))
        self._paused_what = what
        self.props.update(PcrState="Running", PcrTotalSeconds=tot, PcrRemainingSeconds=tot)
        started = float(self.env.now)
        self._ends_at = started + total_s
        idx = 0
        phase_started = started
        try:
            for idx, (dur, lid_val) in enumerate(phases):
                if lid_val is not None:
                    self.refresh()
                    self.props["HeaterLidEnabled"] = lid_val
                phase_started = float(self.env.now)
                if dur > 0:
                    yield self.env.timeout(dur)
            outcome = "completed"
        except simpy.Interrupt as intr:
            outcome = str(intr.cause)
            if outcome == "paused":
                rem_cur = max(0.0, phases[idx][0] - (float(self.env.now) - phase_started))
                self._paused_phases = [(rem_cur, phases[idx][1]), *phases[idx + 1 :]]
                self.props["PcrState"] = "Paused"
                return
        self.props.update(PcrState="Idle", PcrRemainingSeconds=0)
        elapsed_s = round(float(self.env.now) - started)
        self.world.record("pcr", "pcr_program", outcome=outcome, seconds=elapsed_s, **what)


SIM_CLASS = PcrSim
