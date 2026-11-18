"""Startup warmup, read-only channel probes, WorkingStatus=2 recovery, and crash reconcile."""

import sqlite3
from typing import Any

from ..backend import Backend
from ..devices import ISLAND
from ..devices.base import READ
from ..errors import Blocked, DeviceFailed, Frozen, Unknown
from ..lab import Lab
from ..safety import MAX_STATE_AGE_S
from ..store import inbox, island, runs

PREFERRED_READ_PROBES: dict[str, str] = {
    "pcr": "GetPcrStatus",
    "shaker": "GetStatus",
    "arm": "RobotMode",
    "rack": "GetStatus",
}


def sync_device_state(
    conn: sqlite3.Connection,
    raw_state: dict[str, Any],
    *,
    now: float,
) -> bool:
    """Persist backend telemetry and gateway status into `device_state` (caller manages tx)."""
    existing = island.get_all_device_states(conn)
    gw = raw_state.get("_gateway") or {}
    online = bool(gw.get("online"))
    gw_props = {
        "online": online,
        "busy_by_others": bool(gw.get("busy_by_others")),
    }
    island.upsert_device_state(
        conn,
        device="_gateway",
        props=gw_props,
        updated_at=now,
        error=None,
    )

    for dev in ISLAND:
        name = dev.spec.NAME
        code = dev.spec.CODE
        d = raw_state.get(code) or {}
        age_s = d.get("_age_s")
        err = d.get("_error")
        props = {k: v for k, v in d.items() if not k.startswith("_")}
        if age_s is None and err is None and not props:
            continue
        prev = existing.get(name)
        if age_s is not None:
            updated_at = now - float(age_s)
        elif prev is not None and prev["updated_at"] is not None:
            updated_at = float(prev["updated_at"])
        else:
            updated_at = now
        base_props = prev["props"] if prev is not None else {}
        merged_props = {**base_props, **props}
        island.upsert_device_state(
            conn,
            device=name,
            props=merged_props,
            updated_at=updated_at,
            error=err,
        )
    return online


def is_island_state_ready(raw_state: dict[str, Any]) -> bool:
    """Return True iff gateway heartbeat and every ISLAND device state (or error) have arrived."""
    gw = raw_state.get("_gateway") or {}
    if not gw.get("online"):
        return False
    for dev in ISLAND:
        d = raw_state.get(dev.spec.CODE) or {}
        if d.get("_error"):
            continue
        age = d.get("_age_s")
        if age is None or age > MAX_STATE_AGE_S:
            return False
        if any(d.get(prop) is None for prop in dev.spec.STATE_PROPS):
            return False
    return True


def _read_probe_action(dev_mod: Any) -> str | None:
    preferred = PREFERRED_READ_PROBES.get(dev_mod.spec.NAME)
    if preferred and preferred in dev_mod.spec.ACTIONS:
        return preferred
    for act_name, meta in dev_mod.spec.ACTIONS.items():
        if meta.kind == READ:
            return act_name
    return None


def clear_failed_working_status(
    backend: Backend,
    conn: sqlite3.Connection,
) -> list[str]:
    """Send a read-only probe to each device currently holding `WorkingStatus == 2` (04 §5.14)."""
    raw = backend.state()
    probe_lab = Lab(backend, conn)
    cleared: list[str] = []
    attempted = False

    for dev in ISLAND:
        dev_raw = raw.get(dev.spec.CODE) or {}
        if dev_raw.get("WorkingStatus") != 2:
            continue
        act_name = _read_probe_action(dev)
        if act_name is None:
            continue
        attempted = True
        try:
            probe_lab.do(dev.spec.NAME, act_name)
            cleared.append(f"{dev.spec.NAME}.{act_name}")
        except DeviceFailed:
            continue
        except Unknown as exc:
            t = backend.now()
            payload = {
                "reason": str(exc),
                "code": exc.code,
                "operation_id": probe_lab.last_operation_id,
            }
            with conn:
                inbox.append_event(conn, at=t, kind="frozen", payload=payload)
            break

    if attempted:
        t_after = backend.now()
        with conn:
            sync_device_state(conn, backend.state(), now=t_after)
    return cleared


def _freeze_sent_steps(
    backend: Backend,
    conn: sqlite3.Connection,
) -> list[tuple[str, int]]:
    frozen_sent: list[tuple[str, int]] = []
    for status in ("running", "paused"):
        for proto in runs.list_protocols(conn, status=status):
            proto_id = proto["id"]
            for step in runs.list_steps(conn, proto_id):
                if step["status"] != "sent":
                    continue
                step_n = int(step["n"])
                frozen_sent.append((proto_id, step_n))
                t = backend.now()
                reason = (
                    f"服务重启时发现方案 {proto_id} 第 {step_n} 步处于已发出未确认状态（sent）"
                )
                payload = {
                    "protocol_id": proto_id,
                    "step_n": step_n,
                    "reason": reason,
                }
                with conn:
                    if not island.get_freeze(conn)["frozen"]:
                        island.set_frozen(conn, reason=reason, at=t)
                    inbox.append_event(
                        conn,
                        at=t,
                        kind="frozen",
                        ref_id=proto_id,
                        payload=payload,
                    )
    return frozen_sent


def reconcile(
    backend: Backend,
    conn: sqlite3.Connection,
    *,
    warmup_s: float = 10.0,
) -> dict[str, Any]:
    """Persist closed skills, await telemetry warmup, probe read channels, and freeze `sent`."""
    with conn:
        island.set_closed_skills(conn, backend.closed_skills)

    deadline = backend.now() + warmup_s
    while True:
        raw = backend.state()
        if is_island_state_ready(raw):
            break
        now = backend.now()
        if now >= deadline:
            break
        backend.sleep(min(1.0, deadline - now))

    now = backend.now()
    raw = backend.state()
    with conn:
        online = sync_device_state(conn, raw, now=now)
        if not online:
            if not island.get_freeze(conn)["frozen"]:
                island.set_frozen(conn, reason="GATEWAY_OFFLINE", at=now)
            off_payload = {"reason": "GATEWAY_OFFLINE"}
            inbox.append_event(conn, at=now, kind="gateway_offline", payload=off_payload)
            inbox.append_event(conn, at=now, kind="frozen", payload=off_payload)

    probed: list[str] = []
    if online:
        probe_lab = Lab(backend, conn)
        for dev in ISLAND:
            act_name = _read_probe_action(dev)
            if act_name is None:
                continue
            try:
                probe_lab.do(dev.spec.NAME, act_name)
                probed.append(f"{dev.spec.NAME}.{act_name}")
            except Unknown as exc:
                t = backend.now()
                payload = {"reason": str(exc), "code": exc.code}
                with conn:
                    inbox.append_event(conn, at=t, kind="frozen", payload=payload)
                break
            except (DeviceFailed, Blocked, Frozen) as exc:
                t = backend.now()
                msg = f"startup probe {dev.spec.NAME}.{act_name} failed: {exc}"
                with conn:
                    if not island.get_freeze(conn)["frozen"]:
                        island.set_frozen(
                            conn,
                            reason=msg,
                            at=t,
                            operation_id=probe_lab.last_operation_id,
                        )
                    inbox.append_event(conn, at=t, kind="frozen", payload={"reason": msg})
                break

    frozen_sent = _freeze_sent_steps(backend, conn)
    return {
        "gateway_online": online,
        "probed": probed,
        "frozen_sent_steps": frozen_sent,
    }
