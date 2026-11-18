"""Specification for the LK-TECH steering turntable (STEERING_PLATFORM001 on GW_001)."""

from ..base import ACT, Action, Prop

NAME = "turntable"
LABEL = "转向台"
CODE = "STEERING_PLATFORM001"

ACTIONS = {
    "SingleLoop": Action(ACT, moves=True),
    "Stop": Action(ACT, stop=True),
}

STATE_PROPS = ["States", "Angles"]
KEY_PROPS = ["States", "Angles"]
PROPS = {
    "Angles": Prop("角度", unit="°"),
    "States": Prop("电机", values={"Enabled": "已使能"}),
}


def result_ok(action: str, data: dict) -> tuple[bool, str]:
    """Judge turntable RPC result (untested on hardware; verify on first live run)."""
    dd = data.get("deviceData")
    if isinstance(dd, str):
        return False, dd or f"turntable {action} failed"
    if isinstance(dd, dict) and (dd.get("success") is False or dd.get("ok") is False):
        err = dd.get("error") or dd.get("message") or data.get("description")
        return False, str(err or f"turntable {action} failed")
    if data.get("success") is True:
        return True, ""
    return False, str(data.get("description") or f"turntable {action} failed")

