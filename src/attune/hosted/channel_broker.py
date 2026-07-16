"""One-use private broker for verified hosted channel links."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID, uuid4

from .repositories import ConnectionFactory, _fixed_hash
from .vault_crypto import EncryptedCredential, EnvelopeCipher

_LINK_CODE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_APP_REF = re.compile(r"^projects/[1-9][0-9]{5,20}$")
_ACTOR_REF = re.compile(r"^users/[A-Za-z0-9._-]{1,180}$")
_DESTINATION_REF = re.compile(r"^spaces/[A-Za-z0-9_-]{1,180}$")


def decode_channel_reference_key(value: bytes) -> bytes:
    """Decode a canonical 32-byte base64 key with surrounding whitespace."""
    if not isinstance(value, bytes):
        raise ValueError("channel reference HMAC secret must be bytes")
    encoded = value.strip()
    padded = encoded + (b"=" * (-len(encoded) % 4))
    try:
        key = base64.b64decode(padded, altchars=b"-_", validate=True)
    except Exception as exc:
        raise ValueError("channel reference HMAC secret is invalid") from exc
    if len(key) != 32:
        raise ValueError("channel reference HMAC secret must encode 32 bytes")
    unpadded = encoded.rstrip(b"=")
    standard = base64.b64encode(key).rstrip(b"=")
    urlsafe = base64.urlsafe_b64encode(key).rstrip(b"=")
    if not (
        hmac.compare_digest(unpadded, standard)
        or hmac.compare_digest(unpadded, urlsafe)
    ):
        raise ValueError("channel reference HMAC secret is not canonical base64")
    return key


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


@dataclass(frozen=True)
class ClaimedGoogleChatLink:
    transaction_id: UUID
    tenant_id: UUID
    owner_principal_id: UUID
    pre_audit_intent_id: UUID


@dataclass(frozen=True)
class LinkedGoogleChatDestination:
    tenant_id: UUID
    owner_principal_id: UUID
    installation_id: UUID
    destination_id: UUID
    destination_status: str
    outcome_audit_intent_id: UUID


@dataclass(frozen=True)
class ClaimedGoogleChatDelivery:
    tenant_id: UUID
    owner_principal_id: UUID
    encrypted: EncryptedCredential
    pre_audit_intent_id: UUID


@dataclass(frozen=True)
class CompletedGoogleChatDelivery:
    destination_status: str
    outcome_audit_intent_id: UUID


class GoogleChatSender(Protocol):
    def send_connection_test(self, *, space: str, request_id: UUID) -> None: ...


class PostgresChannelBrokerRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def claim(
        self, *, secret_hash: bytes, claim_hash: bytes, expires_at: datetime
    ) -> ClaimedGoogleChatLink:
        _fixed_hash("secret_hash", secret_hash)
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("claim expiry must be timezone-aware")
        row = self._call(
            "SELECT * FROM attune.claim_google_chat_link(%s, %s, %s)",
            (secret_hash, claim_hash, expires_at),
        )
        return ClaimedGoogleChatLink(*row)

    def release(self, *, secret_hash: bytes, claim_hash: bytes) -> bool:
        _fixed_hash("secret_hash", secret_hash)
        _fixed_hash("claim_hash", claim_hash)
        row = self._call(
            "SELECT attune.release_google_chat_link_claim(%s, %s)",
            (secret_hash, claim_hash),
        )
        return row[0] is True

    def resolve_destination_id(
        self, *, secret_hash: bytes, claim_hash: bytes, candidate_id: UUID
    ) -> UUID:
        _fixed_hash("secret_hash", secret_hash)
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(candidate_id, UUID):
            raise TypeError("candidate_id must be a UUID")
        row = self._call(
            "SELECT attune.resolve_google_chat_link_destination(%s, %s, %s)",
            (secret_hash, claim_hash, candidate_id),
        )
        return row[0]

    def consume(
        self,
        *,
        secret_hash: bytes,
        claim_hash: bytes,
        installation_ref_hash: bytes,
        actor_ref_hash: bytes,
        destination_ref_hash: bytes,
        destination_id: UUID,
        encrypted: EncryptedCredential,
    ) -> LinkedGoogleChatDestination:
        for name, value in (
            ("secret_hash", secret_hash),
            ("claim_hash", claim_hash),
            ("installation_ref_hash", installation_ref_hash),
            ("actor_ref_hash", actor_ref_hash),
            ("destination_ref_hash", destination_ref_hash),
        ):
            _fixed_hash(name, value)
        if not isinstance(destination_id, UUID):
            raise TypeError("destination_id must be a UUID")
        row = self._call(
            """
            SELECT * FROM attune.consume_google_chat_link_v2(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                secret_hash,
                claim_hash,
                installation_ref_hash,
                actor_ref_hash,
                destination_ref_hash,
                destination_id,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.wrapped_dek,
                encrypted.key_resource,
                encrypted.format_version,
            ),
        )
        return LinkedGoogleChatDestination(*row)

    def claim_delivery(
        self, *, destination_id: UUID, claim_hash: bytes, expires_at: datetime
    ) -> ClaimedGoogleChatDelivery:
        if not isinstance(destination_id, UUID):
            raise TypeError("destination_id must be a UUID")
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("claim expiry must be timezone-aware")
        row = self._call(
            "SELECT * FROM attune.claim_google_chat_delivery_test(%s, %s, %s)",
            (destination_id, claim_hash, expires_at),
        )
        tenant_id, owner_id, ciphertext, nonce, wrapped, key, version, audit = row
        return ClaimedGoogleChatDelivery(
            tenant_id,
            owner_id,
            EncryptedCredential(ciphertext, nonce, wrapped, key, version),
            audit,
        )

    def complete_delivery(
        self, *, destination_id: UUID, claim_hash: bytes, succeeded: bool
    ) -> CompletedGoogleChatDelivery:
        if not isinstance(destination_id, UUID):
            raise TypeError("destination_id must be a UUID")
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(succeeded, bool):
            raise TypeError("succeeded must be boolean")
        row = self._call(
            "SELECT * FROM attune.complete_google_chat_delivery_test(%s, %s, %s)",
            (destination_id, claim_hash, succeeded),
        )
        return CompletedGoogleChatDelivery(*row)

    def _call(self, statement: str, values: tuple):
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(statement, values)
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("channel broker returned no state")
                connection.commit()
                return row
            except BaseException:
                connection.rollback()
                raise


class ChannelReferenceHasher:
    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("channel reference HMAC key must be exactly 32 bytes")
        self._key = key

    def hash(self, kind: str, value: str) -> bytes:
        patterns = {
            "installation": _APP_REF,
            "actor": _ACTOR_REF,
            "destination": _DESTINATION_REF,
        }
        pattern = patterns.get(kind)
        if pattern is None or not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError("invalid Google Chat reference")
        return hmac.new(
            self._key,
            b"attune-channel-ref-v1\0google_chat\0"
            + kind.encode("ascii")
            + b"\0"
            + value.encode("ascii"),
            hashlib.sha256,
        ).digest()


class GoogleChatLinkBroker:
    def __init__(
        self,
        repository: PostgresChannelBrokerRepository,
        audit_writer: AuditWriter,
        reference_hasher: ChannelReferenceHasher,
        cipher: EnvelopeCipher,
        sender: GoogleChatSender,
    ):
        self._repository = repository
        self._audit_writer = audit_writer
        self._reference_hasher = reference_hasher
        self._cipher = cipher
        self._sender = sender

    def link_owner_dm(
        self,
        *,
        link_code: str,
        app_ref: str,
        actor_ref: str,
        destination_ref: str,
        now: datetime | None = None,
    ) -> LinkedGoogleChatDestination:
        if not isinstance(link_code, str) or not _LINK_CODE.fullmatch(link_code):
            raise ValueError("invalid Google Chat link code")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("link time must be timezone-aware")
        refs = {
            "installation_ref_hash": self._reference_hasher.hash(
                "installation", app_ref
            ),
            "actor_ref_hash": self._reference_hasher.hash("actor", actor_ref),
            "destination_ref_hash": self._reference_hasher.hash(
                "destination", destination_ref
            ),
        }
        secret_hash = hashlib.sha256(link_code.encode("ascii")).digest()
        claim_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
        claim = self._repository.claim(
            secret_hash=secret_hash,
            claim_hash=claim_hash,
            expires_at=current + timedelta(seconds=45),
        )
        try:
            if not self._audit_writer.write(claim.pre_audit_intent_id):
                raise RuntimeError("channel link pre-effect audit is unavailable")
        except BaseException:
            try:
                self._repository.release(
                    secret_hash=secret_hash, claim_hash=claim_hash
                )
            except Exception:
                pass
            raise
        destination_id = self._repository.resolve_destination_id(
            secret_hash=secret_hash, claim_hash=claim_hash, candidate_id=uuid4()
        )
        encrypted = self._cipher.encrypt(
            {"space": destination_ref},
            tenant_id=claim.tenant_id,
            connector_id=destination_id,
            provider="google_chat_route",
            credential_version=1,
        )
        linked = self._repository.consume(
            secret_hash=secret_hash,
            claim_hash=claim_hash,
            destination_id=destination_id,
            encrypted=encrypted,
            **refs,
        )
        if not self._audit_writer.write(linked.outcome_audit_intent_id):
            raise RuntimeError("channel link outcome audit is unavailable")
        return linked

    def test_delivery(
        self, *, destination_id: UUID, now: datetime | None = None
    ) -> CompletedGoogleChatDelivery:
        if not isinstance(destination_id, UUID):
            raise ValueError("invalid destination binding")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("delivery test time must be timezone-aware")
        claim_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
        claim = self._repository.claim_delivery(
            destination_id=destination_id,
            claim_hash=claim_hash,
            expires_at=current + timedelta(seconds=45),
        )
        if not self._audit_writer.write(claim.pre_audit_intent_id):
            self._complete_failed(destination_id, claim_hash)
            raise RuntimeError("channel delivery pre-effect audit is unavailable")
        try:
            route = self._cipher.decrypt(
                claim.encrypted,
                tenant_id=claim.tenant_id,
                connector_id=destination_id,
                provider="google_chat_route",
                credential_version=1,
            )
            space = route.get("space")
            if not isinstance(space, str) or not _DESTINATION_REF.fullmatch(space):
                raise RuntimeError("stored channel route is invalid")
            self._sender.send_connection_test(
                space=space,
                request_id=UUID(bytes=claim_hash[:16], version=4),
            )
        except BaseException:
            self._complete_failed(destination_id, claim_hash)
            raise
        completed = self._repository.complete_delivery(
            destination_id=destination_id, claim_hash=claim_hash, succeeded=True
        )
        if not self._audit_writer.write(completed.outcome_audit_intent_id):
            raise RuntimeError("channel delivery outcome audit is unavailable")
        return completed

    def _complete_failed(self, destination_id: UUID, claim_hash: bytes) -> None:
        try:
            completed = self._repository.complete_delivery(
                destination_id=destination_id,
                claim_hash=claim_hash,
                succeeded=False,
            )
            self._audit_writer.write(completed.outcome_audit_intent_id)
        except Exception:
            pass
