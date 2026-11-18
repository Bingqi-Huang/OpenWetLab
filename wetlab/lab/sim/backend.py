"""SimPy-backed virtual Backend implementation."""

import copy
import importlib
import time
from typing import Any

from ..devices import BY_CODE, ISLAND, gateway
from ..devices.base import READ, SimDevice
from ..errors import DeviceFailed, NoResult
from .faults import FaultInjector
from .scenario import Scenario
from .world import SimWorld


def _default_gateway_devices() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for idx, dev in enumerate(ISLAND, start=1):
        items.append(
            {
                "Id": idx,
                "Name": dev.spec.LABEL,
                "DriverId": idx,
                "DriverName": f"{dev.spec.LABEL}驱动",
                "DriverTags": [],
                "Status": "online",
                "IsSimulation": True,
                "IsEnabled": True,
                "Configs": [{"Key": "Code", "Value": dev.spec.CODE}],
                "CreatedAt": "2026-11-18T00:00:00",
            }
        )
    return items


class SimBackend:
    """Deterministic virtual island backend implementing the Backend protocol."""

    def __init__(
        self,
        scenario: Scenario | dict | None = None,
        realtime: bool = False,
    ) -> None:
        if scenario is None:
            scen = Scenario()
        elif isinstance(scenario, dict):
            scen = Scenario.model_validate(scenario)
        else:
            scen = scenario

        self.realtime: bool = realtime
        self.closed_skills: set[str] = set()
        self.gateway_online: bool = True
        self.world = SimWorld(start_time=scen.start_time, plates=scen.labware)
        self._faults = FaultInjector(scen.faults)
        if scen.gateway_devices is None:
            self._gateway_devices = _default_gateway_devices()
        else:
            self._gateway_devices = copy.deepcopy(scen.gateway_devices)
        self._by_code: dict[str, SimDevice] = {}

        for dev in ISLAND:
            sim_cls = importlib.import_module(f"{dev.__name__}.sim").SIM_CLASS
            sim_dev: SimDevice = sim_cls(self.world)
            self.world.register_device(dev.spec.NAME, sim_dev)
            self._by_code[dev.spec.CODE] = sim_dev

        for dev_name, overrides in scen.devices.items():
            self.world.device(dev_name).props.update(overrides)

        pcr_overrides = scen.devices.get("pcr", {})
        pcr_sim = self.world.device("pcr")
        if (
            self.world.plate_at("pcr") is not None
            and pcr_sim.props["DoorState"] == "Closed"
            and "LidMotorState" not in pcr_overrides
        ):
            pcr_sim.props["LidMotorState"] = "Pressed"

        for sim_dev in self._by_code.values():
            sim_dev.start_from_props()

    def call(
        self,
        device_code: str,
        action: str,
        params: dict[str, Any],
        timeout_s: float,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        _ = operation_id
        dev = BY_CODE.get(device_code)
        if dev is None:
            raise DeviceFailed(f"未知设备: {device_code}")
        sim_dev = self._by_code[device_code]
        call_params = params or {}

        fault = self._faults.next_fault(dev.spec.NAME, action)
        if fault is not None and fault.kind == "no_result":
            self.sleep(timeout_s)
            raise NoResult(f"{device_code}.{action}: no result within {timeout_s}s")

        try:
            if fault is not None and fault.kind == "fail":
                self.sleep(1.0)
                raise DeviceFailed(fault.message)
            method = getattr(sim_dev, action, None)
            if action == "GetDevices":
                sim_dev.refresh()
                result = {"deviceData": copy.deepcopy(self._gateway_devices)}
            elif method is not None:
                proc = self.world.env.process(method(call_params))
                result = self.world.env.run(until=proc)
            else:
                act = dev.spec.ACTIONS.get(action) or gateway.spec.ACTIONS.get(action)
                if act is not None and act.kind == READ:
                    sim_dev.refresh()
                    result = dict(sim_dev.props)
                else:
                    raise DeviceFailed(f"未知功能: {action}")
            if fault is not None and fault.kind == "failed_after_act":
                raise DeviceFailed(fault.message)
        except DeviceFailed:
            sim_dev.props["WorkingStatus"] = 2
            raise

        sim_dev.props["WorkingStatus"] = 0
        return result if result is not None else {}

    def state(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "_gateway": {
                "online": self.gateway_online,
                "busy_by_others": False,
            }
        }
        for code, sim_dev in self._by_code.items():
            sim_dev.refresh()
            out[code] = {
                "_age_s": 0.0,
                "_error": None,
                **sim_dev.props,
            }
        return out

    def now(self) -> float:
        return self.world.now()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.world.env.run(until=self.world.env.timeout(seconds))
            if self.realtime:
                time.sleep(seconds)

    def sync_human_labware(self, labware_id: str, position: str | None) -> None:
        """Sync human labware changes (`place`, `remove`, `correct`) into the virtual tabletop."""
        self.world.sync_human_labware(labware_id, position)
