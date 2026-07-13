"""A thin OpenAI-compatible client that authenticates with a bearer token.

Why this package exists
-----------------------
Some enterprise LLM gateways expose an OpenAI-compatible ``/chat/completions``
surface but authenticate with ``Authorization: Bearer <token>`` where the token
is a long-lived, manually-rotated credential rather than an OpenAI-style API key.

The OpenAI Python SDK already sends ``Authorization: Bearer <api_key>`` under the
hood, so pointing it at such a gateway is *mostly* a matter of setting
``base_url`` and passing the token as ``api_key``. This package exists to add the
two things that raw approach lacks:

1. **Token sourcing** that treats the token as swappable config (constructor
   arg -> explicit env var -> fallback env var), never hard-coded, so rotation
   is a restart, not a code change.
2. **Loud, specific failure on 401** via :class:`TokenRejectedError`, so a
   rejected token surfaces as "rotate me" instead of vanishing into a retry loop.

This package is intentionally gateway-agnostic. It contains **no** Fuel iX (or
any other vendor) specifics: no base URLs, no model identifiers, no routing.
Those belong in the application that consumes this client. That separation is
what makes this reusable by anyone behind a bearer-token gateway.
"""

from __future__ import annotations

import os
from typing import Any

try:
    import openai
    from openai import OpenAI, AsyncOpenAI
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "bearer-openai requires the 'openai' package. Install with "
        "`pip install bearer-openai` (which pulls it in) or `pip install openai`."
    ) from exc

from .exceptions import TokenNotConfiguredError, TokenRejectedError

__all__ = ["BearerClient", "AsyncBearerClient", "resolve_token"]

_DEFAULT_ENV_VAR = "BEARER_OPENAI_TOKEN"


def resolve_token(
    token: str | None = None,
    *,
    env_var: str | None = None,
    fallback_env_var: str = _DEFAULT_ENV_VAR,
) -> str:
    """Resolve a bearer token from, in order: explicit arg, env_var, fallback env.

    Raises :class:`TokenNotConfiguredError` if none is found, so a
    misconfigured deployment fails at construction time with a clear message
    rather than on the first request.
    """
    if token:
        return token
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value
    value = os.environ.get(fallback_env_var)
    if value:
        return value

    tried = [v for v in (env_var, fallback_env_var) if v]
    raise TokenNotConfiguredError(
        "No bearer token provided. Pass token=... explicitly, or set one of "
        f"these environment variables: {', '.join(tried)}. "
        "The token should come from config or a secrets store, never hard-coded."
    )


def _is_401(exc: Exception) -> bool:
    """Best-effort detection of an auth failure across openai SDK versions."""
    if isinstance(exc, openai.AuthenticationError):
        return True
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 401:
        return True
    return False


class _BearerMixin:
    """Shared construction + 401-translation logic for sync/async clients."""

    _gateway_url: str

    @staticmethod
    def _build_kwargs(
        base_url: str,
        token: str | None,
        env_var: str | None,
        fallback_env_var: str,
        extra: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        resolved = resolve_token(
            token, env_var=env_var, fallback_env_var=fallback_env_var
        )
        kwargs: dict[str, Any] = {"base_url": base_url, "api_key": resolved}
        kwargs.update(extra)
        return resolved, kwargs


class BearerClient(_BearerMixin, OpenAI):
    """Synchronous OpenAI-compatible client authenticated with a bearer token.

    Usage (the caller supplies base_url + token; this package supplies neither):

        client = BearerClient(
            base_url="https://api.example-gateway.ai",
            env_var="MY_GATEWAY_TOKEN",   # optional; falls back to BEARER_OPENAI_TOKEN
        )
        resp = client.chat_completions_create(
            model="some-model-id",
            messages=[{"role": "user", "content": "hi"}],
        )

    Use ``chat_completions_create`` (or the async equivalent) rather than the
    raw ``.chat.completions.create`` when you want 401s translated into
    :class:`TokenRejectedError`. The raw SDK surface remains available for
    everything else.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        env_var: str | None = None,
        fallback_env_var: str = _DEFAULT_ENV_VAR,
        **openai_kwargs: Any,
    ) -> None:
        _, kwargs = self._build_kwargs(
            base_url, token, env_var, fallback_env_var, openai_kwargs
        )
        super().__init__(**kwargs)
        # OpenAI owns `_base_url` and stores an httpx.URL there. Overwriting it
        # with a string breaks request construction in current SDK releases.
        self._gateway_url = base_url

    def chat_completions_create(self, **kwargs: Any) -> Any:
        """``chat.completions.create`` with a loud, specific 401 translation."""
        try:
            return self.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised, not swallowed
            if _is_401(exc):
                raise TokenRejectedError(base_url=self._gateway_url) from exc
            raise


class AsyncBearerClient(_BearerMixin, AsyncOpenAI):
    """Asynchronous counterpart to :class:`BearerClient`."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        env_var: str | None = None,
        fallback_env_var: str = _DEFAULT_ENV_VAR,
        **openai_kwargs: Any,
    ) -> None:
        _, kwargs = self._build_kwargs(
            base_url, token, env_var, fallback_env_var, openai_kwargs
        )
        super().__init__(**kwargs)
        self._gateway_url = base_url

    async def chat_completions_create(self, **kwargs: Any) -> Any:
        """Async ``chat.completions.create`` with loud 401 translation."""
        try:
            return await self.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised, not swallowed
            if _is_401(exc):
                raise TokenRejectedError(base_url=self._gateway_url) from exc
            raise
