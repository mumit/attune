"""Build prompt 34, task 1: the 2026-07-28 stateless-core migration.

A fake ``transport`` stands in for a real Streamable HTTP round trip (see
``connectors/mcp_client.py``'s module docstring) so both the "old envelope"
(a stateful server that still tacks on session bookkeeping) and "new
envelope" (a genuinely stateless server) fixture shapes are exercised fully
offline, and so we can assert exactly what was sent -- no handshake, no
session header/field, per-request ``_meta`` instead.
"""

from __future__ import annotations

from attune.connectors.mcp_client import (
    CLIENT_NAME,
    CLIENT_PROTOCOL_VERSION,
    StreamableHttpMcpCaller,
    _client_meta,
)


class RecordingTransport:
    """Records every request it receives and returns a scripted response."""

    def __init__(self, response: dict):
        self.calls: list[tuple[str, dict]] = []
        self._response = response

    def __call__(self, url: str, request: dict) -> dict:
        self.calls.append((url, request))
        return self._response


def _new_envelope(structured: dict) -> dict:
    """A genuinely stateless 2026-07-28 server's response: nothing but the
    tool result, no session bookkeeping anywhere."""
    return {"jsonrpc": "2.0", "result": {"structuredContent": structured}}


def _old_envelope(structured: dict) -> dict:
    """A legacy stateful server's response, still carrying session
    bookkeeping this client must never need or forward."""
    return {
        "jsonrpc": "2.0",
        "result": {
            "structuredContent": structured,
            "_session_id": "sess-legacy-123",
            "protocolVersion": "2025-06-18",
        },
    }


def test_call_tool_sends_no_handshake_and_no_session_field():
    transport = RecordingTransport(_new_envelope({"threads": []}))
    caller = StreamableHttpMcpCaller(urls={"gmail": "https://mcp.example/gmail"}, transport=transport)

    result = caller("gmail", "search_threads", {"query": "is:unread"})

    assert result == {"threads": []}
    # Exactly one round trip -- no preceding initialize/initialized call.
    assert len(transport.calls) == 1
    url, request = transport.calls[0]
    assert url == "https://mcp.example/gmail"
    assert request["method"] == "tools/call"
    # No session id / session token field anywhere in the request.
    flat = str(request)
    assert "session" not in flat.lower()
    # Client identity travels as per-request _meta instead.
    assert request["params"]["_meta"] == _client_meta()
    assert request["params"]["_meta"]["attune/client"]["name"] == CLIENT_NAME
    assert request["params"]["_meta"]["attune/client"]["version"] == CLIENT_PROTOCOL_VERSION


def test_list_tools_sends_no_handshake_and_no_session_field():
    transport = RecordingTransport(
        {"jsonrpc": "2.0", "result": {"tools": [{"name": "search_threads"}, {"name": "get_thread"}]}}
    )
    caller = StreamableHttpMcpCaller(urls={"gmail": "https://mcp.example/gmail"}, transport=transport)

    tools = caller.list_tools("gmail")

    assert tools == frozenset({"search_threads", "get_thread"})
    assert len(transport.calls) == 1
    _, request = transport.calls[0]
    assert request["method"] == "tools/list"
    assert "session" not in str(request).lower()


def test_functions_against_a_stateless_fixture_server_new_envelope():
    transport = RecordingTransport(_new_envelope({"draft_id": "d-1"}))
    caller = StreamableHttpMcpCaller(urls={"gmail": "https://mcp.example/gmail"}, transport=transport)

    result = caller("gmail", "create_draft", {"to": "a@example.com", "subject": "s", "body": "b"})

    assert result == {"draft_id": "d-1"}


def test_old_envelope_shape_still_parses_during_the_deprecation_window():
    """A legacy stateful server's response (extra session bookkeeping
    alongside the result) must parse identically to the new envelope --
    this client reads only the result payload it needs, in both cases."""
    transport = RecordingTransport(_old_envelope({"draft_id": "d-1"}))
    caller = StreamableHttpMcpCaller(urls={"gmail": "https://mcp.example/gmail"}, transport=transport)

    result = caller("gmail", "create_draft", {"to": "a@example.com", "subject": "s", "body": "b"})

    assert result == {"draft_id": "d-1"}


def test_content_text_fallback_still_works_for_both_envelope_shapes():
    for envelope in (
        {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": '{"draft_id": "d-2"}'}]}},
        {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": '{"draft_id": "d-2"}'}],
                "_session_id": "sess-legacy",
            },
        },
    ):
        transport = RecordingTransport(envelope)
        caller = StreamableHttpMcpCaller(urls={"gmail": "https://mcp.example/gmail"}, transport=transport)
        assert caller("gmail", "create_draft", {}) == {"draft_id": "d-2"}


def test_tool_error_result_raises():
    transport = RecordingTransport({"jsonrpc": "2.0", "result": {"isError": True}})
    caller = StreamableHttpMcpCaller(urls={"gmail": "https://mcp.example/gmail"}, transport=transport)

    try:
        caller("gmail", "search_threads", {})
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_no_url_configured_raises_value_error():
    caller = StreamableHttpMcpCaller(urls={}, transport=RecordingTransport(_new_envelope({})))
    try:
        caller("gmail", "search_threads", {})
        assert False, "expected ValueError"
    except ValueError:
        pass
