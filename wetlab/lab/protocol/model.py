"""Pydantic protocol schema and canonical JSON fingerprint (docs/spec/04 section 5.9)."""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillStep(BaseModel):
    """Invoke a registered lab skill with typed arguments."""

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)

    @property
    def kind(self) -> Literal["skill"]:
        return "skill"

    def to_step_dict(self) -> dict[str, Any]:
        return {"skill": self.skill, "args": dict(self.args)}


class WaitSpec(BaseModel):
    """Fixed-duration wait parameters."""

    model_config = ConfigDict(extra="forbid")

    seconds: float = Field(gt=0)


class WaitStep(BaseModel):
    """Wait for a fixed duration on the island clock."""

    model_config = ConfigDict(extra="forbid")

    wait: WaitSpec

    @property
    def kind(self) -> Literal["wait"]:
        return "wait"

    def to_step_dict(self) -> dict[str, Any]:
        return {"wait": {"seconds": self.wait.seconds}}


class WaitUntilSpec(BaseModel):
    """State-condition wait parameters (`device` uses short name)."""

    model_config = ConfigDict(extra="forbid")

    device: str = Field(min_length=1)
    state: dict[str, Any] = Field(min_length=1)
    max_seconds: float = Field(gt=0)


class WaitUntilStep(BaseModel):
    """Poll telemetry every second until `device` reaches `state` or `max_seconds` elapses."""

    model_config = ConfigDict(extra="forbid")

    wait_until: WaitUntilSpec

    @property
    def kind(self) -> Literal["wait_until"]:
        return "wait_until"

    def to_step_dict(self) -> dict[str, Any]:
        return {
            "wait_until": {
                "device": self.wait_until.device,
                "state": dict(self.wait_until.state),
                "max_seconds": self.wait_until.max_seconds,
            }
        }


ProtocolStep = SkillStep | WaitStep | WaitUntilStep


class Protocol(BaseModel):
    """Linear protocol submitted for rehearsal, human approval, and service execution."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    labware: dict[str, str] = Field(default_factory=dict)
    steps: list[ProtocolStep] = Field(min_length=1)


def coerce_protocol(protocol: Protocol | dict[str, Any]) -> Protocol:
    """Return a validated `Protocol` instance from a `Protocol` or raw dict."""
    return protocol if isinstance(protocol, Protocol) else Protocol.model_validate(protocol)


def fingerprint(protocol: Protocol | dict[str, Any]) -> str:
    """Compute a deterministic key-order-independent SHA-256 hex digest of `protocol`."""
    model = coerce_protocol(protocol)
    canonical = json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
