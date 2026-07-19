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


class CompletionClient(Protocol):
    @property
    def chat(self) -> Any: ...

    @property
    def embeddings(self) -> Any: ...


@dataclass(frozen=True)
class ModelResult:
    text: str


@dataclass(frozen=True)
class EmbedResult:
    vector: tuple[float, ...]


class HostedModelGateway:
    def __init__(self, client: CompletionClient, *, models: Mapping[str, str]):
        if set(models) != TASKS:
            raise ValueError("model routes must contain classify, converse, and embed")
        if any(
            not isinstance(model, str) or not MODEL_NAME.fullmatch(model)
            for model in models.values()
        ):
            raise ValueError("model route is invalid")
        self._client = client
        self._models = dict(models)

    def complete(self, *, task: str, messages: object) -> ModelResult:
        normalized = validate_messages(task=task, messages=messages)
        response = self._client.chat.completions.create(
            model=self._models[task],
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
        return ModelResult(text)

    def embed(self, *, text: str) -> EmbedResult:
        normalized = validate_embed_input(text)
        response = self._client.embeddings.create(
            model=self._models["embed"], input=normalized
        )
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
        return EmbedResult(tuple(values))


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
