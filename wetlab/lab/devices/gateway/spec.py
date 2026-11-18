"""Gateway built-in read-only RPC functions answered by GW_001 on any device topic."""

from ..base import Action, READ

ACTIONS = {
    "GetDevices": Action(READ),
    "GetDrivers": Action(READ),
}


def result_ok(action: str, data: dict) -> tuple[bool, str]:
    """Judge gateway built-in read RPC (`GetDevices`, `GetDrivers`)."""
    dd = data.get("deviceData")
    if isinstance(dd, str):
        return False, dd or f"gateway {action} failed"
    if data.get("success") is True and dd is not None:
        return True, ""
    return False, str(data.get("description") or f"gateway {action} failed")

