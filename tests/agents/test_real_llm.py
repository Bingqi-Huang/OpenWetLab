"""Real-LLM virtual-island acceptance tests with tasks distinct from `sops/` (v0.6 #29 & #30b)."""

import json
from pathlib import Path
import socket
import urllib.parse

import pytest

from wetlab.agents.models import _load_config, has_model_credentials
from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import Service, create_virtual_service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, open_db, runs

from .real_llm_validators import (
    REPO_ROOT,
    assert_matches_shake_and_42c_hold,
    assert_matches_two_incubations_and_12cycle_pcr,
)

ENV_FILE = REPO_ROOT / ".env"

def _can_reach_real_llm(env_file: Path) -> bool:
    if not has_model_credentials(env_file):
        return False
    cfg = _load_config(env_file)
    raw_url = cfg.get("LLM_BASE_URL") or "https://api.openai.com/v1"
    parsed = urllib.parse.urlparse(raw_url)
    host = parsed.hostname or "api.openai.com"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


_skip_without_llm = pytest.mark.skipif(
    not _can_reach_real_llm(ENV_FILE),
    reason="Real LLM credentials missing in .env or LLM endpoint unreachable offline",
)


def test_stored_sops_cannot_pass_real_llm_validators() -> None:
    """v0.6 #30b: copying any file under `sops/` verbatim MUST fail both real-LLM validators."""
    for sop_path in (REPO_ROOT / "sops").glob("*.json"):
        sop_data = json.loads(sop_path.read_text(encoding="utf-8"))
        with pytest.raises(AssertionError):
            assert_matches_shake_and_42c_hold(sop_data)
        with pytest.raises(AssertionError):
            assert_matches_two_incubations_and_12cycle_pcr(sop_data)


@_skip_without_llm
@pytest.mark.parametrize("trial", range(5))
def test_real_llm_submits_valid_rehearsed_protocols_for_novel_tasks(
    tmp_path: Path,
    trial: int,
) -> None:
    """Step 9 row 13 + v0.6 #30b: 5 consecutive runs on 2 novel tasks distinct from `sops/`."""
    tasks = [
        (
            "振荡加 42℃ 低温恒温",
            "台面上振荡模组（shaker）已放好 LW-002，PCR 仪（pcr）已放好 LW-001（装有 40 µL 反应液）。"
            "请在同一份方案中先对 LW-002 以 500 rpm 振荡 20 秒，再关闭 PCR 仓门执行 42℃ 保持 45 秒"
            "（不开启热盖加热，体积 40 µL），等待结束后确认完成并打开仓门。",
            assert_matches_shake_and_42c_hold,
        ),
        (
            "双段高温温育加 12 循环扩增",
            "PCR 仪里已放好 LW-001（体积 30 µL），请在同一份方案中依次执行两段独立恒温"
            "（65℃ 120 秒、85℃ 90 秒，每段完成后打开再关闭仓门以便人工加样，体积 30 µL，热盖 105℃），"
            "随后执行扩增循环程序：95℃ 45 秒 ->（95℃ 15 秒、58℃ 20 秒、72℃ 30 秒）× 12 "
            "-> 72℃ 120 秒 -> 10℃ 180 秒（体积 30 µL，热盖 105℃），完成后确认并打开仓门。",
            assert_matches_two_incubations_and_12cycle_pcr,
        ),
    ]
    for idx, (title, req_text, validator) in enumerate(tasks):
        run_dir = tmp_path / f"run_{trial}_{idx}"
        svc = create_virtual_service(run_dir / "wetlab.sqlite", runs_dir=run_dir)
        svc.startup()
        mid = f"M-20261118-{trial}{idx:02d}"
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="lw1",
                at=svc.backend.now(),
                who="ui",
                kind="place_labware",
                payload={"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
            )
            inbox.enqueue_command(
                svc.conn,
                key="lw2",
                at=svc.backend.now(),
                who="ui",
                kind="place_labware",
                payload={"labware_id": "LW-002", "type": "96_plate", "position": "shaker"},
            )
            inbox.enqueue_command(
                svc.conn,
                key="m",
                at=svc.backend.now(),
                who="ui",
                kind="new_mission",
                payload={"mission_id": mid, "title": title, "input_text": req_text},
            )
        svc.step()
        worker = AgentWorker(db_path=run_dir / "wetlab.sqlite", runs_dir=run_dir, env_file=ENV_FILE)
        try:
            worker.run_until_idle()
            svc.step()
            worker.run_until_idle()
            svc.step()
            exps = runs.list_experiments(svc.conn, mission_id=mid)
            assert exps
            protos = runs.list_protocols(svc.conn, experiment_id=exps[0]["id"])
            assert protos and protos[-1]["status"] == "submitted"
            assert protos[-1]["rehearsal"]["ok"] is True
            validator(protos[-1]["content"])
        finally:
            worker.close()
            svc.conn.close()


@_skip_without_llm
def test_real_llm_incorporates_human_rejection_feedback(tmp_path: Path) -> None:
    """Step 9 row 14: revised protocol reflects human rejection comment."""
    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path / "runs")
    svc.startup()
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="lw",
            at=svc.backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="m",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={
                "mission_id": "M-20261118-999",
                "title": "振荡测试",
                "input_text": "请对振荡模组上的 LW-001 振荡 12 秒，转速 350 rpm。",
            },
        )
    svc.step()
    worker = AgentWorker(
        db_path=tmp_path / "wetlab.sqlite",
        runs_dir=tmp_path / "runs",
        env_file=ENV_FILE,
    )
    try:
        worker.run_until_idle()
        svc.step()
        worker.run_until_idle()
        svc.step()
        p1 = runs.list_protocols(svc.conn)[0]
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="rej",
                at=svc.backend.now(),
                who="ui",
                kind="reject",
                target_id=p1["id"],
                payload={
                    "protocol_id": p1["id"],
                    "decided_by": "研究员",
                    "comment": "请把振荡时间改为 25 秒，转速改为 600 rpm。",
                },
            )
        svc.step()
        worker.run_until_idle()
        svc.step()
        p2 = runs.list_protocols(svc.conn)[-1]
        assert p2["version"] == 2 and p2["status"] == "submitted"
        shake_steps = [s for s in p2["content"]["steps"] if s.get("skill") == "shake"]
        assert shake_steps and shake_steps[0]["args"] == {"seconds": 25, "rpm": 600}
    finally:
        worker.close()
        svc.conn.close()


@_skip_without_llm
def test_real_llm_submits_only_remaining_steps_after_mid_protocol_failure(
    tmp_path: Path,
) -> None:
    """v0.6 #29 & #30b: after step 1 (`shake`) succeeds and step 2 (`pcr_open`) fails,
    real-LLM Operator submits a revised protocol containing ONLY remaining steps (`shake` omitted).
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    conn = open_db(db_path)
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "shaker"},
            faults=[
                Fault(device="pcr", action="OpenDoor", nth=1, kind="fail", message="仓门瞬时卡阻")
            ],
        )
    )
    svc = Service(backend, conn, runs_dir=runs_dir)
    svc.startup()

    with conn:
        inbox.enqueue_command(
            conn,
            key="lw",
            at=backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
        )
        inbox.enqueue_command(
            conn,
            key="m",
            at=backend.now(),
            who="ui",
            kind="new_mission",
            payload={
                "mission_id": "M-REM",
                "title": "失败续做测试",
                "input_text": "请对振荡模组上的 LW-001 以 450 rpm 振荡 15 秒，然后打开 PCR 仓门再关闭 PCR 仓门。",
            },
        )
    svc.step()

    worker = AgentWorker(db_path=db_path, runs_dir=runs_dir, env_file=ENV_FILE)
    try:
        worker.run_until_idle()
        svc.step()
        worker.run_until_idle()
        svc.step()

        p1 = runs.list_protocols(conn)[0]
        assert p1["status"] == "submitted"
        with conn:
            inbox.enqueue_command(
                conn,
                key="app1",
                at=backend.now(),
                who="ui",
                kind="approve",
                target_id=p1["id"],
                payload={
                    "protocol_id": p1["id"],
                    "decided_by": "研究员",
                    "fingerprint": p1["fingerprint"],
                },
            )
        for _ in range(8):
            svc.step()
            cur_p1 = runs.get_protocol(conn, p1["id"])
            if cur_p1 is not None and cur_p1["status"] == "failed":
                break
        assert runs.get_protocol(conn, p1["id"])["status"] == "failed"  # type: ignore[index]

        worker.run_until_idle()
        svc.step()
        p2 = runs.list_protocols(conn)[-1]
        assert p2["version"] == 2 and p2["status"] == "submitted"
        assert p2["rehearsal"]["ok"] is True
        v2_skills = [s.get("skill") for s in p2["content"]["steps"]]
        assert "shake" not in v2_skills
        assert "pcr_open" in v2_skills and "pcr_close" in v2_skills
    finally:
        worker.close()
        conn.close()
