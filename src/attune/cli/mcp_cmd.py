"""``attune mcp-server proposals`` — the normal human approval path for
MCP-originated ``attune.propose`` tasks (build prompt 34, tasks 2-4).

A calling agent's ``attune.propose`` invocation only ever produces a durable,
``input_required`` task (see ``mcp_server/server.py``/``mcp_server/proposals.py``)
-- this command is what actually resolves one, exactly the way a human
resolves an ordinary approval card. ``proposal_store_factory`` is injected
(mirrors ``cli/undo_cmd.py``'s ``runtime_factory``) so tests supply an
in-memory store without touching a real data directory.
"""

from __future__ import annotations

from typing import Any, Callable


def _open_store(settings: Any):
    from ..config import Settings
    from ..mcp_server.proposals import JsonMcpProposalStore

    resolved = settings or Settings.from_env()
    return JsonMcpProposalStore(resolved.mcp_server_proposals_path)


def run_mcp_proposals_list(
    *,
    settings: Any = None,
    proposal_store_factory: "Callable[[Any], Any] | None" = None,
    out: "Callable[[str], None]" = print,
) -> int:
    store = (proposal_store_factory or _open_store)(settings)
    pending = store.list_pending()
    if not pending:
        out("No proposals awaiting a decision.")
        return 0
    for proposal in pending:
        out(
            f"{proposal.task_id}  {proposal.capability}  "
            f"from={proposal.calling_agent}  arguments={dict(proposal.arguments)}"
        )
    return 0


def run_mcp_proposals_approve(
    task_id: str,
    *,
    actor: str = "cli",
    settings: Any = None,
    proposal_store_factory: "Callable[[Any], Any] | None" = None,
    out: "Callable[[str], None]" = print,
) -> int:
    from ..mcp_server.proposals import ProposalNotFound, ProposalNotPending
    from ..mcp_server.server import approve_proposal

    store = (proposal_store_factory or _open_store)(settings)
    try:
        proposal = approve_proposal(store, task_id, actor=actor)
    except ProposalNotFound:
        out(f"No such proposal: {task_id}")
        return 2
    except ProposalNotPending:
        out(f"Proposal {task_id} is no longer awaiting a decision.")
        return 2
    out(f"Approved {proposal.task_id} ({proposal.capability}).")
    return 0


def run_mcp_proposals_reject(
    task_id: str,
    *,
    actor: str = "cli",
    settings: Any = None,
    proposal_store_factory: "Callable[[Any], Any] | None" = None,
    out: "Callable[[str], None]" = print,
) -> int:
    from ..mcp_server.proposals import ProposalNotFound, ProposalNotPending
    from ..mcp_server.server import reject_proposal

    store = (proposal_store_factory or _open_store)(settings)
    try:
        proposal = reject_proposal(store, task_id, actor=actor)
    except ProposalNotFound:
        out(f"No such proposal: {task_id}")
        return 2
    except ProposalNotPending:
        out(f"Proposal {task_id} is no longer awaiting a decision.")
        return 2
    out(f"Rejected {proposal.task_id} ({proposal.capability}).")
    return 0
