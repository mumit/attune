"""OpenAI-compatible client construction and semantic model routing."""

from __future__ import annotations

from enum import Enum
from typing import Any

from .config import Settings


class LlmConfigurationError(ValueError):
    pass


class Task(str, Enum):
    CLASSIFY = "classify"
    DRAFT = "draft"
    REASON = "reason"
    CONSOLIDATE = "consolidate"
    CONVERSE = "converse"
    MEMORY_EXTRACT = "memory_extract"


def model_for(task: Task, settings: Settings | None = None) -> str:
    settings = settings or Settings.from_env()
    value = getattr(settings, f"model_{task.value}")
    if not value:
        raise LlmConfigurationError(
            f"No model configured for {task.value}; set ATTUNE_MODEL_{task.value.upper()} or ATTUNE_MODEL_DEFAULT"
        )
    return value


def make_client(*, settings: Settings | None = None, api_key: str | None = None, **kwargs: Any):
    settings = settings or Settings.from_env()
    resolved_key = api_key or settings.llm_api_key
    if not resolved_key:
        raise LlmConfigurationError("ATTUNE_LLM_API_KEY is not configured")
    from openai import OpenAI

    return OpenAI(base_url=settings.llm_base_url, api_key=resolved_key, **kwargs)


def create_chat_completion(client: Any, **kwargs: Any) -> Any:
    """Call the standard OpenAI SDK surface.

    The fallback supports injected pre-migration fakes only; production clients
    always use ``client.chat.completions.create``.
    """
    chat = getattr(client, "chat", None)
    if chat is not None and getattr(chat, "completions", None) is not None:
        return chat.completions.create(**kwargs)
    legacy = getattr(client, "chat_completions_create", None)
    if legacy is not None:
        return legacy(**kwargs)
    raise TypeError("client does not implement the OpenAI Chat Completions surface")
