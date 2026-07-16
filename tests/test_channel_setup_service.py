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
