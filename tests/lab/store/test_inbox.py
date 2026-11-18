"""Tests for wetlab.lab.store.inbox (commands, events, and cursors)."""

from pathlib import Path
import sqlite3

import pytest

from wetlab.lab.store import inbox, open_db


def test_inbox_functions_never_auto_commit(tmp_path: Path) -> None:
    db_file = tmp_path / "wetlab.sqlite"
    writer = open_db(db_file)
    reader = open_db(db_file)
    try:
        assert inbox.enqueue_command(
            writer, key="cmd-1", at=1.0, who="web", kind="new_mission",
            payload={"title": "Test"},
        ) is True
        inbox.append_event(writer, at=1.0, kind="mission_created", ref_id="M-1")
        inbox.set_cursor(writer, "agents", 1)

        assert writer.in_transaction is True
        assert inbox.list_unhandled_commands(reader) == []
        assert inbox.list_events_after(reader, 0) == []
        assert inbox.get_cursor(reader, "agents") == 0

        writer.rollback()
        assert inbox.list_unhandled_commands(writer) == []
        assert inbox.list_events_after(writer, 0) == []
        assert inbox.get_cursor(writer, "agents") == 0
    finally:
        writer.close()
        reader.close()


def test_idempotent_command_enqueue_and_handling() -> None:
    conn = open_db(":memory:")
    try:
        with conn:
            first = inbox.enqueue_command(
                conn,
                key="submit_protocol:E-001:v1",
                at=10.0,
                who="operator",
                kind="submit_protocol",
                target_id="E-001",
                payload={"name": "v1"},
            )
            dup_same_tx = inbox.enqueue_command(
                conn,
                key="submit_protocol:E-001:v1",
                at=11.0,
                who="operator",
                kind="submit_protocol",
                target_id="E-001",
                payload={"name": "mutated"},
            )
            second = inbox.enqueue_command(
                conn,
                key="approve:E-001-v1",
                at=12.0,
                who="Alice",
                kind="approve",
                target_id="E-001-v1",
                payload={"fingerprint": "fp1"},
            )

        assert (first, dup_same_tx, second) == (True, False, True)

        with conn:
            dup_next_tx = inbox.enqueue_command(
                conn,
                key="submit_protocol:E-001:v1",
                at=99.0,
                who="operator",
                kind="submit_protocol",
            )
        assert dup_next_tx is False

        unhandled = inbox.list_unhandled_commands(conn)
        assert [c["key"] for c in unhandled] == [
            "submit_protocol:E-001:v1",
            "approve:E-001-v1",
        ]
        # Original payload and timestamp preserved despite duplicate submissions
        assert unhandled[0]["at"] == 10.0
        assert unhandled[0]["payload"] == {"name": "v1"}

        with conn:
            inbox.mark_command_handled(
                conn, unhandled[0]["id"], handled_at=15.0, outcome="submitted:E-001-v1"
            )

        remaining = inbox.list_unhandled_commands(conn)
        assert [c["key"] for c in remaining] == ["approve:E-001-v1"]

        handled = inbox.get_command_by_key(conn, "submit_protocol:E-001:v1")
        assert handled is not None
        assert (handled["handled_at"], handled["outcome"]) == (15.0, "submitted:E-001-v1")
        assert inbox.get_command_by_key(conn, "nonexistent") is None
    finally:
        conn.close()


def test_events_stream_and_independent_cursors() -> None:
    conn = open_db(":memory:")
    try:
        assert inbox.get_cursor(conn, "agents") == 0
        assert inbox.get_cursor(conn, "notify") == 0

        with conn:
            e1 = inbox.append_event(
                conn, at=1.0, kind="mission_created", ref_id="M-1", payload={"title": "T"}
            )
            e2 = inbox.append_event(
                conn, at=2.0, kind="experiment_created", ref_id="E-1"
            )
            e3 = inbox.append_event(
                conn, at=3.0, kind="protocol_submitted", ref_id="E-1-v1", payload={"v": 1}
            )

        assert (e1, e2, e3) == (1, 2, 3)

        # Single-step consumption with limit=1 (as in agents/worker.py)
        next_batch = inbox.list_events_after(conn, inbox.get_cursor(conn, "agents"), limit=1)
        assert len(next_batch) == 1
        assert next_batch[0] == {
            "id": 1,
            "at": 1.0,
            "kind": "mission_created",
            "ref_id": "M-1",
            "payload": {"title": "T"},
        }

        with conn:
            inbox.set_cursor(conn, "agents", 2)
            inbox.set_cursor(conn, "notify", 3)

        assert inbox.get_cursor(conn, "agents") == 2
        assert inbox.get_cursor(conn, "notify") == 3
        assert [e["id"] for e in inbox.list_events_after(conn, inbox.get_cursor(conn, "agents"))] == [3]
        assert inbox.list_events_after(conn, inbox.get_cursor(conn, "notify")) == []

        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                inbox.set_cursor(conn, "agents", -1)
    finally:
        conn.close()
