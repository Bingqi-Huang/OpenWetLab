"""Single entry point for executing low-level device commands (docs/spec/04 section 5.5)."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3
from typing import Any, NoReturn

from . import safety
from .backend import Backend
from .devices import BY_NAME, CODE_TO_NAME, NAME_TO_CODE, gateway
from .devices.base import READ
from .errors import Blocked, DeviceFailed, Frozen, NoResult, Unknown
from .materials import Materials
from .store import island, records

DEFAULT_TIMEOUT_S = 15.0
TIMEOUT_MARGIN_S = 20.0
FRESH_STATE_TIMEOUT_S = 10.0
FRESH_POLL_INTERVAL_S = 0.2


@dataclass(frozen=True)
class _OpCtx:
    seq: int
    op_id: str
    device: str
    action: str
    params: dict[str, Any]
    started_at: float


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
    else:
        with conn:
            yield


class Lab:
    """Executes low-level commands through freeze -> interlock -> backend -> verify -> record."""

    def __init__(
        self,
        backend: Backend,
        conn: sqlite3.Connection,
        *,
        protocol_id: str | None = None,
        step_n: int | None = None,
    ) -> None:
        self.backend = backend
        self.conn = conn
        self.materials = Materials(conn, backend.now)
        self.protocol_id = protocol_id
        self.step_n = step_n
        self.last_operation_id: str | None = None

    def state(self) -> dict[str, Any]:
        """Return island telemetry keyed by short device names plus `_gateway`."""
        raw = self.backend.state()
        out: dict[str, Any] = {}
        for key, props in raw.items():
            if key == "_gateway":
                out["_gateway"] = dict(props)
            elif key in CODE_TO_NAME:
                out[CODE_TO_NAME[key]] = dict(props)
        return out

    def _record(
        self,
        *,
        device: str,
        action: str,
        params: dict[str, Any],
        status: str,
        started_at: float,
        elapsed_s: float,
        seq: int | None = None,
        op_id: str | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        freeze_reason: str | None = None,
        freeze_at: float | None = None,
    ) -> str:
        with _tx(self.conn):
            rec_seq = seq if seq is not None else records.next_operation_seq(self.conn)
            rec_id = op_id if op_id is not None else f"WL-{int(started_at * 1000)}-{rec_seq}"
            records.insert_operation(
                self.conn,
                id=rec_id,
                seq=rec_seq,
                protocol_id=self.protocol_id,
                step_n=self.step_n,
                device=device,
                action=action,
                params=params,
                status=status,
                result=result,
                error_code=error_code,
                started_at=started_at,
                elapsed_s=elapsed_s,
            )
            if freeze_reason is not None:
                at_val = freeze_at if freeze_at is not None else (started_at + elapsed_s)
                island.set_frozen(
                    self.conn,
                    reason=freeze_reason,
                    at=at_val,
                    operation_id=rec_id,
                )
        self.last_operation_id = rec_id
        return rec_id

    def _fail_unknown(
        self,
        ctx: _OpCtx,
        code: str,
        msg: str,
        result: dict[str, Any] | None = None,
    ) -> NoReturn:
        now = self.backend.now()
        self._record(
            seq=ctx.seq,
            op_id=ctx.op_id,
            device=ctx.device,
            action=ctx.action,
            params=ctx.params,
            status="unknown",
            result=result,
            error_code=code,
            started_at=ctx.started_at,
            elapsed_s=now - ctx.started_at,
            freeze_reason=msg,
            freeze_at=now,
        )
        raise Unknown(code, msg)

    def _wait_fresh_state(self, device: str, returned_at: float) -> bool:
        dev = BY_NAME[device]
        deadline = returned_at + FRESH_STATE_TIMEOUT_S
        while True:
            dev_state = self.state().get(device) or {}
            poll_now = self.backend.now()
            age = dev_state.get("_age_s")
            if (
                age is not None
                and (poll_now - float(age)) >= returned_at - 1e-6
                and dev_state.get("WorkingStatus") != 1
                and all(dev_state.get(prop) is not None for prop in dev.spec.STATE_PROPS)
            ):
                return True
            if poll_now >= deadline:
                return False
            self.backend.sleep(min(FRESH_POLL_INTERVAL_S, deadline - poll_now))

    def do(
        self,
        device: str,
        action: str,
        params: dict[str, Any] | None = None,
        expect: dict[str, Any] | None = None,
        within_s: float = 15,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Run one low-level device command following 04 section 5.5."""
        call_params = dict(params) if params is not None else {}
        started_at = self.backend.now()

        # 1. Frozen island rejects commands unless read-only or stop/safe-shutdown.
        freeze_state = island.get_freeze(self.conn)
        if (
            freeze_state["frozen"]
            and safety.kind(device, action) != READ
            and not safety.is_stop(device, action, call_params)
        ):
            raise Frozen(freeze_state["reason"] or "lab is frozen")

        # 2. Interlock check; if refused, record blocked and send nothing.
        ok, reason = safety.check(device, action, call_params, self.state())
        if not ok:
            err_code = "GATEWAY_OFFLINE" if reason == "GATEWAY_OFFLINE" else "INTERLOCK_BLOCKED"
            self._record(
                device=device,
                action=action,
                params=call_params,
                status="blocked",
                error_code=err_code,
                started_at=started_at,
                elapsed_s=0.0,
            )
            raise Blocked(reason)

        dev = BY_NAME[device]
        act_meta = dev.spec.ACTIONS.get(action) or gateway.spec.ACTIONS[action]
        if timeout_s is not None:
            effective_timeout = float(timeout_s)
        elif act_meta.seconds is not None:
            effective_timeout = float(act_meta.seconds) + TIMEOUT_MARGIN_S
        else:
            effective_timeout = DEFAULT_TIMEOUT_S

        # 3. Allocate operation ID before dispatch and call backend once.
        seq = records.next_operation_seq(self.conn)
        op_id = f"WL-{int(started_at * 1000)}-{seq}"
        ctx = _OpCtx(seq, op_id, device, action, call_params, started_at)
        device_exc: DeviceFailed | None = None
        result: dict[str, Any] = {}
        try:
            result = self.backend.call(
                NAME_TO_CODE[device],
                action,
                call_params,
                effective_timeout,
                operation_id=op_id,
            )
        except DeviceFailed as exc:
            device_exc = exc
        except NoResult:
            msg = f"{device}.{action} timed out after {effective_timeout}s"
            self._fail_unknown(ctx, "TIMEOUT_UNKNOWN", msg)

        # 4. Wait up to 10s for a complete telemetry frame received after `returned_at`.
        returned_at = self.backend.now()
        fresh_ok = self._wait_fresh_state(device, returned_at)
        if device_exc is not None:
            err_payload = {"error": str(device_exc)}
            if safety.moves(device, action):
                msg = f"{device}.{action} failed and may have moved: {device_exc}"
                self._fail_unknown(ctx, "DEVICE_FAILED_MAY_HAVE_MOVED", msg, err_payload)
            elapsed = self.backend.now() - started_at
            self._record(
                seq=seq,
                op_id=op_id,
                device=device,
                action=action,
                params=call_params,
                status="failed",
                result=err_payload,
                error_code="DEVICE_FAILED",
                started_at=started_at,
                elapsed_s=elapsed,
            )
            raise device_exc
        if not fresh_ok and act_meta.kind != READ:
            msg = f"{device}.{action} received no fresh complete telemetry within 10s"
            self._fail_unknown(ctx, "VERIFY_FAILED", msg, result)

        # 5. Poll telemetry every 1s up to `within_s` until all `expect` properties match.
        if expect:
            deadline = self.backend.now() + within_s
            while True:
                dev_state = self.state().get(device) or {}
                if all(dev_state.get(prop) == val for prop, val in expect.items()):
                    break
                now = self.backend.now()
                if now >= deadline:
                    mismatch = {prop: dev_state.get(prop) for prop in expect}
                    msg = f"{device}.{action} verify failed: expected {expect}, got {mismatch}"
                    self._fail_unknown(ctx, "VERIFY_FAILED", msg, result)
                self.backend.sleep(min(1.0, deadline - now))

        # 6. Record succeeded operation and return device result.
        finished_at = self.backend.now()
        self._record(
            seq=seq,
            op_id=op_id,
            device=device,
            action=action,
            params=call_params,
            status="succeeded",
            result=result,
            started_at=started_at,
            elapsed_s=finished_at - started_at,
        )
        return result

    def unfreeze(self) -> None:
        """Clear the island freeze flag (invoked only by human unfreeze handling)."""
        with _tx(self.conn):
            island.clear_frozen(self.conn)

    def last_operation(self, device: str, action: str) -> dict[str, Any] | None:
        """Return the latest succeeded operation record for `(device, action)`, or None."""
        return records.last_operation(self.conn, device, action)
