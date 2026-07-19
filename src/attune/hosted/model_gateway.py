"""Fixed-task OpenAI-compatible model boundary for hosted workers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

TASKS = frozenset({"classify", "converse", "embed"})
_CHAT_TASKS = frozenset({"classify", "converse"})
ROLES = frozenset({"system", "user", "assistant"})
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
MAX_MESSAGES = 8
MAX_MESSAGE_CHARS = 8_000
MAX_TOTAL_CHARS = 32_000
MAX_RESPONSE_CHARS = 16_000
MAX_GATEWAY_RESPONSE_BYTES = 100_000
MAX_EMBED_CHARS = 8_000
MAX_EMBED_DIMENSIONS = 4_096

# Per-tenant model profiles (docs/future-state.md Phase 6 "hosted
# operations"; per-tenant model configuration). A tenant selects among
# OPERATOR-DEFINED profile names from this fixed vocabulary -- a profile
# name never carries a base URL, API key, or model string of its own; the
# gateway's own configuration is the only place a profile name resolves to
# concrete model ids. Extending this vocabulary is a reviewed code change
# (and a paired migration for the CHECK constraint on
# ``attune.tenant_model_preferences``), never data.
STANDARD_PROFILE = "standard"
PROFILES = frozenset({"standard", "premium"})
PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_TOKEN_COUNT = 2_000_000


class CompletionClient(Protocol):
    @property
    def chat(self) -> Any: ...

    @property
    def embeddings(self) -> Any: ...


@dataclass(frozen=True)
class TokenUsage:
    """Content-free token counts as the upstream provider reported them."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ModelResult:
    text: str
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class EmbedResult:
    vector: tuple[float, ...]
    usage: TokenUsage | None = None


def _provider_usage(response: object) -> TokenUsage | None:
    """Best-effort, defensive extraction of usage from an untrusted
    OpenAI-compatible provider response. Never raises: a malformed or
    missing usage field must never break the model call it rode in on --
    it simply yields no usage, exactly like ``complete``/``embed``'s own
    tolerant handling of an unexpected provider shape elsewhere in this
    module reserves hard failure for the ACTUAL text/vector contract."""
    try:
        usage = response.usage  # type: ignore[attr-defined]
        input_tokens = usage.prompt_tokens
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
    except AttributeError:
        return None
    for value in (input_tokens, output_tokens):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= MAX_TOKEN_COUNT
        ):
            return None
    return TokenUsage(input_tokens, output_tokens)


class HostedModelGateway:
    def __init__(
        self,
        client: CompletionClient,
        *,
        models: Mapping[str, str],
        profiles: Mapping[str, Mapping[str, str]] | None = None,
    ):
        if set(models) != TASKS:
            raise ValueError("model routes must contain classify, converse, and embed")
        if any(
            not isinstance(model, str) or not MODEL_NAME.fullmatch(model)
            for model in models.values()
        ):
            raise ValueError("model route is invalid")
        self._client = client
        self._models = dict(models)
        # Dormant unless the operator configures a profile map at all
        # (ATTUNE_ENABLE_TENANT_MODEL_PROFILES) -- ``profiles=None`` is the
        # gate-off state, and ``_resolve`` below falls back to ``self._models``
        # byte-identically to the pre-profile fixed-config behavior in that
        # state, regardless of what a caller passes as ``profile``.
        self._profiles: dict[str, dict[str, str]] | None = None
        if profiles is not None:
            if STANDARD_PROFILE not in profiles:
                raise ValueError("model profiles must define the standard profile")
            parsed: dict[str, dict[str, str]] = {}
            for name, route in profiles.items():
                if not isinstance(name, str) or name not in PROFILES:
                    raise ValueError("model profile name is invalid")
                if set(route) != TASKS or any(
                    not isinstance(model, str) or not MODEL_NAME.fullmatch(model)
                    for model in route.values()
                ):
                    raise ValueError("model profile route is invalid")
                parsed[name] = dict(route)
            if parsed[STANDARD_PROFILE] != self._models:
                raise ValueError(
                    "the standard model profile must match the fixed model routes"
                )
            self._profiles = parsed

    def _resolve_model(self, task: str, profile: str | None) -> str:
        if profile is None or profile == STANDARD_PROFILE:
            return self._models[task]
        if self._profiles is None or profile not in self._profiles:
            raise ValueError("unknown model profile")
        return self._profiles[profile][task]

    def _validate_profile(self, profile: str | None) -> None:
        if profile is not None and (
            not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile)
        ):
            raise ValueError("model profile is invalid")

    def complete(
        self, *, task: str, messages: object, profile: str | None = None
    ) -> ModelResult:
        self._validate_profile(profile)
        normalized = validate_messages(task=task, messages=messages)
        model = self._resolve_model(task, profile)
        response = self._client.chat.completions.create(
            model=model,
            messages=normalized,
            max_tokens=256 if task == "classify" else 1_200,
        )
        try:
            choices = response.choices
            text = choices[0].message.content
        except (AttributeError, IndexError, TypeError) as error:
            raise RuntimeError("model response contract is invalid") from error
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_RESPONSE_CHARS:
            raise RuntimeError("model response contract is invalid")
        return ModelResult(text, _provider_usage(response))

    def embed(self, *, text: str, profile: str | None = None) -> EmbedResult:
        self._validate_profile(profile)
        normalized = validate_embed_input(text)
        model = self._resolve_model("embed", profile)
        response = self._client.embeddings.create(model=model, input=normalized)
        try:
            vector = response.data[0].embedding
        except (AttributeError, IndexError, TypeError) as error:
            raise RuntimeError("model response contract is invalid") from error
        if not isinstance(vector, list) or not 1 <= len(vector) <= MAX_EMBED_DIMENSIONS:
            raise RuntimeError("model response contract is invalid")
        values: list[float] = []
        for value in vector:
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise RuntimeError("model response contract is invalid")
            values.append(float(value))
        return EmbedResult(tuple(values), _provider_usage(response))


def validate_embed_input(text: object) -> str:
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_EMBED_CHARS:
        raise ValueError("embed input is invalid")
    return text


def validate_messages(*, task: str, messages: object) -> list[dict[str, str]]:
    if not isinstance(task, str) or task not in _CHAT_TASKS:
        raise ValueError("unsupported model task")
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


def make_openai_client(*, base_url: str, api_key: str):
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("model base URL must be fixed HTTPS")
    if not isinstance(api_key, str) or not 16 <= len(api_key) <= 8_192:
        raise ValueError("model API credential is invalid")
    import httpx
    from openai import OpenAI

    transport = httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(20.0, connect=5.0),
    )
    return OpenAI(api_key=api_key, base_url=base_url.rstrip("/"), http_client=transport)
