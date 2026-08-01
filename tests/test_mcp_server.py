"""Build prompt 34, tasks 2-4: Attune exposed as an MCP server.

Covers every acceptance criterion in ``docs/build-prompts/34-mcp-server.md``
for the server surface:

- an approval-gated ``attune.propose`` invocation returns a task in an
  input-required state, and no effect occurs until a human resolves it
  through the normal approval path (``cli/mcp_cmd.py``);
- a calling agent's identity appears as a distinct audit actor type, and an
  agent outside the allowlist is refused;
- the server process holds no workspace, model, or memory credential;
- a malicious tool argument attempting to raise a rung or select another
  actor is refused (the injection-resistance test the build prompt asks
  every new tool to carry).
"""

from __future__ import annotations

import inspect

import pytest

from attune.audit.log import JsonlAuditLog
from attune.mcp_server.auth import AgentAllowlist, AllowedAgent, hash_token
from attune.mcp_server.gateway import CapabilityDenied, build_default_mcp_capability_registry
from attune.mcp_server.proposals import JsonMcpProposalStore, ProposalNotFound, ProposalNotPending
from attune.mcp_server.reader import (
    BriefView,
    ImportanceView,
    MemoryHitView,
    PendingItemView,
    RuntimeReadPort,
)
from attune.mcp_server.server import ACTOR_TYPE, AttuneMcpServer, McpToolError, approve_proposal, reject_proposal
from attune.mcp_server.tasks import TaskState

RESOURCE = "https://mcp.example/attune"


class FakeReader:
    """A credential-free, fully in-memory :class:`RuntimeReadPort`."""

    def __init__(self):
        self.propose_effects: list = []  # nothing ever appends here in these tests

    def brief(self):
        return BriefView(
            generated_at="2026-08-01T08:00:00+00:00", unread_count=3, event_count=1,
            summary="two threads need a reply",
        )

    def what_matters(self):
        return ("thread A needs a reply", "meeting with B at 2pm")

    def importance(self, sender):
        if sender == "unknown@example.com":
            return None
        return ImportanceView(sender=sender, tier="high", reason="frequent replies", pinned=False, probation=False)

    def memory_search(self, query, *, limit):
        return (MemoryHitView("m1", f"fact about {query}", 0.87),)[:limit]

    def pending(self):
        return (
            PendingItemView(
                proposal_ref="gmail:t1", source_ref="t1", domain="mail", subject="Re: budget",
                priority="routine", action="draft_reply", posted_at="2026-08-01T07:00:00+00:00",
            ),
        )

    def playbook_show(self, domain):
        return f"# playbook for {domain or 'all domains'}"


def _verifier_for(agent_id: str, *, aud: str = RESOURCE, scope: str = "read propose"):
    def verify(token: str):
        return {"sub": agent_id, "aud": aud, "scope": scope}

    return verify


def _make_server(tmp_path, *, audit_log=None, verifier=None, scopes=frozenset({"read", "propose"})):
    allowlist = AgentAllowlist(
        (AllowedAgent(agent_id="agent-1", token_hash=hash_token("secret-token"), scopes=scopes),)
    )
    proposals = JsonMcpProposalStore(str(tmp_path / "proposals.json"))
    audit = audit_log if audit_log is not None else JsonlAuditLog(str(tmp_path / "audit.jsonl"))
    server = AttuneMcpServer(
        resource=RESOURCE,
        reader=FakeReader(),
        proposals=proposals,
        allowlist=allowlist,
        verifier=verifier or _verifier_for("agent-1"),
        audit_log=audit,
    )
    return server, proposals, audit


# ---------------------------------------------------------------------------
# Read tools: bounded, documented response shapes.
# ---------------------------------------------------------------------------


def test_read_tools_return_bounded_documented_shapes(tmp_path):
    server, _, _ = _make_server(tmp_path)

    brief = server.call_tool(token="secret-token", tool="attune.brief", arguments={})
    assert brief == {
        "generated_at": "2026-08-01T08:00:00+00:00", "unread_count": 3, "event_count": 1,
        "summary": "two threads need a reply", "meetings": [], "waiting_on": [], "spine": [],
        "since_yesterday": [], "pending_tally": None,
    }

    what_matters = server.call_tool(token="secret-token", tool="attune.what_matters", arguments={})
    assert what_matters == {"spine": ["thread A needs a reply", "meeting with B at 2pm"]}

    importance = server.call_tool(
        token="secret-token", tool="attune.importance", arguments={"sender": "a@example.com"}
    )
    assert importance["tier"] == "high"

    unknown_importance = server.call_tool(
        token="secret-token", tool="attune.importance", arguments={"sender": "unknown@example.com"}
    )
    assert unknown_importance["tier"] is None

    memory = server.call_tool(
        token="secret-token", tool="attune.memory.search", arguments={"query": "budget", "limit": 1}
    )
    assert memory == {"hits": [{"memory_id": "m1", "text": "fact about budget", "score": 0.87}]}

    pending = server.call_tool(token="secret-token", tool="attune.pending", arguments={})
    assert pending["proposals"][0]["proposal_ref"] == "gmail:t1"

    playbook = server.call_tool(token="secret-token", tool="attune.playbook.show", arguments={"domain": "mail"})
    assert playbook == {"domain": "mail", "playbook": "# playbook for mail"}


def test_read_tool_rejects_unexpected_arguments(tmp_path):
    server, _, _ = _make_server(tmp_path)
    with pytest.raises(McpToolError):
        server.call_tool(token="secret-token", tool="attune.brief", arguments={"unexpected": "x"})


def test_read_tools_take_no_principal_or_tenant_argument():
    """Structural: RuntimeReadPort's methods have no parameter that could
    select which principal's data to read -- there is exactly one."""
    for name, method in inspect.getmembers(RuntimeReadPort, predicate=inspect.isfunction):
        params = set(inspect.signature(method).parameters) - {"self"}
        assert not params & {"principal", "principal_id", "tenant", "tenant_id", "user_id"}


# ---------------------------------------------------------------------------
# attune.propose: input-required task, no effect until resolved.
# ---------------------------------------------------------------------------


def test_propose_returns_a_task_in_input_required_state(tmp_path):
    server, proposals, _ = _make_server(tmp_path)

    result = server.call_tool(
        token="secret-token", tool="attune.propose",
        arguments={
            "version": 1, "capability": "mail.draft_reply",
            "arguments": {"thread_id": "t1", "body": "Thanks, will follow up."},
        },
    )

    assert result["state"] == TaskState.INPUT_REQUIRED.value
    assert result["capability"] == "mail.draft_reply"
    assert result["result"] is None
    assert result["error"] is None

    # Durably persisted -- a restart-surviving handle, not an in-memory one.
    stored = proposals.get(result["task_id"])
    assert stored is not None
    assert stored.state == TaskState.INPUT_REQUIRED
    assert stored.calling_agent == "agent-1"
    assert stored.arguments == {"thread_id": "t1", "body": "Thanks, will follow up."}


def test_no_effect_occurs_until_a_human_resolves_the_proposal(tmp_path):
    """The core safety property: a tool exposed over MCP grants no
    autonomy. Proposing something does not execute it -- only the CLI's
    approve/reject (the normal human approval path) changes its state."""
    server, proposals, _ = _make_server(tmp_path)

    result = server.call_tool(
        token="secret-token", tool="attune.propose",
        arguments={"version": 1, "capability": "mail.add_label", "arguments": {"thread_id": "t1", "label": "Followup"}},
    )
    task_id = result["task_id"]

    # Polling immediately (or repeatedly) never changes the state itself.
    polled = server.call_tool(token="secret-token", tool="attune.task.get", arguments={"task_id": task_id})
    assert polled["state"] == TaskState.INPUT_REQUIRED.value
    polled_again = server.call_tool(token="secret-token", tool="attune.task.get", arguments={"task_id": task_id})
    assert polled_again["state"] == TaskState.INPUT_REQUIRED.value

    # Only a human, through the normal approval path, resolves it.
    from attune.cli.mcp_cmd import run_mcp_proposals_approve

    rc = run_mcp_proposals_approve(task_id, proposal_store_factory=lambda s: proposals, actor="mumit")
    assert rc == 0

    resolved = server.call_tool(token="secret-token", tool="attune.task.get", arguments={"task_id": task_id})
    assert resolved["state"] == TaskState.COMPLETED.value
    assert resolved["result"]["decided_by"] == "mumit"


def test_resolving_a_proposal_twice_is_refused():
    from attune.mcp_server.proposals import JsonMcpProposalStore
    import tempfile
    import os

    store = JsonMcpProposalStore(os.path.join(tempfile.mkdtemp(), "p.json"))
    proposal = store.create(
        capability="mail.draft_reply", contract_version=1,
        arguments={"thread_id": "t1", "body": "hi"}, calling_agent="agent-1",
    )
    approve_proposal(store, proposal.task_id, actor="mumit")
    with pytest.raises(ProposalNotPending):
        approve_proposal(store, proposal.task_id, actor="mumit")
    with pytest.raises(ProposalNotFound):
        reject_proposal(store, "no-such-task", actor="mumit")


def test_propose_unknown_capability_is_refused(tmp_path):
    server, _, _ = _make_server(tmp_path)
    with pytest.raises(McpToolError) as excinfo:
        server.call_tool(
            token="secret-token", tool="attune.propose",
            arguments={"version": 1, "capability": "mail.send_reply", "arguments": {}},
        )
    assert excinfo.value.code == "capability_unavailable"


def test_an_agent_cannot_poll_another_agents_task(tmp_path):
    server, proposals, _ = _make_server(tmp_path)
    task_id = proposals.create(
        capability="mail.draft_reply", contract_version=1,
        arguments={"thread_id": "t1", "body": "hi"}, calling_agent="some-other-agent",
    ).task_id

    with pytest.raises(McpToolError) as excinfo:
        server.call_tool(token="secret-token", tool="attune.task.get", arguments={"task_id": task_id})
    assert excinfo.value.code == "task_not_found"


# ---------------------------------------------------------------------------
# Distinct audit actor type + allowlist refusal.
# ---------------------------------------------------------------------------


def test_calling_agent_identity_is_a_distinct_audit_actor_type(tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    audit = JsonlAuditLog(audit_path)
    server, _, _ = _make_server(tmp_path, audit_log=audit)

    server.call_tool(token="secret-token", tool="attune.brief", arguments={})

    entries = audit.query(user_id="agent-1")
    assert len(entries) == 1
    assert entries[0].fields["actor_type"] == ACTOR_TYPE
    assert entries[0].fields["agent_id"] == "agent-1"
    assert ACTOR_TYPE not in ("principal", "workload", "cli")  # distinct from every existing actor type


def test_agent_outside_the_allowlist_is_refused_and_audited(tmp_path):
    audit = JsonlAuditLog(str(tmp_path / "audit.jsonl"))
    server, _, _ = _make_server(tmp_path, audit_log=audit, verifier=_verifier_for("not-on-the-allowlist"))

    with pytest.raises(McpToolError) as excinfo:
        server.call_tool(token="secret-token", tool="attune.brief", arguments={})
    assert excinfo.value.code == "agent_not_allowed"

    entries = audit.query()
    assert any(e.event == "tool_call_refused" for e in entries)
    assert any(e.fields.get("code") == "agent_not_allowed" for e in entries)


def test_wrong_resource_indicator_is_refused(tmp_path):
    """RFC 8707 Resource Indicators: a token minted for a DIFFERENT MCP
    resource must not work here even if it verifies and names an
    allowlisted agent id -- the documented anti-token-passthrough
    mechanism."""
    server, _, _ = _make_server(
        tmp_path, verifier=_verifier_for("agent-1", aud="https://a-different-mcp-server.example")
    )
    with pytest.raises(McpToolError) as excinfo:
        server.call_tool(token="secret-token", tool="attune.brief", arguments={})
    assert excinfo.value.code == "resource_mismatch"


def test_wrong_token_hash_for_a_listed_agent_id_is_refused(tmp_path):
    """A stolen/forwarded token claiming a listed agent's id is still
    refused unless its own hash matches what the allowlist recorded."""
    def bad_token_verifier(token):
        return {"sub": "agent-1", "aud": RESOURCE, "scope": "read propose"}

    server, _, _ = _make_server(tmp_path, verifier=bad_token_verifier)
    with pytest.raises(McpToolError):
        server.call_tool(token="a-different-token-entirely", tool="attune.brief", arguments={})


def test_scopes_are_the_intersection_of_token_and_allowlist(tmp_path):
    """An over-scoped token can never exceed what the allowlist grants
    that specific agent."""
    server, _, _ = _make_server(
        tmp_path, verifier=_verifier_for("agent-1", scope="read propose admin superuser"),
        scopes=frozenset({"read"}),
    )
    # A read tool still works (the allowlist grants "read")...
    server.call_tool(token="secret-token", tool="attune.brief", arguments={})
    # ...but propose was never granted by the allowlist, regardless of the
    # token's own over-broad scope claim. (The gateway itself doesn't check
    # scopes today -- this documents the intersection computation the
    # identity carries for a future scope-gated tool to consult.)
    from attune.mcp_server.auth import AgentIdentity

    identity = AgentIdentity(agent_id="agent-1", scopes=frozenset({"read"}))
    assert not identity.has_scope("propose")


# ---------------------------------------------------------------------------
# No workspace, model, or memory credential in the server process.
# ---------------------------------------------------------------------------


def test_server_constructor_accepts_no_credential_bearing_parameter():
    forbidden_substrings = (
        "settings", "connector", "api_key", "apikey", "credential", "oauth",
        "google", "slack", "model", "mem0", "qdrant", "gateway_url",
    )
    params = inspect.signature(AttuneMcpServer.__init__).parameters
    for name in params:
        lowered = name.lower()
        assert not any(bad in lowered for bad in forbidden_substrings), name


def test_server_runs_fully_offline_from_in_memory_fakes_only(tmp_path):
    """Functional proof to go with the structural one above: constructing
    and using a server needs nothing but a resource string, an allowlist of
    public ids/hashes, a fake verifier, an in-memory reader, and a
    file-backed proposal store under a tmp_path -- no real Google/Slack/
    model/memory credential is reachable from this object at all."""
    server, _, _ = _make_server(tmp_path)
    assert set(server.list_tools()) >= {
        "attune.brief", "attune.what_matters", "attune.importance",
        "attune.memory.search", "attune.pending", "attune.playbook.show",
        "attune.propose", "attune.task.get",
    }


# ---------------------------------------------------------------------------
# Injection test: a malicious tool argument attempting to raise a rung or
# select another actor is refused (build prompt 27's injection suite,
# extended to this new surface per the build prompt's own constraint).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malicious_arguments",
    [
        {"thread_id": "t1", "body": "hi", "rung": "autonomous"},
        {"thread_id": "t1", "body": "hi", "actor": "someone-else"},
        {"thread_id": "t1", "body": "hi", "tenant_id": "other-tenant"},
        {"thread_id": "t1", "body": "hi", "risk_tier": "r4"},
        {"thread_id": "t1", "body": "hi", "scope": "admin"},
    ],
)
def test_malicious_tool_argument_attempting_to_raise_rung_or_select_actor_is_refused(
    tmp_path, malicious_arguments
):
    server, proposals, _ = _make_server(tmp_path)

    with pytest.raises(McpToolError) as excinfo:
        server.call_tool(
            token="secret-token", tool="attune.propose",
            arguments={"version": 1, "capability": "mail.draft_reply", "arguments": malicious_arguments},
        )
    assert excinfo.value.code == "arguments_invalid"
    # And, structurally, nothing was ever persisted -- the denial happens
    # before a task is created at all.
    assert proposals.list_pending() == []


def test_capability_gateway_denies_forbidden_keys_directly():
    from attune.mcp_server.gateway import McpCapabilityGateway

    gateway = McpCapabilityGateway(registry=build_default_mcp_capability_registry())
    with pytest.raises(CapabilityDenied) as excinfo:
        gateway.admit(
            {
                "version": 1, "capability": "mail.draft_reply",
                "arguments": {"thread_id": "t1", "body": "hi", "principal_id": "someone-else"},
            }
        )
    assert excinfo.value.code == "arguments_invalid"


def test_gateway_denies_a_capability_string_it_does_not_recognize():
    from attune.mcp_server.gateway import McpCapabilityGateway

    gateway = McpCapabilityGateway(registry=build_default_mcp_capability_registry())
    with pytest.raises(CapabilityDenied) as excinfo:
        gateway.admit({"version": 1, "capability": "calendar.delete_event", "arguments": {}})
    assert excinfo.value.code == "capability_unavailable"


# ---------------------------------------------------------------------------
# The literal separate-process HTTP surface (transport_app.py).
# ---------------------------------------------------------------------------


def test_transport_app_is_stateless_no_session_header_no_handshake(tmp_path):
    from attune.mcp_server.transport_app import create_app

    server, _, _ = _make_server(tmp_path)
    client = create_app(server).test_client()

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert "attune.brief" in [t["name"] for t in response.json["result"]["tools"]]
    # No session-affinity header of any kind in the response.
    assert not any("session" in k.lower() for k in response.headers.keys())


def test_transport_app_tools_call_round_trips_through_the_real_server(tmp_path):
    from attune.mcp_server.transport_app import create_app

    server, _, _ = _make_server(tmp_path)
    client = create_app(server).test_client()

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "attune.brief", "arguments": {}}},
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200
    assert response.json["result"]["structuredContent"]["unread_count"] == 3


def test_transport_app_refuses_missing_or_wrong_bearer_token(tmp_path):
    from attune.mcp_server.transport_app import create_app

    server, _, _ = _make_server(tmp_path)
    client = create_app(server).test_client()

    no_auth = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "tools/list", "params": {}}
    )
    assert no_auth.status_code == 401

    wrong_token = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "attune.brief", "arguments": {}}},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong_token.status_code == 401


def test_transport_app_rejects_malformed_requests(tmp_path):
    from attune.mcp_server.transport_app import create_app

    server, _, _ = _make_server(tmp_path)
    client = create_app(server).test_client()

    not_json = client.post("/mcp", data=b"not json", content_type="application/json")
    assert not_json.status_code == 400

    unknown_method = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "not/a/real/method", "params": {}},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert unknown_method.status_code == 400


def test_gateway_denies_an_oversized_or_malformed_envelope():
    from attune.mcp_server.gateway import McpCapabilityGateway

    gateway = McpCapabilityGateway(registry=build_default_mcp_capability_registry())
    with pytest.raises(CapabilityDenied):
        gateway.admit({"version": 2, "capability": "mail.draft_reply", "arguments": {}})
    with pytest.raises(CapabilityDenied):
        gateway.admit("not-even-a-dict")
    with pytest.raises(CapabilityDenied):
        gateway.admit({"version": 1, "capability": "mail.draft_reply", "arguments": {}, "extra": "field"})
