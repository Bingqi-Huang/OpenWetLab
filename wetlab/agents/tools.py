"""Read-only inspection tools and idempotent terminal tools for Coordinator and Operator."""

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from ..lab import protocol as lab_protocol
from ..lab import registry
from ..lab.snapshot import read_snapshot
from ..lab.store import island
from .context import (
    DEFAULT_LAB_MD_PATH as DEFAULT_LAB_MD_PATH,
    DEFAULT_SOPS_DIR as DEFAULT_SOPS_DIR,
    PROMPTS_DIR as PROMPTS_DIR,
    REPO_ROOT as REPO_ROOT,
    TurnContext as TurnContext,
    WetLabAgentState as WetLabAgentState,
    island_clock as island_clock,
    render_system_prompt as render_system_prompt,
)
from .handoff import ExperimentTask

ExperimentFinalStatus = Literal["completed", "stopped", "failed"]
MissionFinalStatus = Literal["completed", "failed"]


@tool
def get_lab_state(runtime: ToolRuntime[TurnContext]) -> dict[str, Any]:
    """Read current island snapshot from SQLite (devices, labware, gateway, freeze)."""
    with runtime.context.connection() as c:
        return read_snapshot(c, now=island_clock(c))


@tool
def list_skills(runtime: ToolRuntime[TurnContext]) -> dict[str, Any]:
    """Return all registered island skills with availability, parameter schemas, and version."""
    cat = registry.catalog()
    with runtime.context.connection() as c:
        closed = island.get_closed_skills(c)
    skills = [{**item, "available": item["name"] not in closed} for item in cat["skills"]]
    return {"version": cat["version"], "skills": skills}


@tool
def read_sop(name: str, runtime: ToolRuntime[TurnContext]) -> dict[str, Any]:
    """Read a stored SOP or reference protocol file under `sops/` by name."""
    sops_dir = runtime.context.sops_dir
    if not sops_dir.is_dir():
        return {"ok": False, "error": f"sops directory not found: {sops_dir}"}
    candidates = [sops_dir / name, sops_dir / f"{name}.json", sops_dir / f"{name}.md"]
    resolved_root = sops_dir.resolve()
    for path in candidates:
        if path.is_file() and path.resolve().is_relative_to(resolved_root):
            text = path.read_text(encoding="utf-8")
            content = json.loads(text) if path.suffix == ".json" else text
            return {"ok": True, "name": path.name, "content": content}
    available = sorted(p.name for p in sops_dir.iterdir() if p.is_file())
    return {"ok": False, "error": f"SOP {name!r} not found", "available": available}


@tool(return_direct=True)
def create_experiment(
    objective: str,
    procedure: str,
    runtime: ToolRuntime[TurnContext],
    labware: dict[str, str] | None = None,
    max_lab_minutes: int = 120,
) -> dict[str, Any]:
    """Enqueue a new experiment task sheet for the Operator and end this Coordinator turn."""
    ctx = runtime.context
    task = ExperimentTask(
        objective=objective,
        procedure=procedure,
        labware=labware or {},
        constraints={"max_lab_minutes": max_lab_minutes},
    )
    return ctx.enqueue_terminal(
        who="coordinator",
        kind="create_experiment",
        target_id=ctx.mission_id,
        payload={"mission_id": ctx.mission_id, "task": task.model_dump()},
    )


@tool(return_direct=True)
def ask_researcher(question: str, runtime: ToolRuntime[TurnContext]) -> dict[str, Any]:
    """Send a clarifying question to the human researcher and end this Coordinator turn."""
    ctx = runtime.context
    return ctx.enqueue_terminal(
        who="coordinator",
        kind="ask",
        target_id=ctx.mission_id,
        payload={"mission_id": ctx.mission_id, "question": question},
    )


@tool(return_direct=True)
def finish_mission(
    conclusion: str,
    runtime: ToolRuntime[TurnContext],
    status: MissionFinalStatus = "completed",
) -> dict[str, Any]:
    """Complete or fail the entire research mission with a final conclusion."""
    ctx = runtime.context
    return ctx.enqueue_terminal(
        who="coordinator",
        kind="finish_mission",
        target_id=ctx.mission_id,
        payload={"mission_id": ctx.mission_id, "status": status, "conclusion": conclusion},
    )


@tool
def rehearse(protocol: dict[str, Any], runtime: ToolRuntime[TurnContext]) -> dict[str, Any]:
    """Dry-run `protocol` (`{name, labware, steps}`) on an isolated virtual island."""
    with runtime.context.connection() as c:
        outcome = lab_protocol.rehearse(c, protocol)
    if outcome["ok"]:
        rendered = lab_protocol.render(protocol, outcome)
        outcome = {**outcome, "rendered_markdown": rendered}
    return outcome


@tool(return_direct=True)
def submit_protocol(
    protocol: dict[str, Any],
    runtime: ToolRuntime[TurnContext],
) -> dict[str, Any]:
    """Submit `protocol` (`{name, labware, steps}`) for human review and end this Operator turn."""
    ctx = runtime.context
    exp_id = ctx.experiment_id or ctx.thread_id
    return ctx.enqueue_terminal(
        who="operator",
        kind="submit_protocol",
        target_id=exp_id,
        payload={"experiment_id": exp_id, "mission_id": ctx.mission_id, "protocol": protocol},
    )


@tool(return_direct=True)
def finish_experiment(
    status: ExperimentFinalStatus,
    notes: str,
    runtime: ToolRuntime[TurnContext],
) -> dict[str, Any]:
    """Finish this experiment (`completed | stopped | failed`) with factual notes."""
    ctx = runtime.context
    exp_id = ctx.experiment_id or ctx.thread_id
    return ctx.enqueue_terminal(
        who="operator",
        kind="finish_experiment",
        target_id=exp_id,
        payload={"experiment_id": exp_id, "status": status, "notes": notes},
    )


def build_coordinator_tools() -> list[BaseTool]:
    """Return the Coordinator tool list."""
    return [
        get_lab_state,
        list_skills,
        read_sop,
        create_experiment,
        ask_researcher,
        finish_mission,
    ]


def build_operator_tools() -> list[BaseTool]:
    """Return the Operator tool list."""
    return [
        get_lab_state,
        list_skills,
        read_sop,
        rehearse,
        submit_protocol,
        finish_experiment,
    ]
