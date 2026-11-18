"""Tests for `models.py`, `tools.py`, `context_schema`, and `LAB.md` (#2, #23, #24)."""

from pathlib import Path
import subprocess
import sys

import pytest

from wetlab.agents.models import build_chat_model, has_model_credentials
from wetlab.agents.tools import (
    TurnContext,
    build_coordinator_tools,
    build_operator_tools,
    render_system_prompt,
)
from wetlab.agents.worker import AgentWorker
from wetlab.app.cli import main as cli_main
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, island, open_db

from .conftest import ScriptedChatModel, ai_tool_call, make_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_ENV_KEYS = (
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_TIMEOUT_S",
    "OPERATOR_LLM_MODEL",
    "OPERATOR_LLM_BASE_URL",
    "OPERATOR_LLM_API_KEY",
)


def test_coordinator_and_operator_tools_never_talk_to_hardware_and_list_closed_skills() -> None:
    """Step 9 row 2 + v0.6 #2: zero hardware tools exposed; `list_skills` marks `available`."""
    conn = open_db(":memory:")
    try:
        with conn:
            island.set_closed_skills(conn, {"pcr_start_program", "transfer"})

        coord_ctx = TurnContext(
            conn=conn,
            event_id=1,
            mission_id="M-20261118-001",
            sops_dir=REPO_ROOT / "sops",
        )
        op_ctx = TurnContext(
            conn=conn,
            event_id=2,
            mission_id="M-20261118-001",
            experiment_id="E-001",
            sops_dir=REPO_ROOT / "sops",
        )

        coord_tools = {t.name: t for t in build_coordinator_tools()}
        op_tools = {t.name: t for t in build_operator_tools()}

        assert set(coord_tools) == {
            "get_lab_state",
            "list_skills",
            "read_sop",
            "create_experiment",
            "ask_researcher",
            "finish_mission",
        }
        assert set(op_tools) == {
            "get_lab_state",
            "list_skills",
            "read_sop",
            "rehearse",
            "submit_protocol",
            "finish_experiment",
        }

        catalog = coord_tools["list_skills"].invoke({"runtime": make_runtime(coord_ctx)})
        by_name = {s["name"]: s for s in catalog["skills"]}
        assert by_name["pcr_open"]["available"] is True
        assert by_name["shake"]["available"] is True
        assert by_name["pcr_start_program"]["available"] is False
        assert by_name["transfer"]["available"] is False

        hardware_skill_names = set(by_name)
        assert hardware_skill_names.isdisjoint(coord_tools)
        assert hardware_skill_names.isdisjoint(op_tools)

        snap = coord_tools["get_lab_state"].invoke({"runtime": make_runtime(coord_ctx)})
        assert "devices" in snap and "gateway" in snap
        sop = op_tools["read_sop"].invoke(
            {"name": "pcr_37c_30s", "runtime": make_runtime(op_ctx)}
        )
        assert sop["ok"] is True
        assert sop["content"]["name"] == "37℃ 保持 30 秒"
    finally:
        conn.close()

    out = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import sys, wetlab.agents; print('wetlab.lab.mqtt' in sys.modules)",
        ],
        cwd=REPO_ROOT,
        text=True,
    ).strip()
    assert out == "False"


def test_models_operator_all_or_none_and_url_key_timeout_assertions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.6 #23: operator config must set all 3 or none; assert url, key, timeout."""
    for key in MODEL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    empty_env = tmp_path / "empty.env"
    empty_env.write_text("LLM_MODEL=\nLLM_API_KEY=\n", encoding="utf-8")
    assert has_model_credentials(empty_env) is False
    with pytest.raises(ValueError, match="LLM_MODEL"):
        build_chat_model("coordinator", env_file=empty_env)

    # Shared fallback when ALL 3 operator keys are unset; default timeout == 120.0
    shared_env = tmp_path / "shared.env"
    shared_env.write_text(
        "LLM_MODEL=openai:gpt-4o-mini\n"
        "LLM_BASE_URL=https://primary.example.invalid/v1\n"
        "LLM_API_KEY=sk-primary-shared\n",
        encoding="utf-8",
    )
    assert has_model_credentials(shared_env) is True
    m_coord = build_chat_model("coordinator", env_file=shared_env)
    m_op_shared = build_chat_model("operator", env_file=shared_env)
    for m in (m_coord, m_op_shared):
        assert getattr(m, "model_name", None) == "gpt-4o-mini"
        assert getattr(m, "openai_api_base", None) == "https://primary.example.invalid/v1"
        key_val = m.openai_api_key.get_secret_value()  # type: ignore[attr-defined]
        assert key_val == "sk-primary-shared"
        assert getattr(m, "request_timeout", None) == 120.0

    # Partial operator config (e.g. ONLY `OPERATOR_LLM_BASE_URL`, or 2 of 3) MUST fail at startup!
    for partial_extra in (
        "OPERATOR_LLM_BASE_URL=https://op-only.example.invalid/v1\n",
        "OPERATOR_LLM_MODEL=openai:gpt-4o-mini\nOPERATOR_LLM_API_KEY=sk-op\n",
    ):
        bad_env = tmp_path / "partial.env"
        bad_env.write_text(
            "LLM_MODEL=openai:gpt-4o\n"
            "LLM_BASE_URL=https://primary.example.invalid/v1\n"
            "LLM_API_KEY=sk-coord\n" + partial_extra,
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Operator LLM config"):
            build_chat_model("operator", env_file=bad_env)
        with pytest.raises(ValueError, match="Operator LLM config"):
            build_chat_model("coordinator", env_file=bad_env)
        with pytest.raises(ValueError, match="Operator LLM config"):
            AgentWorker(db_path=tmp_path / "bad.sqlite", env_file=bad_env)

    # All 3 operator keys set + custom `LLM_TIMEOUT_S=45`: zero cross-role leakage!
    split_env = tmp_path / "split.env"
    split_env.write_text(
        "LLM_MODEL=openai:gpt-4o\n"
        "LLM_BASE_URL=https://coord.example.invalid/v1\n"
        "LLM_API_KEY=sk-coord-secret\n"
        "LLM_TIMEOUT_S=45\n"
        "OPERATOR_LLM_MODEL=openai:gpt-4o-mini\n"
        "OPERATOR_LLM_BASE_URL=https://operator.example.invalid/v1\n"
        "OPERATOR_LLM_API_KEY=sk-operator-secret\n",
        encoding="utf-8",
    )
    c_model = build_chat_model("coordinator", env_file=split_env)
    o_model = build_chat_model("operator", env_file=split_env)
    assert getattr(c_model, "model_name", None) == "gpt-4o"
    assert getattr(c_model, "openai_api_base", None) == "https://coord.example.invalid/v1"
    c_key = c_model.openai_api_key.get_secret_value()  # type: ignore[attr-defined]
    assert c_key == "sk-coord-secret"
    assert getattr(c_model, "request_timeout", None) == 45.0

    assert getattr(o_model, "model_name", None) == "gpt-4o-mini"
    assert getattr(o_model, "openai_api_base", None) == "https://operator.example.invalid/v1"
    o_key = o_model.openai_api_key.get_secret_value()  # type: ignore[attr-defined]
    assert o_key == "sk-operator-secret"
    assert getattr(o_model, "request_timeout", None) == 45.0


def test_cwd_independent_lab_md_and_missing_lab_md_error_and_agent_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.6 #24: different CWD finds `REPO_ROOT/LAB.md`; missing `LAB.md` raises; agents reused."""
    outside_cwd = tmp_path / "elsewhere"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    rendered = render_system_prompt("coordinator.md")
    assert "热盖" in rendered
    assert "pcr_37c_30s.json" in rendered

    db_path = tmp_path / "runs" / "virtual" / "wetlab.sqlite"
    runs_dir = tmp_path / "runs" / "virtual"
    rc = cli_main(["--db", str(db_path), "--runs-dir", str(runs_dir), "agents", "--once"])
    assert rc == 0
    assert (runs_dir / "agents.sqlite").is_file()

    missing_lab = tmp_path / "missing_LAB.md"
    with pytest.raises(FileNotFoundError, match="LAB.md"):
        render_system_prompt("coordinator.md", lab_md_path=missing_lab)
    with pytest.raises(FileNotFoundError, match="LAB.md"):
        AgentWorker(db_path=db_path, runs_dir=runs_dir, lab_md_path=missing_lab)

    # Verify AgentWorker builds `_coordinator_agent` once and reuses it across turns
    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="m1",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-1", "title": "一", "input_text": "一"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="m2",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-2", "title": "二", "input_text": "二"},
        )
    svc.step()

    coord_model = ScriptedChatModel(
        [
            ai_tool_call(
                "finish_mission",
                {"conclusion": "完成一", "status": "completed"},
                call_id="f1",
            ),
            ai_tool_call(
                "finish_mission",
                {"conclusion": "完成二", "status": "completed"},
                call_id="f2",
            ),
        ]
    )
    worker = AgentWorker(db_path=db_path, runs_dir=runs_dir, coordinator_model=coord_model)
    try:
        assert worker.step_once() is True
        first_graph = worker._coordinator_agent
        assert first_graph is not None
        assert worker.step_once() is True
        assert worker._coordinator_agent is first_graph
        svc.step()
    finally:
        worker.close()
        svc.conn.close()


def test_get_lab_state_non_negative_age_and_staleness_on_virtual_epoch(tmp_path: Path) -> None:
    """`get_lab_state` uses `island_clock` so virtual `DEFAULT_EPOCH` produces `age_s >= 0`."""
    db_path = tmp_path / "wetlab.sqlite"
    svc = create_virtual_service(db_path, runs_dir=tmp_path / "runs")
    svc.startup()
    try:
        ctx = TurnContext(event_id=1, mission_id="M-1", db_path=db_path)
        tools = {t.name: t for t in build_coordinator_tools()}
        snap = tools["get_lab_state"].invoke({"runtime": make_runtime(ctx)})
        assert snap["gateway"]["age_s"] is not None and snap["gateway"]["age_s"] >= 0.0
        for info in snap["devices"].values():
            assert info["age_s"] is not None and info["age_s"] >= 0.0

        pcr_old = snap["devices"]["pcr"]
        svc.backend.sleep(25.0)
        svc.step()
        with svc.conn:
            island.upsert_device_state(
                svc.conn,
                device="pcr",
                props=pcr_old["props"],
                error=None,
                updated_at=float(pcr_old["updated_at"]),
            )
        snap2 = tools["get_lab_state"].invoke({"runtime": make_runtime(ctx)})
        assert snap2["devices"]["pcr"]["age_s"] >= 25.0
    finally:
        svc.conn.close()
