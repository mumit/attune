"""Synchronous facade over the official MCP Streamable HTTP client."""

from __future__ import annotations

import asyncio
import json
from typing import Any


class McpCapabilityError(RuntimeError):
    pass


class StreamableHttpMcpCaller:
    def __init__(
        self,
        *,
        urls: dict[str, str],
        token: str | None = None,
    ) -> None:
        self._urls = urls
        self._token = token

    def __call__(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._call(server, tool, arguments))

    def list_tools(self, server: str) -> frozenset[str]:
        return frozenset(asyncio.run(self._list_tools(server)))

    def _url(self, server: str) -> str:
        try:
            return self._urls[server]
        except KeyError as exc:
            raise ValueError(f"no MCP URL configured for {server}") from exc

    async def _session(self, server: str):  # pragma: no cover - helper shape only
        raise NotImplementedError

    async def _call(
        self, server: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
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
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments=arguments)
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

    async def _list_tools(self, server: str) -> list[str]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise ImportError("MCP backend requires `pip install attune[mcp]`") from exc
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with streamable_http_client(
            self._url(server), headers=headers
        ) as transport:
            async with ClientSession(transport[0], transport[1]) as session:
                await session.initialize()
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
