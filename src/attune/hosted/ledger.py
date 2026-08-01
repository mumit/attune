"""Tenant-scoped, Postgres-backed implementation of the local decision
ledger (build prompt 26, ``docs/plan-2026-h2.md`` P2;
``attune.orchestrator.ledger``).

Follows the ``hosted/intelligence.py`` pattern (rule 3, "Build once"):
storage differs per plane, the dataclasses and the aggregation math are
IMPORTED, never reimplemented. :class:`PostgresDecisionLedger` produces and
consumes the exact same :class:`~attune.orchestrator.ledger.LedgerRow` shape
the local :class:`~attune.orchestrator.ledger.SqliteDecisionLedger` does, so
``orchestrator.ledger.compute_metrics_slice``/``render_metrics_table`` work
unchanged over rows read from either plane.

**Binding, not per-call context** (the same divergence
``hosted/intelligence.py`` documents for
:class:`~attune.hosted.intelligence.PostgresImportanceProfile`):
:class:`PostgresDecisionLedger` takes its ``TenantContext``/``principal_id``
at CONSTRUCTION time, exactly once per hosted job/request, and its methods
match the local :class:`~attune.orchestrator.ledger.DecisionLedger` protocol
shape exactly.

**No hashing of thread_id/proposal_id/memory ids.** Unlike
``hosted/intelligence.py``'s sender/channel/thread references (externally
supplied, often low-entropy provider identifiers with a real dictionary-
attack threat model), a ledger row's ``proposal_id``/``thread_id`` and its
``context_attribution.memory_ids`` are ALREADY internal, tenant-scoped
identifiers this system generated — the same posture
``attune.conversations.external_ref_hash`` documents for internal
identifiers, not the keyed-HMAC posture for provider references. They are
stored as plain, bounded text.

**Dormant.** This stage wires no executor — nothing in the hosted plane
calls this yet — following the exact precedent
``hosted/intelligence.py``'s own module docstring sets. See
``docs/decisions.md`` for the dated record of this and the local module's
design choices.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from ..orchestrator.ledger import ContextAttribution, LedgerRow, compute_edit_metrics
from .repositories import ConnectionFactory, _bounded_text
from .tenant import TenantContext, tenant_transaction

_COLUMNS = (
    "proposal_id", "thread_id", "domain", "action", "proposed_at",
    "autonomy_rung_granted", "autonomy_rung_used", "scope_matched",
    "model_id", "prompt_version", "playbook_commit",
    "memory_ids", "playbook_bullet_ids", "skill_ids",
    "triage_priority", "base_priority", "sender_importance_tier",
    "profile_reason", "eligible_item_count", "batch_id",
    "decision", "decided_at", "actor_ref", "time_to_decision_seconds",
    "edit_char_distance", "edit_distance_normalized", "edit_semantic_similarity",
    "edit_sections_changed", "applied_ok", "apply_skip_reason", "undone", "undone_at",
)


class PostgresDecisionLedger:
    """Hosted ``DecisionLedger`` — see the module docstring for the
    binding-at-construction design note."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        context: TenantContext,
        principal_id: UUID,
    ):
        self._connect = connection_factory
        self._context = context
        self._principal_id = principal_id

    def propose(self, row: LedgerRow) -> None:
        _bounded_text("proposal_id", row.proposal_id, 320)
        _bounded_text("thread_id", row.thread_id, 320)
        _bounded_text("domain", row.domain, 40)
        _bounded_text("action", row.action, 40)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.decision_ledger
                        (tenant_id, principal_id, proposal_id, thread_id, domain,
                         action, proposed_at, autonomy_rung_granted,
                         autonomy_rung_used, scope_matched, model_id,
                         prompt_version, playbook_commit, memory_ids,
                         playbook_bullet_ids, skill_ids, triage_priority,
                         base_priority, sender_importance_tier, profile_reason,
                         eligible_item_count, batch_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, proposal_id) DO NOTHING
                    """,
                    (
                        self._context.tenant_id, self._principal_id,
                        row.proposal_id, row.thread_id, row.domain, row.action,
                        row.proposed_at.astimezone(timezone.utc),
                        row.autonomy_rung_granted, row.autonomy_rung_used,
                        row.scope_matched, row.model_id, row.prompt_version,
                        row.playbook_commit,
                        list(row.context_attribution.memory_ids),
                        list(row.context_attribution.playbook_bullet_ids),
                        list(row.context_attribution.skill_ids),
                        row.triage_priority, row.base_priority,
                        row.sender_importance_tier, row.profile_reason,
                        row.eligible_item_count, row.batch_id,
                    ),
                )

    def complete(
        self,
        proposal_id: str,
        *,
        decision: str,
        decided_at: datetime | None = None,
        actor_ref: str | None = None,
        proposed_text: str | None = None,
        final_text: str | None = None,
        applied_ok: bool | None = None,
        apply_skip_reason: str | None = None,
    ) -> None:
        decided_at = (decided_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        edit = None
        if decision == "edited" and proposed_text is not None and final_text is not None:
            edit = compute_edit_metrics(proposed_text, final_text)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    "SELECT proposed_at FROM attune.decision_ledger "
                    "WHERE tenant_id = %s AND proposal_id = %s",
                    (self._context.tenant_id, proposal_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return
                proposed_at = row[0]
                elapsed = (decided_at - proposed_at).total_seconds()
                cursor.execute(
                    """
                    UPDATE attune.decision_ledger SET
                        decision = %s, decided_at = %s, actor_ref = %s,
                        time_to_decision_seconds = %s,
                        edit_char_distance = %s, edit_distance_normalized = %s,
                        edit_semantic_similarity = %s, edit_sections_changed = %s,
                        applied_ok = %s, apply_skip_reason = %s
                    WHERE tenant_id = %s AND proposal_id = %s
                    """,
                    (
                        decision, decided_at, actor_ref, elapsed,
                        edit.char_distance if edit else None,
                        edit.distance_normalized if edit else None,
                        edit.semantic_similarity if edit else None,
                        list(edit.sections_changed) if edit else [],
                        applied_ok, apply_skip_reason,
                        self._context.tenant_id, proposal_id,
                    ),
                )

    def mark_undone(self, proposal_id: str, *, at: datetime | None = None) -> None:
        at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(
                    "UPDATE attune.decision_ledger SET undone = true, undone_at = %s "
                    "WHERE tenant_id = %s AND proposal_id = %s",
                    (at, self._context.tenant_id, proposal_id),
                )

    def rows(
        self,
        *,
        since: datetime | None = None,
        domain: str | None = None,
        action: str | None = None,
    ) -> list[LedgerRow]:
        statement = (
            f"SELECT {', '.join(_COLUMNS)} FROM attune.decision_ledger "
            "WHERE tenant_id = %s AND principal_id = %s"
        )
        params: list[Any] = [self._context.tenant_id, self._principal_id]
        if since is not None:
            statement += " AND proposed_at >= %s"
            params.append(since.astimezone(timezone.utc))
        if domain is not None:
            statement += " AND domain = %s"
            params.append(domain)
        if action is not None:
            statement += " AND action = %s"
            params.append(action)
        statement += " ORDER BY proposed_at ASC"
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, self._context) as cursor:
                cursor.execute(statement, tuple(params))
                return [_row_from_record(record) for record in cursor.fetchall()]


def _row_from_record(record: Any) -> LedgerRow:
    values = dict(zip(_COLUMNS, record))
    return LedgerRow(
        proposal_id=values["proposal_id"],
        thread_id=values["thread_id"],
        domain=values["domain"],
        action=values["action"],
        proposed_at=values["proposed_at"],
        autonomy_rung_granted=values["autonomy_rung_granted"],
        autonomy_rung_used=values["autonomy_rung_used"],
        scope_matched=bool(values["scope_matched"]),
        model_id=values["model_id"],
        prompt_version=values["prompt_version"],
        playbook_commit=values["playbook_commit"],
        context_attribution=ContextAttribution(
            memory_ids=tuple(values["memory_ids"] or ()),
            playbook_bullet_ids=tuple(values["playbook_bullet_ids"] or ()),
            skill_ids=tuple(values["skill_ids"] or ()),
        ),
        triage_priority=values["triage_priority"],
        base_priority=values["base_priority"],
        sender_importance_tier=values["sender_importance_tier"],
        profile_reason=values["profile_reason"],
        eligible_item_count=values["eligible_item_count"],
        batch_id=values["batch_id"],
        decision=values["decision"],
        decided_at=values["decided_at"],
        actor_ref=values["actor_ref"],
        time_to_decision_seconds=values["time_to_decision_seconds"],
        edit_char_distance=values["edit_char_distance"],
        edit_distance_normalized=values["edit_distance_normalized"],
        edit_semantic_similarity=values["edit_semantic_similarity"],
        edit_sections_changed=tuple(values["edit_sections_changed"] or ()),
        applied_ok=values["applied_ok"],
        apply_skip_reason=values["apply_skip_reason"],
        undone=bool(values["undone"]),
        undone_at=values["undone_at"],
    )
