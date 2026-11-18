"""Chat model factory configured from `.env` and environment variables (04 section 6.2)."""

import os
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

Role = Literal["coordinator", "operator"]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_LLM_TIMEOUT_S = 120.0

ALL_ENV_KEYS = (
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_TIMEOUT_S",
    "OPERATOR_LLM_MODEL",
    "OPERATOR_LLM_BASE_URL",
    "OPERATOR_LLM_API_KEY",
)
OPERATOR_ENV_KEYS = (
    "OPERATOR_LLM_MODEL",
    "OPERATOR_LLM_BASE_URL",
    "OPERATOR_LLM_API_KEY",
)


def _load_config(env_file: str | Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    path = Path(env_file)
    file_vals = dotenv_values(path) if path.is_file() else {}
    merged: dict[str, str] = {
        k: v for k, v in file_vals.items() if isinstance(v, str) and v != ""
    }
    for key in ALL_ENV_KEYS:
        env_val = os.environ.get(key)
        if env_val is not None and env_val != "":
            merged[key] = env_val
    return merged


def validate_model_config(
    env_file: str | Path = DEFAULT_ENV_FILE,
) -> tuple[dict[str, str], float]:
    """Validate operator all-or-none rule and timeout setting (`04` section 6.2)."""
    cfg = _load_config(env_file)
    present_op = [k for k in OPERATOR_ENV_KEYS if cfg.get(k)]
    if 0 < len(present_op) < len(OPERATOR_ENV_KEYS):
        missing = [k for k in OPERATOR_ENV_KEYS if not cfg.get(k)]
        raise ValueError(
            "Operator LLM config must set all 3 of "
            f"{OPERATOR_ENV_KEYS} or none; missing {missing}"
        )
    raw_timeout = cfg.get("LLM_TIMEOUT_S")
    timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_LLM_TIMEOUT_S
    if timeout_s <= 0:
        raise ValueError(f"LLM_TIMEOUT_S must be positive, got {timeout_s}")
    return cfg, timeout_s


def has_model_credentials(env_file: str | Path = DEFAULT_ENV_FILE) -> bool:
    """Return True iff a primary `LLM_MODEL` and `LLM_API_KEY` are configured."""
    cfg = _load_config(env_file)
    return bool(cfg.get("LLM_MODEL") and cfg.get("LLM_API_KEY"))


def build_chat_model(
    role: Role = "coordinator",
    *,
    env_file: str | Path = DEFAULT_ENV_FILE,
    timeout: float | None = None,
) -> BaseChatModel:
    """Construct the configured LangChain chat model for `coordinator` or `operator`."""
    cfg, cfg_timeout = validate_model_config(env_file)
    has_separate_operator = all(cfg.get(k) for k in OPERATOR_ENV_KEYS)
    if role == "operator" and has_separate_operator:
        model_spec = cfg["OPERATOR_LLM_MODEL"]
        base_url = cfg["OPERATOR_LLM_BASE_URL"]
        api_key = cfg["OPERATOR_LLM_API_KEY"]
    else:
        model_spec = cfg.get("LLM_MODEL") or ""
        base_url = cfg.get("LLM_BASE_URL") or ""
        api_key = cfg.get("LLM_API_KEY") or ""

    if not model_spec:
        raise ValueError(f"LLM_MODEL is not configured for role {role!r}")

    normalized = model_spec if ":" in model_spec else f"openai:{model_spec}"
    effective_timeout = timeout if timeout is not None else cfg_timeout
    kwargs: dict[str, Any] = {"timeout": effective_timeout}
    if base_url:
        kwargs["base_url"] = base_url
    if api_key:
        kwargs["api_key"] = api_key
    return init_chat_model(normalized, **kwargs)
