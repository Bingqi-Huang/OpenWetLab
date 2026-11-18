"""Shared scripted `GenericFakeChatModel` and `ToolRuntime` helpers for `tests/agents/`."""

from collections.abc import Callable
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import PrivateAttr

from wetlab.agents.tools import TurnContext


class ScriptedChatModel(GenericFakeChatModel):
    """Subclass of `GenericFakeChatModel` supporting `bind_tools` and recording seen prompts."""

    _seen_calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    _before_call: Callable[[int, list[BaseMessage]], None] | None = PrivateAttr(default=None)

    def __init__(
        self,
        responses: list[AIMessage | str],
        *,
        before_call: Callable[[int, list[BaseMessage]], None] | None = None,
    ) -> None:
        super().__init__(messages=iter(responses))
        self._seen_calls = []
        self._before_call = before_call

    @property
    def seen_calls(self) -> list[list[BaseMessage]]:
        return self._seen_calls

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> "ScriptedChatModel":
        _ = tools, tool_choice, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        idx = len(self._seen_calls)
        snapshot = list(messages)
        self._seen_calls.append(snapshot)
        if self._before_call is not None:
            self._before_call(idx, snapshot)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def ai_tool_call(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str = "tc_1",
    content: str = "",
) -> AIMessage:
    """Construct an `AIMessage` invoking a single tool."""
    return AIMessage(
        content=content,
        tool_calls=[{"id": call_id, "name": name, "args": args}],
    )


def ai_parallel_tool_calls(calls: list[tuple[str, dict[str, Any], str]]) -> AIMessage:
    """Construct an `AIMessage` invoking multiple tools in parallel in one message."""
    return AIMessage(
        content="",
        tool_calls=[{"id": cid, "name": name, "args": args} for name, args, cid in calls],
    )


def make_runtime(ctx: TurnContext, *, tool_call_id: str = "tc_unit") -> ToolRuntime[TurnContext]:
    """Construct a `ToolRuntime` wrapper for direct tool unit tests."""
    return ToolRuntime(
        state={"messages": []},
        context=ctx,
        config={"configurable": {"thread_id": ctx.thread_id}},
        stream_writer=lambda _: None,
        tool_call_id=tool_call_id,
        store=None,
    )
