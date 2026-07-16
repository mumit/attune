import base64
from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.channel_broker import (
    ChannelReferenceHasher,
    ClaimedGoogleChatLink,
    GoogleChatLinkBroker,
    LinkedGoogleChatDestination,
    decode_channel_reference_key,
)

NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)
CODE = "A" * 43
PRE_AUDIT = UUID("10000000-0000-4000-8000-000000000101")
OUTCOME_AUDIT = UUID("10000000-0000-4000-8000-000000000102")


class Repository:
    def __init__(self):
        self.claims = []
        self.releases = []
        self.consumes = []

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

    def consume(self, **kwargs):
        self.consumes.append(kwargs)
        return LinkedGoogleChatDestination(
            UUID("10000000-0000-4000-8000-000000000104"),
            UUID("10000000-0000-4000-8000-000000000105"),
            UUID("10000000-0000-4000-8000-000000000106"),
            UUID("10000000-0000-4000-8000-000000000107"),
            "pending_test",
            OUTCOME_AUDIT,
        )


class Writer:
    def __init__(self, results=(True, True)):
        self.results = iter(results)
        self.calls = []

    def write(self, intent_id):
        self.calls.append(intent_id)
        return next(self.results)


def broker(repository=None, writer=None):
    return GoogleChatLinkBroker(
        repository or Repository(),
        writer or Writer(),
        ChannelReferenceHasher(b"k" * 32),
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
