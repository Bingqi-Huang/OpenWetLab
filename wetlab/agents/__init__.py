"""Event-driven Coordinator and Operator agent system (docs/spec/04 section 6)."""

from .coordinator import build_coordinator_agent
from .handoff import (
    ExperimentResult,
    ExperimentTask,
    LabwareOutcome,
    ProtocolOutcome,
    StepOutcome,
    WakeTarget,
    build_experiment_result,
    classify_event,
)
from .messages import event_to_message
from .models import build_chat_model, has_model_credentials, validate_model_config
from .operator import build_operator_agent
from .tools import (
    TurnContext,
    WetLabAgentState,
    build_coordinator_tools,
    build_operator_tools,
    render_system_prompt,
)
from .worker import AgentWorker, run_worker

__all__ = [
    "AgentWorker",
    "ExperimentResult",
    "ExperimentTask",
    "LabwareOutcome",
    "ProtocolOutcome",
    "StepOutcome",
    "TurnContext",
    "WakeTarget",
    "WetLabAgentState",
    "build_chat_model",
    "build_coordinator_agent",
    "build_coordinator_tools",
    "build_experiment_result",
    "build_operator_agent",
    "build_operator_tools",
    "classify_event",
    "event_to_message",
    "has_model_credentials",
    "render_system_prompt",
    "run_worker",
    "validate_model_config",
]
