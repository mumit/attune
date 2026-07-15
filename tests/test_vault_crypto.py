from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.vault_crypto import EnvelopeCipher

TENANT = UUID("10000000-0000-4000-8000-000000000201")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000202")


class Wrapper:
    key_resource = "projects/test/locations/test/keyRings/test/cryptoKeys/connectors"

    def wrap(self, value):
        return bytes(byte ^ 0xA5 for byte in value)

    def unwrap(self, value):
        return bytes(byte ^ 0xA5 for byte in value)


def test_envelope_cipher_round_trip_and_fresh_deks():
    cipher = EnvelopeCipher(Wrapper())
    credential = {"refresh_token": "restricted", "scopes": ["gmail.readonly"]}
    first = cipher.encrypt(
        credential,
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        provider="google",
        credential_version=1,
    )
    second = cipher.encrypt(
        credential,
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        provider="google",
        credential_version=1,
    )
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_dek != second.wrapped_dek
    assert cipher.decrypt(
        first,
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        provider="google",
        credential_version=1,
    ) == credential


@pytest.mark.parametrize("field", ["tenant", "connector", "provider", "version"])
def test_authenticated_context_rejects_ciphertext_substitution(field):
    cipher = EnvelopeCipher(Wrapper())
    encrypted = cipher.encrypt(
        {"token": "restricted"},
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        provider="google",
        credential_version=1,
    )
    values = {
        "tenant_id": TENANT,
        "connector_id": CONNECTOR,
        "provider": "google",
        "credential_version": 1,
    }
    values[field + "_id" if field in {"tenant", "connector"} else field] = {
        "tenant": UUID("20000000-0000-4000-8000-000000000201"),
        "connector": UUID("20000000-0000-4000-8000-000000000202"),
        "provider": "slack",
        "version": 2,
    }[field]
    with pytest.raises(Exception):
        cipher.decrypt(encrypted, **values)


def test_cipher_refuses_empty_or_oversized_credentials():
    cipher = EnvelopeCipher(Wrapper())
    with pytest.raises(ValueError):
        cipher.encrypt(
            {}, tenant_id=TENANT, connector_id=CONNECTOR,
            provider="google", credential_version=1,
        )
    with pytest.raises(ValueError):
        cipher.encrypt(
            {"token": "x" * 70_000}, tenant_id=TENANT,
            connector_id=CONNECTOR, provider="google", credential_version=1,
        )
