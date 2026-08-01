"""``AttuneMcpServer`` (build prompt 34, task 3): the tool dispatcher every
MCP transport-facing entrypoint (:mod:`attune.mcp_server.transport_app`)
delegates to.

Holds no workspace, model, or memory credential. Its constructor accepts
only credential-free abstractions -- a resource identifier (a string), the
:class:`~attune.mcp_server.reader.RuntimeReadPort` protocol, an
:class:`~attune.mcp_server.auth.AgentAllowlist` (public agent ids and token
hashes, never a real secret), a pluggable token verifier, a proposal store,
and an audit log. Nothing here can reach Gmail, Calendar, a model gateway,
or Mem0/Qdrant directly -- see ``docs/decisions.md`` for the recorded
process-boundary decision this enforces.

Every tool call is authorized (:func:`attune.mcp_server.auth.authorize_request`)
before it touches a reader, the proposal store, or anything else, and every
outcome -- success or refusal -- is audited with ``actor_type="mcp_agent"``,
a distinct audit actor type from the human principal's own actions
(``actor_type="cli"``/unset elsewhere in this codebase) or a workload's
(``hosted``'s ``actor_type="workload"``). A calling agent is not the
principal; its actions must always be attributable as its own.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .auth import AgentAllowlist, AgentAuthorizationError, AgentIdentity, TokenVerifier, authorize_request
from .gateway import CapabilityDenied, McpCapabilityGateway, McpCapabilityRegistry
from .proposals import McpProposalStore, ProposalNotFound, ProposalNotPending
from .reader import READ_TOOLS, ReadArgumentError, RuntimeReadPort

ACTOR_TYPE = "mcp_agent"

TOOL_PROPOSE = "attune.propose"
TOOL_TASK_GET = "attune.task.get"


class McpToolError(Exception):
    """A refused or malformed tool call. ``code`` is a stable, non-
    reflected reason string suitable for an MCP error result."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AuditLog(Protocol):
    def record(
        self,
        *,
        thread_id: str,
        workflow: str,
        events: "list[dict[str, Any]]",
        domain: "str | None" = None,
        user_id: "str | None" = None,
    ) -> None: ...


class AttuneMcpServer:
    def __init__(
        self,
        *,
        resource: str,
        reader: RuntimeReadPort,
        proposals: McpProposalStore,
        allowlist: AgentAllowlist,
        verifier: "TokenVerifier",
        audit_log: AuditLog,
        capability_registry: "McpCapabilityRegistry | None" = None,
    ):
        if not resource:
            raise ValueError("resource indicator must be non-empty")
        self._resource = resource
        self._reader = reader
        self._proposals = proposals
        self._allowlist = allowlist
        self._verifier = verifier
        self._audit_log = audit_log
        from .gateway import build_default_mcp_capability_registry

        self._gateway = McpCapabilityGateway(
            registry=capability_registry or build_default_mcp_capability_registry()
        )

    def list_tools(self) -> "tuple[str, ...]":
        return tuple(READ_TOOLS) + (TOOL_PROPOSE, TOOL_TASK_GET)

    def list_tools_authorized(self, *, token: str) -> "tuple[str, ...]":
        """Same catalog as :meth:`list_tools`, but behind the same
        fail-closed authorization every tool CALL goes through --
        "authorization is not optional" applies to discovery too, not only
        to invocation."""
        self._authorize(token)
        return self.list_tools()

    def call_tool(self, *, token: str, tool: str, arguments: "Mapping[str, Any]") -> "dict[str, Any]":
        identity = self._authorize(token, tool=tool)

        self._audit(
            agent_id=identity.agent_id,
            event="tool_call",
            fields={"tool": tool},
        )

        if tool == TOOL_PROPOSE:
            return self._propose(identity, arguments)
        if tool == TOOL_TASK_GET:
            return self._task_get(identity, arguments)

        handler = READ_TOOLS.get(tool)
        if handler is None:
            raise McpToolError("unknown_tool")
        try:
            return handler(self._reader, arguments)
        except ReadArgumentError as exc:
            raise McpToolError(exc.code) from exc

    def _authorize(self, token: str, *, tool: "str | None" = None) -> AgentIdentity:
        try:
            return authorize_request(
                token, resource=self._resource, allowlist=self._allowlist, verifier=self._verifier
            )
        except AgentAuthorizationError as exc:
            self._audit(
                agent_id=None,
                event="tool_call_refused",
                fields={"tool": tool, "code": exc.code},
            )
            raise McpToolError(exc.code) from exc

    def _propose(self, identity: AgentIdentity, arguments: "Mapping[str, Any]") -> "dict[str, Any]":
        try:
            admitted = self._gateway.admit(arguments)
        except CapabilityDenied as exc:
            self._audit(
                agent_id=identity.agent_id,
                event="propose_denied",
                fields={"code": exc.code},
            )
            raise McpToolError(exc.code) from exc

        # No effect occurs here -- this only persists a durable,
        # input-required task. See proposals.py's module docstring.
        proposal = self._proposals.create(
            capability=admitted.capability,
            contract_version=admitted.contract_version,
            arguments=admitted.arguments,
            calling_agent=identity.agent_id,
        )
        self._audit(
            agent_id=identity.agent_id,
            event="propose_admitted",
            fields={"task_id": proposal.task_id, "capability": admitted.capability},
        )
        return proposal.to_task().to_dict()

    def _task_get(self, identity: AgentIdentity, arguments: "Mapping[str, Any]") -> "dict[str, Any]":
        if set(arguments) != {"task_id"} or not isinstance(arguments.get("task_id"), str):
            raise McpToolError("arguments_invalid")
        proposal = self._proposals.get(arguments["task_id"])
        if proposal is None:
            raise McpToolError("task_not_found")
        if proposal.calling_agent != identity.agent_id:
            # An agent may only poll a task it created -- one more place
            # "nothing an agent sends selects ... an actor" holds: the
            # task_id alone is never sufficient to read someone else's
            # proposal.
            raise McpToolError("task_not_found")
        return proposal.to_task().to_dict()

    def _audit(self, *, agent_id: "str | None", event: str, fields: "dict[str, Any]") -> None:
        self._audit_log.record(
            thread_id=f"mcp:{agent_id or 'unauthorized'}",
            workflow="mcp_server",
            domain=None,
            user_id=agent_id,
            events=[{"event": event, "actor_type": ACTOR_TYPE, "agent_id": agent_id, **fields}],
        )


def approve_proposal(proposals: McpProposalStore, task_id: str, *, actor: str):
    """The normal human approval path's ``approve`` half -- a thin
    function so ``cli/mcp_cmd.py`` and tests share exactly one code path.
    Raises :class:`~attune.mcp_server.proposals.ProposalNotFound` /
    :class:`~attune.mcp_server.proposals.ProposalNotPending` verbatim."""
    return proposals.approve(task_id, actor=actor)


def reject_proposal(proposals: McpProposalStore, task_id: str, *, actor: str):
    return proposals.reject(task_id, actor=actor)


__all__ = [
    "ACTOR_TYPE",
    "AttuneMcpServer",
    "McpToolError",
    "ProposalNotFound",
    "ProposalNotPending",
    "approve_proposal",
    "reject_proposal",
]
