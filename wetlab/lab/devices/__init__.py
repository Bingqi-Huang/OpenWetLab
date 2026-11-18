"""Island device registry and derived lookup tables."""

from . import arm, gateway, pcr, rack, shaker, turntable
from .base import Position, Prop

ISLAND = [pcr, shaker, arm, turntable, rack]

DEVICE_LABELS = {dev.spec.NAME: dev.spec.LABEL for dev in ISLAND}
PROPS: dict[str, dict[str, Prop]] = {dev.spec.NAME: dict(dev.spec.PROPS) for dev in ISLAND}

BY_NAME = {dev.spec.NAME: dev for dev in ISLAND}
BY_CODE = {dev.spec.CODE: dev for dev in ISLAND}

NAME_TO_CODE = {dev.spec.NAME: dev.spec.CODE for dev in ISLAND}
CODE_TO_NAME = {dev.spec.CODE: dev.spec.NAME for dev in ISLAND}

POSITIONS: dict[str, Position] = {
    pos_name: pos
    for dev in ISLAND
    for pos_name, pos in dev.spec.POSITIONS.items()
}
POSITION_DEVICE = {
    pos_name: dev
    for dev in ISLAND
    for pos_name in dev.spec.POSITIONS
}
POINT_TO_POSITION = {pos.point: pos_name for pos_name, pos in POSITIONS.items()}

TAUGHT_POINTS: dict[str, Position] = {
    **{pos.point: pos for pos in POSITIONS.values()},
    arm.spec.HOME.point: arm.spec.HOME,
}

KEY_PROPS = {dev.spec.NAME: list(dev.spec.KEY_PROPS) for dev in ISLAND}
