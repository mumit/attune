"""Durable store for MCP-originated proposals (build prompt 34, tasks 2+3).

An ``attune.propose`` invocation is admitted by
:mod:`attune.mcp_server.gateway` and then persisted here as an
:class:`McpProposal` in :data:`~attune.mcp_server.tasks.TaskState.INPUT_REQUIRED`
-- durable, so a caller polling the returned task id survives a process
restart, and so no side effect can occur before a human resolves it through
the normal approval path (:func:`JsonMcpProposalStore.approve` /
:func:`JsonMcpProposalStore.reject`, driven by ``attune mcp-server
proposals approve/reject`` -- see ``cli/mcp_cmd.py``).

Same file-backed shape as ``orchestrator/pending.py``'s
``JsonPendingApprovals`` and ``orchestrator/attention.py``'s
``JsonAttentionStore``: atomic temp-file-plus-``os.replace`` writes, an
in-process ``threading.RLock`` plus ``fslock.locked`` around every
read-modify-write critical section (finding F2's cross-process lock).

Deliberately a SEPARATE registry from ``orchestrator.pending.PendingApprovals``:
that one is keyed by a LangGraph workflow thread id and expects a live
draft-approve graph behind every entry. An MCP-originated proposal has
neither -- resolving it (this build prompt's scope) only records a human
decision; it does not yet drive a connector effect. See
``docs/decisions.md`` for the recorded scope boundary.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from ..fslock import locked
from .tasks import TERMINAL_STATES, Task, TaskState


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class McpProposal:
    task_id: str
    capability: str
    contract_version: int
    arguments: "Mapping[str, Any]"
    calling_agent: str
    created_at: str
    updated_at: str
    state: TaskState
    decided_by: "str | None" = None

    def to_task(self) -> Task:
        result = None
        if self.state in TERMINAL_STATES:
            result = {"decision": self.state.value, "decided_by": self.decided_by}
        return Task(
            task_id=self.task_id,
            state=self.state,
            capability=self.capability,
            created_at=self.created_at,
            updated_at=self.updated_at,
            result=result,
        )


class ProposalNotFound(Exception):
    pass


class ProposalNotPending(Exception):
    """The normal approval path only ever resolves a proposal that is
    still ``INPUT_REQUIRED`` -- resolving twice, or resolving a proposal
    the caller already canceled, is refused rather than silently
    overwriting the first decision."""


class McpProposalStore(Protocol):
    def create(
        self, *, capability: str, contract_version: int, arguments: "Mapping[str, Any]", calling_agent: str
    ) -> McpProposal: ...

    def get(self, task_id: str) -> "McpProposal | None": ...

    def list_pending(self) -> "list[McpProposal]": ...

    def approve(self, task_id: str, *, actor: str) -> McpProposal: ...

    def reject(self, task_id: str, *, actor: str) -> McpProposal: ...


class JsonMcpProposalStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()

    def create(
        self,
        *,
        capability: str,
        contract_version: int,
        arguments: "Mapping[str, Any]",
        calling_agent: str,
    ) -> McpProposal:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            task_id = str(uuid.uuid4())
            now = _now_iso()
            data[task_id] = {
                "capability": capability,
                "contract_version": contract_version,
                "arguments": dict(arguments),
                "calling_agent": calling_agent,
                "created_at": now,
                "updated_at": now,
                "state": TaskState.INPUT_REQUIRED.value,
                "decided_by": None,
            }
            self._save(data)
            return self._to_proposal(task_id, data[task_id])

    def get(self, task_id: str) -> "McpProposal | None":
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        raw = data.get(task_id)
        return self._to_proposal(task_id, raw) if raw else None

    def list_pending(self) -> "list[McpProposal]":
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        return [
            self._to_proposal(task_id, raw)
            for task_id, raw in data.items()
            if raw["state"] == TaskState.INPUT_REQUIRED.value
        ]

    def approve(self, task_id: str, *, actor: str) -> McpProposal:
        return self._resolve(task_id, state=TaskState.COMPLETED, actor=actor)

    def reject(self, task_id: str, *, actor: str) -> McpProposal:
        return self._resolve(task_id, state=TaskState.REJECTED, actor=actor)

    def _resolve(self, task_id: str, *, state: TaskState, actor: str) -> McpProposal:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            raw = data.get(task_id)
            if raw is None:
                raise ProposalNotFound(task_id)
            if raw["state"] != TaskState.INPUT_REQUIRED.value:
                raise ProposalNotPending(task_id)
            raw["state"] = state.value
            raw["decided_by"] = actor
            raw["updated_at"] = _now_iso()
            self._save(data)
            return self._to_proposal(task_id, raw)

    @staticmethod
    def _to_proposal(task_id: str, raw: "dict[str, Any]") -> McpProposal:
        return McpProposal(
            task_id=task_id,
            capability=raw["capability"],
            contract_version=raw["contract_version"],
            arguments=raw["arguments"],
            calling_agent=raw["calling_agent"],
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            state=TaskState(raw["state"]),
            decided_by=raw.get("decided_by"),
        )

    def _load(self) -> "dict[str, Any]":
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as fh:
            return json.load(fh)

    def _save(self, data: "dict[str, Any]") -> None:
        parent = os.path.dirname(self._path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=parent)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp_path, self._path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
