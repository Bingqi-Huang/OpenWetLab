"""Virtual SimPy model for the passive storage rack placeholder device."""

from ..base import SimDevice


class RackSim(SimDevice):
    """SimPy model of T006001 using telemetry camelCase property names."""

    def __init__(self, world) -> None:
        super().__init__(
            world,
            {
                "rackState": 0,
                "rackStateText": "正常",
                "errorCode": 0,
                "errorText": "",
                "WorkingStatus": 0,
            },
        )


SIM_CLASS = RackSim
