from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from attune.hosted.google_provider import (
    CalendarEventSummary,
    GmailProfile,
    GmailThreadSummary,
    ProviderFailure,
)
from attune.hosted.secret_broker import SecretBroker
from attune.hosted.tenant import TenantContext
from attune.hosted.vault import LeasedCredentialIntent
from attune.hosted.vault_crypto import EncryptedCredential

INTENT = UUID("10000000-0000-4000-8000-000000000601")
TENANT = UUID("10000000-0000-4000-8000-000000000602")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000603")
ENCRYPTED = EncryptedCredential(b"ciphertext-with-tag", bytes(12), b"dek", "key")


def use_intent(capability="google.gmail.profile.read"):
    return LeasedCredentialIntent(
        INTENT, TenantContext(TENANT), CONNECTOR, "google", "use",
        capability, UUID("10000000-0000-4000-8000-000000000604"), 2, ENCRYPTED,
    )


class Vault:
    def __init__(self, leased=None, finalize=True):
        self.leased = leased or use_intent()
        self.finalize_result = finalize
        self.finalized = []

    def lease(self, *args, **kwargs):
        return self.leased

    def finalize(self, intent_id, **kwargs):
        self.finalized.append((intent_id, kwargs))
        return self.finalize_result


class Cipher:
    def __init__(self):
        self.calls = []

    def decrypt(self, encrypted, **context):
        self.calls.append((encrypted, context))
        return {"refresh_token": "secret"}


class Google:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def gmail_profile(self, credential):
        self.calls.append(credential)
        if self.error:
            raise self.error
        return GmailProfile("99", 8, 5)

    def calendar_primary(self, credential):
        self.calls.append(credential)
        if self.error:
            raise self.error

    def gmail_threads(self, credential, **kwargs):
        self.calls.append((credential, kwargs))
        return (GmailThreadSummary("thread_1", "Subject", "From", "Date", "Snippet"),)

    def calendar_events(self, credential, **kwargs):
        self.calls.append((credential, kwargs))
        return (CalendarEventSummary(
            "event_1", "Appointment", "start", "end", "Office", "confirmed"
        ),)


class Audit:
    def __init__(self, results=None):
        self.results = iter(results or [True, True])
        self.events = []

    def record(self, intent, **event):
        self.events.append(event)
        return next(self.results)


def test_profile_use_decrypts_after_audit_and_consumes_intent():
    vault, cipher, google, audit = Vault(), Cipher(), Google(), Audit()
    result = SecretBroker(
        vault=vault, cipher=cipher, google=google, audit=audit
    ).google_gmail_profile(INTENT)
    assert result.status_code == 200
    assert result.body == {"history_id": "99", "messages_total": 8, "threads_total": 5}
    assert [event["outcome"] for event in audit.events] == ["allowed", "observed"]
    assert cipher.calls[0][1] == {
        "tenant_id": TENANT,
        "connector_id": CONNECTOR,
        "provider": "google",
        "credential_version": 2,
    }
    assert vault.finalized == [
        (INTENT, {"producer_kind": "worker", "outcome": "consumed"})
    ]


def test_wrong_capability_is_audited_denied_without_decrypting():
    vault, cipher, audit = Vault(use_intent("gmail.send")), Cipher(), Audit([True])
    result = SecretBroker(
        vault=vault, cipher=cipher, google=Google(), audit=audit
    ).google_gmail_profile(INTENT)
    assert result.status_code == 404
    assert cipher.calls == []
    assert audit.events[0]["outcome"] == "denied"
    assert vault.finalized[0][1]["outcome"] == "failed"


def test_provider_failure_is_content_free_audited_and_finalized():
    vault, audit = Vault(), Audit([True, True])
    result = SecretBroker(
        vault=vault,
        cipher=Cipher(),
        google=Google(ProviderFailure("provider secret")),
        audit=audit,
    ).google_gmail_profile(INTENT)
    assert result.status_code == 502 and result.body is None
    assert [event["outcome"] for event in audit.events] == ["allowed", "failed"]
    assert vault.finalized[0][1]["outcome"] == "failed"


def test_post_effect_audit_or_finalize_failure_never_returns_provider_result():
    vault = Vault(finalize=False)
    result = SecretBroker(
        vault=vault, cipher=Cipher(), google=Google(), audit=Audit()
    ).google_gmail_profile(INTENT)
    assert result.status_code == 503 and result.body is None

    vault = Vault()
    result = SecretBroker(
        vault=vault, cipher=Cipher(), google=Google(), audit=Audit([True, False])
    ).google_gmail_profile(INTENT)
    assert result.status_code == 503 and vault.finalized == []


def test_calendar_use_is_separately_authorized_and_returns_no_provider_data():
    vault = Vault(use_intent("google.calendar.primary.read"))
    google, audit = Google(), Audit()
    result = SecretBroker(
        vault=vault, cipher=Cipher(), google=google, audit=audit
    ).google_calendar_primary(INTENT)
    assert result.status_code == 204 and result.body is None
    assert google.calls == [{"refresh_token": "secret"}]
    assert [event for event in audit.events] == [
        {
            "action": "credential.use.google.calendar.primary.read",
            "outcome": "allowed",
        },
        {
            "action": "credential.use.google.calendar.primary.read",
            "outcome": "observed",
        },
    ]
    assert vault.finalized[0][1]["outcome"] == "consumed"


def test_calendar_failure_is_content_free_audited_and_finalized():
    vault = Vault(use_intent("google.calendar.primary.read"))
    result = SecretBroker(
        vault=vault,
        cipher=Cipher(),
        google=Google(ProviderFailure("provider secret")),
        audit=Audit([True, True]),
    ).google_calendar_primary(INTENT)
    assert result.status_code == 502 and result.body is None
    assert vault.finalized[0][1]["outcome"] == "failed"


def test_conversation_reads_are_separate_capabilities_and_bounded_results():
    gmail_vault = Vault(use_intent("google.gmail.threads.read"))
    gmail = SecretBroker(
        vault=gmail_vault, cipher=Cipher(), google=Google(), audit=Audit()
    ).google_gmail_threads(INTENT, query="newer_than:7d", limit=10)
    assert gmail.status_code == 200
    assert gmail.body == {"threads": [{
        "thread_id": "thread_1", "subject": "Subject", "sender": "From",
        "date": "Date", "snippet": "Snippet",
    }]}

    calendar_vault = Vault(use_intent("google.calendar.events.read"))
    lower = datetime(2026, 7, 16, tzinfo=timezone.utc)
    calendar = SecretBroker(
        vault=calendar_vault, cipher=Cipher(), google=Google(), audit=Audit()
    ).google_calendar_events(
        INTENT, time_min=lower,
        time_max=datetime(2026, 7, 18, tzinfo=timezone.utc), limit=25,
    )
    assert calendar.status_code == 200
    assert calendar.body["events"][0]["summary"] == "Appointment"
