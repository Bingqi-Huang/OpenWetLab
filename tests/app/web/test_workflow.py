"""A researcher's whole path through the web UI against a real virtual-island service."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from wetlab.app.web import MAIN_PATH, MISSIONS_PAGE
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, records, runs

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def _html(at: AppTest) -> str:
    return "\n".join(h.value for h in at.html)


def _text(at: AppTest) -> str:
    return "\n".join([m.value for m in at.markdown] + [c.value for c in at.caption])


def test_full_researcher_workflow_on_virtual_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    monkeypatch.setenv("WETLAB_RUNS_DIR", str(runs_dir))
    svc = create_virtual_service(db_file, runs_dir=runs_dir)
    svc.startup()
    try:
        # 1. 实验岛页：顶部显示当前连接模式，放上孔板并登记；服务处理后页面给出回执
        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not at.exception
        assert "当前连接：虚拟实验岛（virtual）" in _html(at)
        assert "网关在线" in _html(at)
        assert at.text_input(key="place_id").value == "LW-001"
        at.selectbox(key="place_pos").set_value("pcr")
        at.button(key="place_submit").click().run()
        assert "正在交给实验岛服务处理：登记 LW-001 放在 PCR仪" in _html(at)
        svc.tick()
        at.run()
        assert not at.exception
        assert any("登记 LW-001 放在 PCR仪：已完成" in t.value for t in at.toast)
        lw_pcr = records.get_labware_at(svc.conn, "pcr")
        assert lw_pcr is not None
        assert lw_pcr["id"] == "LW-001"
        assert "96孔PCR板" in _html(at)

        # 2. 任务页：新建表单不再单独选 mode（由实验岛自身模式决定）；提交后自动选中新任务
        at.switch_page(str(MISSIONS_PAGE)).run()
        assert "当前连接：虚拟实验岛（virtual）" in _html(at)
        assert at.radio(key="mission").value == "new"
        at.button(key="nm_submit").click().run()
        assert any("请填写任务标题和实验要求" in t.value for t in at.toast)
        assert inbox.list_unhandled_commands(svc.conn) == []
        at.text_input(key="nm_title").set_value("37度恒温测试任务")
        at.text_area(key="nm_text").set_value("将 PCR 中的 LW-001 在 37℃ 保持 30 秒后开门。")
        at.button(key="nm_submit").click().run()
        svc.tick()
        at.run()
        mission = runs.list_missions(svc.conn)[0]
        mid = mission["id"]
        assert mission["mode"] == "virtual"
        assert at.radio(key="mission").value == mid

        # 地址栏 ?mission=… 直接打开指定的一项
        link = AppTest.from_file(str(MAIN_PATH), default_timeout=20)
        link.query_params["mission"] = "new"
        link.switch_page(str(MISSIONS_PAGE)).run()
        assert link.radio(key="mission").value == "new"

        # 3. 操作员提交方案后，默认打开“方案”；核对项全部勾选并填写姓名后才能批准
        sop = json.loads((SOPS_DIR / "pcr_37c_30s.json").read_text("utf-8"))
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="op:submit:1",
                at=svc.backend.now(),
                who="operator",
                kind="submit_protocol",
                payload={"mission_id": mid, "protocol": sop},
            )
        svc.tick()
        at.run()
        pid = runs.list_protocols(svc.conn, status="submitted")[0]["id"]
        assert at.segmented_control(key=f"view_{mid}").value == "方案"
        assert "预演通过，无事故" in _html(at)
        assert at.button(key=f"approve_{pid}").disabled is True
        at.checkbox(key=f"chk_{pid}_LW-001").check().run()
        at.checkbox(key=f"chk_{pid}_site").check().run()
        assert at.button(key=f"approve_{pid}").disabled is True
        at.text_input(key=f"approver_{pid}").set_value("李研究员").run()
        assert at.button(key=f"approve_{pid}").disabled is False
        at.button(key=f"approve_{pid}").click().run()
        svc.tick()
        approved = runs.get_protocol(svc.conn, pid)
        assert approved is not None
        assert approved["status"] == "running"
        assert approved["decided_by"] == "李研究员"

        # 4. 执行视图：写明停止限制；暂停 → 继续 → 跑完
        at.run()
        at.segmented_control(key=f"view_{mid}").set_value("执行").run()
        assert "物理急停按钮" in _text(at)
        at.button(key=f"pause_{pid}").click().run()
        svc.tick()
        paused = runs.get_protocol(svc.conn, pid)
        assert paused is not None
        assert paused["status"] == "paused"
        at.run()
        at.button(key=f"resume_{pid}").click().run()
        for _ in range(60):
            svc.step()
            cur = runs.get_protocol(svc.conn, pid)
            if cur is not None and cur["status"] == "done":
                break
        done_proto = runs.get_protocol(svc.conn, pid)
        assert done_proto is not None
        assert done_proto["status"] == "done"

        # 5. 对话视图：显示研究计划和实验助手的提问；研究人员在对话框里回答
        (runs_dir / mid).mkdir(parents=True, exist_ok=True)
        (runs_dir / mid / "plan.md").write_text("- [x] 完成 37℃ 30 秒验证\n", encoding="utf-8")
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="coord:ask:1",
                at=svc.backend.now(),
                who="coordinator",
                kind="ask",
                payload={"mission_id": mid, "question": "是否需要继续下一轮反应？"},
            )
        svc.tick()
        at.run()
        at.segmented_control(key=f"view_{mid}").set_value("对话").run()
        assert "是否需要继续下一轮反应" in _text(at)
        assert "完成 37℃ 30 秒验证" in _text(at)
        at.chat_input(key=f"chat_{mid}").set_value("不需要，请直接出报告。").run()
        svc.tick()
        assert inbox.list_events_after(svc.conn, 0)[-1]["payload"]["text"] == "不需要，请直接出报告。"

        # 6. 报告视图：结束任务后显示 report.md、批准记录和孔板经历
        exp_id = runs.list_experiments(svc.conn, mission_id=mid)[0]["id"]
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="op:fin_exp:1",
                at=svc.backend.now(),
                who="operator",
                kind="finish_experiment",
                payload={"experiment_id": exp_id, "status": "completed", "notes": "恒温执行无偏差"},
            )
            inbox.enqueue_command(
                svc.conn,
                key="coord:fin_mis:1",
                at=svc.backend.now(),
                who="coordinator",
                kind="finish_mission",
                payload={"mission_id": mid, "status": "completed", "conclusion": "验证顺利完成"},
            )
        svc.tick()
        at.run()
        at.segmented_control(key=f"view_{mid}").set_value("报告").run()
        assert not at.exception
        assert "验证顺利完成" in _text(at)
        assert "恒温执行无偏差" in _text(at)
        assert "李研究员" in _html(at)
        assert "人工放到 PCR仪" in _html(at)
    finally:
        svc.conn.close()
