"""The actual separate-process HTTP surface (build prompt 34, task 3):
Streamable HTTP, stateless, one POST endpoint, exactly the composition
shape `hosted/dispatch_broker_service.py`'s `create_app` already uses for
the hosted brokers.

This is what makes "a separate process from the runtime holding user
credentials" concrete rather than aspirational: this WSGI app can be run
under any server (gunicorn, Cloud Run, ...) as its own process, and the
only thing it holds is an :class:`~attune.mcp_server.server.AttuneMcpServer`
built from credential-free abstractions (see that module's own docstring).

No session, no handshake: every request is a single stateless
``tools/list`` or ``tools/call`` JSON-RPC-shaped POST, matching the
2026-07-28 spec migration in ``connectors/mcp_client.py`` — this server
never issues or expects an ``Mcp-Session-Id``.
"""

from __future__ import annotations

from typing import Any

from .server import AttuneMcpServer, McpToolError

MAX_REQUEST_BYTES = 32_768


def create_app(server: AttuneMcpServer):
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    def _token_from_header() -> str:
        header = request.headers.get("Authorization", "")
        if len(header) > 4096 or not header.startswith("Bearer "):
            return ""
        return header[7:]

    def _error(code: str, http_status: int):
        return jsonify({"jsonrpc": "2.0", "error": {"code": code}}), http_status

    @app.post("/mcp")
    def handle():
        body: Any = request.get_json(silent=True)
        if not isinstance(body, dict) or "method" not in body:
            return _error("invalid_request", 400)
        method = body["method"]
        params = body.get("params")
        if not isinstance(params, dict):
            params = {}
        request_id = body.get("id")

        token = _token_from_header()

        if method == "tools/list":
            try:
                tools = server.list_tools_authorized(token=token)
            except McpToolError as exc:
                return _error(exc.code, 401)
            return jsonify({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": t} for t in tools]}})

        if method == "tools/call":
            tool = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(tool, str) or not isinstance(arguments, dict):
                return _error("invalid_request", 400)
            try:
                result = server.call_tool(token=token, tool=tool, arguments=arguments)
            except McpToolError as exc:
                status = 401 if exc.code in ("token_invalid", "resource_mismatch", "agent_not_allowed") else 400
                return _error(exc.code, status)
            return jsonify(
                {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": result}}
            )

        return _error("unknown_method", 400)

    return app
