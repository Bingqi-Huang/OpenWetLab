"""Event-driven Agent worker loop with crash-safe checkpoint recovery (04 section 6.4)."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from ..lab.store import inbox, open_db
from .checkpoint import (
    bind_agents_db_identity,
    enqueue_agent_error,
    enqueue_autofail,
    export_plan_md,
    extract_turn_reply,
    resolve_worker_paths,
    turn_wrote_required,
)
from .context import DEFAULT_LAB_MD_PATH, DEFAULT_SOPS_DIR, TurnContext
from .coordinator import build_coordinator_agent
from .handoff import WakeTarget, classify_event
from .messages import event_to_message
from .models import DEFAULT_ENV_FILE, validate_model_config
from .operator import build_operator_agent

CURSOR_NAME = "agents"
AUTOFAIL_REASON = "Agent 没有给出结果"
MODEL_ERROR_REASON = "模型接口出错"
INITIAL_BACKOFF_S = 5.0
MAX_BACKOFF_S = 300.0
MAX_RETRY_WINDOW_S = 3600.0


class AgentWorker:
    """Single-threaded event consumer waking Coordinator and Operator turns one by one."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        mode: str | None = None,
        agents_db_path: str | Path | None = None,
        runs_dir: str | Path | None = None,
        sops_dir: str | Path = DEFAULT_SOPS_DIR,
        lab_md_path: str | Path = DEFAULT_LAB_MD_PATH,
        env_file: str | Path = DEFAULT_ENV_FILE,
        coordinator_model: BaseChatModel | Callable[[], BaseChatModel] | None = None,
        operator_model: BaseChatModel | Callable[[], BaseChatModel] | None = None,
        now_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        summarize_trigger_tokens: int = 24000,
        summarize_keep_messages: int = 20,
    ) -> None:
        resolved_db, resolved_runs, open_mode = resolve_worker_paths(db_path, mode, runs_dir)
        self.db_path = resolved_db
        self.runs_dir = resolved_runs
        self._open_mode = open_mode
        self.sops_dir = Path(sops_dir)
        self.lab_md_path = Path(lab_md_path)
        self.env_file = Path(env_file)

        if not self.lab_md_path.is_file():
            raise FileNotFoundError(f"LAB.md not found: {self.lab_md_path}")
        if coordinator_model is None or operator_model is None:
            validate_model_config(self.env_file)

        self._coordinator_model = coordinator_model
        self._operator_model = operator_model
        self._coordinator_agent: Any | None = None
        self._operator_agent: Any | None = None
        self._now = now_fn
        self._sleep = sleep_fn
        self._summarize_trigger_tokens = summarize_trigger_tokens
        self._summarize_keep_messages = summarize_keep_messages

        ckpt_path = (
            Path(agents_db_path)
            if agents_db_path is not None
            else (self.runs_dir / "agents.sqlite")
        )
        if str(ckpt_path) != ":memory:":
            ckpt_path.parent.mkdir(parents=True, exist_ok=True)
        self._ckpt_conn = sqlite3.connect(str(ckpt_path), check_same_thread=False)
        try:
            self.saver = SqliteSaver(self._ckpt_conn)
            self.saver.setup()
            bind_agents_db_identity(self._ckpt_conn, self.db_path, self._open_mode)
        except Exception:
            self._ckpt_conn.close()
            raise

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = open_db(self.db_path, mode=self._open_mode)
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        self._ckpt_conn.close()

    def _resolve_model(self, role: str) -> BaseChatModel | None:
        src = self._coordinator_model if role == "coordinator" else self._operator_model
        if callable(src) and not isinstance(src, BaseChatModel):
            return src()
        return src

    def _get_agent(self, role: str) -> Any:
        if role == "coordinator":
            if self._coordinator_agent is None:
                self._coordinator_agent = build_coordinator_agent(
                    model=self._resolve_model("coordinator"),
                    checkpointer=self.saver,
                    lab_md_path=self.lab_md_path,
                    sops_dir=self.sops_dir,
                    env_file=self.env_file,
                    summarize_trigger_tokens=self._summarize_trigger_tokens,
                    summarize_keep_messages=self._summarize_keep_messages,
                )
            return self._coordinator_agent
        if self._operator_agent is None:
            self._operator_agent = build_operator_agent(
                model=self._resolve_model("operator"),
                checkpointer=self.saver,
                lab_md_path=self.lab_md_path,
                sops_dir=self.sops_dir,
                env_file=self.env_file,
                summarize_trigger_tokens=self._summarize_trigger_tokens,
                summarize_keep_messages=self._summarize_keep_messages,
            )
        return self._operator_agent

    def _wrote_required(self, target: WakeTarget, event_id: int) -> bool:
        with self._db() as c:
            return turn_wrote_required(c, target, event_id)

    def _invoke_with_backoff(
        self,
        graph: Any,
        initial_input: dict[str, Any] | None,
        cfg: dict[str, Any],
        *,
        ctx: TurnContext,
        target: WakeTarget,
    ) -> bool:
        payload: dict[str, Any] | None = initial_input
        first_fail_at: float | None = None
        delay = INITIAL_BACKOFF_S
        while True:
            try:
                graph.invoke(payload, cfg, context=ctx, durability="sync")
                return True
            except Exception as exc:
                now = self._now()
                if first_fail_at is None:
                    first_fail_at = now
                    err_text = str(exc).strip() or type(exc).__name__
                    with self._db() as c:
                        enqueue_agent_error(c, target, ctx.event_id, err_text)
                elapsed = now - first_fail_at
                if elapsed >= MAX_RETRY_WINDOW_S:
                    with self._db() as c:
                        enqueue_autofail(c, target, ctx.event_id, MODEL_ERROR_REASON)
                    return False
                self._sleep(min(delay, MAX_RETRY_WINDOW_S - elapsed))
                delay = min(delay * 2.0, MAX_BACKOFF_S)
                st = graph.get_state(cfg)
                if st and st.next:
                    payload = None

    def _run_turn(self, event: dict[str, Any], target: WakeTarget) -> None:
        event_id = int(event["id"])
        ctx = TurnContext(
            event_id=event_id,
            mission_id=target.mission_id,
            thread_id=target.thread_id,
            db_path=self.db_path,
            open_mode=self._open_mode,
            experiment_id=target.experiment_id,
            sops_dir=self.sops_dir,
        )
        graph = self._get_agent(target.role)
        cfg = {"configurable": {"thread_id": target.thread_id}}
        msg_id = f"event-{event_id}"

        st = graph.get_state(cfg)
        vals = st.values if (st and st.values) else {}
        last_ev = int(vals.get("last_event_id") or 0)
        if last_ev < event_id:
            with self._db() as c:
                prompt_text = event_to_message(c, event)
            user_msg = HumanMessage(content=prompt_text, id=msg_id)
            inp = {"messages": [user_msg], "last_event_id": event_id}
            if not self._invoke_with_backoff(graph, inp, cfg, ctx=ctx, target=target):
                return
        elif st and st.next:
            if not self._invoke_with_backoff(graph, None, cfg, ctx=ctx, target=target):
                return

        if target.required_commands is None:
            st_after = graph.get_state(cfg)
            after_vals = st_after.values if (st_after and st_after.values) else {}
            msgs_after = list(after_vals.get("messages", []))
            reply_text = extract_turn_reply(msgs_after, msg_id)
            if reply_text:
                ctx.enqueue_reply(mission_id=target.mission_id, text=reply_text)
        elif not self._wrote_required(target, event_id):
            st_mid = graph.get_state(cfg)
            vals_mid = st_mid.values if (st_mid and st_mid.values) else {}
            reminded_ev = int(vals_mid.get("reminded_event_id") or 0)
            if reminded_ev < event_id:
                allowed = "、".join(f"`{c}`" for c in sorted(target.required_commands))
                reminder = (
                    f"你上一轮结束时没有调用规定的结束工具。本轮必须调用 {allowed} 之一结束，请立即调用。"
                )
                rem_msg = HumanMessage(content=reminder, id=f"reminder-{event_id}")
                rem_inp = {"messages": [rem_msg], "reminded_event_id": event_id}
                if not self._invoke_with_backoff(graph, rem_inp, cfg, ctx=ctx, target=target):
                    return
            elif st_mid and st_mid.next:
                if not self._invoke_with_backoff(graph, None, cfg, ctx=ctx, target=target):
                    return
            if not self._wrote_required(target, event_id):
                with self._db() as c:
                    enqueue_autofail(c, target, event_id, AUTOFAIL_REASON)

        if target.role == "coordinator" and target.mission_id:
            st_final = graph.get_state(cfg)
            final_vals = st_final.values if (st_final and st_final.values) else {}
            todos = list(final_vals.get("todos", []))
            export_plan_md(target.mission_id, todos, self.runs_dir)

    def step_once(self) -> bool:
        """Process at most one unread event after the `agents` cursor."""
        with self._db() as c:
            cursor = inbox.get_cursor(c, CURSOR_NAME)
            events = inbox.list_events_after(c, cursor, limit=1)
            if not events:
                return False
            event = events[0]
            event_id = int(event["id"])
            target = classify_event(c, event)
            if target is None:
                with c:
                    inbox.set_cursor(c, CURSOR_NAME, event_id)
                return True

        self._run_turn(event, target)
        with self._db() as c, c:
            inbox.set_cursor(c, CURSOR_NAME, event_id)
        return True

    def run_until_idle(self) -> int:
        """Process all currently pending events in order and return the count processed."""
        processed = 0
        while self.step_once():
            processed += 1
        return processed

    def serve_forever(
        self,
        *,
        poll_interval_s: float = 0.5,
        stop_event: Any | None = None,
    ) -> None:
        """Continuously poll SQLite `events` and run Agent turns until `stop_event` is set."""
        while stop_event is None or not stop_event.is_set():
            if not self.step_once():
                self._sleep(poll_interval_s)


def run_worker(
    *,
    mode: str | None = None,
    db_path: str | Path | None = None,
    runs_dir: str | Path | None = None,
    once: bool = False,
    poll_interval_s: float = 0.5,
) -> int:
    """Entry point invoked by `wetlab agents`."""
    worker = AgentWorker(db_path=db_path, mode=mode, runs_dir=runs_dir)
    try:
        if once:
            return worker.run_until_idle()
        worker.serve_forever(poll_interval_s=poll_interval_s)
        return 0
    finally:
        worker.close()
