from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.slack_channel_broker import (
    AcceptedSlackMessage,
    ClaimedSlackConversationDelivery,
    ClaimedSlackDelivery,
    ClaimedSlackInstall,
    CompletedSlackConversationDelivery,
    CompletedSlackDelivery,
    InstalledSlackDestination,
    SlackInstallBroker,
    SlackReferenceHasher,
)
from attune.hosted.slack_provider import SlackInstallation
from attune.hosted.vault_crypto import EncryptedCredential, EnvelopeCipher

NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)
STATE = "A" * 43
TENANT = UUID("10000000-0000-4000-8000-000000000104")
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000105")
DESTINATION = UUID("10000000-0000-4000-8000-000000000107")
CONVERSATION_JOB = UUID("10000000-0000-4000-8000-000000000112")
PRE_AUDIT = UUID("10000000-0000-4000-8000-000000000101")
OUTCOME_AUDIT = UUID("10000000-0000-4000-8000-000000000102")
DELIVERY_PRE_AUDIT = UUID("10000000-0000-4000-8000-000000000108")
DELIVERY_OUTCOME_AUDIT = UUID("10000000-0000-4000-8000-000000000109")
MESSAGE_AUDIT = UUID("10000000-0000-4000-8000-000000000110")
MESSAGE_DISPATCH = UUID("10000000-0000-4000-8000-000000000111")


class Wrapper:
    key_resource = "projects/test/locations/test/keyRings/test/cryptoKeys/test"

    def wrap(self, value):
        return value

    def unwrap(self, value):
        return value


CIPHER = EnvelopeCipher(Wrapper())
ROUTE_ENCRYPTED = CIPHER.encrypt(
    {"team": "T0123456789", "channel": "D0123456789"},
    tenant_id=TENANT,
    connector_id=DESTINATION,
    provider="slack_route",
    credential_version=1,
)
TOKEN_ENCRYPTED = CIPHER.encrypt(
    {"bot_token": "xoxb-1234567890-abcdefghij"},
    tenant_id=TENANT,
    connector_id=DESTINATION,
    provider="slack_bot_token",
    credential_version=1,
)


class Repository:
    def __init__(self):
        self.claims = []
        self.releases = []
        self.consumes = []
        self.delivery_claims = []
        self.delivery_completions = []
        self.messages = []
        self.reply_claims = []
        self.reply_completions = []

    def claim(self, **kwargs):
        self.claims.append(kwargs)
        return ClaimedSlackInstall(
            UUID("10000000-0000-4000-8000-000000000103"),
            TENANT,
            PRINCIPAL,
            PRE_AUDIT,
        )

    def release(self, **kwargs):
        self.releases.append(kwargs)
        return True

    def resolve_destination_id(self, **kwargs):
        return kwargs["candidate_id"]

    def consume(self, **kwargs):
        self.consumes.append(kwargs)
        return InstalledSlackDestination(
            TENANT,
            PRINCIPAL,
            UUID("10000000-0000-4000-8000-000000000106"),
            kwargs["destination_id"],
            "pending_test",
            OUTCOME_AUDIT,
        )

    def claim_delivery(self, **kwargs):
        self.delivery_claims.append(kwargs)
        return ClaimedSlackDelivery(
            TENANT, PRINCIPAL, ROUTE_ENCRYPTED, TOKEN_ENCRYPTED, DELIVERY_PRE_AUDIT
        )

    def complete_delivery(self, **kwargs):
        self.delivery_completions.append(kwargs)
        return CompletedSlackDelivery(
            "active" if kwargs["succeeded"] else "pending_test",
            DELIVERY_OUTCOME_AUDIT,
        )

    def accept_message(self, **kwargs):
        self.messages.append(kwargs)
        return AcceptedSlackMessage(MESSAGE_DISPATCH, MESSAGE_AUDIT, True)

    def claim_conversation_delivery(self, **kwargs):
        self.reply_claims.append(kwargs)
        return ClaimedSlackConversationDelivery(
            TENANT, ROUTE_ENCRYPTED, TOKEN_ENCRYPTED,
            "Canonical assistant response", DELIVERY_PRE_AUDIT, False,
        )

    def complete_conversation_delivery(self, **kwargs):
        self.reply_completions.append(kwargs)
        return CompletedSlackConversationDelivery(
            "delivered" if kwargs["succeeded"] else "failed",
            DELIVERY_OUTCOME_AUDIT,
        )


class Writer:
    def __init__(self, results=(True, True)):
        self.results = iter(results)
        self.calls = []

    def write(self, intent_id):
        self.calls.append(intent_id)
        return next(self.results)


class Provider:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def exchange_code(self, **kwargs):
        self.calls.append(("exchange", kwargs))
        if self.error:
            raise self.error
        return SlackInstallation(
            "A0123456789", "T0123456789", "U0123456789", "U0BOT456789",
            "xoxb-1234567890-abcdefghij",
        )

    def open_owner_dm(self, **kwargs):
        self.calls.append(("open_dm", kwargs))
        if self.error:
            raise self.error
        return "D0123456789"

    def send_connection_test(self, **kwargs):
        self.calls.append(("test", kwargs))
        if self.error:
            raise self.error
        return "1752600000.000300"

    def send_message(self, **kwargs):
        self.calls.append(("message", kwargs))
        if self.error:
            raise self.error
        return "1752600000.000400"


def broker(repository=None, writer=None, provider=None):
    return SlackInstallBroker(
        repository or Repository(),
        writer or Writer(),
        SlackReferenceHasher(b"k" * 32),
        CIPHER,
        provider or Provider(),
        redirect_uri="https://dev.attune.example/v1/onboarding/channel-installations/slack/callback",
    )


def test_install_exchanges_once_hashes_references_and_writes_both_audits():
    repository, writer, provider = Repository(), Writer(), Provider()
    result = broker(repository, writer, provider).install(
        state=STATE, code="code-123",
        owner_tenant_id=TENANT, owner_principal_id=PRINCIPAL, now=NOW,
    )
    assert result.destination_status == "pending_test"
    assert writer.calls == [PRE_AUDIT, OUTCOME_AUDIT]
    assert provider.calls[0][0] == "exchange"
    assert provider.calls[1] == (
        "open_dm",
        {"bot_token": "xoxb-1234567890-abcdefghij", "user_id": "U0123456789"},
    )
    consumed = repository.consumes[0]
    assert consumed["owner_tenant_id"] == TENANT
    assert consumed["owner_principal_id"] == PRINCIPAL
    assert len({
        consumed["installation_ref_hash"],
        consumed["actor_ref_hash"],
        consumed["destination_ref_hash"],
    }) == 3
    route = CIPHER.decrypt(
        consumed["encrypted_route"], tenant_id=TENANT,
        connector_id=consumed["destination_id"], provider="slack_route",
        credential_version=1,
    )
    assert route == {"team": "T0123456789", "channel": "D0123456789"}
    token = CIPHER.decrypt(
        consumed["encrypted_token"], tenant_id=TENANT,
        connector_id=consumed["destination_id"], provider="slack_bot_token",
        credential_version=1,
    )
    assert token == {"bot_token": "xoxb-1234567890-abcdefghij"}
    assert repository.releases == []


def test_install_binding_mismatch_releases_claim_before_any_provider_call():
    repository, provider = Repository(), Provider()
    with pytest.raises(RuntimeError, match="binding"):
        broker(repository, Writer(), provider).install(
            state=STATE, code="code-123",
            owner_tenant_id=UUID("20000000-0000-4000-8000-000000000104"),
            owner_principal_id=PRINCIPAL, now=NOW,
        )
    assert provider.calls == []
    assert len(repository.releases) == 1
    assert repository.consumes == []


def test_install_pre_effect_audit_failure_releases_claim_and_never_exchanges():
    repository, provider = Repository(), Provider()
    with pytest.raises(RuntimeError, match="pre-effect"):
        broker(repository, Writer((False,)), provider).install(
            state=STATE, code="code-123",
            owner_tenant_id=TENANT, owner_principal_id=PRINCIPAL, now=NOW,
        )
    assert provider.calls == []
    assert len(repository.releases) == 1
    assert repository.consumes == []


def test_install_provider_failure_releases_claim():
    repository = Repository()
    with pytest.raises(RuntimeError):
        broker(repository, Writer(), Provider(RuntimeError("provider"))).install(
            state=STATE, code="code-123",
            owner_tenant_id=TENANT, owner_principal_id=PRINCIPAL, now=NOW,
        )
    assert len(repository.releases) == 1
    assert repository.consumes == []


def test_install_rejects_noncanonical_state_or_code():
    with pytest.raises(ValueError):
        broker().install(
            state="short", code="code",
            owner_tenant_id=TENANT, owner_principal_id=PRINCIPAL, now=NOW,
        )
    with pytest.raises(ValueError):
        broker().install(
            state=STATE, code="",
            owner_tenant_id=TENANT, owner_principal_id=PRINCIPAL, now=NOW,
        )


def test_fixed_delivery_decrypts_route_and_token_sends_once_and_activates():
    repository, writer, provider = Repository(), Writer(), Provider()
    result = broker(repository, writer, provider).test_delivery(
        destination_id=DESTINATION, now=NOW
    )
    assert result.destination_status == "active"
    assert writer.calls == [DELIVERY_PRE_AUDIT, DELIVERY_OUTCOME_AUDIT]
    assert provider.calls == [(
        "test",
        {"bot_token": "xoxb-1234567890-abcdefghij", "channel": "D0123456789"},
    )]
    assert repository.delivery_completions[-1]["succeeded"] is True


def test_delivery_failure_remains_pending_and_never_claims_success():
    repository = Repository()
    with pytest.raises(RuntimeError, match="provider"):
        broker(repository, Writer(), Provider(RuntimeError("provider failed"))).test_delivery(
            destination_id=DESTINATION, now=NOW
        )
    assert repository.delivery_completions[-1]["succeeded"] is False


def test_message_acceptance_hashes_all_provider_authority_and_writes_audit():
    repository, writer = Repository(), Writer((True,))
    accepted = broker(repository, writer).accept_message(
        team_ref="teams/T0123456789",
        actor_ref="teams/T0123456789/users/U0123456789",
        destination_ref="teams/T0123456789/channels/D0123456789",
        message_ref="teams/T0123456789/channels/D0123456789/messages/1752600000.000100",
        text="what is on my calendar?",
    )
    assert accepted.dispatch_intent_id == MESSAGE_DISPATCH
    assert writer.calls == [MESSAGE_AUDIT]
    call = repository.messages[0]
    assert call["text"] == "what is on my calendar?"
    assert len({
        call["installation_ref_hash"], call["actor_ref_hash"],
        call["destination_ref_hash"], call["message_ref_hash"],
    }) == 4


def test_reference_hashes_are_domain_separated_from_google_chat():
    from attune.hosted.channel_broker import ChannelReferenceHasher

    slack = SlackReferenceHasher(b"k" * 32)
    with pytest.raises(ValueError):
        slack.hash("destination", "spaces/AAAA-test")
    with pytest.raises(ValueError):
        ChannelReferenceHasher(b"k" * 32).hash(
            "destination", "teams/T0123456789/channels/D0123456789"
        )


def test_conversation_reply_uses_canonical_stored_text_and_hashes_provider_ts():
    repository, writer, provider = Repository(), Writer(), Provider()
    assert broker(repository, writer, provider).deliver_reply(
        destination_id=DESTINATION, job_id=CONVERSATION_JOB, now=NOW
    )
    kind, call = provider.calls[0]
    assert kind == "message"
    assert call["channel"] == "D0123456789"
    assert call["text"] == "Canonical assistant response"
    assert repository.reply_completions[0]["succeeded"] is True
    assert len(repository.reply_completions[0]["provider_message_ref_hash"]) == 32
    assert writer.calls == [DELIVERY_PRE_AUDIT, DELIVERY_OUTCOME_AUDIT]


def test_conversation_reply_failure_records_failed_completion():
    repository = Repository()
    with pytest.raises(RuntimeError):
        broker(repository, Writer(), Provider(RuntimeError("provider failed"))).deliver_reply(
            destination_id=DESTINATION, job_id=CONVERSATION_JOB, now=NOW
        )
    assert repository.reply_completions[-1]["succeeded"] is False
    assert repository.reply_completions[-1]["provider_message_ref_hash"] is None
