from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from attune.hosted.customer_export import CustomerExportStart, CustomerExportStatus
from attune.hosted.customer_export_service import CustomerExportService
from attune.hosted.tenant import TenantContext

TENANT = UUID(int=1)
PRINCIPAL = UUID(int=2)
SESSION = UUID(int=3)
EXPORT = UUID(int=4)
INTENT = UUID(int=5)
NOW = datetime.now(timezone.utc)


class Requests:
    def __init__(self, *, was_created=True, state="requested"):
        self.started = CustomerExportStart(EXPORT, "account", state, NOW, was_created)
        self.status = CustomerExportStatus(
            EXPORT, "account", state, NOW, NOW, None, None, None, None
        )

    def request_or_existing(self, *args, **kwargs):
        return self.started

    def list(self, *args, **kwargs):
        return (self.status,)


class Dispatches:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def enqueue(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.failure:
            raise self.failure
        return SimpleNamespace(intent=SimpleNamespace(id=INTENT))


class Broker:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = []

    def dispatch(self, intent_id):
        self.calls.append(intent_id)
        return self.accepted


class Authorizations:
    pass


def test_new_and_adopted_requested_exports_dispatch_the_canonical_intent():
    for created in (True, False):
        requests = Requests(was_created=created)
        dispatches = Dispatches()
        broker = Broker()
        result = CustomerExportService(
            requests, dispatches, broker, Authorizations()
        ).request(
            TenantContext(TENANT),
            principal_id=PRINCIPAL,
            session_id=SESSION,
            scope="account",
        )
        assert result.accepted is created
        assert dispatches.calls[0][1]["payload"] == {"export_id": str(EXPORT)}
        assert broker.calls == [INTENT]


def test_already_dispatched_adopted_export_is_not_duplicated():
    service = CustomerExportService(
        Requests(was_created=False),
        Dispatches(RuntimeError("existing dispatch job is no longer queued")),
        Broker(),
        Authorizations(),
    )
    assert service.request(
        TenantContext(TENANT),
        principal_id=PRINCIPAL,
        session_id=SESSION,
        scope="account",
    ).accepted is False


def test_broker_refusal_remains_visible_for_safe_retry():
    service = CustomerExportService(
        Requests(), Dispatches(), Broker(False), Authorizations()
    )
    with pytest.raises(RuntimeError, match="dispatch was refused"):
        service.request(
            TenantContext(TENANT),
            principal_id=PRINCIPAL,
            session_id=SESSION,
            scope="account",
        )
