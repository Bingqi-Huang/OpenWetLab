"""Labware position and experience ledger (docs/spec/04 section 5.7)."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import sqlite3
import time
from typing import Any

from .devices import POSITIONS
from .errors import Blocked
from .store import records


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
    else:
        with conn:
            yield


class Materials:
    """Tracks labware positions and per-plate history in SQLite."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._conn = conn
        self._now = now

    def where(self, labware_id: str) -> str | None:
        """Return the current island position of `labware_id`, or None."""
        row = records.get_labware(self._conn, labware_id)
        return row["position"] if row is not None else None

    def at(self, position: str) -> str | None:
        """Return the `labware_id` currently at `position`, or None."""
        row = records.get_labware_at(self._conn, position)
        return row["id"] if row is not None else None

    def place(self, labware_id: str, type: str, position: str) -> None:
        """Register a plate placed on the island by a human; raises Blocked on conflict."""
        if position not in POSITIONS:
            raise Blocked(f"unknown position: {position}")
        existing = records.get_labware(self._conn, labware_id)
        if existing is not None and existing["position"] is not None:
            raise Blocked(
                f"labware {labware_id} is already on the island at {existing['position']}"
            )
        occupant = records.get_labware_at(self._conn, position)
        if occupant is not None:
            raise Blocked(f"position {position} is already occupied by {occupant['id']}")

        with _tx(self._conn):
            records.upsert_labware(self._conn, id=labware_id, type=type, position=position)
            records.insert_labware_event(
                self._conn,
                labware_id=labware_id,
                at=self._now(),
                what="placed",
                by="human",
                detail={"type": type, "position": position},
            )

    def remove(self, labware_id: str) -> None:
        """Record that a human removed `labware_id` from the island."""
        existing = records.get_labware(self._conn, labware_id)
        if existing is None or existing["position"] is None:
            raise Blocked(f"labware {labware_id} is not on the island")

        with _tx(self._conn):
            records.update_labware_position(self._conn, labware_id, None)
            records.insert_labware_event(
                self._conn,
                labware_id=labware_id,
                at=self._now(),
                what="removed",
                by="human",
                detail={"from": existing["position"]},
            )

    def correct(self, labware_id: str, position: str | None) -> None:
        """Reconcile `labware_id`'s position with reality as reported by a human."""
        existing = records.get_labware(self._conn, labware_id)
        if existing is None:
            raise Blocked(f"unknown labware: {labware_id}")
        if position is not None:
            if position not in POSITIONS:
                raise Blocked(f"unknown position: {position}")
            occupant = records.get_labware_at(self._conn, position)
            if occupant is not None and occupant["id"] != labware_id:
                raise Blocked(
                    f"position {position} is already occupied by {occupant['id']}"
                )

        with _tx(self._conn):
            records.update_labware_position(self._conn, labware_id, position)
            records.insert_labware_event(
                self._conn,
                labware_id=labware_id,
                at=self._now(),
                what="corrected",
                by="human",
                detail={"from": existing["position"], "to": position},
            )

    def move(
        self,
        labware_id: str,
        to_position: str,
        operation_id: str | None = None,
    ) -> None:
        """Update `labware_id` after a successful robot arm transfer; raises Blocked on conflict."""
        if to_position not in POSITIONS:
            raise Blocked(f"unknown position: {to_position}")
        existing = records.get_labware(self._conn, labware_id)
        if existing is None or existing["position"] is None:
            raise Blocked(f"labware {labware_id} is not on the island")
        occupant = records.get_labware_at(self._conn, to_position)
        if occupant is not None:
            raise Blocked(f"position {to_position} is already occupied by {occupant['id']}")

        with _tx(self._conn):
            records.update_labware_position(self._conn, labware_id, to_position)
            records.insert_labware_event(
                self._conn,
                labware_id=labware_id,
                at=self._now(),
                what="moved",
                by="device",
                detail={"from": existing["position"], "to": to_position},
                operation_id=operation_id,
            )

    def record_at(self, position: str, what: str, **detail: Any) -> None:
        """Record an experience on whichever plate occupies `position` (no-op if empty)."""
        occupant = records.get_labware_at(self._conn, position)
        if occupant is None:
            return

        with _tx(self._conn):
            records.insert_labware_event(
                self._conn,
                labware_id=occupant["id"],
                at=self._now(),
                what=what,
                by="device",
                detail=dict(detail),
            )

    def history(self, labware_id: str) -> list[dict[str, Any]]:
        """Return all recorded events for `labware_id` in chronological order."""
        return records.list_labware_events(self._conn, labware_id)
