"""Daemon service lifecycle: verify DB mode before backend, reconcile, and run 1s loop."""

from collections.abc import Callable
from pathlib import Path
import sqlite3
from typing import Any

from ..backend import Backend
from ..mqtt import MqttBackend
from ..sim import Scenario, SimBackend
from ..snapshot import read_snapshot
from ..store import db, island, open_db
from . import loop, reconcile


def _resolve_paths(
    mode: str,
    db_path: str | Path | None,
    runs_dir: str | Path | None,
) -> tuple[Path, Path]:
    if db_path is None:
        base_dir = Path("runs") if runs_dir is None else Path(runs_dir)
        target_db = db.default_db_path(mode, base_dir=base_dir)
        target_runs = db.island_dir(mode, base_dir=base_dir)
        return target_db, target_runs
    target_db = Path(db_path)
    target_runs = db.island_dir(mode) if runs_dir is None else Path(runs_dir)
    return target_db, target_runs


class Service:
    """Single-threaded island control service owning the Backend and SQLite write loop."""

    def __init__(
        self,
        backend: Backend,
        conn: sqlite3.Connection,
        *,
        runs_dir: str | Path = Path("runs/virtual"),
        notify_fn: Callable[[dict[str, Any]], None] | None = None,
        on_labware_change: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self.backend = backend
        self.conn = conn
        self.runs_dir = Path(runs_dir)
        self.notify_fn = notify_fn
        self.on_labware_change = on_labware_change
        self.gateway_was_online: bool = False
        self.reconciled: bool = False
        with self.conn:
            island.set_closed_skills(self.conn, self.backend.closed_skills)

    def startup(self, *, warmup_s: float = 10.0) -> dict[str, Any]:
        """Run startup telemetry warmup, read-only probes, and `sent` step reconciliation."""
        info = reconcile.reconcile(self.backend, self.conn, warmup_s=warmup_s)
        self.gateway_was_online = bool(info["gateway_online"])
        self.reconciled = True
        return info

    def tick(self) -> None:
        """Run steps 1..5 once without sleeping."""
        if not self.reconciled:
            self.startup()
        self.gateway_was_online = loop.tick(
            self.backend,
            self.conn,
            runs_dir=self.runs_dir,
            gateway_was_online=self.gateway_was_online,
            notify_fn=self.notify_fn,
            on_labware_change=self.on_labware_change,
        )

    def step(self) -> None:
        """Run one full 1-second service round (steps 1..6)."""
        if not self.reconciled:
            self.startup()
        self.gateway_was_online = loop.step_round(
            self.backend,
            self.conn,
            runs_dir=self.runs_dir,
            gateway_was_online=self.gateway_was_online,
            notify_fn=self.notify_fn,
            on_labware_change=self.on_labware_change,
        )

    def run(self, *, max_rounds: int | None = None) -> None:
        """Start (if not yet reconciled) and run the 1-second loop."""
        if not self.reconciled:
            self.startup()
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            self.step()
            rounds += 1


def create_virtual_service(
    db_path: str | Path | None = None,
    *,
    runs_dir: str | Path | None = None,
) -> Service:
    """Open virtual-mode SQLite DB and initialize a `SimBackend` resumed from snapshot."""
    resolved = _resolve_paths("virtual", db_path, runs_dir)
    target_db = resolved[0]
    target_runs = resolved[1]
    conn = open_db(target_db, mode="virtual")
    snap = read_snapshot(conn)
    backend = SimBackend(Scenario.from_snapshot(snap))
    return Service(
        backend,
        conn,
        runs_dir=target_runs,
        on_labware_change=backend.sync_human_labware,
    )


def run_daemon(
    *,
    mode: str | None = None,
    db_path: str | Path | None = None,
    runs_dir: str | Path | None = None,
    max_rounds: int | None = None,
) -> None:
    """Entry point for `wetlab serve`; validates DB mode BEFORE connecting any backend."""
    resolved_mode = db.resolve_mode(mode)
    resolved = _resolve_paths(resolved_mode, db_path, runs_dir)
    target_db = resolved[0]
    target_runs = resolved[1]
    conn = open_db(target_db, mode=resolved_mode)
    mqtt_backend: MqttBackend | None = None
    on_lw: Callable[[str, str | None], None] | None = None
    try:
        if resolved_mode == "virtual":
            snap = read_snapshot(conn)
            sim_backend = SimBackend(
                Scenario.from_snapshot(snap),
                realtime=(max_rounds is None),
            )
            backend: Backend = sim_backend
            on_lw = sim_backend.sync_human_labware
        else:
            mqtt_backend = MqttBackend(raw_dir=target_runs / "raw")
            mqtt_backend.start()
            backend = mqtt_backend

        svc = Service(backend, conn, runs_dir=target_runs, on_labware_change=on_lw)
        svc.run(max_rounds=max_rounds)
    finally:
        if mqtt_backend is not None:
            mqtt_backend.close()
        conn.close()
