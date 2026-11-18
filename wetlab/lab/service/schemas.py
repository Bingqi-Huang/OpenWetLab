"""Pydantic payload schemas for all 20 commands in the `commands` table (04 section 5.14)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CommandError(Exception):
    """Raised when a command fails validation or a domain precondition check."""


def _require_non_empty(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty")
    return stripped


class NewMissionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str | None = None
    title: str = "未命名任务"
    input_text: str = ""
    mode: str | None = None


class LabwareContentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    wells: list[str] | None = None
    volume_ul: float | None = Field(default=None, ge=0.0)
    note: str | None = None

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, v: str) -> str:
        return _require_non_empty(v, "name")

    @field_validator("wells")
    @classmethod
    def _normalize_wells(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = [w.strip() for w in v if w.strip()]
        return cleaned or None


class PlaceLabwarePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labware_id: str
    type: str = "96_plate"
    position: str
    contents: list[LabwareContentItem] = Field(default_factory=list)

    @field_validator("labware_id", "type", "position")
    @classmethod
    def _non_empty(cls, v: str, info: Any) -> str:
        return _require_non_empty(v, str(info.field_name))

    @field_validator("contents", mode="before")
    @classmethod
    def _default_contents(cls, v: Any) -> Any:
        return [] if v is None else v


class SetLabwareContentsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labware_id: str
    contents: list[LabwareContentItem]

    @field_validator("labware_id")
    @classmethod
    def _non_empty_lw(cls, v: str) -> str:
        return _require_non_empty(v, "labware_id")


class RemoveLabwarePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labware_id: str

    @field_validator("labware_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        return _require_non_empty(v, "labware_id")


class CorrectLabwarePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labware_id: str
    position: str | None = None

    @field_validator("labware_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        return _require_non_empty(v, "labware_id")


class TaskSheetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    procedure: str = ""
    labware: dict[str, str] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("objective")
    @classmethod
    def _non_empty_objective(cls, v: str) -> str:
        return _require_non_empty(v, "objective")


class CreateExperimentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str | None = None
    experiment_id: str | None = None
    task: TaskSheetPayload


class SubmitProtocolPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str | None = None
    mission_id: str | None = None
    protocol: dict[str, Any]


class ApprovePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: str | None = None
    decided_by: str
    fingerprint: str

    @field_validator("decided_by", "fingerprint")
    @classmethod
    def _non_empty(cls, v: str, info: Any) -> str:
        return _require_non_empty(v, str(info.field_name))


class RejectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: str | None = None
    decided_by: str | None = None
    comment: str

    @field_validator("comment")
    @classmethod
    def _non_empty_comment(cls, v: str) -> str:
        return _require_non_empty(v, "comment")


class PausePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: str | None = None


class ResumePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: str | None = None


class StopAllPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: str | None = None


class UnfreezePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conclusion: Literal["step_done", "step_failed", "abort"]
    labware_id: str | None = None
    labware_position: str | None = None


class AbortPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str | None = None
    protocol_id: str | None = None


class ReplyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str | None = None
    text: str


class FinishExperimentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str | None = None
    status: Literal["completed", "stopped", "failed"]
    notes: str = ""


class AskPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str | None = None
    question: str


class MessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str | None = None
    text: str


class FinishMissionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str | None = None
    status: Literal["completed", "failed"] = "completed"
    conclusion: str = ""


class AgentErrorPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    error: str = ""
    message: str | None = None
    mission_id: str | None = None
    experiment_id: str | None = None
    role: str | None = None
    thread_id: str | None = None
    event_id: int | None = None

    @model_validator(mode="after")
    def _normalize_error_text(self) -> "AgentErrorPayload":
        resolved = (self.error or self.message or "").strip()
        if not resolved:
            raise ValueError("agent_error requires non-empty error or message")
        self.error = resolved
        return self


PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "new_mission": NewMissionPayload,
    "place_labware": PlaceLabwarePayload,
    "set_labware_contents": SetLabwareContentsPayload,
    "remove_labware": RemoveLabwarePayload,
    "correct_labware": CorrectLabwarePayload,
    "create_experiment": CreateExperimentPayload,
    "submit_protocol": SubmitProtocolPayload,
    "approve": ApprovePayload,
    "reject": RejectPayload,
    "pause": PausePayload,
    "resume": ResumePayload,
    "stop_all": StopAllPayload,
    "unfreeze": UnfreezePayload,
    "abort": AbortPayload,
    "reply": ReplyPayload,
    "finish_experiment": FinishExperimentPayload,
    "ask": AskPayload,
    "message": MessagePayload,
    "finish_mission": FinishMissionPayload,
    "agent_error": AgentErrorPayload,
}
COMMAND_SCHEMAS = PAYLOAD_SCHEMAS


def validate_command_payload(kind: str, raw_payload: Any) -> BaseModel:
    """Validate `raw_payload` against the strict Pydantic schema for `kind`."""
    schema_cls = PAYLOAD_SCHEMAS.get(kind)
    if schema_cls is None:
        raise CommandError(f"unknown command kind: {kind}")
    if not isinstance(raw_payload, dict):
        raise CommandError("command payload must be a JSON object")
    return schema_cls.model_validate(raw_payload)
