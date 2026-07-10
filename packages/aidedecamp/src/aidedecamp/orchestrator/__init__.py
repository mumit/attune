"""LangGraph orchestration (design doc 4.2).

Model each workflow as a small, single-purpose, checkpointed graph rather than
one giant graph. Checkpointing lets a 'waiting for your approval' state survive a
restart; the human-in-the-loop interrupt/resume primitives are what make rung-2
autonomy (propose, wait) work.

The autonomy gate (``autonomy.py``) is consulted before any action leaves a
graph, and it fails safe: without an explicit per-(action,domain) grant, the
graph always routes through human approval.
"""

from .autonomy import (
    Action,
    Domain,
    PermissionMatrix,
    Rung,
    default_matrix,
)
from .state import DraftApproveState
from .draft_approve import MAX_ITERATIONS, build_draft_approve_graph

__all__ = [
    "Action",
    "Domain",
    "Rung",
    "PermissionMatrix",
    "default_matrix",
    "DraftApproveState",
    "build_draft_approve_graph",
    "MAX_ITERATIONS",
]
