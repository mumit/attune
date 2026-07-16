import base64
from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.channel_broker import (
    ChannelReferenceHasher,
    ClaimedGoogleChatDelivery,
    ClaimedGoogleChatLink,
    CompletedGoogleChatDelivery,
    GoogleChatLinkBroker,
    LinkedGoogleChatDestination,
    decode_channel_reference_key,
)
from attune.hosted.vault_crypto import EncryptedCredential, EnvelopeCipher

NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)
CODE = "A" * 43
PRE_AUDIT = UUID("10000000-0000-4000-8000-000000000101")
OUTCOME_AUDIT = UUID("10000000-0000-4000-8000-000000000102")
DELIVERY_PRE_AUDIT = UUID("10000000-0000-4000-8000-000000000108")
DELIVERY_OUTCOME_AUDIT = UUID("10000000-0000-4000-8000-000000000109")


class Repository:
    def __init__(self):
        self.claims = []
        self.releases = []
        self.consumes = []
        self.delivery_claims = []
        self.delivery_completions = []

    def claim(self, **kwargs):
        self.claims.append(kwargs)
        return ClaimedGoogleChatLink(
            UUID("10000000-0000-4000-8000-000000000103"),
            UUID("10000000-0000-4000-8000-000000000104"),
            UUID("10000000-0000-4000-8000-000000000105"),
            PRE_AUDIT,
        )

    def release(self, **kwargs):
        self.releases.append(kwargs)
        return True

    def resolve_destination_id(self, **kwargs):
        return kwargs["candidate_id"]

    def consume(self, **kwargs):
        self.consumes.append(kwargs)
        return LinkedGoogleChatDestination(
            UUID("10000000-0000-4000-8000-000000000104"),
            UUID("10000000-0000-4000-8000-000000000105"),
            UUID("10000000-0000-4000-8000-000000000106"),
            kwargs["destination_id"],
            "pending_test",
            OUTCOME_AUDIT,
        )

    def claim_delivery(self, **kwargs):
        self.delivery_claims.append(kwargs)
        return ClaimedGoogleChatDelivery(
            UUID("10000000-0000-4000-8000-000000000104"),
            UUID("10000000-0000-4000-8000-000000000105"),
            kwargs.get("encrypted", TEST_ENCRYPTED),
            DELIVERY_PRE_AUDIT,
        )

    def complete_delivery(self, **kwargs):
        self.delivery_completions.append(kwargs)
        return CompletedGoogleChatDelivery(
            "active" if kwargs["succeeded"] else "pending_test",
            DELIVERY_OUTCOME_AUDIT,
        )


class Writer:
    def __init__(self, results=(True, True)):
        self.results = iter(results)
        self.calls = []

    def write(self, intent_id):
        self.calls.append(intent_id)
        return next(self.results)


class Wrapper:
    key_resource = "projects/test/locations/test/keyRings/test/cryptoKeys/test"

    def wrap(self, value):
        return value

    def unwrap(self, value):
        return value


class Sender:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def send_connection_test(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


DESTINATION = UUID("10000000-0000-4000-8000-000000000107")
CIPHER = EnvelopeCipher(Wrapper())
TEST_ENCRYPTED = CIPHER.encrypt(
    {"space": "spaces/AAAA-test"},
    tenant_id=UUID("10000000-0000-4000-8000-000000000104"),
    connector_id=DESTINATION,
    provider="google_chat_route",
    credential_version=1,
)


def broker(repository=None, writer=None, sender=None):
    return GoogleChatLinkBroker(
        repository or Repository(),
        writer or Writer(),
        ChannelReferenceHasher(b"k" * 32),
        CIPHER,
        sender or Sender(),
    )


@pytest.mark.parametrize("padding", [True, False])
def test_channel_reference_key_accepts_canonical_secret_manager_payload(padding):
    key = bytes(range(32))
    encoded = base64.b64encode(key)
    if not padding:
        encoded = encoded.rstrip(b"=")
    assert decode_channel_reference_key(b"\n " + encoded + b"\r\n") == key


@pytest.mark.parametrize(
    "encoded",
    [b"not base64", base64.b64encode(b"short"), base64.b64encode(bytes(32)) + b"="],
)
def test_channel_reference_key_rejects_invalid_or_noncanonical_payload(encoded):
    with pytest.raises(ValueError):
        decode_channel_reference_key(encoded)


def test_link_hashes_references_by_domain_and_writes_both_audits():
    repository, writer = Repository(), Writer()
    result = broker(repository, writer).link_owner_dm(
        link_code=CODE,
        app_ref="projects/624765747204",
        actor_ref="users/123456",
        destination_ref="spaces/AAAA-test",
        now=NOW,
    )
    assert result.destination_status == "pending_test"
    assert writer.calls == [PRE_AUDIT, OUTCOME_AUDIT]
    assert len(repository.claims[0]["secret_hash"]) == 32
    consumed = repository.consumes[0]
    assert len({
        consumed["installation_ref_hash"],
        consumed["actor_ref_hash"],
        consumed["destination_ref_hash"],
    }) == 3
    assert consumed["destination_id"] == result.destination_id
    assert consumed["encrypted"].ciphertext
    assert repository.releases == []


def test_pre_effect_audit_failure_releases_claim_and_never_consumes():
    repository = Repository()
    with pytest.raises(RuntimeError, match="pre-effect"):
        broker(repository, Writer((False,))).link_owner_dm(
            link_code=CODE,
            app_ref="projects/624765747204",
            actor_ref="users/123456",
            destination_ref="spaces/AAAA-test",
            now=NOW,
        )
    assert len(repository.releases) == 1
    assert repository.consumes == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("link_code", "short"),
        ("app_ref", "projects/not-a-number"),
        ("actor_ref", "spaces/wrong-kind"),
        ("destination_ref", "rooms/wrong-kind"),
    ],
)
def test_link_rejects_noncanonical_provider_references(field, value):
    values = {
        "link_code": CODE,
        "app_ref": "projects/624765747204",
        "actor_ref": "users/123456",
        "destination_ref": "spaces/AAAA-test",
    }
    values[field] = value
    with pytest.raises(ValueError):
        broker().link_owner_dm(**values, now=NOW)


def test_reference_hashes_are_keyed_and_domain_separated():
    first = ChannelReferenceHasher(b"a" * 32)
    second = ChannelReferenceHasher(b"b" * 32)
    value = "spaces/AAAA-test"
    assert first.hash("destination", value) != second.hash("destination", value)
    with pytest.raises(ValueError):
        first.hash("actor", value)


def test_fixed_delivery_decrypts_canonical_route_sends_once_and_activates():
    repository, writer, sender = Repository(), Writer(), Sender()
    result = broker(repository, writer, sender).test_delivery(
        destination_id=DESTINATION, now=NOW
    )
    assert result.destination_status == "active"
    assert writer.calls == [DELIVERY_PRE_AUDIT, DELIVERY_OUTCOME_AUDIT]
    assert sender.calls[0]["space"] == "spaces/AAAA-test"
    assert repository.delivery_completions[-1]["succeeded"] is True


def test_delivery_failure_remains_pending_and_never_claims_success():
    repository = Repository()
    with pytest.raises(RuntimeError, match="provider"):
        broker(repository, Writer(), Sender(RuntimeError("provider failed"))).test_delivery(
            destination_id=DESTINATION, now=NOW
        )
    assert repository.delivery_completions[-1]["succeeded"] is False
