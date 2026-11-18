"""Static, closed-skill, and ledger validation for protocols (docs/spec/04 section 5.9)."""

import sqlite3
from typing import Any

from pydantic import ValidationError

from .. import registry
from ..devices import BY_NAME, POSITIONS
from ..store import island, records
from .model import Protocol, SkillStep, WaitUntilStep, coerce_protocol


def validate(
    protocol: Protocol | dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Validate skill existence, closed skills, argument schemas, and labware ledger.

    Returns a list of human-readable problem strings (empty list means valid).
    """
    try:
        proto = coerce_protocol(protocol)
    except ValidationError as exc:
        return [
            f"方案格式错误 ({'.'.join(str(p) for p in err['loc'])}): {err['msg']}"
            for err in exc.errors()
        ]

    problems: list[str] = []
    seen_positions: dict[str, str] = {}
    for lw_id, pos in proto.labware.items():
        if pos not in POSITIONS:
            problems.append(f"器皿 {lw_id} 的位置未知: {pos!r}")
        elif pos in seen_positions:
            problems.append(
                f"器皿 {lw_id} 与 {seen_positions[pos]} 声明在同一位置 {pos!r}"
            )
        else:
            seen_positions[pos] = lw_id

    closed_skills: set[str] = set()
    if conn is not None:
        closed_skills = island.get_closed_skills(conn)
        ledger = {
            item["id"]: item["position"]
            for item in records.list_labware(conn, active_only=True)
        }
        for lw_id, expected_pos in proto.labware.items():
            actual_pos = ledger.get(lw_id)
            if actual_pos is None:
                problems.append(
                    f"器皿 {lw_id} 不在实验岛账本中（方案要求位于 {expected_pos}）"
                )
            elif actual_pos != expected_pos:
                problems.append(
                    f"器皿 {lw_id} 当前账本位置为 {actual_pos}，与方案要求的 {expected_pos} 不一致"
                )

    for idx, step in enumerate(proto.steps, start=1):
        if isinstance(step, SkillStep):
            if registry.get_skill(step.skill) is None:
                problems.append(f"第 {idx} 步：未知技能 {step.skill!r}")
            else:
                if step.skill in closed_skills:
                    problems.append(
                        f"第 {idx} 步：技能 {step.skill} 在这座实验岛上还没有开放"
                    )
                try:
                    registry.validate_args(step.skill, step.args)
                except (ValidationError, ValueError) as exc:
                    problems.append(f"第 {idx} 步（{step.skill}）参数不合法: {exc}")
        elif isinstance(step, WaitUntilStep):
            if step.wait_until.device not in BY_NAME:
                problems.append(
                    f"第 {idx} 步（wait_until）：未知设备 {step.wait_until.device!r}"
                )

    return problems
