"""LangGraph orchestration (design doc 4.2).

Model each workflow as a small, single-purpose, checkpointed graph rather than
one giant graph. Checkpointing lets a 'waiting for your approval' state survive a
restart; the human-in-the-loop interrupt/resume primitives are what make rung-2
autonomy (propose, wait) work.

The autonomy gate (``autonomy.py``) is consulted before any action leaves a
graph, and it fails safe: without an explicit per-(action,domain) grant, the
graph always routes through human approval.
"""

from .attention import (
    AttentionItem,
    AttentionStore,
    JsonAttentionStore,
)
from .autonomy import (
    Action,
    Domain,
    GrantScope,
    PermissionMatrix,
    Rung,
    ScopedGrant,
    default_matrix,
)
from .correlation import (
    CorrelatableItem,
    correlate,
    from_attention_item,
    from_calendar_event,
    from_mail_thread,
)
from .state import DraftApproveState
from .draft_approve import (
    HYGIENE_ACTIONS,
    MAX_ITERATIONS,
    apply_confirmation,
    archive_draft_fn,
    build_draft_approve_graph,
    calendar_action_draft_fn,
    make_calendar_action_apply_fn,
    make_connector_apply_fn,
    make_label_apply_fn,
    resume_workflow,
)
from .followup import (
    JsonNudgeState,
    find_nudge_candidates,
    run_follow_up_nudges,
)
from .grants import (
    DEMOTION_PREFIX,
    GRADUATION_CARD_EXCLUDED_ACTIONS,
    GRADUATION_CARD_MAX_RUNG,
    GRADUATION_PREFIX,
    GRADUATION_REJECTION_COOLDOWN_DAYS,
    MAX_AUTONOMY_CARDS_PER_RUN,
    DemotionSuggestion,
    GraduationSuggestion,
    JsonGraduationState,
    JsonPermissionMatrixStore,
    TrackRecord,
    demotion_thread_id,
    grant,
    graduation_thread_id,
    parse_scope,
    render_scope,
    resolve_autonomy_card,
    revoke,
    show_matrix,
    suggest_demotions,
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
    "GrantScope",
    "ScopedGrant",
    "default_matrix",
    "DraftApproveState",
    "apply_confirmation",
    "archive_draft_fn",
    "build_draft_approve_graph",
    "calendar_action_draft_fn",
    "make_calendar_action_apply_fn",
    "make_connector_apply_fn",
    "make_label_apply_fn",
    "resume_workflow",
    "HYGIENE_ACTIONS",
    "MAX_ITERATIONS",
    "JsonNudgeState",
    "find_nudge_candidates",
    "run_follow_up_nudges",
    "GraduationSuggestion",
    "DemotionSuggestion",
    "JsonPermissionMatrixStore",
    "JsonGraduationState",
    "TrackRecord",
    "grant",
    "revoke",
    "parse_scope",
    "render_scope",
    "show_matrix",
    "suggest_graduations",
    "suggest_demotions",
    "track_records",
    "resolve_autonomy_card",
    "graduation_thread_id",
    "demotion_thread_id",
    "GRADUATION_PREFIX",
    "DEMOTION_PREFIX",
    "GRADUATION_CARD_EXCLUDED_ACTIONS",
    "GRADUATION_CARD_MAX_RUNG",
    "GRADUATION_REJECTION_COOLDOWN_DAYS",
    "MAX_AUTONOMY_CARDS_PER_RUN",
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
    "AttentionItem",
    "AttentionStore",
    "JsonAttentionStore",
    "CorrelatableItem",
    "correlate",
    "from_attention_item",
    "from_calendar_event",
    "from_mail_thread",
]
