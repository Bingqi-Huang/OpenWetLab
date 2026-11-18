"""Skill discovery, parameter validation, execution wrapper, and catalog (docs/spec/04 section 5.8)."""

from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import inspect
import json
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, ValidationError, create_model, model_validator

from .devices import ISLAND, POSITIONS
from .devices.base import SkillMeta
from .errors import Blocked, DeviceFailed, Frozen, SkillFailed, Unknown
from .snapshot import extract_key_state
from .store import records


@dataclass(frozen=True)
class RegisteredSkill:
    meta: SkillMeta
    device: str
    fn: Callable[..., Any]
    param_names: tuple[str, ...]
    args_model: type[BaseModel]
    source: str


def _build_args_model(name: str, fn: Callable[..., Any]) -> tuple[tuple[str, ...], type[BaseModel]]:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if not params:
        raise ValueError(f"skill {name!r} must accept `lab` as its first parameter")
    fields: dict[str, Any] = {}
    param_names: list[str] = []
    for p in params[1:]:
        param_names.append(p.name)
        ann = Any if p.annotation is inspect.Parameter.empty else p.annotation
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[p.name] = (ann, default)

    validators: dict[str, Any] = {}
    extra_validator = getattr(fn, "__skill_validator__", None)
    if extra_validator is not None:
        validators["__skill_check__"] = model_validator(mode="after")(extra_validator)

    model = create_model(
        f"{name}_Args",
        __config__=ConfigDict(extra="forbid", strict=True),
        __validators__=validators or None,
        **fields,
    )
    return tuple(param_names), model


def _discover_skills() -> dict[str, RegisteredSkill]:
    discovered: dict[str, RegisteredSkill] = {}
    for dev in ISLAND:
        mod_name = f"{dev.__name__}.skills"
        if importlib.util.find_spec(mod_name) is None:
            continue
        mod = importlib.import_module(mod_name)
        for obj in vars(mod).values():
            meta: SkillMeta | None = getattr(obj, "__skill__", None)
            if meta is None or not callable(obj):
                continue
            param_names, args_model = _build_args_model(meta.name, obj)
            discovered[meta.name] = RegisteredSkill(
                meta=meta,
                device=dev.spec.NAME,
                fn=obj,
                param_names=param_names,
                args_model=args_model,
                source=inspect.getsource(obj),
            )
    return discovered


SKILLS: dict[str, RegisteredSkill] = _discover_skills()


def get_skill(name: str) -> RegisteredSkill | None:
    """Look up a registered skill by name."""
    return SKILLS.get(name)


def validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate skill arguments against its Pydantic model and return validated kwargs."""
    entry = SKILLS.get(name)
    if entry is None:
        raise ValueError(f"unknown skill: {name}")
    parsed = entry.args_model.model_validate(args)
    return {k: getattr(parsed, k) for k in entry.param_names}


def _collect_operation_ids(conn: Any, after_seq: int) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM operations WHERE seq > ? ORDER BY seq ASC",
        (after_seq,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def _build_state_after(lab: Any) -> dict[str, Any]:
    active = records.list_labware(lab.conn, active_only=True)
    labware_map = {item["id"]: item["position"] for item in active}
    return extract_key_state(lab.state(), labware_map)


def _envelope(
    *,
    status: str,
    result: dict[str, Any],
    error: dict[str, str] | None,
    operation_ids: list[str],
    state_after: dict[str, Any],
    elapsed_s: float,
) -> dict[str, Any]:
    return {
        "ok": status == "succeeded",
        "status": status,
        "result": result,
        "error": error,
        "operation_ids": operation_ids,
        "state_after": state_after,
        "elapsed_s": round(max(0.0, elapsed_s), 3),
    }


def call(lab: Any, name: str, **args: Any) -> dict[str, Any]:
    """Single entry point to invoke a lab skill and return the unified 01 section 5.1 envelope."""
    started_at = lab.backend.now()
    entry = SKILLS.get(name)
    if entry is None:
        return _envelope(
            status="blocked",
            result={},
            error={"code": "INVALID_ARGUMENT", "message": f"unknown skill: {name}"},
            operation_ids=[],
            state_after=_build_state_after(lab),
            elapsed_s=lab.backend.now() - started_at,
        )

    if name in lab.backend.closed_skills:
        return _envelope(
            status="not_available",
            result={},
            error={"code": "NOT_AVAILABLE", "message": f"skill {name!r} is not yet commissioned"},
            operation_ids=[],
            state_after=_build_state_after(lab),
            elapsed_s=lab.backend.now() - started_at,
        )

    try:
        parsed = entry.args_model.model_validate(args)
    except ValidationError as exc:
        return _envelope(
            status="blocked",
            result={},
            error={"code": "INVALID_ARGUMENT", "message": str(exc)},
            operation_ids=[],
            state_after=_build_state_after(lab),
            elapsed_s=lab.backend.now() - started_at,
        )

    validated_kwargs = {k: getattr(parsed, k) for k in entry.param_names}
    pre_seq = records.next_operation_seq(lab.conn) - 1
    lab.positions = POSITIONS

    try:
        raw_res = entry.fn(lab, **validated_kwargs)
    except Blocked as exc:
        code = "GATEWAY_OFFLINE" if str(exc) == "GATEWAY_OFFLINE" else "INTERLOCK_BLOCKED"
        status, res, err = "blocked", {}, {"code": code, "message": str(exc)}
    except DeviceFailed as exc:
        status, res, err = "failed", {}, {"code": "DEVICE_FAILED", "message": str(exc)}
    except SkillFailed as exc:
        status, res, err = "failed", {}, {"code": exc.code, "message": str(exc)}
    except Unknown as exc:
        status, res, err = "unknown", {}, {"code": exc.code, "message": str(exc)}
    except Frozen as exc:
        status, res, err = "frozen", {}, {"code": "FROZEN", "message": str(exc)}
    else:
        status = "succeeded"
        res = raw_res if isinstance(raw_res, dict) else ({} if raw_res is None else {"value": raw_res})
        err = None

    return _envelope(
        status=status,
        result=res,
        error=err,
        operation_ids=_collect_operation_ids(lab.conn, pre_seq),
        state_after=_build_state_after(lab),
        elapsed_s=lab.backend.now() - started_at,
    )


def catalog() -> dict[str, Any]:
    """Export all registered skills, parameter JSON schemas, and deterministic version hash."""
    items: list[dict[str, Any]] = []
    fingerprint_payload: list[dict[str, Any]] = []
    for entry in SKILLS.values():
        schema = entry.args_model.model_json_schema()
        item = {
            "name": entry.meta.name,
            "device": entry.device,
            "kind": entry.meta.kind,
            "label": entry.meta.label,
            "description": entry.meta.description,
            "parameters": schema,
        }
        items.append(item)
        fingerprint_payload.append({**item, "source": entry.source})

    canonical = json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False)
    version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {"version": version, "skills": items}
