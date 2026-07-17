from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from attune.hosted.channel_setup_service import HostedChannelSetupService
from attune.hosted.channel_setup import HostedChannelSetupTransaction
from attune.hosted.tenant import TenantContext

TENANT = UUID("10000000-0000-4000-8000-000000000001")
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000011")
SESSION = UUID("10000000-0000-4000-8000-000000000012")
CONTEXT = TenantContext(TENANT)
NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


class Setups:
    def __init__(self):
        self.calls = []

    def begin(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return HostedChannelSetupTransaction(
            uuid4(), 2, kwargs["provider"], kwargs["mechanism"], "pending", kwargs["expires_at"]
        )

    def read(self, context, **kwargs):
        return (context, kwargs)

    def pending_destination_id(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return UUID("10000000-0000-4000-8000-000000000107")

    def disconnect(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return True


class Audit:
    def __init__(self):
        self.calls = []

    def request(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return type("Intent", (), {"id": uuid4()})()


class Writer:
    def __init__(self, results=(True, True)):
        self.results = iter(results)

    def write(self, _intent_id):
        return next(self.results)


class Delivery:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def test_google_chat_delivery(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_setup_generates_one_use_secret_and_mandatory_audits():
    setups, audit = Setups(), Audit()
    result = HostedChannelSetupService(setups, audit, Writer()).begin(
        CONTEXT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        provider="google_chat",
        now=NOW,
    )
    assert len(result.one_time_secret) == 43
    assert "one_time_secret=<redacted>" in repr(result)
    call = setups.calls[0][1]
    assert call["mechanism"] == "link_code"
    assert len(call["secret_hash"]) == 32
    assert call["expires_at"] > NOW
    assert [item[1]["outcome"] for item in audit.calls] == ["allowed", "observed"]
    assert all(item[1]["metadata"] == {"schema_version": 1} for item in audit.calls)


def test_setup_refuses_unknown_provider_before_audit():
    audit = Audit()
    with pytest.raises(ValueError, match="provider"):
        HostedChannelSetupService(Setups(), audit, Writer()).begin(
            CONTEXT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            provider="teams",
            now=NOW,
        )
    assert audit.calls == []


def test_setup_fails_closed_before_mutation_when_audit_is_unavailable():
    setups = Setups()
    with pytest.raises(RuntimeError, match="pre-effect"):
        HostedChannelSetupService(setups, Audit(), Writer((False,))).begin(
            CONTEXT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            provider="slack",
            now=NOW,
        )
    assert setups.calls == []


def test_setup_records_failed_outcome_without_leaking_secret():
    class Broken(Setups):
        def begin(self, context, **kwargs):
            raise RuntimeError("database unavailable")

    audit = Audit()
    with pytest.raises(RuntimeError, match="database"):
        HostedChannelSetupService(Broken(), audit, Writer()).begin(
            CONTEXT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            provider="google_chat",
            now=NOW,
        )
    assert [item[1]["outcome"] for item in audit.calls] == ["allowed", "failed"]


def test_delivery_test_is_canonical_fixed_profile_and_mandatory_audit():
    setups, audit, delivery = Setups(), Audit(), Delivery()
    result = HostedChannelSetupService(
        setups, audit, Writer(), delivery
    ).test_delivery(
        CONTEXT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        provider="google_chat",
    )
    assert result[0] == CONTEXT
    assert list(delivery.calls[0]) == ["destination_id"]
    assert [item[1]["outcome"] for item in audit.calls] == ["allowed", "observed"]
    assert all(
        item[1]["metadata"]["content_profile"] == "fixed_connection_test_v1"
        for item in audit.calls
    )


def test_disconnect_is_provider_fixed_recent_bound_and_mandatory_audit():
    setups, audit = Setups(), Audit()
    result = HostedChannelSetupService(setups, audit, Writer()).disconnect(
        CONTEXT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        provider="google_chat",
    )
    assert result[0] == CONTEXT
    assert setups.calls == [(
        CONTEXT,
        {
            "principal_id": PRINCIPAL,
            "session_id": SESSION,
            "provider": "google_chat",
        },
    )]
    assert [item[1]["outcome"] for item in audit.calls] == ["allowed", "observed"]
    assert all(
        item[1]["action"] == "hosted.channels.destination.disconnect"
        for item in audit.calls
    )
    assert all(
        item[1]["metadata"] == {"schema_version": 1, "provider": "google_chat"}
        for item in audit.calls
    )


def test_disconnect_refuses_unknown_provider_before_audit():
    audit = Audit()
    with pytest.raises(ValueError, match="provider"):
        HostedChannelSetupService(Setups(), audit, Writer()).disconnect(
            CONTEXT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            provider="teams",
        )
    assert audit.calls == []


class SlackDelivery(Delivery):
    def test_slack_delivery(self, **kwargs):
        self.calls.append(("slack_test", kwargs))
        return self.result

    def install_slack(self, **kwargs):
        self.calls.append(("install", kwargs))
        return self.result


def test_slack_delivery_test_routes_to_slack_broker_with_audits():
    setups, audit, delivery = Setups(), Audit(), SlackDelivery()
    HostedChannelSetupService(setups, audit, Writer(), delivery).test_delivery(
        CONTEXT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        provider="slack",
    )
    assert setups.calls[0][1] == {"principal_id": PRINCIPAL, "provider": "slack"}
    assert delivery.calls == [(
        "slack_test",
        {"destination_id": UUID("10000000-0000-4000-8000-000000000107")},
    )]
    assert [item[1]["outcome"] for item in audit.calls] == ["allowed", "observed"]


def test_slack_install_completion_binds_session_tenant_and_audits_outcome():
    setups, audit, delivery = Setups(), Audit(), SlackDelivery()
    assert HostedChannelSetupService(
        setups, audit, Writer(), delivery
    ).complete_slack_install(
        CONTEXT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        state="x" * 43,
        code="code-123",
    )
    assert delivery.calls == [(
        "install",
        {
            "state": "x" * 43,
            "code": "code-123",
            "tenant_id": TENANT,
            "principal_id": PRINCIPAL,
        },
    )]
    assert [item[1]["outcome"] for item in audit.calls] == ["allowed", "observed"]
    assert all(
        item[1]["action"] == "hosted.channels.slack.install.callback"
        for item in audit.calls
    )


def test_slack_install_failure_records_failed_outcome_and_returns_false():
    setups, audit = Setups(), Audit()
    delivery = SlackDelivery(result=False)
    assert not HostedChannelSetupService(
        setups, audit, Writer(), delivery
    ).complete_slack_install(
        CONTEXT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        state="x" * 43,
        code="code-123",
    )
    assert [item[1]["outcome"] for item in audit.calls] == ["allowed", "failed"]


def test_slack_install_pre_effect_audit_failure_never_contacts_broker():
    delivery = SlackDelivery()
    with pytest.raises(RuntimeError, match="pre-effect"):
        HostedChannelSetupService(
            Setups(), Audit(), Writer((False,)), delivery
        ).complete_slack_install(
            CONTEXT,
            principal_id=PRINCIPAL,
            session_id=SESSION,
            state="x" * 43,
            code="code-123",
        )
    assert delivery.calls == []


def test_slack_disconnect_records_provider_metadata():
    setups, audit = Setups(), Audit()
    HostedChannelSetupService(setups, audit, Writer()).disconnect(
        CONTEXT,
        principal_id=PRINCIPAL,
        session_id=SESSION,
        provider="slack",
    )
    disconnects = [
        item for item in audit.calls
        if item[1]["action"] == "hosted.channels.destination.disconnect"
    ]
    assert disconnects
    assert all(
        item[1]["metadata"] == {"schema_version": 1, "provider": "slack"}
        for item in disconnects
    )
