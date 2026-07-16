"""One-use private broker for verified hosted channel links."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from .repositories import ConnectionFactory, _fixed_hash

_LINK_CODE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_APP_REF = re.compile(r"^projects/[1-9][0-9]{5,20}$")
_ACTOR_REF = re.compile(r"^users/[A-Za-z0-9._-]{1,180}$")
_DESTINATION_REF = re.compile(r"^spaces/[A-Za-z0-9_-]{1,180}$")


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

    def consume(
        self,
        *,
        secret_hash: bytes,
        claim_hash: bytes,
        installation_ref_hash: bytes,
        actor_ref_hash: bytes,
        destination_ref_hash: bytes,
    ) -> LinkedGoogleChatDestination:
        for name, value in (
            ("secret_hash", secret_hash),
            ("claim_hash", claim_hash),
            ("installation_ref_hash", installation_ref_hash),
            ("actor_ref_hash", actor_ref_hash),
            ("destination_ref_hash", destination_ref_hash),
        ):
            _fixed_hash(name, value)
        row = self._call(
            "SELECT * FROM attune.consume_google_chat_link(%s, %s, %s, %s, %s)",
            (
                secret_hash,
                claim_hash,
                installation_ref_hash,
                actor_ref_hash,
                destination_ref_hash,
            ),
        )
        return LinkedGoogleChatDestination(*row)

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
    ):
        self._repository = repository
        self._audit_writer = audit_writer
        self._reference_hasher = reference_hasher

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
        linked = self._repository.consume(
            secret_hash=secret_hash,
            claim_hash=claim_hash,
            **refs,
        )
        if not self._audit_writer.write(linked.outcome_audit_intent_id):
            raise RuntimeError("channel link outcome audit is unavailable")
        return linked
