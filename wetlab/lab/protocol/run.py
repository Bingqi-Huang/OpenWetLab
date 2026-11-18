"""Single-step protocol execution shared by rehearsal and live service (04 section 5.9)."""

from typing import Any

from pydantic import TypeAdapter

from .. import registry
from ..devices import BY_NAME
from ..lab import Lab
from ..safety import MAX_STATE_AGE_S, device_fault
from ..snapshot import extract_key_state
from ..store import island, records
from .model import ProtocolStep, SkillStep, WaitStep, WaitUntilStep

STALE_FREEZE_S = 60.0
_STEP_ADAPTER = TypeAdapter(ProtocolStep)


def _state_after(lab: Lab) -> dict[str, Any]:
    active = records.list_labware(lab.conn, active_only=True)
    labware_map = {item["id"]: item["position"] for item in active}
    return extract_key_state(lab.state(), labware_map)


def _is_device_stale(device: str, live_state: dict[str, Any]) -> bool:
    dev_mod = BY_NAME[device]
    dev_state = live_state.get(device) or {}
    age = dev_state.get("_age_s")
    if age is None or float(age) > MAX_STATE_AGE_S:
        return True
    return any(dev_state.get(prop) is None for prop in dev_mod.spec.STATE_PROPS)


def check_wait_until(
    device: str,
    target_state: dict[str, Any],
    live_state: dict[str, Any],
    *,
    now: float,
    deadline: float,
    stale_since: float | None = None,
) -> tuple[str, str | None, str | None]:
    """Evaluate one `wait_until` poll tick per 04 section 5.9.

    Returns `(outcome, error_code, error_message)` where `outcome` is one of:
    - `'done'`: all `target_state` properties match on fresh complete telemetry
    - `'waiting'`: not yet matched (or stale `<= 60s`) and before `deadline`
    - `'freeze'`: telemetry missing/stale continuously for `> 60s`
    - `'failed'`: device error/fault, gateway offline, or `now >= deadline`
    """
    dev_mod = BY_NAME.get(device)
    if dev_mod is None:
        return "failed", "INVALID_ARGUMENT", f"unknown device: {device}"

    gw = live_state.get("_gateway") or {}
    if not gw.get("online"):
        return "failed", "GATEWAY_OFFLINE", f"gateway went offline while waiting for {device}"

    fault_err = device_fault(device, live_state)
    if fault_err:
        return "failed", "DEVICE_ERROR", fault_err

    dev_state = live_state.get(device) or {}
    if _is_device_stale(device, live_state):
        if now >= deadline:
            return (
                "failed",
                "WAIT_TIMEOUT",
                f"wait_until timed out waiting for {device} {target_state} (telemetry stale)",
            )
        age = dev_state.get("_age_s")
        if stale_since is not None:
            stale_s = now - stale_since
        elif age is not None:
            stale_s = float(age)
        else:
            stale_s = 0.0
        if stale_s > STALE_FREEZE_S:
            return "freeze", "STALE_TELEMETRY", f"等待期间失去 {device} 的状态"
        return "waiting", None, None

    if all(dev_state.get(prop) == val for prop, val in target_state.items()):
        return "done", None, None

    if now >= deadline:
        actual = {prop: dev_state.get(prop) for prop in target_state}
        return (
            "failed",
            "WAIT_TIMEOUT",
            f"wait_until timed out waiting for {device} {target_state} (got {actual})",
        )

    return "waiting", None, None


def run_step(lab: Lab, step: ProtocolStep | dict[str, Any]) -> dict[str, Any]:
    """Execute one protocol step (`skill`, `wait`, or `wait_until`) on `lab`."""
    parsed = step if isinstance(step, ProtocolStep) else _STEP_ADAPTER.validate_python(step)

    if isinstance(parsed, SkillStep):
        return registry.call(lab, parsed.skill, **parsed.args)

    if isinstance(parsed, WaitStep):
        started_at = lab.backend.now()
        lab.backend.sleep(parsed.wait.seconds)
        elapsed = round(max(0.0, lab.backend.now() - started_at), 3)
        return {
            "ok": True,
            "status": "succeeded",
            "result": {"waited_s": elapsed},
            "error": None,
            "operation_ids": [],
            "state_after": _state_after(lab),
            "elapsed_s": elapsed,
        }

    assert isinstance(parsed, WaitUntilStep)
    spec = parsed.wait_until
    started_at = lab.backend.now()
    deadline = started_at + spec.max_seconds
    stale_since: float | None = None
    while True:
        now = lab.backend.now()
        live = lab.state()
        if spec.device in BY_NAME and _is_device_stale(spec.device, live):
            if stale_since is None:
                age = (live.get(spec.device) or {}).get("_age_s")
                if age is not None and float(age) > MAX_STATE_AGE_S:
                    stale_since = now - float(age)
                else:
                    stale_since = now
        else:
            stale_since = None

        outcome, code, msg = check_wait_until(
            spec.device,
            spec.state,
            live,
            now=now,
            deadline=deadline,
            stale_since=stale_since,
        )
        if outcome == "done":
            elapsed = round(max(0.0, now - started_at), 3)
            return {
                "ok": True,
                "status": "succeeded",
                "result": {"waited_s": elapsed, "matched": dict(spec.state)},
                "error": None,
                "operation_ids": [],
                "state_after": _state_after(lab),
                "elapsed_s": elapsed,
            }
        if outcome == "freeze":
            reason = msg or f"等待期间失去 {spec.device} 的状态"
            if lab.conn.in_transaction:
                island.set_frozen(lab.conn, reason=reason, at=now)
            else:
                with lab.conn:
                    island.set_frozen(lab.conn, reason=reason, at=now)
            elapsed = round(max(0.0, now - started_at), 3)
            return {
                "ok": False,
                "status": "unknown",
                "result": {},
                "error": {"code": code or "STALE_TELEMETRY", "message": reason},
                "operation_ids": [],
                "state_after": _state_after(lab),
                "elapsed_s": elapsed,
            }
        if outcome == "failed":
            elapsed = round(max(0.0, now - started_at), 3)
            return {
                "ok": False,
                "status": "failed",
                "result": {},
                "error": {"code": code or "WAIT_FAILED", "message": msg or "wait_until failed"},
                "operation_ids": [],
                "state_after": _state_after(lab),
                "elapsed_s": elapsed,
            }
        lab.backend.sleep(min(1.0, deadline - now))
