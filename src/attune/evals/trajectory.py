"""Trajectory-level assertions (build prompt 27, task 4): agents fail at the
step level — wrong tool, wrong arguments, state not propagated, goal drift —
not just in the final text, so this module asserts invariants over the
audit trail and final state a REAL ``orchestrator.draft_approve`` graph
invocation produces. Every assertion here is a pure function over a
completed graph ``result`` dict; none of them reimplement the gate/apply
logic they're checking — they only look at what that code already recorded.

:class:`RecordingMemoryStore` is the one piece of test scaffolding this
module provides: a thin wrapper any real ``MemoryStore`` can be dropped
behind to prove the retrieval floor (build prompt 24's "pass min_score at
all four call sites") is actually threaded through at a given call site,
without re-deriving score filtering itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..orchestrator.autonomy import Rung
from ..memory.base import MemoryStore


@dataclass(frozen=True)
class TrajectoryViolation:
    case_id: str
    assertion: str
    detail: str


def _events(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result.get("audit_events") or []


def _find(result: dict[str, Any], event_name: str) -> dict[str, Any] | None:
    return next((e for e in _events(result) if e.get("event") == event_name), None)


def assert_capability_selected(
    case_id: str, result: dict[str, Any], *, expected_action: str, expected_domain: str
) -> list[TrajectoryViolation]:
    """The right capability was selected: the graph's own state agrees with
    what the caller asked it to run."""
    violations = []
    if result.get("action") != expected_action:
        violations.append(TrajectoryViolation(
            case_id, "capability_selected",
            f"expected action={expected_action!r}, state has {result.get('action')!r}",
        ))
    if result.get("domain") != expected_domain:
        violations.append(TrajectoryViolation(
            case_id, "capability_selected",
            f"expected domain={expected_domain!r}, state has {result.get('domain')!r}",
        ))
    return violations


def assert_autonomy_rung_respected(case_id: str, result: dict[str, Any]) -> list[TrajectoryViolation]:
    """The autonomy rung was respected: an auto-applied decision must never
    have been reached below ACT_NOTIFY, and a route through human approval
    must show a recorded human decision once the workflow has decided."""
    violations = []
    gate = _find(result, "autonomy_gate")
    if gate is None:
        return violations
    max_rung = gate.get("max_rung")
    routed_to = gate.get("routed_to")
    if routed_to == "auto_apply" and (max_rung is None or max_rung < int(Rung.ACT_NOTIFY)):
        violations.append(TrajectoryViolation(
            case_id, "autonomy_rung_respected",
            f"auto_applied with max_rung={max_rung!r} < ACT_NOTIFY",
        ))
    if routed_to == "approve" and result.get("decision") is not None:
        if _find(result, "human_decision") is None:
            violations.append(TrajectoryViolation(
                case_id, "autonomy_rung_respected",
                "routed to approve, decided, but no human_decision audit event",
            ))
    return violations


def assert_no_write_on_read_only(case_id: str, result: dict[str, Any]) -> list[TrajectoryViolation]:
    """No write occurred on the approval (read-only-until-decided) route
    without a recorded human decision authorizing it."""
    violations = []
    gate = _find(result, "autonomy_gate")
    if gate is not None and gate.get("routed_to") == "approve" and result.get("applied_ref"):
        if _find(result, "human_decision") is None:
            violations.append(TrajectoryViolation(
                case_id, "no_write_on_read_only",
                "a write occurred on the approval route with no recorded human decision",
            ))
    return violations


def assert_freshness_checked_before_apply(case_id: str, result: dict[str, Any]) -> list[TrajectoryViolation]:
    """A stale source is honestly reported, not silently swallowed: when
    ``apply`` detects a ``SourceChangedError``, the audit trail must show
    the honest ``apply_skipped``/``source_changed`` event (draft_approve.py's
    own ``apply`` node contract)."""
    violations = []
    if result.get("apply_error") == "source_changed":
        skipped = _find(result, "apply_skipped")
        if skipped is None or skipped.get("reason") != "source_changed":
            violations.append(TrajectoryViolation(
                case_id, "freshness_checked_before_apply",
                "source_changed reported but not honestly recorded as apply_skipped",
            ))
    return violations


def run_trajectory_assertions(
    case_id: str,
    result: dict[str, Any],
    *,
    expected_action: str,
    expected_domain: str,
) -> list[TrajectoryViolation]:
    """The full assertion battery for one completed graph result."""
    violations: list[TrajectoryViolation] = []
    violations += assert_capability_selected(
        case_id, result, expected_action=expected_action, expected_domain=expected_domain
    )
    violations += assert_autonomy_rung_respected(case_id, result)
    violations += assert_no_write_on_read_only(case_id, result)
    violations += assert_freshness_checked_before_apply(case_id, result)
    return violations


class RecordingMemoryStore(MemoryStore):
    """Wraps a real ``MemoryStore`` and records every ``search`` call's
    kwargs — used to prove a call site actually threads ``min_score``
    through (build prompt 24), rather than re-deriving score filtering here.
    Every other method delegates unchanged."""

    def __init__(self, inner: MemoryStore):
        self._inner = inner
        self.search_calls: list[dict[str, Any]] = []

    def add(self, messages, *, user_id, metadata=None, infer=True):
        return self._inner.add(messages, user_id=user_id, metadata=metadata, infer=infer)

    def search(self, query, *, user_id, limit=8, min_score=None):
        self.search_calls.append({"query": query, "user_id": user_id, "limit": limit, "min_score": min_score})
        return self._inner.search(query, user_id=user_id, limit=limit, min_score=min_score)

    def get_all(self, *, user_id, limit=100):
        return self._inner.get_all(user_id=user_id, limit=limit)

    def delete(self, memory_id):
        return self._inner.delete(memory_id)

    def consolidate(self, *, user_id, audit_log=None):
        return self._inner.consolidate(user_id=user_id, audit_log=audit_log)


def assert_retrieval_requested_score_floor(
    case_id: str, recording_store: RecordingMemoryStore, *, expected_min_score: float
) -> list[TrajectoryViolation]:
    """Every ``search`` call the recorded run made passed the expected
    relevance floor — the trajectory-level version of build prompt 24's
    fix ("pass min_score at all four call sites")."""
    violations = []
    for call in recording_store.search_calls:
        if call.get("min_score") != expected_min_score:
            violations.append(TrajectoryViolation(
                case_id, "retrieval_score_floor",
                f"search({call['query']!r}) called with min_score={call.get('min_score')!r}, "
                f"expected {expected_min_score!r}",
            ))
    return violations
