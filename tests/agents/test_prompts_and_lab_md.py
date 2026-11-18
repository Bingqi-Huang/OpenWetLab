"""Prompt template and `LAB.md` invariants test (`04` v0.6 section 16.3 #22)."""

from pathlib import Path
import re

from langchain.agents.middleware import TodoListMiddleware

from wetlab.agents.tools import (
    PROMPTS_DIR,
    build_coordinator_tools,
    build_operator_tools,
    render_system_prompt,
)
from wetlab.lab import registry
from wetlab.lab.devices import ISLAND, POSITIONS

REPO_ROOT = Path(__file__).resolve().parents[2]

KNOWN_EVENT_KINDS = {
    "mission_created",
    "experiment_created",
    "protocol_submitted",
    "protocol_rejected",
    "protocol_done",
    "protocol_failed",
    "protocol_stopped",
    "reply",
    "experiment_finished",
    "question_asked",
    "message",
    "frozen",
    "gateway_offline",
    "stop_failed",
    "command_failed",
    "agent_error",
    "mission_finished",
}
KNOWN_PROTOCOL_TOKENS = {
    "skill",
    "args",
    "wait",
    "seconds",
    "wait_until",
    "device",
    "state",
    "max_seconds",
}


def test_prompts_have_no_json_or_tool_signatures_and_inject_sop_example() -> None:
    """v0.6 #22 (1) & (2): prompt templates contain zero JSON literals or tool parameter lists,
    and `operator.md` injects `sops/pcr_37c_30s.json` verbatim.
    """
    coord_raw = (PROMPTS_DIR / "coordinator.md").read_text(encoding="utf-8")
    op_raw = (PROMPTS_DIR / "operator.md").read_text(encoding="utf-8")

    all_tool_names = (
        {t.name for t in build_coordinator_tools()}
        | {t.name for t in build_operator_tools()}
        | {t.name for t in TodoListMiddleware().tools}
    )

    for name, raw in (("coordinator.md", coord_raw), ("operator.md", op_raw)):
        assert "{{" not in raw and "}}" not in raw, f"{name} must not contain escaped JSON braces"
        assert '"steps"' not in raw and '"labware"' not in raw
        for tool_name in all_tool_names:
            assert f"{tool_name}(" not in raw, f"{name} must not copy signature `{tool_name}(...)`"

    sop_json_text = (REPO_ROOT / "sops" / "pcr_37c_30s.json").read_text(encoding="utf-8").strip()
    rendered_op = render_system_prompt("operator.md")
    assert sop_json_text in rendered_op

    rendered_coord = render_system_prompt("coordinator.md")
    assert "pcr_start_program" in rendered_coord
    assert "rotate_turntable" in rendered_coord
    assert "task_thermal_workflow.json" in rendered_coord


def test_lab_md_omits_duplicate_skill_ranges_and_obsolete_pcr_status_workaround() -> None:
    """v0.6 #22 (3) & (4): `LAB.md` omits duplicated parameter ranges and `pcr_status` recovery."""
    lab_text = (REPO_ROOT / "LAB.md").read_text(encoding="utf-8")
    forbidden_fragments = (
        "30～110",
        "30~110",
        "4～99",
        "4~99",
        "1～3600",
        "1~3600",
        "2～99",
        "2~99",
        "30 步",
        "30步",
        "60 秒",
        "60秒",
        "pcr_status",
        "WorkingStatus",
    )
    for frag in forbidden_fragments:
        assert frag not in lab_text, f"LAB.md must not contain {frag!r}"


def test_all_backticked_names_in_prompts_and_lab_md_exist() -> None:
    """v0.6 #22 acceptance: every backticked tool/skill/device/position/event in both prompt
    templates and `LAB.md` refers to a real registered symbol.
    """
    valid_tools = (
        {t.name for t in build_coordinator_tools()}
        | {t.name for t in build_operator_tools()}
        | {t.name for t in TodoListMiddleware().tools}
    )
    valid_skills = set(registry.SKILLS)
    valid_devices = {dev.spec.NAME for dev in ISLAND}
    valid_positions = set(POSITIONS)
    allowed = (
        valid_tools
        | valid_skills
        | valid_devices
        | valid_positions
        | KNOWN_EVENT_KINDS
        | KNOWN_PROTOCOL_TOKENS
    )

    seen_tools_and_skills: set[str] = set()
    files = [
        PROMPTS_DIR / "coordinator.md",
        PROMPTS_DIR / "operator.md",
        REPO_ROOT / "LAB.md",
    ]
    for path in files:
        raw = path.read_text(encoding="utf-8")
        stripped = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
        tokens = re.findall(r"`([^`]+)`", stripped)
        assert tokens, f"expected backticked names in {path.name}"
        for tok in tokens:
            assert "(" not in tok and ")" not in tok, f"signature in {path.name}: {tok}"
            assert tok in allowed, f"unknown backticked name {tok!r} in {path.name}"
            if tok in (valid_tools | valid_skills):
                seen_tools_and_skills.add(tok)

    # Confirm core Agent tools and lab skills are genuinely covered by the check
    assert {"get_lab_state", "list_skills", "rehearse", "submit_protocol"} <= seen_tools_and_skills
    assert {"pcr_close", "pcr_start_program", "shake"} <= seen_tools_and_skills
