"""Per-turn runtime context, agent state schema, and system prompt renderer (04 section 6)."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, NotRequired

from langchain.agents import AgentState

from ..lab import registry
from ..lab.devices import ISLAND, LABWARE_POSITIONS
from ..lab.snapshot import read_snapshot
from ..lab.store import inbox, open_db

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LAB_MD_PATH = REPO_ROOT / "LAB.md"
DEFAULT_SOPS_DIR = REPO_ROOT / "sops"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class WetLabAgentState(AgentState):
    """Agent state schema persisting event tracking alongside conversation messages."""

    last_event_id: NotRequired[int]
    reminded_event_id: NotRequired[int]


def island_clock(conn: sqlite3.Connection) -> float:
    """Return a clock >= both wall time and the database's latest recorded timestamp."""
    return max(time.time(), read_snapshot(conn)["last_timestamp"])


def render_system_prompt(
    template_filename: str,
    *,
    lab_md_path: Path = DEFAULT_LAB_MD_PATH,
    sops_dir: Path = DEFAULT_SOPS_DIR,
) -> str:
    """Render an Agent prompt template from live island, catalog, `LAB.md`, and `sops/`."""
    if not lab_md_path.is_file():
        raise FileNotFoundError(f"LAB.md not found: {lab_md_path}")
    template = (PROMPTS_DIR / template_filename).read_text(encoding="utf-8")

    island_lines: list[str] = []
    for dev in ISLAND:
        pos_items = [
            f"`{name}`（{pos.label}）"
            for name, pos in LABWARE_POSITIONS.items()
            if pos.device == dev.spec.NAME
        ]
        positions = ", ".join(pos_items) or "无放板位置"
        key_props = ", ".join(f"`{p}`" for p in dev.spec.KEY_PROPS)
        island_lines.append(
            f"- `{dev.spec.NAME}`：位置 [{positions}]；关键状态属性 [{key_props}]"
        )

    cat = registry.catalog()
    skill_lines: list[str] = [f"技能目录版本：`{cat['version']}`"]
    for item in cat["skills"]:
        props = item["parameters"].get("properties", {})
        params_json = json.dumps(props, ensure_ascii=False)
        req_json = json.dumps(item["parameters"].get("required", []), ensure_ascii=False)
        skill_lines.append(
            f"- `{item['name']}`（设备 `{item['device']}`，类型 `{item['kind']}`，"
            f"显示模板“{item['label']}”）：{item['description']} | "
            f"参数属性：`{params_json}`，必填：`{req_json}`"
        )

    sop_names = sorted(p.name for p in sops_dir.iterdir() if p.is_file())
    available_sops = ", ".join(f"`{n}`" for n in sop_names) if sop_names else "（暂无存档 SOP）"
    lab_notes = lab_md_path.read_text(encoding="utf-8").strip()
    example_file = sops_dir / "pcr_37c_30s.json"
    protocol_example = example_file.read_text(encoding="utf-8").strip()

    return template.format(
        island_overview="\n".join(island_lines),
        skills_catalog="\n".join(skill_lines),
        available_sops=available_sops,
        lab_notes=lab_notes,
        protocol_example=protocol_example,
    )


@dataclass
class TurnContext:
    """Per-turn runtime context injected via `create_agent(context_schema=TurnContext)`."""

    event_id: int
    mission_id: str
    thread_id: str = ""
    db_path: str | Path | None = None
    open_mode: str | None = None
    conn: Any = None
    experiment_id: str | None = None
    sops_dir: Path = DEFAULT_SOPS_DIR

    def __post_init__(self) -> None:
        if not self.thread_id:
            self.thread_id = self.experiment_id or self.mission_id

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a thread-local SQLite connection (`04` section 5.13)."""
        if self.db_path is not None and str(self.db_path) != ":memory:":
            thread_conn = open_db(self.db_path, mode=self.open_mode)
            try:
                yield thread_conn
            finally:
                thread_conn.close()
            return
        if self.conn is None:
            raise ValueError("TurnContext requires either `db_path` or `conn`")
        yield self.conn

    def turn_key(self) -> str:
        return f"turn:{self.thread_id}:ev{self.event_id}"

    def enqueue_terminal(
        self,
        *,
        who: str,
        kind: str,
        target_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Enqueue the single terminal command allowed in this turn (`04` section 6.1 & 6.4)."""
        key = self.turn_key()
        normalized = json.loads(json.dumps(payload, ensure_ascii=False))
        with self.connection() as c:
            with c:
                inserted = inbox.enqueue_command(
                    c,
                    key=key,
                    at=island_clock(c),
                    who=who,
                    kind=kind,
                    target_id=target_id,
                    payload=normalized,
                )
            if inserted:
                return {"ok": True, "command": kind, "key": key, "newly_enqueued": True}
            existing = inbox.get_command_by_key(c, key)
        assert existing is not None
        if existing["kind"] == kind and existing["payload"] == normalized:
            return {"ok": True, "command": kind, "key": key, "newly_enqueued": False}
        return {"ok": False, "error": f"这一轮已经发出过 {existing['kind']} 命令"}

    def enqueue_reply(self, *, mission_id: str, text: str) -> dict[str, Any]:
        """Enqueue a Researcher reply message idempotently (`reply:<thread_id>:ev<event_id>`)."""
        key = f"reply:{self.thread_id}:ev{self.event_id}"
        payload = {"mission_id": mission_id, "text": text}
        with self.connection() as c, c:
            inserted = inbox.enqueue_command(
                c,
                key=key,
                at=island_clock(c),
                who="coordinator",
                kind="reply",
                target_id=mission_id,
                payload=payload,
            )
        return {"ok": True, "command": "reply", "key": key, "newly_enqueued": inserted}
