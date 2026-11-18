"""Island device registry and derived lookup tables."""

from . import arm, gateway, pcr, rack, shaker, turntable
from .base import Position, Prop
from .positions import (
    LABWARE_POSITIONS,
    POINT_TO_POSITION,
    POSITION_DEVICE,
    POSITIONS,
    TAUGHT_POINTS,
    load_positions,
    positions_hash,
)

ISLAND = [pcr, shaker, arm, turntable, rack]

DEVICE_LABELS = {dev.spec.NAME: dev.spec.LABEL for dev in ISLAND}
PROPS: dict[str, dict[str, Prop]] = {dev.spec.NAME: dict(dev.spec.PROPS) for dev in ISLAND}

BY_NAME = {dev.spec.NAME: dev for dev in ISLAND}
BY_CODE = {dev.spec.CODE: dev for dev in ISLAND}

NAME_TO_CODE = {dev.spec.NAME: dev.spec.CODE for dev in ISLAND}
CODE_TO_NAME = {dev.spec.CODE: dev.spec.NAME for dev in ISLAND}

KEY_PROPS = {dev.spec.NAME: list(dev.spec.KEY_PROPS) for dev in ISLAND}

__all__ = [
    "BY_CODE",
    "BY_NAME",
    "CODE_TO_NAME",
    "DEVICE_LABELS",
    "ISLAND",
    "KEY_PROPS",
    "LABWARE_POSITIONS",
    "NAME_TO_CODE",
    "POINT_TO_POSITION",
    "POSITIONS",
    "POSITION_DEVICE",
    "PROPS",
    "Position",
    "Prop",
    "TAUGHT_POINTS",
    "arm",
    "gateway",
    "load_positions",
    "pcr",
    "positions_hash",
    "rack",
    "shaker",
    "turntable",
]
