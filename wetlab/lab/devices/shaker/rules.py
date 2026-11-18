"""Interlock, fault, and arm-handoff rules for the thermo-shaker module."""

SHAKER_SPEED_RPM = (200, 3000)
SHAKER_TEMP_C = (-10, 120)
SHAKER_MAX_SECONDS = 172800
FAULT_EXEMPT_ACTIONS = {"ClearError"}


def _is_int(val: object) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _is_number(val: object) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def fault(state: dict) -> str | None:
    """Return shaker fault description from telemetry state, or None."""
    s = state.get("shaker") or {}
    if s.get("ErrorCode") not in (None, 0):
        return f"shaker error code {s.get('ErrorCode')}"
    return None


def check(action: str, params: dict, state: dict) -> str | None:
    """Return a rejection reason for a shaker action, or None if allowed."""
    s = state.get("shaker") or {}
    if action not in ("StopShake", "EmergencyStop", "ClearError"):
        fault_err = fault(state)
        if fault_err:
            return fault_err

    if action == "SetTargetSpeed":
        rpm = params.get("rpm")
        if not _is_int(rpm) or not (SHAKER_SPEED_RPM[0] <= rpm <= SHAKER_SPEED_RPM[1]):
            return f"rpm={rpm} outside {SHAKER_SPEED_RPM}"

    if action == "SetTargetTemperature":
        c = params.get("celsius")
        if not _is_number(c) or not (SHAKER_TEMP_C[0] <= c <= SHAKER_TEMP_C[1]):
            return f"celsius={c} outside {SHAKER_TEMP_C}"

    if action in ("StartShake", "StartShakeTimed"):
        if s.get("ActualSpeed") not in (None, 0) or s.get("ShakeState") == 0:
            return "shaker is already turning"
        if s.get("ElmState") != 1:
            return "clamp the plate (SetClamp true) before shaking"
        if action == "StartShakeTimed":
            sec = params.get("seconds")
            if not _is_int(sec) or not (1 <= sec <= SHAKER_MAX_SECONDS):
                return f"seconds={sec} outside 1..{SHAKER_MAX_SECONDS}"

    if action == "SetClamp":
        clamp = params.get("clamp")
        if not isinstance(clamp, bool):
            return "SetClamp needs boolean clamp"
        if clamp is False and (s.get("ActualSpeed") not in (None, 0) or s.get("ShakeState") == 0):
            return "do not release the clamp while shaking"

    return None


def ready_for_arm(state: dict) -> str | None:
    """Return why the arm must not enter the shaker, or None if safe."""
    s = state.get("shaker") or {}
    if s.get("ActualSpeed") not in (0, None) or s.get("ShakeState") != 1:
        return "shaker must be stopped and homed"
    if s.get("ElmState") != 2:
        return "shaker clamp must be released (ElmState 2)"
    return None
