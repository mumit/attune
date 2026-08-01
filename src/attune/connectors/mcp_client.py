"""Synchronous facade over MCP Streamable HTTP, migrated to the 2026-07-28
specification (build prompt 34, task 1): the core protocol goes stateless.

Concretely, three assumptions this module held before are now wrong and are
removed here:

- No ``initialize``/``initialized`` handshake is performed before a tool call.
- No ``Mcp-Session-Id`` header (or any other session-affinity token) is sent
  or expected; a conformant 2026-07-28 server may sit behind plain
  round-robin with no sticky routing.
- Client identity travels in per-request ``_meta`` (see :func:`_client_meta`)
  on every ``tools/call``/``tools/list``, not in a one-time handshake payload.

See ``docs/mcp-contract.md`` v2.0 for the client/server compatibility matrix
this migration produces, and ``docs/decisions.md`` for the recorded decision.

``transport`` is an injected seam (``Callable[[str, dict], dict]``: one
Streamable HTTP request/response cycle, given a URL and a JSON-RPC-shaped
request dict, returning the parsed JSON-RPC-shaped response dict) so this
module's *request-building and envelope-parsing* logic — the part this
migration actually changes — is fully offline-testable against a fixture
server, without needing the optional real ``mcp`` package or a live
network call. Production omits ``transport`` and falls back to the real
SDK's Streamable HTTP client.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

# The Attune MCP CLIENT's own identity, sent as per-request ``_meta`` (2026-
# 07-28 spec) rather than a one-time ``initialize`` handshake payload.
# Distinct from ``connectors.mcp.MCP_CONTRACT_VERSION`` (the Gmail/Calendar
# TOOL contract version, currently "1") -- this is the TRANSPORT/client
# protocol version, tracked in ``docs/mcp-contract.md``'s compatibility
# matrix.
CLIENT_NAME = "attune"
CLIENT_PROTOCOL_VERSION = "2.0"

Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


class McpCapabilityError(RuntimeError):
    pass


def _client_meta() -> dict[str, Any]:
    """Per-request client identity (2026-07-28 spec section on stateless
    core): replaces the eliminated ``initialize`` handshake's ``clientInfo``.
    Attached to every ``tools/call``/``tools/list`` request under the
    reserved ``_meta`` key -- never inside ``arguments``, which stays
    exactly what the tool contract defines."""
    return {"attune/client": {"name": CLIENT_NAME, "version": CLIENT_PROTOCOL_VERSION}}


def _build_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """The request envelope this client sends. Deliberately carries no
    session id / session token field of any kind -- the eliminated
    ``Mcp-Session-Id`` had no successor field to replace it; a stateless
    server needs none."""
    return {"jsonrpc": "2.0", "method": method, "params": {**params, "_meta": _client_meta()}}


def _parse_tool_result(response: dict[str, Any]) -> dict[str, Any]:
    """Extract a tool call's structured result from a JSON-RPC-shaped
    response.

    Tolerant of extra legacy bookkeeping fields (e.g. a stateful server's
    ``result._session_id``/``result.protocolVersion``) alongside the
    result payload -- this client never reads or forwards them, so a
    fixture exercising the "old envelope" shape and one exercising the
    "new, stateless envelope" shape both parse identically here. That is
    the compatibility-window property ``docs/mcp-contract.md`` v2.0
    documents: this client works against either.
    """
    if "error" in response:
        raise McpCapabilityError(str(response["error"]))
    result = response.get("result", {})
    if result.get("isError"):
        raise RuntimeError("MCP tool call failed")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        text = item.get("text") if isinstance(item, dict) else None
        if text:
            try:
                decoded = json.loads(text)
            except ValueError:
                return {"text": text}
            return decoded if isinstance(decoded, dict) else {"result": decoded}
    return {}


def _parse_list_tools_result(response: dict[str, Any]) -> list[str]:
    if "error" in response:
        raise McpCapabilityError(str(response["error"]))
    result = response.get("result", {})
    return [tool["name"] for tool in result.get("tools", [])]


class StreamableHttpMcpCaller:
    def __init__(
        self,
        *,
        urls: dict[str, str],
        token: str | None = None,
        transport: "Transport | None" = None,
    ) -> None:
        self._urls = urls
        self._token = token
        self._transport = transport

    def __call__(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._call(server, tool, arguments))

    def list_tools(self, server: str) -> frozenset[str]:
        return frozenset(asyncio.run(self._list_tools(server)))

    def _url(self, server: str) -> str:
        try:
            return self._urls[server]
        except KeyError as exc:
            raise ValueError(f"no MCP URL configured for {server}") from exc

    async def _call(
        self, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        request = _build_request("tools/call", {"name": tool, "arguments": arguments})
        if self._transport is not None:
            return _parse_tool_result(self._transport(self._url(server), request))

        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise ImportError("MCP backend requires `pip install attune[mcp]`") from exc

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with streamable_http_client(
            self._url(server), headers=headers
        ) as transport:
            read, write = transport[0], transport[1]
            # No ``session.initialize()`` call: the 2026-07-28 core is
            # stateless, so a tool call is issued directly. Client identity
            # travels as per-request ``_meta`` (``_client_meta()``) instead
            # of the eliminated ``initialize`` handshake's ``clientInfo``.
            async with ClientSession(read, write) as session:
                result = await self._call_tool_with_meta(session, tool, arguments)
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {server}.{tool} failed")
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        for item in getattr(result, "content", []):
            text = getattr(item, "text", None)
            if text:
                try:
                    decoded = json.loads(text)
                except ValueError:
                    return {"text": text}
                return decoded if isinstance(decoded, dict) else {"result": decoded}
        return {}

    @staticmethod
    async def _call_tool_with_meta(session: Any, tool: str, arguments: dict[str, Any]) -> Any:
        """Best-effort per-request ``_meta`` on the real SDK: falls back to
        a plain call if the installed ``mcp`` package predates ``meta=``
        support on ``call_tool``, rather than hard-failing every real call
        on an SDK version mismatch."""
        try:
            return await session.call_tool(tool, arguments=arguments, meta=_client_meta())
        except TypeError:
            return await session.call_tool(tool, arguments=arguments)

    async def _list_tools(self, server: str) -> list[str]:
        request = _build_request("tools/list", {})
        if self._transport is not None:
            return _parse_list_tools_result(self._transport(self._url(server), request))

        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise ImportError("MCP backend requires `pip install attune[mcp]`") from exc
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with streamable_http_client(
            self._url(server), headers=headers
        ) as transport:
            # No ``session.initialize()`` here either -- same stateless
            # posture as ``_call`` above.
            async with ClientSession(transport[0], transport[1]) as session:
                result = await session.list_tools()
                return [tool.name for tool in result.tools]


def make_mcp_caller(settings) -> StreamableHttpMcpCaller:
    shared = settings.mcp_url
    urls = {
        "gmail": settings.mcp_gmail_url or shared,
        "calendar": settings.mcp_calendar_url or shared,
    }
    return StreamableHttpMcpCaller(
        urls={name: url for name, url in urls.items() if url},
        token=settings.mcp_token,
    )
