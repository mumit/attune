"""Bounded, read-only hosted web conversation execution.

The web surface reuses the Google Chat conversation machinery (routing,
Gmail/Calendar reads, model classification and reply generation) but has no
destination, no channel broker, and no reply delivery hop: the stored
assistant turn is itself the delivery, so this executor appends the turn and
stops -- it never calls a reply broker.
"""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .durable import HostedTurn
from .google_chat_conversation_executor import (
    DraftCapabilityAdmissions,
    DraftCapabilityGateway,
    GoogleChatConversationExecutor,
    MemoryAuditSink,
    MemoryRepository,
    build_turn_provenance,
)
from .intelligence import ImportanceSignalRecorder
from .repositories import ConnectionFactory, HostedJob
from .tenant import TenantContext, tenant_transaction

PURPOSE = "channel.web.converse"
CAPABILITY = "assistant.conversation.read"


@dataclass(frozen=True)
class WebConversationWork:
    conversation_id: UUID
    principal_id: UUID
    connector_id: UUID
    user_sequence: int


class PostgresWebConversationWorkRepository:
    """Resolves canonical conversation authority for the web surface.

    Unlike the channel surfaces, there is no destination and no channel
    preference to check -- an active policy and an active Google connector
    are the whole authority, mirroring the acceptance function.
    """

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def resolve(self, context: TenantContext, job: HostedJob) -> WebConversationWork:
        payload = _payload(job)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT conversation.id, conversation.principal_id, connector.id,
                           (job.payload->>'user_sequence')::bigint
                      FROM attune.jobs job
                      JOIN attune.provider_events event
                        ON event.tenant_id = job.tenant_id
                       AND event.id = (job.payload->>'provider_event_id')::uuid
                      JOIN attune.conversations conversation
                        ON conversation.tenant_id = job.tenant_id
                       AND conversation.id = (job.payload->>'conversation_id')::uuid
                      JOIN attune.principals principal
                        ON principal.tenant_id = job.tenant_id
                       AND principal.id = conversation.principal_id
                      JOIN attune.connectors connector
                        ON connector.tenant_id = job.tenant_id
                       AND connector.principal_id = conversation.principal_id
                       AND connector.provider = 'google'
                     WHERE job.tenant_id = %s AND job.id = %s
                       AND job.kind = %s
                       AND job.capability = 'assistant.conversation.read'
                       AND job.state = 'leased'
                       AND conversation.surface = 'web'
                       AND principal.status = 'active'
                       AND connector.status = 'active'
                       AND event.provider = 'web'
                       AND event.kind = 'web.message'
                       AND event.signal->>'conversation_id' = conversation.id::text
                       AND event.signal->>'user_sequence' = job.payload->>'user_sequence'
                       AND EXISTS (
                           SELECT 1 FROM attune.policies policy
                            WHERE policy.tenant_id = job.tenant_id AND policy.active
                       )
                       AND EXISTS (
                           SELECT 1 FROM attune.conversation_turns turn
                            WHERE turn.tenant_id = job.tenant_id
                              AND turn.conversation_id = conversation.id
                              AND turn.sequence = (job.payload->>'user_sequence')::bigint
                              AND turn.actor_type = 'user'
                       )
                     LIMIT 2
                    """,
                    (context.tenant_id, job.id, PURPOSE),
                )
                rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError("conversation job authority is unavailable")
        work = WebConversationWork(*rows[0])
        if work != WebConversationWork(
            payload["conversation_id"], rows[0][1], rows[0][2], payload["user_sequence"]
        ):
            raise RuntimeError("conversation job payload changed")
        return work

    def recent(
        self, context: TenantContext, conversation_id: UUID, *, limit: int
    ) -> list[HostedTurn]:
        if not 1 <= limit <= 6:
            raise ValueError("conversation window is invalid")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT conversation_id, sequence, actor_type, content, provenance
                      FROM attune.conversation_turns
                     WHERE tenant_id = %s AND conversation_id = %s
                     ORDER BY sequence DESC LIMIT %s
                    """,
                    (context.tenant_id, conversation_id, limit),
                )
                return [HostedTurn(*row) for row in reversed(cursor.fetchall())]

    def append_assistant(
        self, context: TenantContext, *, conversation_id: UUID, content: str, job_id: UUID,
        extra_provenance: dict[str, object] | None = None,
    ) -> HostedTurn:
        if not isinstance(content, str) or not 1 <= len(content) <= 8_000:
            raise ValueError("assistant response is invalid")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT id FROM attune.conversations WHERE tenant_id = %s AND id = %s FOR UPDATE",
                    (context.tenant_id, conversation_id),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("conversation is unavailable")
                cursor.execute(
                    """
                    SELECT conversation_id, sequence, actor_type, content, provenance
                      FROM attune.conversation_turns
                     WHERE tenant_id = %s AND conversation_id = %s
                       AND actor_type = 'assistant' AND provenance->>'job_id' = %s
                    """,
                    (context.tenant_id, conversation_id, str(job_id)),
                )
                existing = cursor.fetchall()
                if len(existing) > 1:
                    raise RuntimeError("assistant response is ambiguous")
                if existing:
                    if existing[0][3] != content:
                        raise RuntimeError("assistant response changed")
                    return HostedTurn(*existing[0])
                cursor.execute(
                    """
                    SELECT COALESCE(max(sequence), 0) + 1
                      FROM attune.conversation_turns
                     WHERE tenant_id = %s AND conversation_id = %s
                    """,
                    (context.tenant_id, conversation_id),
                )
                sequence = cursor.fetchone()[0]
                provenance = build_turn_provenance(job_id, extra_provenance)
                cursor.execute(
                    """
                    INSERT INTO attune.conversation_turns
                        (tenant_id, conversation_id, sequence, actor_type, content, provenance)
                    VALUES (%s, %s, %s, 'assistant', %s, %s::jsonb)
                    """,
                    (context.tenant_id, conversation_id, sequence, content,
                     json.dumps(provenance, sort_keys=True, separators=(",", ":"))),
                )
                cursor.execute(
                    "UPDATE attune.conversations SET updated_at = clock_timestamp() WHERE tenant_id = %s AND id = %s",
                    (context.tenant_id, conversation_id),
                )
                return HostedTurn(conversation_id, sequence, "assistant", content, provenance)


class WebConversationExecutor(GoogleChatConversationExecutor):
    """Appends the assistant turn and stops -- no reply broker exists."""

    def __init__(
        self,
        work: PostgresWebConversationWorkRepository,
        intents,
        workspace,
        models,
        *,
        now=None,
        timezone_name: str = "UTC",
        memory: MemoryRepository | None = None,
        memory_audit: MemoryAuditSink | None = None,
        capability_gateway: DraftCapabilityGateway | None = None,
        capability_admissions: DraftCapabilityAdmissions | None = None,
        importance_signals: ImportanceSignalRecorder | None = None,
    ):
        super().__init__(
            work, intents, workspace, models, None,
            now=now, timezone_name=timezone_name,
            reply_method="_web_conversation_has_no_reply_broker",
            intent_key_prefix="attune-web-converse-v1:",
            memory=memory, memory_audit=memory_audit,
            capability_gateway=capability_gateway,
            capability_admissions=capability_admissions,
            importance_signals=importance_signals,
        )

    def __call__(self, context: TenantContext, job: HostedJob) -> None:
        authority, answer, extra_provenance = self._respond(context, job)
        self._work.append_assistant(
            context, conversation_id=authority.conversation_id,
            content=answer, job_id=job.id, extra_provenance=extra_provenance,
        )


def _payload(job: HostedJob) -> dict[str, Any]:
    if job.kind != PURPOSE or job.capability != CAPABILITY:
        raise ValueError("conversation job does not match the fixed route")
    expected = {"schema_version", "provider_event_id", "conversation_id", "user_sequence"}
    if not isinstance(job.payload, dict) or set(job.payload) != expected or job.payload.get("schema_version") != 1:
        raise ValueError("conversation job payload does not match the contract")
    parsed: dict[str, Any] = {"user_sequence": job.payload["user_sequence"]}
    if type(parsed["user_sequence"]) is not int or not 1 <= parsed["user_sequence"] < 2**63:
        raise ValueError("conversation sequence is invalid")
    for field in ("provider_event_id", "conversation_id"):
        value = job.payload[field]
        if not isinstance(value, str):
            raise ValueError("conversation job reference is invalid")
        try:
            identifier = UUID(value)
        except ValueError as error:
            raise ValueError("conversation job reference is invalid") from error
        if str(identifier) != value:
            raise ValueError("conversation job reference is invalid")
        parsed[field] = identifier
    return parsed
