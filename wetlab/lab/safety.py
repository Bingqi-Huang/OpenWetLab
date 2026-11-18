"""Island-wide interlock entry point (docs/spec/04 section 5.6)."""

from .devices import (
    BY_NAME,
    ISLAND,
    POINT_TO_POSITION,
    POSITION_DEVICE,
    POSITIONS,
    TAUGHT_POINTS,
    gateway,
)
from .devices.base import Action, READ

MAX_STATE_AGE_S = 10


def _lookup_action(device: str, action: str) -> Action | None:
    dev = BY_NAME.get(device)
    if dev is None:
        return None
    return dev.spec.ACTIONS.get(action) or gateway.spec.ACTIONS.get(action)


def kind(device: str, action: str) -> str | None:
    """Return 'read', 'act', or None if the device/action is not allowed."""
    act = _lookup_action(device, action)
    return act.kind if act is not None else None


def is_stop(device: str, action: str, params: dict | None = None) -> bool:
    """Return True when `(device, action, params)` is a stop or safe-shutdown command."""
    act = _lookup_action(device, action)
    if act is None:
        return False
    if act.stop:
        return True
    dev = BY_NAME[device]
    rule_is_stop = getattr(dev.rules, "is_stop", None) if dev.rules is not None else None
    if rule_is_stop is not None:
        return bool(rule_is_stop(action, params or {}))
    return False


def device_fault(device: str, state: dict) -> str | None:
    """Return any active error or fault reason on `device`, or None."""
    dev = BY_NAME[device]
    s = state.get(device) or {}
    if s.get("_error"):
        return f"{device} error: {s['_error']}"
    if s.get("WorkingStatus") == 2:
        return f"{device} reports WorkingStatus=2 (fault); read its error first"
    if dev.rules is not None:
        return dev.rules.fault(state)
    return None


def _telemetry_error(device: str, state: dict) -> str | None:
    dev = BY_NAME[device]
    s = state.get(device) or {}
    age = s.get("_age_s")
    if age is None or age > MAX_STATE_AGE_S:
        return f"no fresh telemetry for {device} (age {age}s)"
    missing = [prop for prop in dev.spec.STATE_PROPS if s.get(prop) is None]
    if missing:
        return f"{device} missing state properties: {missing}"
    return None


def check(device: str, action: str, params: dict | None, state: dict) -> tuple[bool, str]:
    """Run all 8 ordered interlock checks. Return (ok, reason)."""
    call_params = params or {}

    # 1. Allow-list: device must be in ISLAND and action in its ACTIONS (or gateway built-ins).
    act = _lookup_action(device, action)
    if act is None:
        return False, f"{device}.{action} is not on the allow list"

    # 2. Read-only actions are always allowed.
    if act.kind == READ:
        return True, "read-only"

    # 3. Gateway must be online.
    gw = state.get("_gateway") or {}
    if not gw.get("online"):
        return False, "GATEWAY_OFFLINE"

    # 4. Gateway must not be occupied by external commands or busy devices (stop actions exempt).
    stop_action = is_stop(device, action, call_params)
    if not stop_action:
        if gw.get("busy_by_others"):
            return False, "gateway is busy with an external command"
        for d in ISLAND:
            name = d.spec.NAME
            if (state.get(name) or {}).get("WorkingStatus") == 1:
                return False, f"{name} is busy (WorkingStatus=1)"

    # 5. Fresh complete telemetry for target device.
    telem_err = _telemetry_error(device, state)
    if telem_err:
        return False, telem_err

    # 6. Target device must not carry an error or fault (stop actions exempt from WS=2 & fault).
    s = state.get(device) or {}
    if s.get("_error"):
        return False, f"{device} error: {s['_error']}"
    dev = BY_NAME[device]
    if not stop_action:
        if s.get("WorkingStatus") == 2:
            return False, f"{device} reports WorkingStatus=2 (fault); read its error first"
        exempt = getattr(dev.rules, "FAULT_EXEMPT_ACTIONS", ()) if dev.rules is not None else ()
        if action not in exempt and dev.rules is not None:
            fault_err = dev.rules.fault(state)
            if fault_err:
                return False, fault_err

    # 7. Device-local rules.
    if dev.rules is not None:
        err = dev.rules.check(action, call_params, state)
        if err:
            return False, err

    # 8. Cross-device hand-off for arm transfers.
    touched_fn = getattr(dev.rules, "positions_touched", None) if dev.rules is not None else None
    if touched_fn is not None:
        points = touched_fn(action, call_params)
        unknown = [pt for pt in points if pt not in TAUGHT_POINTS]
        if unknown:
            return False, f"{action} blocked: positions not in the taught table {unknown}"
        for pt in points:
            pos_name = POINT_TO_POSITION.get(pt)
            if pos_name is None:
                continue
            target_dev = POSITION_DEVICE[pos_name]
            target_name = target_dev.spec.NAME
            if target_dev.rules is not None:
                label = POSITIONS[pos_name].label
                target_telem_err = _telemetry_error(target_name, state)
                if target_telem_err:
                    return False, f"hand-off at {label}: {target_telem_err}"
                target_fault = device_fault(target_name, state)
                if target_fault:
                    return False, f"hand-off at {label}: {target_fault}"
                handoff_err = target_dev.rules.ready_for_arm(state)
                if handoff_err:
                    return False, f"hand-off at {label}: {handoff_err}"

    return True, "ok"
