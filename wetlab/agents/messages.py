"""Chinese wakeup message formatter for Coordinator and Operator turns (04 section 6.4)."""

import json
import sqlite3
from typing import Any

from ..lab.store import records, runs
from .handoff import build_experiment_result, has_unanswered_question


def _protocol_factual_summary(
    conn: sqlite3.Connection,
    protocol_id: str,
) -> dict[str, Any]:
    proto = runs.get_protocol(conn, protocol_id)
    steps = runs.list_steps(conn, protocol_id) if proto is not None else []
    active_items = records.list_labware(conn, active_only=True)
    active_lw = {item["id"]: item["position"] for item in active_items}
    return {
        "protocol_id": protocol_id,
        "version": proto["version"] if proto else None,
        "status": proto["status"] if proto else None,
        "reject_comment": proto["reject_comment"] if proto else None,
        "steps": [
            {
                "n": s["n"],
                "kind": s["kind"],
                "content": s["content"],
                "status": s["status"],
                "result": s["result"],
            }
            for s in steps
        ],
        "labware": active_lw,
    }


def event_to_message(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    """Format one wakeup `event` into the Chinese input message for the target Agent."""
    kind = event["kind"]
    ref_id = str(event.get("ref_id") or "")
    payload = event.get("payload") or {}

    if kind == "mission_created":
        mission = runs.get_mission(conn, ref_id) or {}
        active_items = records.list_labware(conn, active_only=True)
        active_lw = {lw["id"]: lw["position"] for lw in active_items}
        lw_json = json.dumps(active_lw, ensure_ascii=False)
        return (
            f"【事件：新任务已创建】任务编号：{ref_id}\n"
            f"标题：{mission.get('title', '')}\n"
            f"运行模式：{mission.get('mode', '')}\n"
            f"当前在岛器皿：{lw_json}\n"
            f"研究人员要求：\n{mission.get('input_text', '')}\n\n"
            "请先查看状态和可用技能，更新待办清单，然后调用 `create_experiment`、"
            "`ask_researcher` 或 `finish_mission` 结束本轮。"
        )

    if kind == "experiment_finished":
        result = build_experiment_result(conn, ref_id)
        dump_json = json.dumps(result.model_dump(), ensure_ascii=False, indent=2)
        return (
            f"【事件：实验已结束】实验编号：{ref_id}\n"
            f"实验结果（事实与操作员说明）：\n{dump_json}\n\n"
            "请更新待办清单，决定创建下一个实验（`create_experiment`）、"
            "向研究人员提问（`ask_researcher`），或结束整个任务（`finish_mission`）。"
        )

    if kind == "message":
        mid = str(payload.get("mission_id") or ref_id)
        text = str(payload.get("text") or "")
        if has_unanswered_question(conn, mid, int(event["id"])):
            return (
                f"【事件：研究人员回答了你的提问】\n{text}\n\n"
                "本任务存在尚未推进的提问。请结合研究人员的回答更新待办清单，"
                "并调用 `create_experiment`、`ask_researcher` 或 `finish_mission` 结束本轮。"
            )
        return (
            f"【事件：研究人员发来留言】\n{text}\n\n"
            "如果实验正在等待批准或正在执行，请直接用清晰的中文文字回复研究人员；"
            "若需要推进下一步，也可调用相应的工具命令。"
        )

    if kind == "command_failed":
        who = str(payload.get("who") or "")
        cmd_kind = str(payload.get("kind") or "未知命令")
        err = str(payload.get("error") or payload.get("reason") or "命令被实验岛服务拒绝")
        if who == "coordinator":
            return (
                f"【事件：上一轮命令未通过】失败命令：`{cmd_kind}`\n"
                f"失败原因：{err}\n\n"
                "请修正参数后调用 `create_experiment`、`ask_researcher` 或 `finish_mission` 结束本轮。"
            )
        return (
            f"【事件：上一轮命令未通过】失败命令：`{cmd_kind}`\n"
            f"失败原因：{err}\n\n"
            "请修正后重新调用 `submit_protocol` 或 `finish_experiment` 结束本轮。"
        )

    if kind == "experiment_created":
        exp = runs.get_experiment(conn, ref_id) or {}
        task_json = json.dumps(exp.get("task", {}), ensure_ascii=False, indent=2)
        return (
            f"【事件：新实验任务单】实验编号：{ref_id}（所属任务：{exp.get('mission_id', '')}）\n"
            f"实验任务单：\n{task_json}\n\n"
            "请先查看状态和技能，编写线性方案并调用 `rehearse` 预演，"
            "预演通过后调用 `submit_protocol` 提交；"
            "若任务在当前硬件条件下无法执行，调用 `finish_experiment` 说明原因。"
        )

    if kind == "protocol_rejected":
        proto = runs.get_protocol(conn, ref_id)
        feedback = (
            payload.get("comment")
            or (proto["reject_comment"] if proto else None)
            or payload.get("reason")
            or "未提供具体意见"
        )
        return (
            f"【事件：方案被退回】方案编号：{ref_id}\n"
            f"退回意见 / 失败原因：\n{feedback}\n\n"
            "请认真按照上述退回意见修改方案，重新调用 `rehearse` 预演通过后"
            "调用 `submit_protocol` 提交；若确实无法继续，调用 `finish_experiment` 结束本实验。"
        )

    if kind == "protocol_done":
        facts = _protocol_factual_summary(conn, ref_id)
        facts_json = json.dumps(facts, ensure_ascii=False, indent=2)
        return (
            f"【事件：方案已执行完成】方案编号：{ref_id}\n"
            f"执行事实记录：\n{facts_json}\n\n"
            "请核对事实记录，调用 `finish_experiment` 撰写实验说明并结束本实验。"
        )

    facts = _protocol_factual_summary(conn, ref_id)
    facts_json = json.dumps(facts, ensure_ascii=False, indent=2)
    payload_json = json.dumps(payload, ensure_ascii=False)
    label = "方案执行失败" if kind == "protocol_failed" else "方案已被人工停止"
    return (
        f"【事件：{label}】方案编号：{ref_id}\n"
        f"事件详情：{payload_json}\n"
        f"数据库事实记录（注意哪些步骤已完成 `done`、哪一步中断、哪些步骤尚未执行）：\n"
        f"{facts_json}\n\n"
        "如果现场允许继续完成剩余工作，请编写**只包含尚未完成的剩余步骤**的修订方案，"
        "经 `rehearse` 预演通过后调用 `submit_protocol` 提交；"
        "否则调用 `finish_experiment` 记录偏差并结束实验。"
    )
