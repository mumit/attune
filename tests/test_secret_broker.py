from __future__ import annotations

from uuid import UUID

from attune.hosted.secret_broker import SecretBroker
from attune.hosted.tenant import TenantContext
from attune.hosted.vault import LeasedCredentialIntent
from attune.hosted.vault_crypto import EncryptedCredential

INTENT = UUID("10000000-0000-4000-8000-000000000301")
TENANT = UUID("10000000-0000-4000-8000-000000000302")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000303")


def intent(operation="install", version=None):
    return LeasedCredentialIntent(
        INTENT, TenantContext(TENANT), CONNECTOR, "google", operation,
        "connector.manage", None, version, None,
    )


class Vault:
    def __init__(self, leased):
        self.leased = leased
        self.stored = []
        self.revoked = []

    def lease(self, *args, **kwargs):
        return self.leased

    def store(self, intent_id, encrypted):
        self.stored.append((intent_id, encrypted))
        return UUID("10000000-0000-4000-8000-000000000304"), 1

    def revoke(self, intent_id):
        self.revoked.append(intent_id)
        return True


class Cipher:
    def __init__(self):
        self.calls = []

    def encrypt(self, credential, **context):
        self.calls.append((credential, context))
        return EncryptedCredential(b"ciphertext-with-tag", bytes(12), b"dek", "key")


class Audit:
    def __init__(self, results=(True, True)):
        self.results = iter(results)
        self.events = []

    def record(self, intent, **event):
        self.events.append(event)
        return next(self.results)


class FailingAudit:
    def record(self, *args, **kwargs):
        raise RuntimeError("audit detail")


def test_install_audits_before_encrypting_and_storing():
    vault, cipher, audit = Vault(intent()), Cipher(), Audit()
    result = SecretBroker(vault=vault, cipher=cipher, audit=audit).install(
        INTENT, {"refresh_token": "restricted"}
    )
    assert result.status_code == 204
    assert [event["outcome"] for event in audit.events] == ["allowed", "observed"]
    assert cipher.calls[0][1] == {
        "tenant_id": TENANT,
        "connector_id": CONNECTOR,
        "provider": "google",
        "credential_version": 1,
    }
    assert vault.stored[0][0] == INTENT


def test_install_does_not_touch_secret_when_pre_audit_fails():
    vault, cipher = Vault(intent()), Cipher()
    result = SecretBroker(vault=vault, cipher=cipher, audit=Audit((False,))).install(
        INTENT, {"refresh_token": "restricted"}
    )
    assert result.status_code == 503
    assert cipher.calls == [] and vault.stored == []


def test_install_fails_closed_when_audit_raises():
    vault, cipher = Vault(intent()), Cipher()
    result = SecretBroker(vault=vault, cipher=cipher, audit=FailingAudit()).install(
        INTENT, {"refresh_token": "restricted"}
    )
    assert result.status_code == 503
    assert cipher.calls == [] and vault.stored == []


def test_revoke_requires_matching_intent_and_two_phase_audit():
    vault, audit = Vault(intent("revoke", 1)), Audit()
    result = SecretBroker(vault=vault, cipher=Cipher(), audit=audit).revoke(INTENT)
    assert result.status_code == 204
    assert vault.revoked == [INTENT]
    assert [event["outcome"] for event in audit.events] == ["allowed", "observed"]


def test_wrong_operation_is_not_accepted_by_endpoint():
    vault = Vault(intent("revoke", 1))
    result = SecretBroker(vault=vault, cipher=Cipher(), audit=Audit()).install(
        INTENT, {"refresh_token": "restricted"}
    )
    assert result.status_code == 404
    assert vault.stored == []
