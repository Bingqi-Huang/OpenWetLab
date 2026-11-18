"""Tests for labware contents schemas, `handle_labware`, and `get_lab_state` (#39)."""

from pathlib import Path

from langchain.tools import ToolRuntime
from pydantic import ValidationError
import pytest

from wetlab.agents.context import TurnContext
from wetlab.agents.tools import build_coordinator_tools
from wetlab.lab.errors import Blocked
from wetlab.lab.materials import Materials
from wetlab.lab.service import mission_commands, schemas
from wetlab.lab.snapshot import read_snapshot
from wetlab.lab.store import open_db


def test_labware_content_schemas_and_validation() -> None:
    assert "set_labware_contents" in schemas.COMMAND_SCHEMAS
    assert schemas.COMMAND_SCHEMAS is schemas.PAYLOAD_SCHEMAS

    placed_default = schemas.validate_command_payload(
        "place_labware",
        {"labware_id": "LW-001", "type": "96孔PCR板", "position": "rack:1-2"},
    )
    assert isinstance(placed_default, schemas.PlaceLabwarePayload)
    assert placed_default.contents == []

    placed_with = schemas.validate_command_payload(
        "place_labware",
        {
            "labware_id": "LW-003",
            "type": "96孔PCR板",
            "position": "rack:1-2",
            "contents": [
                {"name": "X", "wells": ["A1", "A2"], "volume_ul": 25.0, "note": "样品"},
                {"name": "Water", "wells": []},
            ],
        },
    )
    assert isinstance(placed_with, schemas.PlaceLabwarePayload)
    assert [c.model_dump() for c in placed_with.contents] == [
        {"name": "X", "wells": ["A1", "A2"], "volume_ul": 25.0, "note": "样品"},
        {"name": "Water", "wells": None, "volume_ul": None, "note": None},
    ]

    set_model = schemas.validate_command_payload(
        "set_labware_contents",
        {"labware_id": "LW-003", "contents": [{"name": "SynCom-1", "volume_ul": 50}]},
    )
    assert isinstance(set_model, schemas.SetLabwareContentsPayload)
    assert set_model.contents[0].model_dump() == {
        "name": "SynCom-1",
        "wells": None,
        "volume_ul": 50.0,
        "note": None,
    }

    with pytest.raises(ValidationError):
        schemas.validate_command_payload(
            "set_labware_contents",
            {"labware_id": "LW-003", "contents": [{"name": "   "}]},
        )
    with pytest.raises(ValidationError):
        schemas.validate_command_payload(
            "set_labware_contents",
            {"labware_id": "LW-003", "contents": [{"name": "X", "volume_ul": -1.0}]},
        )


def test_handle_labware_place_and_set_contents_visible_in_get_lab_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "island.sqlite"
    conn = open_db(db_path, mode="virtual")
    synced: list[tuple[str, str | None]] = []
    try:
        place_model = schemas.PlaceLabwarePayload.model_validate(
            {
                "labware_id": "LW-003",
                "type": "96孔PCR板",
                "position": "rack:1-2",
                "contents": [
                    {"name": "X", "wells": ["A1"], "volume_ul": 20.0, "note": "原样"}
                ],
            }
        )
        with conn:
            res = mission_commands.handle_labware(
                conn,
                place_model,
                100.0,
                lambda lw, pos: synced.append((lw, pos)),
            )
        assert res == {"ok": True, "labware_id": "LW-003"}
        assert synced == [("LW-003", "rack:1-2")]

        ctx = TurnContext(event_id=1, mission_id="M-1", db_path=db_path)
        runtime = ToolRuntime(
            state={"messages": []},
            context=ctx,
            config={"configurable": {"thread_id": ctx.thread_id}},
            stream_writer=lambda _: None,
            tool_call_id="tc_unit",
            store=None,
        )
        tools = {t.name: t for t in build_coordinator_tools()}
        state = tools["get_lab_state"].invoke({"runtime": runtime})
        assert state["labware"] == {
            "LW-003": {
                "position": "rack:1-2",
                "type": "96孔PCR板",
                "contents": [
                    {"name": "X", "wells": ["A1"], "volume_ul": 20.0, "note": "原样"}
                ],
            }
        }
        found_plates = [
            lw_id
            for lw_id, info in state["labware"].items()
            if any(item["name"] == "X" for item in info["contents"])
        ]
        assert found_plates == ["LW-003"]

        upd_model = schemas.SetLabwareContentsPayload.model_validate(
            {
                "labware_id": "LW-003",
                "contents": [{"name": "Y", "wells": None, "volume_ul": 40.0, "note": "整板"}],
            }
        )
        with conn:
            upd_res = mission_commands.handle_labware(
                conn,
                upd_model,
                120.0,
                lambda lw, pos: synced.append((lw, pos)),
            )
        assert upd_res == {"ok": True, "labware_id": "LW-003"}
        assert synced == [("LW-003", "rack:1-2")]

        snap = read_snapshot(conn)
        assert snap["labware"]["LW-003"]["contents"] == [
            {"name": "Y", "wells": None, "volume_ul": 40.0, "note": "整板"}
        ]
        hist = Materials(conn).history("LW-003")
        assert [ev["what"] for ev in hist] == ["placed", "contents_updated"]

        missing_model = schemas.SetLabwareContentsPayload.model_validate(
            {"labware_id": "LW-404", "contents": [{"name": "Z"}]}
        )
        with pytest.raises(Blocked, match="unknown labware"):
            mission_commands.handle_labware(conn, missing_model, 130.0, None)
    finally:
        conn.close()
