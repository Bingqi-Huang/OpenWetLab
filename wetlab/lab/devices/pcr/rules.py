"""Interlock, stop-classification, fault, and arm-handoff rules for the PCR thermal cycler."""

import json

PCR_PROGRAM_START = {"RunProgram", "RunFile", "StartRun"}
LID_TEMP_C = (30.0, 110.0)
PLATE_CAPACITY_UL = (1, 255)

# Official 0x03C0 limits from docs/自动化热封PCR仪属性定义-Rev 5.pdf.
PCR_PROGRAM = {
    "max_steps": 30,
    "temp_c": (4.0, 99.0),
    "max_gradient_c": 36.0,
    "hold_s": (1, 3600),
    "ramp_per_cycle_c": (-99.0, 99.0),
    "ramp_per_cycle_s": (-3600, 3600),
    "cycles": (1, 99),
    "ramp_rate_c_per_s": (0.0, 4.0),
}


def _is_number(val: object) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _is_int(val: object) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def is_stop(action: str, params: dict) -> bool:
    """Return True when `(action, params)` turns off the PCR heated lid."""
    return (
        action == "SetHeater"
        and params.get("Zone") == "Lid"
        and params.get("Enabled") is False
    )


def fault(state: dict) -> str | None:
    """Return PCR fault description from telemetry state, or None."""
    s = state.get("pcr") or {}
    fault_text = s.get("FaultText")
    if fault_text not in (None, "无故障"):
        return f"PCR fault: {fault_text}"
    if s.get("PcrState") == "Fault":
        return "PCR fault: PcrState=Fault"
    return None


def validate_pcr_steps(steps: object) -> str | None:
    """Validate a RunProgram step list against official 0x03C0 rules."""
    lim = PCR_PROGRAM
    if not isinstance(steps, list) or not (1 <= len(steps) <= lim["max_steps"]):
        return f"program needs 1..{lim['max_steps']} steps"
    loops: list[tuple[int, int]] = []
    for i, st in enumerate(steps, start=1):
        if not isinstance(st, dict):
            return f"step {i}: step must be an object"
        try:
            a = float(st["ZoneATarget"])
            c = float(st["ZoneCTarget"])
            hold = int(st["HoldSeconds"])
            cyc = int(st.get("Cycles", 1))
            rc = float(st.get("RampPerCycleCelsius", 0))
            rs = int(st.get("RampPerCycleSeconds", 0))
            nxt = int(st.get("NextStep", 0))
            rate = float(st.get("RampRate", 0))
        except (KeyError, TypeError, ValueError) as exc:
            return f"step {i}: missing or bad field ({exc})"
        if not (lim["cycles"][0] <= cyc <= lim["cycles"][1]):
            return f"step {i}: Cycles={cyc} outside {lim['cycles']}"
        if not (lim["hold_s"][0] <= hold <= lim["hold_s"][1]):
            return f"step {i}: HoldSeconds={hold} outside {lim['hold_s']}"
        if not (lim["ramp_per_cycle_c"][0] <= rc <= lim["ramp_per_cycle_c"][1]):
            return f"step {i}: RampPerCycleCelsius={rc} out of range"
        if not (lim["ramp_per_cycle_s"][0] <= rs <= lim["ramp_per_cycle_s"][1]):
            return f"step {i}: RampPerCycleSeconds={rs} out of range"
        if not (lim["ramp_rate_c_per_s"][0] <= rate <= lim["ramp_rate_c_per_s"][1]):
            return f"step {i}: RampRate={rate} outside {lim['ramp_rate_c_per_s']}"
        last = cyc - 1
        for name, t in (("ZoneATarget", a), ("ZoneCTarget", c)):
            for v in (t, t + rc * last):
                if not (lim["temp_c"][0] <= v <= lim["temp_c"][1]):
                    return f"step {i}: {name} reaches {v} C, allowed {lim['temp_c']}"
        if abs(c - a) > lim["max_gradient_c"]:
            return f"step {i}: zone C/A difference {abs(c - a)} C > {lim['max_gradient_c']}"
        if not (lim["hold_s"][0] <= hold + rs * last <= lim["hold_s"][1]):
            return f"step {i}: hold time leaves {lim['hold_s']} after ramping"
        if nxt and cyc > 1:
            if not (1 <= nxt < i):
                return f"step {i}: NextStep={nxt} must jump back to an earlier step"
            loops.append((nxt, i))
    loops.sort()
    for (s1, e1), (s2, e2) in zip(loops, loops[1:]):
        if s2 <= e1:
            return f"loops {s1}-{e1} and {s2}-{e2} overlap (nesting not allowed)"
    return None


def check(action: str, params: dict, state: dict) -> str | None:
    """Return a rejection reason for a PCR action, or None if allowed."""
    s = state.get("pcr") or {}
    if action not in ("StopRun", "PauseRun") and not is_stop(action, params):
        fault_err = fault(state)
        if fault_err:
            return fault_err
    # 0x0204: 1 = the touch screen holds the lock and 485 commands are ignored; 2 = the 485
    # side (our gateway) holds it, which is the state we control the PCR in.
    if s.get("InterfaceLock") == 1:
        lock_desc = s.get("InterfaceLockText", s.get("InterfaceLock"))
        return f"PCR touch screen holds the interface lock ({lock_desc})"

    if action == "RunProgram":
        raw = params.get("Steps")
        try:
            steps = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return "Steps is not valid JSON"
        err = validate_pcr_steps(steps)
        if err:
            return f"invalid PCR program: {err}"

    if action in PCR_PROGRAM_START:
        if s.get("DoorState") != "Closed":
            return f"door must be Closed before heating, is {s.get('DoorState')}"
        if s.get("LidMotorState") != "Pressed":
            return f"lid must be Pressed before heating, is {s.get('LidMotorState')}"
        if action in ("RunProgram", "RunFile") and s.get("PcrState") != "Idle":
            return f"PCR must be Idle to start a program, is {s.get('PcrState')}"

    if action == "OpenDoor":
        if s.get("PcrState") != "Idle":
            return "PCR must be Idle to open the door"
        if s.get("LidMotorState") != "Lifted":
            return f"lift the lid before opening the door (lid is {s.get('LidMotorState')})"

    if action == "CloseDoor" and params.get("WaitLid") is not False:
        return "CloseDoor must pass WaitLid=false (otherwise the driver waits 20 s for the lid)"

    if action == "SetLidMotor":
        act = params.get("Action")
        if act not in ("Lift", "Press"):
            return "SetLidMotor needs an explicit Action of Lift or Press"
        if act == "Lift" and s.get("PcrState") != "Idle":
            return "do not lift the lid while a program runs"
        if act == "Press" and s.get("DoorState") != "Closed":
            return "press the lid only with the door closed"

    if action == "SetHeater":
        if params.get("Zone") != "Lid":
            return f"SetHeater Zone must be Lid, got {params.get('Zone')!r}"
        enabled = params.get("Enabled")
        if not isinstance(enabled, bool):
            return "SetHeater needs boolean Enabled"
        tc = params.get("TargetCelsius")
        if enabled:
            if s.get("DoorState") != "Closed":
                return f"door must be Closed before heating lid, is {s.get('DoorState')}"
            if s.get("LidMotorState") != "Pressed":
                return f"lid must be Pressed before heating lid, is {s.get('LidMotorState')}"
            if not _is_number(tc) or not (LID_TEMP_C[0] <= tc <= LID_TEMP_C[1]):
                return f"TargetCelsius={tc} outside {LID_TEMP_C}"
        elif tc is not None:
            if not _is_number(tc) or not (LID_TEMP_C[0] <= tc <= LID_TEMP_C[1]):
                return f"TargetCelsius={tc} outside {LID_TEMP_C}"

    if action == "SetPlateCapacity":
        ul = params.get("Microliters")
        if not _is_int(ul) or not (PLATE_CAPACITY_UL[0] <= ul <= PLATE_CAPACITY_UL[1]):
            return f"Microliters={ul} outside {PLATE_CAPACITY_UL}"

    if action == "SetTubeMode":
        if params.get("Mode") != "Block":
            return f"SetTubeMode Mode must be Block, got {params.get('Mode')!r}"

    return None


def ready_for_arm(state: dict) -> str | None:
    """Return why the arm must not enter the PCR, or None if safe."""
    s = state.get("pcr") or {}
    if s.get("DoorState") != "Open":
        return f"PCR door must be Open, is {s.get('DoorState')}"
    if s.get("LidMotorState") != "Lifted":
        return f"PCR lid must be Lifted, is {s.get('LidMotorState')}"
    if s.get("PcrState") != "Idle":
        return f"PCR must be Idle, is {s.get('PcrState')}"
    return None
