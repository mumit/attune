from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from attune.hosted.secret_audit import SecretBrokerAudit, _digest
from attune.hosted.tenant import TenantContext
from attune.hosted.vault import LeasedCredentialIntent

INTENT = UUID("10000000-0000-4000-8000-000000000511")
TENANT = UUID("10000000-0000-4000-8000-000000000512")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000513")
AUDIT_INTENT = UUID("10000000-0000-4000-8000-000000000514")


class Producer:
    def __init__(self):
        self.calls = []

    def request(self, context, **event):
        self.calls.append((context, event))
        return SimpleNamespace(id=AUDIT_INTENT)


class Writer:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def write(self, intent_id):
        self.calls.append(intent_id)
        return self.result


def leased_intent():
    return LeasedCredentialIntent(
        INTENT,
        TenantContext(TENANT),
        CONNECTOR,
        "google",
        "install",
        "connector.manage",
        None,
        None,
        None,
    )


def test_secret_audit_derives_content_free_tenant_bound_intent():
    producer, writer = Producer(), Writer()
    audit = SecretBrokerAudit(producer, writer)
    assert audit.record(
        leased_intent(),
        action="credential.install",
        outcome="allowed",
    )
    assert writer.calls == [AUDIT_INTENT]
    assert producer.calls == [
        (
            TenantContext(TENANT),
            {
                "idempotency_key": _digest(
                    f"attune-secret-audit-v1:{INTENT}:credential.install:allowed"
                ),
                "actor_type": "workload",
                "action": "credential.install",
                "outcome": "allowed",
                "target_type": "connector",
                "target_ref_hash": _digest(
                    f"attune-connector-v1:{CONNECTOR}"
                ),
            },
        )
    ]
