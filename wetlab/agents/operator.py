"""Operator agent builder using `create_agent` and official middlewares (04 section 6.2)."""

from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import ValidationError

from .models import DEFAULT_ENV_FILE, build_chat_model
from .tools import (
    DEFAULT_LAB_MD_PATH,
    DEFAULT_SOPS_DIR,
    TurnContext,
    WetLabAgentState,
    build_operator_tools,
    render_system_prompt,
)


def _on_tool_error(exc: Exception, req: Any) -> str | None:
    if isinstance(exc, (ValidationError, ValueError)):
        return f"工具参数或调用错误 ({req.tool_call['name']}): {exc}"
    return None


def build_operator_agent(
    *,
    model: BaseChatModel | None = None,
    checkpointer: Any = None,
    lab_md_path: Path = DEFAULT_LAB_MD_PATH,
    sops_dir: Path = DEFAULT_SOPS_DIR,
    env_file: Path = DEFAULT_ENV_FILE,
    max_tool_calls: int = 20,
    max_model_calls: int = 25,
    summarize_trigger_tokens: int = 24000,
    summarize_keep_messages: int = 20,
) -> Any:
    """Create the compiled Operator LangGraph agent reusable across turns."""
    llm = model if model is not None else build_chat_model("operator", env_file=env_file)
    prompt = render_system_prompt(
        "operator.md",
        lab_md_path=lab_md_path,
        sops_dir=sops_dir,
    )
    middlewares = [
        ToolCallLimitMiddleware(run_limit=max_tool_calls),
        ModelCallLimitMiddleware(run_limit=max_model_calls),
        SummarizationMiddleware(
            model=llm,
            trigger=("tokens", summarize_trigger_tokens),
            keep=("messages", summarize_keep_messages),
        ),
        ToolErrorMiddleware(_on_tool_error),
    ]
    return create_agent(
        model=llm,
        tools=build_operator_tools(),
        system_prompt=prompt,
        middleware=middlewares,
        state_schema=WetLabAgentState,
        context_schema=TurnContext,
        checkpointer=checkpointer,
        name="operator",
    )
