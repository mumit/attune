"""OpenAI-compatible client construction and semantic model routing."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

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


# ---------------------------------------------------------------------------
# Phase P9 (docs/plan-2026-h2.md, build prompt 35): the bounded chat-messages
# envelope hosted's model gateway always enforced, moved here as the
# canonical shared home -- following the `hosted/intelligence.py` pattern of
# core owning the shape. `hosted/model_gateway.py` imports these same
# constants and `validate_messages` instead of redefining them, so a bound
# changed once applies to both planes. `validate_messages` here checks only
# message shape and bounds; task-identity validation of an UNTRUSTED task
# string (hosted's own callers) stays in hosted's own wrapper, since local's
# `Task` is already a checked enum, never untrusted input.
# ---------------------------------------------------------------------------

MAX_MESSAGES = 8
MAX_MESSAGE_CHARS = 8_000
MAX_TOTAL_CHARS = 32_000
MAX_RESPONSE_CHARS = 16_000
ROLES = frozenset({"system", "user", "assistant"})


def validate_messages(messages: object) -> list[dict[str, str]]:
    """Bounded validation for a chat ``messages`` list: shape, a per-message
    character cap, a running total budget, and a required leading system
    boundary. Available for any local call site that opts in; existing call
    sites are not yet wired to it (docs/decisions.md, build prompt 35)."""
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("model messages are invalid")
    normalized: list[dict[str, str]] = []
    total = 0
    for item in messages:
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError("model message schema is invalid")
        role = item["role"]
        content = item["content"]
        if not isinstance(role, str) or role not in ROLES or not isinstance(content, str):
            raise ValueError("model message schema is invalid")
        if not 1 <= len(content) <= MAX_MESSAGE_CHARS:
            raise ValueError("model message content is invalid")
        total += len(content)
        if total > MAX_TOTAL_CHARS:
            raise ValueError("model message budget exceeded")
        normalized.append({"role": role, "content": content})
    if normalized[0]["role"] != "system":
        raise ValueError("model messages require a system boundary")
    return normalized


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


# ---------------------------------------------------------------------------
# Build prompt 28 (docs/plan-2026-h2.md P3): capability probing.
#
# ``CLAUDE.md``'s provider-neutrality boundary is read here as *graceful
# degradation*, not a lowest-common-denominator floor (docs/decisions.md):
# every feature below is used when the configured gateway declares support
# and falls back to exactly today's request shape when it does not.
# Capabilities are resolved from ``Settings`` (explicit configuration,
# defaulting to off) -- never sniffed from an untrusted provider response --
# mirroring ``connectors/base.py``'s existing ``supports_*()`` probes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCapabilities:
    """What the configured gateway is declared to support, plus the call
    hygiene knobs every production chat-completion call site now threads
    through :func:`call_kwargs`/:func:`call_with_retry`. Every field defaults
    to the gate-off value; at those defaults, a call's request shape and
    retry behavior are byte-identical to before this record existed."""

    supports_tools: bool = False
    supports_structured_output: bool = False
    supports_prompt_cache: bool = False
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    max_retries: int = 0
    retry_base_delay: float = 0.2


def resolve_capabilities(settings: Settings | None = None) -> ModelCapabilities:
    """Resolve :class:`ModelCapabilities` from configuration."""
    settings = settings or Settings.from_env()
    return ModelCapabilities(
        supports_tools=settings.model_supports_tools,
        supports_structured_output=settings.model_supports_structured_output,
        supports_prompt_cache=settings.model_supports_prompt_cache,
        max_tokens=settings.model_max_tokens,
        timeout_seconds=settings.model_timeout_seconds,
        max_retries=settings.model_max_retries,
    )


def call_kwargs(capabilities: ModelCapabilities) -> dict[str, Any]:
    """Extra ``chat.completions.create`` kwargs for the call-hygiene knobs
    (task 6) -- built conditionally, never a literal ``None``, so a request
    made with the gate-off defaults carries neither key at all."""
    kwargs: dict[str, Any] = {}
    if capabilities.max_tokens is not None:
        kwargs["max_tokens"] = capabilities.max_tokens
    if capabilities.timeout_seconds is not None:
        kwargs["timeout"] = capabilities.timeout_seconds
    return kwargs


def call_with_retry(
    fn: Callable[[], Any],
    *,
    capabilities: ModelCapabilities,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> Any:
    """Bounded retry with jittered exponential backoff around one model call.

    ``max_retries=0`` (the gate-off default) calls ``fn`` exactly once and
    re-raises immediately on failure -- identical to every call site's
    behavior before this existed. An explicit ``ATTUNE_MODEL_MAX_RETRIES``
    opt-in retries on any exception, since the target is an untrusted network
    call, not a specific error type.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception:  # noqa: BLE001 — retry on any transient call failure
            if attempt >= capabilities.max_retries:
                raise
            delay = capabilities.retry_base_delay * (2**attempt) + rand() * capabilities.retry_base_delay
            sleep(delay)
            attempt += 1


@dataclass(frozen=True)
class Usage:
    """Content-free token counts read from ``response.usage``, plus (when the
    provider reports it) the cached-prefix count that makes a prompt-cache
    hit observable (task 3/6: "Report cache hit/miss in the ledger")."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None = None

    @property
    def cache_hit(self) -> bool | None:
        """``None`` when the provider never reported a cached-token count
        (an ungated gateway, or one that doesn't support caching at all);
        otherwise whether any of the input was served from cache."""
        return None if self.cached_input_tokens is None else self.cached_input_tokens > 0


def read_usage(response: Any) -> Usage | None:
    """Best-effort, defensive extraction of usage from an untrusted
    OpenAI-compatible provider response -- mirrors
    ``hosted.model_gateway._provider_usage``'s tolerance exactly, since a
    malformed or missing usage field must never break the call it rode in
    on. ``cached_input_tokens`` reads OpenAI's own
    ``usage.prompt_tokens_details.cached_tokens`` shape when present."""
    try:
        usage = response.usage
        input_tokens = usage.prompt_tokens
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
    except AttributeError:
        return None
    for value in (input_tokens, output_tokens):
        if not isinstance(value, int) or isinstance(value, bool):
            return None
    cached: int | None = None
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        candidate = getattr(details, "cached_tokens", None)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            cached = candidate
    return Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached,
    )
