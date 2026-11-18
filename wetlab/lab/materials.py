"""Labware position, contents, and experience ledger (docs/spec/04 section 5.7)."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import sqlite3
import time
from typing import Any

from .devices import LABWARE_POSITIONS
from .errors import Blocked
from .store import records


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
    else:
        with conn:
            yield


def _normalize_content_item(item: Mapping[str, Any]) -> dict[str, Any]:
    raw_name = item.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        raise Blocked("content item name must not be empty")
    raw_wells = item.get("wells")
    wells: list[str] | None = None
    if raw_wells is not None:
        cleaned = [str(w).strip() for w in raw_wells if str(w).strip()]
        wells = cleaned or None
    raw_vol = item.get("volume_ul")
    volume_ul = float(raw_vol) if raw_vol is not None else None
    if volume_ul is not None and volume_ul < 0:
        raise Blocked("content item volume_ul must not be negative")
    raw_note = item.get("note")
    note = str(raw_note) if raw_note is not None else None
    return {"name": name, "wells": wells, "volume_ul": volume_ul, "note": note}


class Materials:
    """Tracks labware positions, contents, and per-plate history in SQLite."""

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

    def place(
        self,
        labware_id: str,
        type: str,
        position: str,
        contents: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Register a plate placed on the island by a human; raises Blocked on conflict."""
        if position not in LABWARE_POSITIONS:
            raise Blocked(f"unknown position: {position}")
        existing = records.get_labware(self._conn, labware_id)
        if existing is not None and existing["position"] is not None:
            raise Blocked(
                f"labware {labware_id} is already on the island at {existing['position']}"
            )
        occupant = records.get_labware_at(self._conn, position)
        if occupant is not None:
            raise Blocked(f"position {position} is already occupied by {occupant['id']}")
        normalized = [_normalize_content_item(item) for item in (contents or [])]

        detail: dict[str, Any] = {"type": type, "position": position}
        if normalized:
            detail["contents"] = normalized

        with _tx(self._conn):
            records.upsert_labware(self._conn, id=labware_id, type=type, position=position)
            records.replace_labware_contents(self._conn, labware_id, normalized)
            records.insert_labware_event(
                self._conn,
                labware_id=labware_id,
                at=self._now(),
                what="placed",
                by="human",
                detail=detail,
            )

    def set_contents(
        self,
        labware_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        by: str = "human",
    ) -> None:
        """Replace all content rows on `labware_id` and record a `contents_updated` event."""
        existing = records.get_labware(self._conn, labware_id)
        if existing is None:
            raise Blocked(f"unknown labware: {labware_id}")
        normalized = [_normalize_content_item(item) for item in items]

        with _tx(self._conn):
            records.replace_labware_contents(self._conn, labware_id, normalized)
            records.insert_labware_event(
                self._conn,
                labware_id=labware_id,
                at=self._now(),
                what="contents_updated",
                by=by,
                detail={"contents": normalized},
            )

    def contents(self, labware_id: str) -> list[dict[str, Any]]:
        """Return normalized content items for `labware_id` ordered by row `n`."""
        return records.list_labware_contents(self._conn, labware_id)

    def find(self, name: str) -> list[str]:
        """Return deduplicated on-island `labware_id` list holding sample/reagent `name`."""
        query = name.strip()
        if not query:
            return []
        return records.find_labware_by_content(self._conn, query, active_only=True)

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
            if position not in LABWARE_POSITIONS:
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
        if to_position not in LABWARE_POSITIONS:
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
