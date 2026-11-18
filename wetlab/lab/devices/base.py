"""Foundational declarations shared by device specifications, rules, and skills."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import inspect
from typing import Any, Callable

READ = "read"
ACT = "act"


@dataclass(frozen=True)
class Action:
    """Metadata for a single low-level device RPC action."""

    kind: str
    seconds: int | None = None
    until_done: bool = False
    stop: bool = False
    result_topic: str = "device"

    def __post_init__(self) -> None:
        if self.kind not in (READ, ACT):
            raise ValueError(f"invalid action kind: {self.kind!r}")


@dataclass(frozen=True)
class Position:
    """A named taught point on the island."""

    label: str
    point: str
    posture: int = 0


@dataclass(frozen=True)
class Prop:
    """How a key telemetry property is shown to people: Chinese name, unit, value meanings.

    `unit="s"` marks a duration in seconds. `values` only lists meanings documented in
    02_GW001设备手册.md; any other value is shown as reported.
    """

    label: str
    unit: str = ""
    values: Mapping[Any, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillMeta:
    """Metadata attached to a skill function by @skill."""

    name: str
    kind: str
    label: str
    description: str


def skill(kind: str, label: str, *, name: str | None = None) -> Callable:
    """Mark a function as a lab skill without importing upper layers."""
    if kind not in (READ, ACT):
        raise ValueError(f"invalid skill kind: {kind!r}")

    def decorate(fn: Callable) -> Callable:
        fn.__skill__ = SkillMeta(
            name=name or fn.__name__,
            kind=kind,
            label=label,
            description=inspect.getdoc(fn) or "",
        )
        return fn

    return decorate


class SimDevice:
    """Base class for virtual SimPy device models."""

    def __init__(self, world, props: dict) -> None:
        self.world = world
        self.env = world.env
        self.props: dict = dict(props)

    def start_from_props(self) -> None:
        """Start background processes implied by initial props (override if needed)."""

    def refresh(self) -> None:
        """Refresh time-derived properties before reading state."""

    def ready_for_arm_truth(self) -> str | None:
        """Return why arm entry causes a physical accident, or None if safe."""
        return None
