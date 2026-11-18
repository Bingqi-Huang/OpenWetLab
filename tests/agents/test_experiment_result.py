"""Tests for multi-version `ExperimentResult` and labware scoping (`04` v0.6 #9)."""

from pathlib import Path

from wetlab.agents.handoff import build_experiment_result
from wetlab.lab.service.main import Service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, open_db, runs


def test_experiment_result_lists_all_protocol_versions_and_scopes_labware(tmp_path: Path) -> None:
    """v0.6 #9: `ExperimentResult` includes every protocol version (rejected unexecuted + failed
    with steps + done with steps) and scopes `labware` to declared IDs & this experiment's window.
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    conn = open_db(db_path)
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "shaker", "LW-OTHER": "rack:1-3"},
            faults=[Fault(device="pcr", action="OpenDoor", nth=1, kind="fail", message="门卡住")],
        )
    )
    svc = Service(backend, conn, runs_dir=runs_dir)
    svc.startup()

    with conn:
        inbox.enqueue_command(
            conn,
            key="lw1",
            at=backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
        )
        inbox.enqueue_command(
            conn,
            key="lw_other",
            at=backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-OTHER", "type": "96_plate", "position": "rack:1-3"},
        )
        inbox.enqueue_command(
            conn,
            key="m",
            at=backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-9", "title": "多版结果", "input_text": "测试"},
        )
        inbox.enqueue_command(
            conn,
            key="e1",
            at=backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={"mission_id": "M-9", "task": {"objective": "前置实验", "procedure": "振荡"}},
        )
        inbox.enqueue_command(
            conn,
            key="sub_e1",
            at=backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id="E-001",
            payload={
                "experiment_id": "E-001",
                "protocol": {
                    "name": "前置振荡",
                    "labware": {"LW-001": "shaker"},
                    "steps": [{"skill": "shake", "args": {"seconds": 3, "rpm": 250}}],
                },
            },
        )
    svc.step()
    p_e1 = runs.get_protocol(conn, "E-001-v1")
    assert p_e1 is not None
    with conn:
        inbox.enqueue_command(
            conn,
            key="app_e1",
            at=backend.now(),
            who="ui",
            kind="approve",
            target_id="E-001-v1",
            payload={
                "protocol_id": "E-001-v1",
                "decided_by": "王",
                "fingerprint": p_e1["fingerprint"],
            },
        )
    svc.step()
    svc.step()
    with conn:
        inbox.enqueue_command(
            conn,
            key="fe_e1",
            at=backend.now(),
            who="operator",
            kind="finish_experiment",
            target_id="E-001",
            payload={"experiment_id": "E-001", "status": "completed", "notes": "前置完成"},
        )
    svc.step()

    # Target E-002: v1 rejected (never executed), v2 step 1 done + step 2 failed, v3 done
    with conn:
        inbox.enqueue_command(
            conn,
            key="e2",
            at=backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={"mission_id": "M-9", "task": {"objective": "主实验", "procedure": "振荡+开门"}},
        )
        inbox.enqueue_command(
            conn,
            key="sub_v1",
            at=backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id="E-002",
            payload={
                "experiment_id": "E-002",
                "protocol": {
                    "name": "v1初稿",
                    "labware": {"LW-001": "shaker"},
                    "steps": [{"skill": "shake", "args": {"seconds": 5, "rpm": 400}}],
                },
            },
        )
    svc.step()
    with conn:
        inbox.enqueue_command(
            conn,
            key="rej_v1",
            at=backend.now(),
            who="ui",
            kind="reject",
            target_id="E-002-v1",
            payload={"protocol_id": "E-002-v1", "decided_by": "王", "comment": "请加开门步骤"},
        )
        inbox.enqueue_command(
            conn,
            key="sub_v2",
            at=backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id="E-002",
            payload={
                "experiment_id": "E-002",
                "protocol": {
                    "name": "v2执行失败版",
                    "labware": {"LW-001": "shaker"},
                    "steps": [
                        {"skill": "shake", "args": {"seconds": 6, "rpm": 600}},
                        {"skill": "pcr_open", "args": {}},
                        {"skill": "pcr_close", "args": {}},
                    ],
                },
            },
        )
    svc.step()
    p_v2 = runs.get_protocol(conn, "E-002-v2")
    assert p_v2 is not None
    with conn:
        inbox.enqueue_command(
            conn,
            key="app_v2",
            at=backend.now(),
            who="ui",
            kind="approve",
            target_id="E-002-v2",
            payload={
                "protocol_id": "E-002-v2",
                "decided_by": "王",
                "fingerprint": p_v2["fingerprint"],
            },
        )
    for _ in range(5):
        svc.step()
        cur_v2 = runs.get_protocol(conn, "E-002-v2")
        if cur_v2 is not None and cur_v2["status"] == "failed":
            break

    with conn:
        inbox.enqueue_command(
            conn,
            key="sub_v3",
            at=backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id="E-002",
            payload={
                "experiment_id": "E-002",
                "protocol": {
                    "name": "v3剩余步骤",
                    "labware": {"LW-001": "shaker"},
                    "steps": [
                        {"skill": "pcr_open", "args": {}},
                        {"skill": "pcr_close", "args": {}},
                    ],
                },
            },
        )
    svc.step()
    p_v3 = runs.get_protocol(conn, "E-002-v3")
    assert p_v3 is not None
    with conn:
        inbox.enqueue_command(
            conn,
            key="app_v3",
            at=backend.now(),
            who="ui",
            kind="approve",
            target_id="E-002-v3",
            payload={
                "protocol_id": "E-002-v3",
                "decided_by": "王",
                "fingerprint": p_v3["fingerprint"],
            },
        )
    for _ in range(5):
        svc.step()
        cur_v3 = runs.get_protocol(conn, "E-002-v3")
        if cur_v3 is not None and cur_v3["status"] == "done":
            break
    with conn:
        inbox.enqueue_command(
            conn,
            key="fe_e2",
            at=backend.now(),
            who="operator",
            kind="finish_experiment",
            target_id="E-002",
            payload={"experiment_id": "E-002", "status": "completed", "notes": "v2失败后v3补完"},
        )
    svc.step()

    try:
        res = build_experiment_result(conn, "E-002")
        assert [p.id for p in res.protocols] == ["E-002-v1", "E-002-v2", "E-002-v3"]
        assert res.protocols[0].status == "rejected"
        assert res.protocols[0].steps == []

        assert res.protocols[1].status == "failed"
        assert [(s.n, s.skill, s.status) for s in res.protocols[1].steps] == [
            (1, "shake", "succeeded"),
            (2, "pcr_open", "failed"),
            (3, "pcr_close", "pending"),
        ]
        assert res.protocols[2].status == "done"
        assert [(s.n, s.skill, s.status) for s in res.protocols[2].steps] == [
            (1, "pcr_open", "succeeded"),
            (2, "pcr_close", "succeeded"),
        ]

        assert set(res.labware) == {"LW-001"}
        lw_hist = res.labware["LW-001"].history
        assert any("600" in item for item in lw_hist)
        assert not any("250" in item for item in lw_hist)
        assert not any("placed" in item for item in lw_hist)
    finally:
        conn.close()
