"""Tests for wetlab.lab.protocol.model (schema validation and canonical fingerprint)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from wetlab.lab.protocol.model import Protocol, fingerprint

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def test_fingerprint_is_key_order_independent_and_sensitive_to_changes() -> None:
    p_a = {
        "name": "37℃ 保持 30 秒",
        "labware": {"LW-001": "pcr", "LW-002": "shaker"},
        "steps": [
            {"skill": "pcr_close"},
            {
                "skill": "pcr_start_program",
                "args": {
                    "volume_ul": 50,
                    "lid_c": None,
                    "stages": [{"hold_seconds": 30, "temperature_c": 37}],
                },
            },
            {"wait": {"seconds": 10}},
        ],
    }
    p_b = {
        "steps": [
            {"args": {}, "skill": "pcr_close"},
            {
                "args": {
                    "stages": [{"temperature_c": 37, "hold_seconds": 30}],
                    "lid_c": None,
                    "volume_ul": 50,
                },
                "skill": "pcr_start_program",
            },
            {"wait": {"seconds": 10}},
        ],
        "labware": {"LW-002": "shaker", "LW-001": "pcr"},
        "name": "37℃ 保持 30 秒",
    }
    assert fingerprint(p_a) == fingerprint(p_b)
    assert fingerprint(Protocol.model_validate(p_a)) == fingerprint(p_b)

    # Changing any single parameter produces a different fingerprint
    p_modified = json.loads(json.dumps(p_a))
    p_modified["steps"][1]["args"]["stages"][0]["hold_seconds"] = 31
    assert fingerprint(p_modified) != fingerprint(p_a)


def test_both_sop_files_parse_cleanly_and_invalid_steps_are_rejected() -> None:
    sop1 = Protocol.model_validate_json((SOPS_DIR / "pcr_37c_30s.json").read_text("utf-8"))
    sop2 = Protocol.model_validate_json(
        (SOPS_DIR / "task_thermal_workflow.json").read_text("utf-8")
    )
    assert len(sop1.steps) == 5
    assert len(sop2.steps) == 20

    # Empty steps or non-positive wait seconds are rejected
    with pytest.raises(ValidationError):
        Protocol.model_validate({"name": "bad", "labware": {}, "steps": []})
    with pytest.raises(ValidationError):
        Protocol.model_validate(
            {"name": "bad", "labware": {}, "steps": [{"wait": {"seconds": 0}}]}
        )
