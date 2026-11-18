"""Tests for wetlab.lab.protocol.validate (skill, closed_skills #2, argument, and ledger checks)."""

import json
from pathlib import Path

from wetlab.lab.materials import Materials
from wetlab.lab.protocol import validate
from wetlab.lab.store import island, open_db

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def test_validate_catches_unknown_skills_closed_skills_bad_args_and_ledger_mismatches() -> None:
    conn = open_db(":memory:")
    mat = Materials(conn)
    try:
        sop1 = json.loads((SOPS_DIR / "pcr_37c_30s.json").read_text("utf-8"))

        assert validate(sop1) == []
        ledger_issues = validate(sop1, conn)
        assert len(ledger_issues) == 1
        assert "不在实验岛账本中" in ledger_issues[0]

        mat.place("LW-001", "pcr_plate_96", "shaker")
        mismatch = validate(sop1, conn)
        assert len(mismatch) == 1
        assert "shaker" in mismatch[0] and "pcr" in mismatch[0]

        mat.move("LW-001", "pcr")
        assert validate(sop1, conn) == []

        # #2: Closed skill persisted in `meta` is caught with exact Chinese step message
        with conn:
            island.set_closed_skills(conn, {"pcr_start_program"})
        closed_issues = validate(sop1, conn)
        assert closed_issues == [
            "第 2 步：技能 pcr_start_program 在这座实验岛上还没有开放"
        ]
        with conn:
            island.set_closed_skills(conn, set())
        assert validate(sop1, conn) == []

        bad_proto = {
            "name": "bad",
            "labware": {"LW-001": "pcr"},
            "steps": [
                {"skill": "nonexistent_skill", "args": {}},
                {"skill": "shake", "args": {"seconds": 61, "rpm": 300}},
                {"wait_until": {"device": "washer", "state": {"Idle": True}, "max_seconds": 10}},
            ],
        }
        issues = validate(bad_proto, conn)
        assert len(issues) == 3
        assert "nonexistent_skill" in issues[0]
        assert "shake" in issues[1]
        assert "washer" in issues[2]
    finally:
        conn.close()
