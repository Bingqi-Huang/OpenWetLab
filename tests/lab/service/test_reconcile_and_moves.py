"""v0.6 §16.6 tests: #31 moving failure freeze in service & #34 startup GetDevices reconcile."""

import copy
import json
from pathlib import Path

from wetlab.lab.devices import positions_hash
from wetlab.lab.service import Service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, island, open_db, records, runs

CAPTURES = Path(__file__).resolve().parents[3] / "captures"


def test_turntable_failed_after_act_freezes_island_and_marks_step_unknown(
    tmp_path: Path,
) -> None:
    """§16.6 #31: Virtual island injecting turntable 'failed after rotating to 90' freezes island
    and marks the protocol step `unknown` with `DEVICE_FAILED_MAY_HAVE_MOVED`.
    """
    fault = Fault(
        device="turntable",
        action="SingleLoop",
        nth=1,
        kind="failed_after_act",
        message="转向命令未收到电机应答",
    )
    sim = SimBackend(Scenario(faults=[fault]))
    conn = open_db(tmp_path / "tt.sqlite", mode="virtual")
    svc = Service(sim, conn, runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        proto = {
            "name": "转向台90度",
            "labware": {},
            "steps": [{"skill": "rotate_turntable", "args": {"angle": 90}}],
        }
        with conn:
            inbox.enqueue_command(
                conn,
                key="m31",
                at=sim.now(),
                who="researcher",
                kind="new_mission",
                payload={"mission_id": "M-31", "title": "转向台故障"},
            )
            inbox.enqueue_command(
                conn,
                key="s31",
                at=sim.now(),
                who="operator",
                kind="submit_protocol",
                payload={"mission_id": "M-31", "protocol": proto},
            )
        svc.step()
        submitted = runs.get_protocol(conn, "E-001-v1")
        assert submitted is not None
        with conn:
            inbox.enqueue_command(
                conn,
                key="a31",
                at=sim.now(),
                who="researcher",
                kind="approve",
                payload={
                    "protocol_id": "E-001-v1",
                    "decided_by": "林老师",
                    "fingerprint": submitted["fingerprint"],
                },
            )
        svc.step()

        assert sim.state()["STEERING_PLATFORM001"]["Angles"] == "90"
        frz = island.get_freeze(conn)
        assert frz["frozen"] is True
        assert "转向命令未收到电机应答" in (frz["reason"] or "")

        step1 = runs.get_step(conn, "E-001-v1", 1)
        assert step1 is not None
        assert step1["status"] == "unknown"
        assert step1["result"]["error"]["code"] == "DEVICE_FAILED_MAY_HAVE_MOVED"
        assert runs.get_protocol(conn, "E-001-v1")["status"] == "running"  # type: ignore[index]

        tt_op = records.list_operations(conn, device="turntable")[-1]
        assert tt_op["status"] == "unknown"
        assert tt_op["error_code"] == "DEVICE_FAILED_MAY_HAVE_MOVED"
        assert any(e["kind"] == "frozen" for e in inbox.list_events_after(conn, 0))
    finally:
        conn.close()


def _load_gw001_devices() -> list[dict]:
    raw = (CAPTURES / "20261118_GW_001_devices_after_change.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _code_of(entry: dict) -> str | None:
    for cfg in entry.get("Configs", []):
        if cfg.get("Key") == "Code":
            return str(cfg["Value"])
    return None


def test_startup_persists_positions_hash_and_accepts_real_gw001_device_list(
    tmp_path: Path,
) -> None:
    """§5.16 & §16.6 #34: Startup writes `positions_hash` to `meta` and passes when all ISLAND
    devices are present and enabled (ignoring disabled T007001 and extra T009001).
    """
    gw_devices = _load_gw001_devices()
    sim = SimBackend(Scenario(gateway_devices=gw_devices))
    conn = open_db(tmp_path / "ok.sqlite", mode="virtual")
    svc = Service(sim, conn, runs_dir=tmp_path / "runs")
    try:
        info = svc.startup()
        assert island.get_meta(conn, "positions_hash") == positions_hash()
        assert island.get_freeze(conn)["frozen"] is False
        assert info["probed"] == [
            "pcr.GetPcrStatus",
            "shaker.GetStatus",
            "arm.RobotMode",
            "rack.GetStatus",
        ]
        ops = records.list_operations(conn)
        assert ops[0]["action"] == "GetDevices"
        assert ops[0]["status"] == "succeeded"
    finally:
        conn.close()


def test_startup_get_devices_freezes_and_names_missing_or_disabled_device(
    tmp_path: Path,
) -> None:
    """§16.6 #34: Missing or disabled ISLAND device in `GetDevices` freezes startup and names it."""
    full_list = _load_gw001_devices()

    # 1. Missing `STEERING_PLATFORM001` (`turntable`)
    missing_tt = [d for d in full_list if _code_of(d) != "STEERING_PLATFORM001"]
    sim_miss = SimBackend(Scenario(gateway_devices=missing_tt))
    conn_miss = open_db(tmp_path / "miss.sqlite", mode="virtual")
    svc_miss = Service(sim_miss, conn_miss, runs_dir=tmp_path / "runs_miss")
    try:
        svc_miss.startup()
        frz_miss = island.get_freeze(conn_miss)
        assert frz_miss["frozen"] is True
        reason_miss = frz_miss["reason"] or ""
        assert "turntable" in reason_miss
        assert "STEERING_PLATFORM001" in reason_miss
        assert "转向台" in reason_miss
        evs_miss = [e for e in inbox.list_events_after(conn_miss, 0) if e["kind"] == "frozen"]
        assert len(evs_miss) == 1
        assert "STEERING_PLATFORM001" in evs_miss[0]["payload"]["reason"]
    finally:
        conn_miss.close()

    # 2. Disabled `T002001` (`shaker`)
    disabled_shk = copy.deepcopy(full_list)
    for entry in disabled_shk:
        if _code_of(entry) == "T002001":
            entry["IsEnabled"] = False
    sim_dis = SimBackend(Scenario(gateway_devices=disabled_shk))
    conn_dis = open_db(tmp_path / "dis.sqlite", mode="virtual")
    svc_dis = Service(sim_dis, conn_dis, runs_dir=tmp_path / "runs_dis")
    try:
        svc_dis.startup()
        frz_dis = island.get_freeze(conn_dis)
        assert frz_dis["frozen"] is True
        reason_dis = frz_dis["reason"] or ""
        assert "shaker" in reason_dis
        assert "T002001" in reason_dis
        assert "恒温振荡模组" in reason_dis
        assert "停用" in reason_dis
        evs_dis = [e for e in inbox.list_events_after(conn_dis, 0) if e["kind"] == "frozen"]
        assert len(evs_dis) == 1
        assert "T002001" in evs_dis[0]["payload"]["reason"]
    finally:
        conn_dis.close()
