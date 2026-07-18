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
from .draft_approve import (
    MAX_ITERATIONS,
    apply_confirmation,
    build_draft_approve_graph,
    make_connector_apply_fn,
    resume_workflow,
)
from .followup import (
    JsonNudgeState,
    find_nudge_candidates,
    run_follow_up_nudges,
)
from .grants import (
    GraduationSuggestion,
    JsonPermissionMatrixStore,
    TrackRecord,
    grant,
    revoke,
    show_matrix,
    suggest_graduations,
    track_records,
)
from .importance import (
    ImportanceProfile,
    ImportanceTier,
    JsonImportanceProfile,
    TierAssessment,
)
from .pending import (
    JsonPendingApprovals,
    PendingApproval,
    PendingApprovals,
    sweep_ignored,
)
from .triage import Priority, TriageResult, triage_thread
from .scheduling import ConflictResult, detect_conflict

__all__ = [
    "Action",
    "Domain",
    "Rung",
    "PermissionMatrix",
    "default_matrix",
    "DraftApproveState",
    "apply_confirmation",
    "build_draft_approve_graph",
    "make_connector_apply_fn",
    "resume_workflow",
    "MAX_ITERATIONS",
    "JsonNudgeState",
    "find_nudge_candidates",
    "run_follow_up_nudges",
    "GraduationSuggestion",
    "JsonPermissionMatrixStore",
    "TrackRecord",
    "grant",
    "revoke",
    "show_matrix",
    "suggest_graduations",
    "track_records",
    "JsonPendingApprovals",
    "PendingApproval",
    "PendingApprovals",
    "sweep_ignored",
    "ImportanceProfile",
    "ImportanceTier",
    "JsonImportanceProfile",
    "TierAssessment",
    "Priority",
    "TriageResult",
    "triage_thread",
    "ConflictResult",
    "detect_conflict",
]
