"""Envelope encryption for hosted connector credentials."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import UUID

MAX_CREDENTIAL_BYTES = 65_536


class KeyWrapper(Protocol):
    key_resource: str

    def wrap(self, plaintext_dek: bytes) -> bytes: ...

    def unwrap(self, wrapped_dek: bytes) -> bytes: ...


@dataclass(frozen=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes
    wrapped_dek: bytes
    key_resource: str
    format_version: int = 1


class EnvelopeCipher:
    """Use one random AES-256-GCM DEK per credential version."""

    def __init__(self, wrapper: KeyWrapper):
        if not wrapper.key_resource:
            raise ValueError("a KMS key resource is required")
        self._wrapper = wrapper

    def encrypt(
        self,
        credential: Mapping[str, Any],
        *,
        tenant_id: UUID,
        connector_id: UUID,
        provider: str,
        credential_version: int,
    ) -> EncryptedCredential:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = _credential_json(credential)
        aad = _associated_data(
            tenant_id, connector_id, provider, credential_version
        )
        dek = bytearray(os.urandom(32))
        nonce = os.urandom(12)
        try:
            ciphertext = AESGCM(bytes(dek)).encrypt(nonce, plaintext, aad)
            wrapped = self._wrapper.wrap(bytes(dek))
        finally:
            dek[:] = bytes(len(dek))
        return EncryptedCredential(
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_dek=wrapped,
            key_resource=self._wrapper.key_resource,
        )

    def decrypt(
        self,
        encrypted: EncryptedCredential,
        *,
        tenant_id: UUID,
        connector_id: UUID,
        provider: str,
        credential_version: int,
    ) -> dict[str, Any]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if encrypted.format_version != 1:
            raise ValueError("unsupported credential encryption format")
        if encrypted.key_resource != self._wrapper.key_resource:
            raise ValueError("credential KMS key does not match this broker")
        dek = bytearray(self._wrapper.unwrap(encrypted.wrapped_dek))
        if len(dek) != 32:
            dek[:] = bytes(len(dek))
            raise ValueError("unwrapped credential DEK must be 32 bytes")
        try:
            plaintext = AESGCM(bytes(dek)).decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                _associated_data(
                    tenant_id, connector_id, provider, credential_version
                ),
            )
        finally:
            dek[:] = bytes(len(dek))
        parsed = json.loads(plaintext)
        if not isinstance(parsed, dict):
            raise ValueError("credential payload must be an object")
        return parsed


class GoogleKmsKeyWrapper:
    """Wrap DEKs with Cloud KMS and verify CRC32C request/response integrity."""

    def __init__(self, key_resource: str, client: Any | None = None):
        if not key_resource.startswith("projects/") or "/cryptoKeys/" not in key_resource:
            raise ValueError("a full Cloud KMS CryptoKey resource is required")
        if client is None:
            from google.cloud import kms_v1

            client = kms_v1.KeyManagementServiceClient()
        self.key_resource = key_resource
        self._client = client

    def wrap(self, plaintext_dek: bytes) -> bytes:
        from google.protobuf.wrappers_pb2 import Int64Value

        checksum = _crc32c(plaintext_dek)
        response = self._client.encrypt(
            request={
                "name": self.key_resource,
                "plaintext": plaintext_dek,
                "plaintext_crc32c": Int64Value(value=checksum),
            }
        )
        if not response.verified_plaintext_crc32c:
            raise RuntimeError("KMS did not verify the DEK checksum")
        if _checksum_value(response.ciphertext_crc32c) != _crc32c(
            response.ciphertext
        ):
            raise RuntimeError("KMS wrapped-DEK checksum mismatch")
        return bytes(response.ciphertext)

    def unwrap(self, wrapped_dek: bytes) -> bytes:
        from google.protobuf.wrappers_pb2 import Int64Value

        response = self._client.decrypt(
            request={
                "name": self.key_resource,
                "ciphertext": wrapped_dek,
                "ciphertext_crc32c": Int64Value(value=_crc32c(wrapped_dek)),
            }
        )
        if _checksum_value(response.plaintext_crc32c) != _crc32c(
            response.plaintext
        ):
            raise RuntimeError("KMS unwrapped-DEK checksum mismatch")
        return bytes(response.plaintext)


def _credential_json(value: Mapping[str, Any]) -> bytes:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("credential payload must be a non-empty object")
    encoded = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(encoded) > MAX_CREDENTIAL_BYTES:
        raise ValueError("credential payload exceeds the size limit")
    return encoded


def _associated_data(
    tenant_id: UUID, connector_id: UUID, provider: str, version: int
) -> bytes:
    if not isinstance(tenant_id, UUID) or not isinstance(connector_id, UUID):
        raise TypeError("tenant and connector identifiers must be UUIDs")
    if not provider or len(provider) > 40 or ":" in provider:
        raise ValueError("invalid credential provider")
    if version < 1:
        raise ValueError("credential version must be positive")
    return (
        f"attune-credential-v1:{tenant_id}:{connector_id}:{provider}:{version}"
    ).encode()


def _crc32c(value: bytes) -> int:
    import google_crc32c

    return int.from_bytes(google_crc32c.Checksum(value).digest(), "big")


def _checksum_value(value: Any) -> int:
    """Normalize protobuf-plus scalar and wrapper checksum representations."""

    candidate = value if isinstance(value, int) and not isinstance(value, bool) else None
    if candidate is None:
        candidate = getattr(value, "value", None)
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        raise RuntimeError("KMS response checksum is unavailable")
    return candidate
