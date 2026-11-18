"""Fixed-order 1-second service loop (docs/spec/04 section 5.14)."""

from collections.abc import Callable
from pathlib import Path
import sqlite3
from typing import Any

from ..backend import Backend
from ..store import inbox, island
from . import advance, commands, reconcile


def _freeze_on_unexpected_error(
    backend: Backend,
    conn: sqlite3.Connection,
    exc: BaseException,
) -> None:
    if conn.in_transaction:
        conn.rollback()
    now = backend.now()
    reason = f"service unexpected error ({type(exc).__name__}): {exc}"
    payload = {"reason": reason, "unexpected_error": True}
    with conn:
        island.set_frozen(conn, reason=reason, at=now)
        inbox.append_event(conn, at=now, kind="frozen", payload=payload)


def tick(
    backend: Backend,
    conn: sqlite3.Connection,
    *,
    runs_dir: Path,
    gateway_was_online: bool,
    notify_fn: Callable[[dict[str, Any]], None] | None = None,
    on_labware_change: Callable[[str, str | None], None] | None = None,
) -> bool:
    """Execute steps 1..5 of one service round; freeze without exiting on unexpected errors."""
    try:
        for cmd in inbox.list_unhandled_commands(conn):
            commands.process_command(
                backend,
                conn,
                cmd,
                runs_dir=runs_dir,
                on_labware_change=on_labware_change,
            )

        now = backend.now()
        raw = backend.state()
        with conn:
            island.set_closed_skills(conn, backend.closed_skills)
            online_now = reconcile.sync_device_state(conn, raw, now=now)
            if gateway_was_online and not online_now:
                if not island.get_freeze(conn)["frozen"]:
                    island.set_frozen(conn, reason="GATEWAY_OFFLINE", at=now)
                off_payload = {"reason": "GATEWAY_OFFLINE"}
                inbox.append_event(conn, at=now, kind="gateway_offline", payload=off_payload)
                inbox.append_event(conn, at=now, kind="frozen", payload=off_payload)
        gateway_was_online = online_now

        advance.advance_running_protocol(backend, conn)

        if notify_fn is not None:
            last_id = inbox.get_cursor(conn, "notify")
            for ev in inbox.list_events_after(conn, last_id):
                notify_fn(ev)
                with conn:
                    inbox.set_cursor(conn, "notify", int(ev["id"]))

        return gateway_was_online
    except Exception as exc:
        _freeze_on_unexpected_error(backend, conn, exc)
        return gateway_was_online


def step_round(
    backend: Backend,
    conn: sqlite3.Connection,
    *,
    runs_dir: Path,
    gateway_was_online: bool,
    notify_fn: Callable[[dict[str, Any]], None] | None = None,
    on_labware_change: Callable[[str, str | None], None] | None = None,
) -> bool:
    """Run one complete 1-second loop iteration (steps 1..5 + `backend.sleep(1)`)."""
    updated_online = tick(
        backend,
        conn,
        runs_dir=runs_dir,
        gateway_was_online=gateway_was_online,
        notify_fn=notify_fn,
        on_labware_change=on_labware_change,
    )
    try:
        backend.sleep(1.0)
        with conn:
            conn.execute(
                "UPDATE device_state SET updated_at = ? WHERE device = '_gateway'",
                (backend.now(),),
            )
    except Exception as exc:
        _freeze_on_unexpected_error(backend, conn, exc)
    return updated_online
