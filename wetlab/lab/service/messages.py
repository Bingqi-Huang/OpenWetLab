"""Translate command validation, database, and domain errors into researcher Chinese (04 §16.7)."""

import re
import sqlite3
from typing import Any

from pydantic import ValidationError

from ..devices import POSITIONS

_FIELD_LABELS: dict[str, str] = {
    "mission_id": "任务编号", "title": "任务标题", "input_text": "任务说明", "mode": "运行模式",
    "labware_id": "器皿编号", "type": "器皿类型", "position": "位置",
    "labware_position": "器皿实际位置", "contents": "板内样品内容",
    "name": "名称", "wells": "孔位", "volume_ul": "体积", "note": "备注",
    "experiment_id": "实验编号", "task": "任务单", "objective": "实验目标",
    "procedure": "实验步骤说明", "labware": "涉及器皿", "constraints": "限制条件",
    "protocol": "实验方案", "protocol_id": "方案编号", "decided_by": "审批人",
    "fingerprint": "方案指纹", "comment": "退回意见", "conclusion": "结论",
    "status": "状态", "notes": "实验说明", "question": "提问内容", "text": "消息内容",
    "error": "错误信息", "message": "提示信息", "role": "角色", "thread_id": "会话编号",
    "event_id": "事件编号", "count": "数量", "code": "编码", "flag": "开关标记",
    "steps": "步骤列表", "skill": "技能", "args": "技能参数",
}

_STATUS_LABELS: dict[str, str] = {
    "running": "运行中", "completed": "已完成", "failed": "已失败", "stopped": "已终止",
    "submitted": "待审阅", "approved": "已批准", "rejected": "已退回",
    "paused": "已暂停", "aborted": "已中止",
}

_STATIC_DOMAIN: dict[str, str] = {
    "command payload must be a JSON object": "命令内容格式不对：payload 应该是一张表",
    "agent_error requires non-empty error or message": (
        "Agent 报错记录缺少必填项：error 或 message 不能为空"
    ),
    "fingerprint mismatch": "方案指纹（fingerprint）与待审阅版本不一致，请刷新后重新确认",
    "another protocol is already active": "当前已有正在执行或已暂停的方案，请先等待其结束",
    "island is not frozen": "实验岛当前没有冻结，无需解除冻结",
    "unfreeze on a transfer step requires labware_position": (
        "当前冻结在搬运步骤，解除冻结时必须填写器皿的实际位置（labware_position）"
    ),
}

_EXP_ALREADY_RE = re.compile(r"^experiment (\S+) is already (\S+)$")
_LW_ALREADY_ON_RE = re.compile(r"^labware (\S+) is already on the island at (\S+)$")
_POS_OCCUPIED_RE = re.compile(r"^position (\S+) is already occupied by (\S+)$")
_LW_NOT_ON_RE = re.compile(r"^labware (\S+) is not on the island$")


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _pos_label(pos: str) -> str:
    info = POSITIONS.get(pos)
    if info is not None and info.label != pos:
        return f"{info.label}（{pos}）"
    return pos


def _cmd_str(cmd: dict[str, Any] | None, *keys: str, use_target: bool = False) -> str | None:
    if not isinstance(cmd, dict):
        return None
    raw_payload = cmd.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    if use_target:
        tid = cmd.get("target_id")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()
    return None


def _loc_path(loc: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in loc)


def _field_display(loc: tuple[Any, ...]) -> str:
    if not loc:
        return "命令参数"
    path = _loc_path(loc)
    cn_parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            cn_parts.append(f"第 {part + 1} 项")
        else:
            cn_parts.append(_FIELD_LABELS.get(str(part), str(part)))
    joined = "的".join(cn_parts).replace("的的第 ", "第 ").replace("的第 ", "第 ")
    return path if joined == path else f"{joined}（{path}）"


def _translate_value_error(inner: str, loc: tuple[Any, ...]) -> str:
    disp = _field_display(loc)
    if inner.endswith(" must not be empty"):
        field_key = inner[: -len(" must not be empty")].strip()
        target = disp if loc else _field_display((field_key,))
        return f"{target}不能为空"
    if "agent_error requires non-empty error or message" in inner:
        return _STATIC_DOMAIN["agent_error requires non-empty error or message"]
    if _contains_cjk(inner):
        return f"{disp}：{inner}" if loc else inner
    return f"{disp}填写不符合要求"


def _translate_pydantic_item(err: Any) -> str:
    err_type = str(err.get("type") or "")
    loc = tuple(err.get("loc") or ())
    path = _loc_path(loc)
    disp = _field_display(loc)
    ctx = err.get("ctx") or {}

    if err_type == "missing":
        return f"缺少必填项：{disp}"
    if err_type in ("model_type", "model_attributes_type", "dict_type"):
        top_cn = _FIELD_LABELS.get(path) if len(loc) == 1 else None
        if top_cn is not None:
            return f"{top_cn}格式不对：{path} 应该是一张表"
        return f"{disp}格式不对：{path or '内容'} 应该是一张表"
    if err_type in ("list_type", "tuple_type", "set_type", "frozenset_type"):
        return f"{disp}格式不对：应该是一组列表"
    if err_type in ("string_type", "string_unicode"):
        return f"{disp}格式不对：应该是文字（字符串）"
    if err_type in ("int_type", "int_parsing", "int_parsing_size", "int_from_float"):
        return f"{disp}格式不对：应该是整数"
    if err_type in ("float_type", "float_parsing", "decimal_type", "decimal_parsing"):
        return f"{disp}格式不对：应该是数字"
    if err_type in ("bool_type", "bool_parsing"):
        return f"{disp}格式不对：应该是布尔值（是或否）"
    if err_type in ("literal_error", "enum"):
        allowed = str(ctx.get("expected") or "").replace(" or ", "、").replace(", ", "、")
        return f"{disp}取值不在允许范围内（允许值：{allowed}）" if allowed else f"{disp}取值不在允许范围内"
    if err_type == "extra_forbidden":
        return f"包含不允许的多余字段：{disp}"
    if err_type == "greater_than":
        return f"{disp}数值超出范围：必须大于 {ctx.get('gt')}"
    if err_type == "greater_than_equal":
        return f"{disp}数值超出范围：不能小于 {ctx.get('ge')}"
    if err_type == "less_than":
        return f"{disp}数值超出范围：必须小于 {ctx.get('lt')}"
    if err_type == "less_than_equal":
        return f"{disp}数值超出范围：不能大于 {ctx.get('le')}"
    if err_type == "string_too_short":
        min_len = ctx.get("min_length")
        return f"{disp}不能为空" if min_len == 1 else f"{disp}长度不足：至少需要 {min_len} 个字符"
    if err_type == "string_too_long":
        return f"{disp}长度超出限制：最多允许 {ctx.get('max_length')} 个字符"
    if err_type == "too_short":
        return f"{disp}数量不足：至少需要 {ctx.get('min_length')} 项"
    if err_type == "too_long":
        return f"{disp}数量超出限制：最多允许 {ctx.get('max_length')} 项"
    if err_type in ("value_error", "assertion_error"):
        msg = str(err.get("msg") or "")
        for prefix in ("Value error, ", "Assertion failed, "):
            if msg.startswith(prefix):
                msg = msg[len(prefix) :]
        return _translate_value_error(msg, loc)
    return f"{disp}格式或取值不符合要求"


def translate_validation_error(exc: ValidationError) -> str:
    """Translate a Pydantic `ValidationError` into researcher-readable Chinese."""
    parts = [_translate_pydantic_item(err) for err in exc.errors()]
    return "；".join(parts) if parts else "提交的数据格式不符合要求"


def translate_sqlite_error(raw: str, cmd: dict[str, Any] | None = None) -> str:
    """Translate a SQLite `IntegrityError` or constraint error into researcher Chinese."""
    if raw.startswith("UNIQUE constraint failed:"):
        cols = raw.split(":", 1)[1].strip()
        if cols == "missions.id":
            mid = _cmd_str(cmd, "mission_id", use_target=True)
            return f"任务编号 {mid} 已存在" if mid else "任务编号已存在"
        if cols == "experiments.id":
            eid = _cmd_str(cmd, "experiment_id", use_target=True)
            return f"实验编号 {eid} 已存在" if eid else "实验编号已存在"
        if cols == "experiments.mission_id, experiments.n":
            mid = _cmd_str(cmd, "mission_id", use_target=True)
            return f"任务 {mid} 下的实验序号已存在" if mid else "同一任务下的实验序号已存在"
        if cols in ("protocols.id", "protocols.experiment_id, protocols.version"):
            pid = _cmd_str(cmd, "protocol_id", use_target=True)
            eid = _cmd_str(cmd, "experiment_id")
            if cols == "protocols.id" and pid:
                return f"方案编号 {pid} 已存在"
            return f"实验 {eid} 的方案版本已存在" if eid else "方案编号或版本已存在"
        if cols == "labware.id":
            lw = _cmd_str(cmd, "labware_id", use_target=True)
            return f"器皿编号 {lw} 已存在" if lw else "器皿编号已存在"
        if cols == "labware.position":
            pos = _cmd_str(cmd, "position", "labware_position")
            return f"位置 {_pos_label(pos)} 已有其他器皿" if pos else "该位置已有其他器皿"
        if cols == "commands.key":
            return "该命令已提交过，请勿重复提交"
        return f"记录已存在，不能重复创建（{cols}）"

    if "FOREIGN KEY constraint failed" in raw:
        kind = str((cmd or {}).get("kind") or "")
        lw = _cmd_str(cmd, "labware_id")
        lw_kinds = ("place_labware", "remove_labware", "correct_labware", "set_labware_contents")
        if kind in lw_kinds or lw:
            lw_id = lw or _cmd_str(cmd, "labware_id", use_target=True)
            return f"关联的器皿 {lw_id} 不存在" if lw_id else "关联的器皿不存在"
        pid = _cmd_str(cmd, "protocol_id")
        if kind in ("approve", "reject", "pause", "resume") or pid:
            p_val = pid or _cmd_str(cmd, "protocol_id", use_target=True)
            return f"关联的方案 {p_val} 不存在" if p_val else "关联的方案不存在"
        eid = _cmd_str(cmd, "experiment_id")
        if kind in ("submit_protocol", "finish_experiment", "abort") and eid:
            return f"关联的实验 {eid} 或任务不存在"
        mid = _cmd_str(cmd, "mission_id", use_target=True)
        return f"关联的任务 {mid} 不存在" if mid else "关联的任务、实验或器皿不存在"

    if raw.startswith("NOT NULL constraint failed:"):
        col = raw.split(":", 1)[1].strip().split(".")[-1]
        return f"缺少必填项：{_field_display((col,))}不能为空"

    if raw.startswith("CHECK constraint failed:"):
        if "mode" in raw:
            return "运行模式（mode）取值不合法，只允许 virtual（虚拟岛）或 real（真机）"
        if "frozen" in raw:
            return "实验岛冻结状态取值不合法"
        return "提交的数据未通过完整性检查"

    return "数据库约束检查未通过，无法保存该操作"


def translate_text_error(raw_text: str, cmd: dict[str, Any] | None = None) -> str:
    """Translate known English domain/ledger error text into Chinese; keep existing Chinese."""
    raw = raw_text.strip()
    if raw in _STATIC_DOMAIN:
        return _STATIC_DOMAIN[raw]
    if raw.startswith("unknown command kind:"):
        kind = raw.split(":", 1)[1].strip()
        return f"不支持的命令类型：{kind}"
    if raw.endswith(" must not be empty"):
        return _translate_value_error(raw, ())
    if raw.startswith("unknown mission:"):
        mid = raw.split(":", 1)[1].strip()
        return f"任务 {mid} 不存在" if mid and mid != "None" else "缺少有效的任务编号（mission_id）"
    if raw.startswith("unknown experiment:"):
        eid = raw.split(":", 1)[1].strip()
        return f"实验 {eid} 不存在" if eid and eid != "None" else "缺少有效的实验编号（experiment_id）"
    m_exp = _EXP_ALREADY_RE.match(raw)
    if m_exp is not None:
        st_cn = _STATUS_LABELS.get(m_exp.group(2), m_exp.group(2))
        return f"实验 {m_exp.group(1)} 处于{st_cn}状态，不能再提交新方案"
    pid = _cmd_str(cmd, "protocol_id", use_target=True)
    if raw == "protocol is not in submitted status":
        return f"方案 {pid} 当前不是待审阅状态，无法批准" if pid else "当前没有待审阅的方案，无法批准"
    if raw == "reject requires a submitted protocol":
        return f"方案 {pid} 当前不是待审阅状态，无法退回" if pid else "当前没有待审阅的方案，无法退回"
    if raw == "no running protocol to pause":
        return f"方案 {pid} 当前不在运行中，无法暂停" if pid else "当前没有正在运行的方案，无法暂停"
    if raw == "resume is only allowed for a paused protocol":
        return f"方案 {pid} 未处于已暂停状态，无法继续执行" if pid else "只有已暂停的方案才能继续执行"
    if raw == "cannot resolve experiment to abort":
        eid = _cmd_str(cmd, "experiment_id", use_target=True)
        return f"找不到要终止的实验：{eid}" if eid else "当前没有可终止的实验"
    if raw.startswith("unknown position:"):
        pos = raw.split(":", 1)[1].strip()
        if pos in POSITIONS:
            return f"位置 {_pos_label(pos)} 不能放置器皿"
        return f"未知的台面位置：{pos}"
    m_lw_on = _LW_ALREADY_ON_RE.match(raw)
    if m_lw_on is not None:
        pos_cn = _pos_label(m_lw_on.group(2))
        return f"器皿 {m_lw_on.group(1)} 已在实验岛的 {pos_cn} 上，不能重复放置"
    m_occ = _POS_OCCUPIED_RE.match(raw)
    if m_occ is not None:
        return f"位置 {_pos_label(m_occ.group(1))} 已被器皿 {m_occ.group(2)} 占用"
    m_not_on = _LW_NOT_ON_RE.match(raw)
    if m_not_on is not None:
        return f"器皿 {m_not_on.group(1)} 当前不在实验岛台面上"
    if raw.startswith("unknown labware:"):
        return f"器皿 {raw.split(':', 1)[1].strip()} 未在账本中登记"
    if _contains_cjk(raw):
        return raw
    return f"命令未能执行：{raw}" if raw else "命令未能执行：未知错误"


def format_command_error(exc: BaseException, cmd: dict[str, Any] | None = None) -> str:
    """Convert any command exception into clear Chinese for biologists."""
    if isinstance(exc, ValidationError):
        return translate_validation_error(exc)
    if isinstance(exc, sqlite3.Error):
        return translate_sqlite_error(str(exc), cmd)
    return translate_text_error(str(exc), cmd)
