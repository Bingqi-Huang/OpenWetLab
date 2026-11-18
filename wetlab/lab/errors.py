"""The ways a device command or skill can end without success (docs/spec/04 section 5.5)."""


class DeviceFailed(Exception):
    """The device answered and reported a failure. Raised by backends."""


class NoResult(Exception):
    """No final result arrived in time. The action may still happen. Raised by backends."""


class Blocked(Exception):
    """An interlock or a resource rule refused the action. Nothing was sent."""


class Unknown(Exception):
    """The outcome is not known (no result, or the state does not match). The lab is now frozen."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Frozen(Exception):
    """The lab is frozen and refuses actions until a person unfreezes it."""


class SkillFailed(Exception):
    """A skill detected an invalid post-condition (e.g. program ended early)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
