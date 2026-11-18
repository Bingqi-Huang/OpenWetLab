"""Specification for the storage rack placeholder device (T006001 on GW_001)."""

from ..base import Action, Prop, READ

NAME = "rack"
LABEL = "料架"
CODE = "T006001"

ACTIONS = {
    "GetStatus": Action(READ),
}

STATE_PROPS = ["rackState", "errorCode"]
KEY_PROPS = ["rackState", "errorCode"]
PROPS = {
    "rackState": Prop("料架状态", values={0: "正常"}),
    "errorCode": Prop("错误码", values={0: "无"}),
}


def result_ok(action: str, data: dict) -> tuple[bool, str]:
    """Judge rack RPC result (placeholder driver; verify on first live run)."""
    dd = data.get("deviceData")
    if isinstance(dd, str):
        return False, dd or f"rack {action} failed"
    if isinstance(dd, dict) and (dd.get("success") is False or dd.get("ok") is False):
        err = dd.get("error") or dd.get("message") or data.get("description")
        return False, str(err or f"rack {action} failed")
    if data.get("success") is True:
        return True, ""
    return False, str(data.get("description") or f"rack {action} failed")

