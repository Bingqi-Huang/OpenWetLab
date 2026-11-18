"""Deterministic fault injection for SimBackend."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..devices import BY_NAME, CODE_TO_NAME


class Fault(BaseModel):
    """Inject a device failure or timeout on the nth call to (device, action)."""

    device: str
    action: str
    nth: int = Field(default=1, ge=1)
    kind: Literal["fail", "no_result"]
    message: str = "injected device failure"

    @field_validator("device")
    @classmethod
    def _normalize_device(cls, v: str) -> str:
        short = CODE_TO_NAME.get(v, v)
        if short not in BY_NAME:
            raise ValueError(f"unknown device: {v!r}")
        return short


class FaultInjector:
    """Count (device, action) calls and match configured Fault triggers."""

    def __init__(self, faults: list[Fault]) -> None:
        self._faults = list(faults)
        self._counts: dict[tuple[str, str], int] = {}

    def next_fault(self, device_name: str, action: str) -> Fault | None:
        key = (device_name, action)
        nth = self._counts[key] = self._counts.get(key, 0) + 1
        for fault in self._faults:
            if fault.device == device_name and fault.action == action and fault.nth == nth:
                return fault
        return None
