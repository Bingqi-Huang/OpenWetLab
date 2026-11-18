"""Tests for wetlab.lab.protocol.render (04 section 5.8, 5.9 & 11 Step 5)."""

import json
from pathlib import Path
import re

from wetlab.lab.protocol.model import Protocol
from wetlab.lab.protocol.render import format_pcr_stages, render

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def test_pcr_stages_formatting_matches_spec_04_section_5_8() -> None:
    stages = [
        {"temperature_c": 98, "hold_seconds": 60},
        {
            "repeat": 23,
            "steps": [
                {"temperature_c": 98, "hold_seconds": 10},
                {"temperature_c": 60, "hold_seconds": 5},
                {"temperature_c": 72, "hold_seconds": 5},
            ],
        },
        {"temperature_c": 72, "hold_seconds": 60},
    ]
    assert (
        format_pcr_stages(stages)
        == "98℃ 1 分钟 →（98℃ 10 秒 → 60℃ 5 秒 → 72℃ 5 秒）× 23 → 72℃ 1 分钟"
    )


def test_render_has_checklist_one_line_per_step_trajectories_and_verdict() -> None:
    sop2 = Protocol.model_validate(
        json.loads((SOPS_DIR / "task_thermal_workflow.json").read_text("utf-8"))
    )
    rehearsal = {
        "ok": True,
        "total_s": 4000.0,
        "steps": [{"n": i, "elapsed_s": 200.0} for i in range(1, len(sop2.steps) + 1)],
    }
    text = render(sop2, rehearsal)

    # 1. Pre-execution checklist near the top
    assert "## 执行前现场应该是这样" in text
    assert "- [ ] 器皿 LW-001 位于 PCR仪（pcr）" in text
    assert text.index("## 执行前现场应该是这样") < text.index("## 执行步骤")

    # 2. Exactly one numbered line per protocol step
    step_lines = [line for line in text.splitlines() if re.match(r"^\d+\.\s", line)]
    assert len(step_lines) == len(sop2.steps)

    # 3. PCR cycling program rendered on single line matching 04 section 5.8
    assert (
        "98℃ 1 分钟 →（98℃ 10 秒 → 60℃ 5 秒 → 72℃ 5 秒）× 23 → 72℃ 1 分钟 → 4℃ 5 分钟"
        in step_lines[16]
    )

    # 4. Labware destination & rehearsal conclusion present
    assert "## 器皿去向" in text
    assert "## 预演结论" in text
    assert "预演通过" in text
