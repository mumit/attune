"""Control-plane repository, turns reader, and audited service for the web
conversation surface.

There is no installation, preference, or destination ceremony for 'web': an
ordinary authenticated owner session, an active policy, and an active
Google connector are the whole authority, matching what the executor itself
re-checks. Reading replies is polling -- there is no push delivery, no
channel-broker involvement, and no delivery row. The stored assistant turn
is the delivery.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from .repositories import ConnectionFactory
from .tenant import TenantContext, tenant_transaction


@dataclass(frozen=True)
class AcceptedWebMessage:
    dispatch_intent_id: UUID
    pre_audit_intent_id: UUID
    conversation_id: UUID
    user_sequence: int
    accepted_new: bool


@dataclass(frozen=True)
class WebConversationTurn:
    sequence: int
    actor_type: str
    content: str


class PostgresWebConversationRepository:
    """Calls the tenant-scoped acceptance function and reads canonical turns."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def accept(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        text: str,
    ) -> AcceptedWebMessage:
        if not isinstance(text, str) or not 1 <= len(text) <= 8_000:
            raise ValueError("web message text is invalid")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.accept_web_owner_message(%s, %s, %s)",
                    (principal_id, session_id, text),
                )
                row = cursor.fetchone()
        return AcceptedWebMessage(
            dispatch_intent_id=row[0],
            pre_audit_intent_id=row[1],
            conversation_id=row[2],
            user_sequence=row[3],
            accepted_new=row[4],
        )

    def turns(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        after: int,
        limit: int = 50,
    ) -> tuple[tuple[WebConversationTurn, ...], bool]:
        if not isinstance(after, int) or after < 0:
            raise ValueError("after sequence is invalid")
        if not 1 <= limit <= 50:
            raise ValueError("turns page limit is invalid")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT conversation.id
                      FROM attune.conversations conversation
                     WHERE conversation.tenant_id = %s
                       AND conversation.principal_id = %s
                       AND conversation.surface = 'web'
                    """,
                    (context.tenant_id, principal_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return (), False
                conversation_id = row[0]
                cursor.execute(
                    """
                    SELECT sequence, actor_type, content
                      FROM attune.conversation_turns
                     WHERE tenant_id = %s AND conversation_id = %s
                       AND sequence > %s
                     ORDER BY sequence ASC
                     LIMIT %s
                    """,
                    (context.tenant_id, conversation_id, after, limit),
                )
                turns = tuple(
                    WebConversationTurn(sequence=item[0], actor_type=item[1], content=item[2])
                    for item in cursor.fetchall()
                )
                cursor.execute(
                    """
                    SELECT actor_type FROM attune.conversation_turns
                     WHERE tenant_id = %s AND conversation_id = %s
                     ORDER BY sequence DESC LIMIT 1
                    """,
                    (context.tenant_id, conversation_id),
                )
                newest = cursor.fetchone()
        pending = newest is not None and newest[0] == "user"
        return turns, pending


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


class DispatchBroker(Protocol):
    def dispatch(self, intent_id: UUID) -> bool: ...


class WebConversationService:
    """Delivers the acceptance audit and dispatches the read-only job.

    Mirrors how the Slack/Google Chat channel broker delivers its own
    `pre_audit_intent_id` right after calling the acceptance function, and
    how the ingress layer then dispatches unconditionally: there is no
    separate ingress hop here, so both happen in one place, the control
    plane itself.
    """

    def __init__(
        self,
        repository: PostgresWebConversationRepository,
        audit_writer: AuditWriter,
        dispatch_broker: DispatchBroker,
    ):
        self._repository = repository
        self._audit_writer = audit_writer
        self._dispatch_broker = dispatch_broker

    def send(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        text: str,
    ) -> AcceptedWebMessage:
        accepted = self._repository.accept(
            context, principal_id=principal_id, session_id=session_id, text=text,
        )
        if not self._audit_writer.write(accepted.pre_audit_intent_id):
            raise RuntimeError("web conversation pre-effect audit is unavailable")
        if not self._dispatch_broker.dispatch(accepted.dispatch_intent_id):
            raise RuntimeError("web conversation dispatch was refused")
        return accepted

    def turns(
        self, context: TenantContext, *, principal_id: UUID, after: int,
    ) -> tuple[tuple[WebConversationTurn, ...], bool]:
        return self._repository.turns(context, principal_id=principal_id, after=after)
