from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.tenant import TenantContext
from attune.hosted.web_conversation import (
    AcceptedWebMessage,
    WebConversationService,
    WebConversationTurn,
)

TENANT = UUID("10000000-0000-4000-8000-000000001001")
PRINCIPAL = UUID("10000000-0000-4000-8000-000000001002")
SESSION = UUID("10000000-0000-4000-8000-000000001003")
CONVERSATION = UUID("10000000-0000-4000-8000-000000001004")
DISPATCH_INTENT = UUID("10000000-0000-4000-8000-000000001005")
AUDIT_INTENT = UUID("10000000-0000-4000-8000-000000001006")


class Repository:
    def __init__(self, accepted=None, turns_result=((), False)):
        self.accepted = accepted or AcceptedWebMessage(
            dispatch_intent_id=DISPATCH_INTENT,
            pre_audit_intent_id=AUDIT_INTENT,
            conversation_id=CONVERSATION,
            user_sequence=1,
            accepted_new=True,
        )
        self.turns_result = turns_result
        self.accept_calls = []
        self.turns_calls = []

    def accept(self, context, **kwargs):
        self.accept_calls.append((context, kwargs))
        return self.accepted

    def turns(self, context, **kwargs):
        self.turns_calls.append((context, kwargs))
        return self.turns_result


class AuditWriter:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def write(self, audit_intent_id):
        self.calls.append(audit_intent_id)
        return self.result


class DispatchBroker:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def dispatch(self, intent_id):
        self.calls.append(intent_id)
        return self.result


def test_web_conversation_service_delivers_the_audit_then_dispatches():
    repository = Repository()
    audit = AuditWriter()
    dispatch = DispatchBroker()
    service = WebConversationService(repository, audit, dispatch)
    accepted = service.send(
        TenantContext(TENANT), principal_id=PRINCIPAL, session_id=SESSION, text="hi",
    )
    assert accepted.conversation_id == CONVERSATION
    assert repository.accept_calls == [
        (TenantContext(TENANT), {"principal_id": PRINCIPAL, "session_id": SESSION, "text": "hi"})
    ]
    assert audit.calls == [AUDIT_INTENT]
    assert dispatch.calls == [DISPATCH_INTENT]


def test_web_conversation_service_fails_closed_when_the_audit_is_unavailable():
    repository = Repository()
    audit = AuditWriter(result=False)
    dispatch = DispatchBroker()
    service = WebConversationService(repository, audit, dispatch)
    with pytest.raises(RuntimeError, match="audit"):
        service.send(
            TenantContext(TENANT), principal_id=PRINCIPAL, session_id=SESSION, text="hi",
        )
    assert dispatch.calls == []


def test_web_conversation_service_fails_closed_when_dispatch_is_refused():
    repository = Repository()
    audit = AuditWriter()
    dispatch = DispatchBroker(result=False)
    service = WebConversationService(repository, audit, dispatch)
    with pytest.raises(RuntimeError, match="dispatch"):
        service.send(
            TenantContext(TENANT), principal_id=PRINCIPAL, session_id=SESSION, text="hi",
        )
    # The turn, job, dispatch, and audit rows were already committed inside
    # the acceptance function; only the broker call was refused, so there is
    # no retry here -- the worker's own reconciliation sweep covers it.
    assert audit.calls == [AUDIT_INTENT]


def test_web_conversation_service_reads_turns_and_pending_through_the_repository():
    turns = (WebConversationTurn(sequence=2, actor_type="assistant", content="hi"),)
    repository = Repository(turns_result=(turns, False))
    service = WebConversationService(repository, AuditWriter(), DispatchBroker())
    result, pending = service.turns(
        TenantContext(TENANT), principal_id=PRINCIPAL, after=1,
    )
    assert result == turns
    assert pending is False
    assert repository.turns_calls == [
        (TenantContext(TENANT), {"principal_id": PRINCIPAL, "after": 1})
    ]
