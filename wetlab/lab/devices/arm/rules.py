"""Interlock, fault, and touched-position rules for the robot arm."""

ARM_SPEEDS = {"低速"}


def fault(state: dict) -> str | None:
    """Return robot arm fault description from telemetry state, or None."""
    arm = state.get("arm") or {}
    if arm.get("SafetyStatus") not in (None, "NORMAL"):
        return f"arm safety status: {arm.get('SafetyStatus')}"
    return None


def check(action: str, params: dict, state: dict) -> str | None:
    """Return a rejection reason for an arm action, or None if allowed."""
    if action != "MovMulti":
        return None
    arm = state.get("arm") or {}
    if arm.get("RobotMode") != "使能空闲" or arm.get("SafetyStatus") != "NORMAL":
        return f"arm not ready: mode={arm.get('RobotMode')} safety={arm.get('SafetyStatus')}"
    if arm.get("GripperCommStatus") != "CONNECTED":
        return "gripper not connected"
    tasks = params.get("Tasks")
    if not isinstance(tasks, list) or not tasks:
        return "MovMulti blocked: Tasks must be a non-empty list"
    for i, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            return f"MovMulti task {i}: must be a dict"
        pos_id = task.get("PositionId")
        if isinstance(pos_id, bool) or not isinstance(pos_id, int):
            return f"MovMulti task {i}: PositionId must be an integer"
    if (params.get("Material") or {}).get("Speed") not in ARM_SPEEDS:
        return f"arm Speed must be one of {sorted(ARM_SPEEDS)}"
    return None


def positions_touched(action: str, params: dict) -> list[str]:
    """Return taught point IDs touched by an arm motion command."""
    if action != "MovMulti":
        return []
    return [str(t.get("PositionId")) for t in (params.get("Tasks") or [])]
