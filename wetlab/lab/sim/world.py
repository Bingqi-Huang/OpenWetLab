"""Ground-truth plate ledger, history, accident log, and device lookup for virtual runs."""

import simpy

from ..devices import POINT_TO_POSITION, POSITION_DEVICE
from ..devices.base import SimDevice


class SimWorld:
    """Physical ground truth of the virtual island (invisible to agents)."""

    def __init__(self, *, start_time: float, plates: dict[str, str]) -> None:
        self.env = simpy.Environment()
        self.start_time = float(start_time)
        self.plates: dict[str, str] = dict(plates)
        self.history: dict[str, list[dict]] = {lw_id: [] for lw_id in self.plates}
        self.accidents: list[dict] = []
        self._devices: dict[str, SimDevice] = {}

    def now(self) -> float:
        return self.start_time + float(self.env.now)

    def register_device(self, name: str, sim_dev: SimDevice) -> None:
        self._devices[name] = sim_dev

    def device(self, name: str) -> SimDevice:
        return self._devices[name]

    def position_of_point(self, point: str) -> str | None:
        return POINT_TO_POSITION.get(point)

    def owner_of_position(self, pos_name: str) -> str:
        return POSITION_DEVICE[pos_name].spec.NAME

    def plate_at(self, position: str) -> str | None:
        for lw_id, pos in self.plates.items():
            if pos == position:
                return lw_id
        return None

    def move_plate(self, labware_id: str, to_position: str, *, record_move: bool = False) -> None:
        self.plates[labware_id] = to_position
        if record_move:
            self.history.setdefault(labware_id, []).append(
                {"at": self.now(), "what": "moved", "to": to_position}
            )

    def sync_human_labware(self, labware_id: str, position: str | None) -> None:
        """Reflect human labware placement, removal, or correction in ground truth."""
        if position is None:
            self.plates.pop(labware_id, None)
        else:
            self.plates[labware_id] = position
            self.history.setdefault(labware_id, [])

    def record(self, position: str, what: str, **detail) -> None:
        plate = self.plate_at(position)
        if plate is not None:
            self.history.setdefault(plate, []).append(
                {"at": self.now(), "what": what, **detail}
            )

    def accident(self, what: str) -> None:
        self.accidents.append({"at": self.now(), "what": what})
