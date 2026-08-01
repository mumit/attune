"""The Tasks extension vocabulary (build prompt 34, task 2).

MCP's 2026-07-28 extensions framework adds Tasks — async long-running
operations with polling, mid-flight input, and a durable handle — which is a
near-exact match for Attune's draft-approve interrupt: a capability
invocation returns a task handle, the task enters an input-required state
pending human approval, and the caller polls.

Vocabulary is kept deliberately compatible with A2A's eight-state task
lifecycle (``docs/landscape-2026.md`` §10, ``docs/plan-2026-h2.md`` P8):
``input_required``/``auth_required`` name exactly the same interrupt A2A
already standardized, so an MCP host that also speaks A2A doesn't need a
second vocabulary to reason about Attune's approval gate. This module names
the states; :mod:`attune.mcp_server.proposals` is what actually persists one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class TaskState(str, Enum):
    """A2A-aligned task lifecycle states. Only the subset
    ``attune.propose`` actually reaches is used today (``SUBMITTED`` ->
    ``INPUT_REQUIRED`` -> ``COMPLETED``/``REJECTED``); the rest are named
    now so a later capability that needs them (a long-running search, a
    task requiring re-authentication mid-flight) doesn't invent a second
    vocabulary."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    AUTH_REQUIRED = "auth_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


# States a poller should stop polling at -- the task will never change state
# again on its own. INPUT_REQUIRED/AUTH_REQUIRED/WORKING/SUBMITTED are all
# still "keep polling."
TERMINAL_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED}
)


@dataclass(frozen=True)
class Task:
    """The durable handle a ``tools/call`` on a gated capability returns.

    ``result``/``error`` are populated only once the task reaches a
    terminal state; both are ``None`` while the task is still
    ``input_required``, matching the acceptance criterion that a caller
    polling immediately after ``attune.propose`` sees no effect yet.
    """

    task_id: str
    state: TaskState
    capability: str
    created_at: str
    updated_at: str
    result: "Mapping[str, Any] | None" = None
    error: "str | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "state": self.state.value,
            "capability": self.capability,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": dict(self.result) if self.result is not None else None,
            "error": self.error,
        }
