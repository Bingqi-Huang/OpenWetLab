"""Interlock, fault, and arm-handoff rules for the steering turntable."""

ALLOWED_ANGLES = {0, 90, 180, 270}


def fault(state: dict) -> str | None:
    """Return steering turntable fault description from telemetry state, or None."""
    _ = state
    return None


def check(action: str, params: dict, state: dict) -> str | None:
    """Return a rejection reason for a turntable action, or None if allowed."""
    if action != "SingleLoop":
        return None
    arm = state.get("arm") or {}
    if arm.get("RobotMode") == "运行中" or arm.get("WorkingStatus") == 1:
        return "do not rotate the turntable while the arm is moving"
    s = state.get("turntable") or {}
    if s.get("States") != "Enabled":
        return f"turntable not enabled ({s.get('States')})"
    ang = params.get("Angle")
    if isinstance(ang, bool) or ang not in ALLOWED_ANGLES:
        return f"Angle={ang}: only 0/90/180/270 allowed until tested"
    return None


def ready_for_arm(state: dict) -> str | None:
    """Return why the arm must not access the turntable, or None if safe."""
    s = state.get("turntable") or {}
    if s.get("States") != "Enabled":
        return f"turntable not enabled ({s.get('States')})"
    return None
