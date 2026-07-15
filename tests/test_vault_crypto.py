from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from attune.hosted.vault_crypto import (
    EnvelopeCipher,
    GoogleKmsKeyWrapper,
    _crc32c,
)

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


class KmsClient:
    def __init__(self, *, wrapped_checksums=False):
        self.wrapped_checksums = wrapped_checksums

    def _checksum(self, value):
        checksum = _crc32c(value)
        return SimpleNamespace(value=checksum) if self.wrapped_checksums else checksum

    def encrypt(self, request):
        plaintext = request["plaintext"]
        ciphertext = b"wrapped:" + plaintext
        return SimpleNamespace(
            verified_plaintext_crc32c=True,
            ciphertext=ciphertext,
            ciphertext_crc32c=self._checksum(ciphertext),
        )

    def decrypt(self, request):
        plaintext = request["ciphertext"].removeprefix(b"wrapped:")
        return SimpleNamespace(
            plaintext=plaintext,
            plaintext_crc32c=self._checksum(plaintext),
        )


@pytest.mark.parametrize("wrapped_checksums", [False, True])
def test_google_kms_wrapper_accepts_client_checksum_representations(
    wrapped_checksums,
):
    wrapper = GoogleKmsKeyWrapper(
        Wrapper.key_resource,
        client=KmsClient(wrapped_checksums=wrapped_checksums),
    )
    plaintext = bytes(range(32))
    assert wrapper.unwrap(wrapper.wrap(plaintext)) == plaintext
