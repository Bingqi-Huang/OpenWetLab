"""Real-LLM run of 04 #41: the task names only the sample; the agents must find the plate themselves."""

from pathlib import Path

from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, runs

from .test_real_llm import ENV_FILE, _skip_without_llm

MISSION_ID = "M-20261118-041"
TASK = (
    "请把样品 X 做一次 PCR：98℃ 30 秒 →（98℃ 10 秒、60℃ 15 秒、72℃ 20 秒）× 5 → 72℃ 60 秒，"
    "孔内 25 µL，热盖 105℃。做完把板放回原来的位置。"
)
PLATES = [
    {"labware_id": "LW-001", "type": "96孔PCR板", "position": "rack:1-3",
     "contents": [{"name": "Y", "wells": ["A1"], "volume_ul": 25, "note": None}]},
    {"labware_id": "LW-003", "type": "96孔PCR板", "position": "rack:1-2",
     "contents": [{"name": "X", "wells": ["A1"], "volume_ul": 25, "note": None}]},
]


def _skill_steps(content: dict) -> list[tuple[str, dict]]:
    return [(s["skill"], s.get("args", {})) for s in content["steps"] if "skill" in s]


@_skip_without_llm
def test_real_llm_finds_plate_by_sample_name_and_plans_round_trip(tmp_path: Path) -> None:
    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path)
    svc.startup()
    with svc.conn:
        for plate in PLATES:
            inbox.enqueue_command(
                svc.conn, key=f"lw-{plate['labware_id']}", at=svc.backend.now(), who="ui",
                kind="place_labware", payload=plate,
            )
        inbox.enqueue_command(
            svc.conn, key="m", at=svc.backend.now(), who="ui", kind="new_mission",
            payload={"mission_id": MISSION_ID, "title": "样品 X 的 PCR", "input_text": TASK},
        )
    svc.step()
    worker = AgentWorker(db_path=tmp_path / "wetlab.sqlite", runs_dir=tmp_path, env_file=ENV_FILE)
    try:
        worker.run_until_idle()
        svc.step()
        worker.run_until_idle()
        svc.step()
        exps = runs.list_experiments(svc.conn, mission_id=MISSION_ID)
        protos = runs.list_protocols(svc.conn, experiment_id=exps[0]["id"]) if exps else []
        last = protos[-1] if protos else None
        steps = _skill_steps(last["content"]) if last else []
        transfers = [args for name, args in steps if name == "transfer"]
        print("\n[#41 real LLM] experiment labware:", exps[0]["task"].get("labware") if exps else None)
        print("[#41 real LLM] protocol status:", last and last["status"],
              "| rehearsal ok:", last and last["rehearsal"]["ok"])
        print("[#41 real LLM] skill steps:", [name for name, _ in steps])
        print("[#41 real LLM] transfers:", transfers)

        assert exps, "coordinator created no experiment"
        assert "LW-003" in exps[0]["task"].get("labware", {}), "task sheet does not name the plate holding X"
        assert last is not None and last["status"] == "submitted"
        assert last["rehearsal"]["ok"] is True
        assert {"labware_id": "LW-003", "to_position": "pcr"} in transfers
        assert {"labware_id": "LW-003", "to_position": "rack:1-2"} in transfers
        names = [name for name, _ in steps]
        assert names.index("pcr_open") < names.index("transfer")
        assert "pcr_start_program" in names and "pcr_confirm_finished" in names
    finally:
        worker.close()
        svc.conn.close()
