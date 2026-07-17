"""One-use private broker for verified hosted Slack installations."""

from __future__ import annotations

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
from .slack_provider import SlackInstallation, SlackProvider, validate_bot_token
from .vault_crypto import EncryptedCredential, EnvelopeCipher

_OAUTH_STATE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TEAM_REF = re.compile(r"^teams/T[A-Z0-9]{4,20}$")
_ACTOR_REF = re.compile(r"^teams/T[A-Z0-9]{4,20}/users/[UW][A-Z0-9]{4,20}$")
_DESTINATION_REF = re.compile(r"^teams/T[A-Z0-9]{4,20}/channels/D[A-Z0-9]{4,20}$")
_MESSAGE_REF = re.compile(
    r"^teams/T[A-Z0-9]{4,20}/channels/D[A-Z0-9]{4,20}"
    r"/messages/[0-9]{6,20}\.[0-9]{1,10}$"
)
ROUTE_PROVIDER = "slack_route"
TOKEN_PROVIDER = "slack_bot_token"


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


@dataclass(frozen=True)
class ClaimedSlackInstall:
    transaction_id: UUID
    tenant_id: UUID
    owner_principal_id: UUID
    pre_audit_intent_id: UUID


@dataclass(frozen=True)
class InstalledSlackDestination:
    tenant_id: UUID
    owner_principal_id: UUID
    installation_id: UUID
    destination_id: UUID
    destination_status: str
    outcome_audit_intent_id: UUID


@dataclass(frozen=True)
class ClaimedSlackDelivery:
    tenant_id: UUID
    owner_principal_id: UUID
    encrypted_route: EncryptedCredential
    encrypted_token: EncryptedCredential
    pre_audit_intent_id: UUID


@dataclass(frozen=True)
class CompletedSlackDelivery:
    destination_status: str
    outcome_audit_intent_id: UUID


@dataclass(frozen=True)
class AcceptedSlackMessage:
    dispatch_intent_id: UUID
    pre_audit_intent_id: UUID
    accepted_new: bool


@dataclass(frozen=True)
class ClaimedSlackConversationDelivery:
    tenant_id: UUID
    encrypted_route: EncryptedCredential | None
    encrypted_token: EncryptedCredential | None
    reply_text: str | None
    pre_audit_intent_id: UUID | None
    already_delivered: bool


@dataclass(frozen=True)
class CompletedSlackConversationDelivery:
    delivery_state: str
    outcome_audit_intent_id: UUID


class SlackReferenceHasher:
    """Keyed, domain-separated HMAC references for Slack provider identifiers."""

    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError("channel reference HMAC key must be exactly 32 bytes")
        self._key = key

    def hash(self, kind: str, value: str) -> bytes:
        patterns = {
            "installation": _TEAM_REF,
            "actor": _ACTOR_REF,
            "destination": _DESTINATION_REF,
            "message": _MESSAGE_REF,
        }
        pattern = patterns.get(kind)
        if pattern is None or not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError("invalid Slack reference")
        return hmac.new(
            self._key,
            b"attune-channel-ref-v1\0slack\0"
            + kind.encode("ascii")
            + b"\0"
            + value.encode("ascii"),
            hashlib.sha256,
        ).digest()


class PostgresSlackChannelBrokerRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def claim(
        self, *, state_hash: bytes, claim_hash: bytes, expires_at: datetime
    ) -> ClaimedSlackInstall:
        _fixed_hash("state_hash", state_hash)
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("claim expiry must be timezone-aware")
        row = self._call(
            "SELECT * FROM attune.claim_slack_install(%s, %s, %s)",
            (state_hash, claim_hash, expires_at),
        )
        return ClaimedSlackInstall(*row)

    def release(self, *, state_hash: bytes, claim_hash: bytes) -> bool:
        _fixed_hash("state_hash", state_hash)
        _fixed_hash("claim_hash", claim_hash)
        row = self._call(
            "SELECT attune.release_slack_install_claim(%s, %s)",
            (state_hash, claim_hash),
        )
        return row[0] is True

    def resolve_destination_id(
        self, *, state_hash: bytes, claim_hash: bytes, candidate_id: UUID
    ) -> UUID:
        _fixed_hash("state_hash", state_hash)
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(candidate_id, UUID):
            raise TypeError("candidate_id must be a UUID")
        row = self._call(
            "SELECT attune.resolve_slack_install_destination(%s, %s, %s)",
            (state_hash, claim_hash, candidate_id),
        )
        return row[0]

    def consume(
        self,
        *,
        state_hash: bytes,
        claim_hash: bytes,
        owner_tenant_id: UUID,
        owner_principal_id: UUID,
        installation_ref_hash: bytes,
        actor_ref_hash: bytes,
        destination_ref_hash: bytes,
        destination_id: UUID,
        encrypted_route: EncryptedCredential,
        encrypted_token: EncryptedCredential,
    ) -> InstalledSlackDestination:
        for name, value in (
            ("state_hash", state_hash),
            ("claim_hash", claim_hash),
            ("installation_ref_hash", installation_ref_hash),
            ("actor_ref_hash", actor_ref_hash),
            ("destination_ref_hash", destination_ref_hash),
        ):
            _fixed_hash(name, value)
        if (
            not isinstance(destination_id, UUID)
            or not isinstance(owner_tenant_id, UUID)
            or not isinstance(owner_principal_id, UUID)
        ):
            raise TypeError("Slack install identifiers must be UUIDs")
        row = self._call(
            """
            SELECT * FROM attune.consume_slack_install(
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                state_hash,
                claim_hash,
                owner_tenant_id,
                owner_principal_id,
                installation_ref_hash,
                actor_ref_hash,
                destination_ref_hash,
                destination_id,
                encrypted_route.ciphertext,
                encrypted_route.nonce,
                encrypted_route.wrapped_dek,
                encrypted_route.key_resource,
                encrypted_route.format_version,
                encrypted_token.ciphertext,
                encrypted_token.nonce,
                encrypted_token.wrapped_dek,
                encrypted_token.key_resource,
                encrypted_token.format_version,
            ),
        )
        return InstalledSlackDestination(*row)

    def claim_delivery(
        self, *, destination_id: UUID, claim_hash: bytes, expires_at: datetime
    ) -> ClaimedSlackDelivery:
        if not isinstance(destination_id, UUID):
            raise TypeError("destination_id must be a UUID")
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("claim expiry must be timezone-aware")
        row = self._call(
            "SELECT * FROM attune.claim_slack_delivery_test(%s, %s, %s)",
            (destination_id, claim_hash, expires_at),
        )
        (
            tenant_id, owner_id,
            route_ciphertext, route_nonce, route_wrapped, route_key, route_version,
            token_ciphertext, token_nonce, token_wrapped, token_key, token_version,
            audit,
        ) = row
        return ClaimedSlackDelivery(
            tenant_id,
            owner_id,
            EncryptedCredential(
                route_ciphertext, route_nonce, route_wrapped, route_key, route_version
            ),
            EncryptedCredential(
                token_ciphertext, token_nonce, token_wrapped, token_key, token_version
            ),
            audit,
        )

    def complete_delivery(
        self, *, destination_id: UUID, claim_hash: bytes, succeeded: bool
    ) -> CompletedSlackDelivery:
        if not isinstance(destination_id, UUID):
            raise TypeError("destination_id must be a UUID")
        _fixed_hash("claim_hash", claim_hash)
        if not isinstance(succeeded, bool):
            raise TypeError("succeeded must be boolean")
        row = self._call(
            "SELECT * FROM attune.complete_slack_delivery_test(%s, %s, %s)",
            (destination_id, claim_hash, succeeded),
        )
        return CompletedSlackDelivery(*row)

    def accept_message(
        self,
        *,
        installation_ref_hash: bytes,
        actor_ref_hash: bytes,
        destination_ref_hash: bytes,
        message_ref_hash: bytes,
        text: str,
    ) -> AcceptedSlackMessage:
        for name, value in (
            ("installation_ref_hash", installation_ref_hash),
            ("actor_ref_hash", actor_ref_hash),
            ("destination_ref_hash", destination_ref_hash),
            ("message_ref_hash", message_ref_hash),
        ):
            _fixed_hash(name, value)
        if not isinstance(text, str) or not 1 <= len(text) <= 8_000:
            raise ValueError("invalid Slack message")
        row = self._call(
            "SELECT * FROM attune.accept_slack_owner_message(%s, %s, %s, %s, %s)",
            (
                installation_ref_hash,
                actor_ref_hash,
                destination_ref_hash,
                message_ref_hash,
                text,
            ),
        )
        return AcceptedSlackMessage(*row)

    def claim_conversation_delivery(
        self, *, destination_id: UUID, job_id: UUID, claim_hash: bytes,
        expires_at: datetime,
    ) -> ClaimedSlackConversationDelivery:
        if not isinstance(destination_id, UUID) or not isinstance(job_id, UUID):
            raise TypeError("delivery references must be UUIDs")
        _fixed_hash("claim_hash", claim_hash)
        row = self._call(
            "SELECT * FROM attune.claim_slack_conversation_delivery(%s, %s, %s, %s)",
            (destination_id, job_id, claim_hash, expires_at),
        )
        (
            tenant_id,
            route_ciphertext, route_nonce, route_wrapped, route_key, route_version,
            token_ciphertext, token_nonce, token_wrapped, token_key, token_version,
            text, audit, delivered,
        ) = row
        encrypted_route = None if delivered else EncryptedCredential(
            route_ciphertext, route_nonce, route_wrapped, route_key, route_version
        )
        encrypted_token = None if delivered else EncryptedCredential(
            token_ciphertext, token_nonce, token_wrapped, token_key, token_version
        )
        return ClaimedSlackConversationDelivery(
            tenant_id, encrypted_route, encrypted_token, text, audit, delivered
        )

    def complete_conversation_delivery(
        self, *, job_id: UUID, claim_hash: bytes, succeeded: bool,
        provider_message_ref_hash: bytes | None,
    ) -> CompletedSlackConversationDelivery:
        if not isinstance(job_id, UUID) or not isinstance(succeeded, bool):
            raise TypeError("delivery completion is invalid")
        _fixed_hash("claim_hash", claim_hash)
        if provider_message_ref_hash is not None:
            _fixed_hash("provider_message_ref_hash", provider_message_ref_hash)
        row = self._call(
            "SELECT * FROM attune.complete_slack_conversation_delivery(%s, %s, %s, %s)",
            (job_id, claim_hash, succeeded, provider_message_ref_hash),
        )
        return CompletedSlackConversationDelivery(*row)

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


class SlackInstallBroker:
    def __init__(
        self,
        repository: PostgresSlackChannelBrokerRepository,
        audit_writer: AuditWriter,
        reference_hasher: SlackReferenceHasher,
        cipher: EnvelopeCipher,
        provider: SlackProvider,
        *,
        redirect_uri: str,
    ):
        if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
            raise ValueError("Slack redirect URI must be HTTPS")
        self._repository = repository
        self._audit_writer = audit_writer
        self._reference_hasher = reference_hasher
        self._cipher = cipher
        self._provider = provider
        self._redirect_uri = redirect_uri

    def install(
        self,
        *,
        state: str,
        code: str,
        owner_tenant_id: UUID,
        owner_principal_id: UUID,
        now: datetime | None = None,
    ) -> InstalledSlackDestination:
        if not isinstance(state, str) or not _OAUTH_STATE.fullmatch(state):
            raise ValueError("invalid Slack OAuth state")
        if not isinstance(code, str) or not 1 <= len(code) <= 512:
            raise ValueError("invalid Slack authorization code")
        if not isinstance(owner_tenant_id, UUID) or not isinstance(
            owner_principal_id, UUID
        ):
            raise ValueError("invalid Slack install binding")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("install time must be timezone-aware")
        state_hash = hashlib.sha256(state.encode("ascii")).digest()
        claim_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
        claim = self._repository.claim(
            state_hash=state_hash,
            claim_hash=claim_hash,
            expires_at=current + timedelta(seconds=45),
        )
        try:
            if (
                claim.tenant_id != owner_tenant_id
                or claim.owner_principal_id != owner_principal_id
            ):
                raise RuntimeError("Slack install browser binding does not match")
            if not self._audit_writer.write(claim.pre_audit_intent_id):
                raise RuntimeError("Slack install pre-effect audit is unavailable")
            installation = self._provider.exchange_code(
                code=code, redirect_uri=self._redirect_uri
            )
            dm_channel = self._provider.open_owner_dm(
                bot_token=installation.bot_token,
                user_id=installation.installer_user_id,
            )
        except BaseException:
            try:
                self._repository.release(
                    state_hash=state_hash, claim_hash=claim_hash
                )
            except Exception:
                pass
            raise
        refs = self._references(installation, dm_channel)
        destination_id = self._repository.resolve_destination_id(
            state_hash=state_hash, claim_hash=claim_hash, candidate_id=uuid4()
        )
        encrypted_route = self._cipher.encrypt(
            {"team": installation.team_id, "channel": dm_channel},
            tenant_id=claim.tenant_id,
            connector_id=destination_id,
            provider=ROUTE_PROVIDER,
            credential_version=1,
        )
        encrypted_token = self._cipher.encrypt(
            {"bot_token": installation.bot_token},
            tenant_id=claim.tenant_id,
            connector_id=destination_id,
            provider=TOKEN_PROVIDER,
            credential_version=1,
        )
        try:
            installed = self._repository.consume(
                state_hash=state_hash,
                claim_hash=claim_hash,
                owner_tenant_id=owner_tenant_id,
                owner_principal_id=owner_principal_id,
                destination_id=destination_id,
                encrypted_route=encrypted_route,
                encrypted_token=encrypted_token,
                **refs,
            )
        except BaseException:
            try:
                self._repository.release(
                    state_hash=state_hash, claim_hash=claim_hash
                )
            except Exception:
                pass
            raise
        if not self._audit_writer.write(installed.outcome_audit_intent_id):
            raise RuntimeError("Slack install outcome audit is unavailable")
        return installed

    def test_delivery(
        self, *, destination_id: UUID, now: datetime | None = None
    ) -> CompletedSlackDelivery:
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
            channel, bot_token = self._decrypt_route_and_token(
                claim.encrypted_route, claim.encrypted_token,
                tenant_id=claim.tenant_id, destination_id=destination_id,
            )
            self._provider.send_connection_test(
                bot_token=bot_token, channel=channel
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

    def accept_message(
        self,
        *,
        team_ref: str,
        actor_ref: str,
        destination_ref: str,
        message_ref: str,
        text: str,
    ) -> AcceptedSlackMessage:
        accepted = self._repository.accept_message(
            installation_ref_hash=self._reference_hasher.hash(
                "installation", team_ref
            ),
            actor_ref_hash=self._reference_hasher.hash("actor", actor_ref),
            destination_ref_hash=self._reference_hasher.hash(
                "destination", destination_ref
            ),
            message_ref_hash=self._reference_hasher.hash("message", message_ref),
            text=text,
        )
        if not self._audit_writer.write(accepted.pre_audit_intent_id):
            raise RuntimeError("channel message pre-effect audit is unavailable")
        return accepted

    def deliver_reply(
        self, *, destination_id: UUID, job_id: UUID,
        now: datetime | None = None,
    ) -> bool:
        if not isinstance(destination_id, UUID) or not isinstance(job_id, UUID):
            raise ValueError("invalid conversation delivery")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("delivery time must be timezone-aware")
        claim_hash = hashlib.sha256(secrets.token_bytes(32)).digest()
        claim = self._repository.claim_conversation_delivery(
            destination_id=destination_id, job_id=job_id,
            claim_hash=claim_hash, expires_at=current + timedelta(seconds=45),
        )
        if claim.already_delivered:
            return True
        if (
            claim.encrypted_route is None or claim.encrypted_token is None
            or claim.reply_text is None or claim.pre_audit_intent_id is None
        ):
            raise RuntimeError("conversation delivery claim is invalid")
        if not self._audit_writer.write(claim.pre_audit_intent_id):
            self._complete_reply_failed(job_id, claim_hash)
            raise RuntimeError("conversation delivery pre-effect audit is unavailable")
        try:
            channel, bot_token = self._decrypt_route_and_token(
                claim.encrypted_route, claim.encrypted_token,
                tenant_id=claim.tenant_id, destination_id=destination_id,
            )
            team = self._decrypt_team(
                claim.encrypted_route, tenant_id=claim.tenant_id,
                destination_id=destination_id,
            )
            posted_ts = self._provider.send_message(
                bot_token=bot_token, channel=channel, text=claim.reply_text,
                request_id=job_id,
            )
            message_ref = f"teams/{team}/channels/{channel}/messages/{posted_ts}"
            message_hash = self._reference_hasher.hash("message", message_ref)
        except BaseException:
            self._complete_reply_failed(job_id, claim_hash)
            raise
        completed = self._repository.complete_conversation_delivery(
            job_id=job_id, claim_hash=claim_hash, succeeded=True,
            provider_message_ref_hash=message_hash,
        )
        if completed.delivery_state != "delivered":
            raise RuntimeError("conversation delivery completion is invalid")
        if not self._audit_writer.write(completed.outcome_audit_intent_id):
            raise RuntimeError("conversation delivery outcome audit is unavailable")
        return True

    def _references(
        self, installation: SlackInstallation, dm_channel: str
    ) -> dict[str, bytes]:
        team_ref = f"teams/{installation.team_id}"
        actor_ref = f"teams/{installation.team_id}/users/{installation.installer_user_id}"
        destination_ref = f"teams/{installation.team_id}/channels/{dm_channel}"
        return {
            "installation_ref_hash": self._reference_hasher.hash(
                "installation", team_ref
            ),
            "actor_ref_hash": self._reference_hasher.hash("actor", actor_ref),
            "destination_ref_hash": self._reference_hasher.hash(
                "destination", destination_ref
            ),
        }

    def _decrypt_route_and_token(
        self,
        encrypted_route: EncryptedCredential,
        encrypted_token: EncryptedCredential,
        *,
        tenant_id: UUID,
        destination_id: UUID,
    ) -> tuple[str, str]:
        route = self._cipher.decrypt(
            encrypted_route,
            tenant_id=tenant_id,
            connector_id=destination_id,
            provider=ROUTE_PROVIDER,
            credential_version=1,
        )
        channel = route.get("channel")
        team = route.get("team")
        if (
            not isinstance(channel, str)
            or not re.fullmatch(r"D[A-Z0-9]{4,20}", channel)
            or not isinstance(team, str)
            or not re.fullmatch(r"T[A-Z0-9]{4,20}", team)
        ):
            raise RuntimeError("stored channel route is invalid")
        credential = self._cipher.decrypt(
            encrypted_token,
            tenant_id=tenant_id,
            connector_id=destination_id,
            provider=TOKEN_PROVIDER,
            credential_version=1,
        )
        bot_token = validate_bot_token(credential.get("bot_token"))
        return channel, bot_token

    def _decrypt_team(
        self, encrypted_route: EncryptedCredential, *, tenant_id: UUID,
        destination_id: UUID,
    ) -> str:
        route = self._cipher.decrypt(
            encrypted_route,
            tenant_id=tenant_id,
            connector_id=destination_id,
            provider=ROUTE_PROVIDER,
            credential_version=1,
        )
        team = route.get("team")
        if not isinstance(team, str) or not re.fullmatch(r"T[A-Z0-9]{4,20}", team):
            raise RuntimeError("stored channel route is invalid")
        return team

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

    def _complete_reply_failed(self, job_id: UUID, claim_hash: bytes) -> None:
        try:
            completed = self._repository.complete_conversation_delivery(
                job_id=job_id, claim_hash=claim_hash, succeeded=False,
                provider_message_ref_hash=None,
            )
            self._audit_writer.write(completed.outcome_audit_intent_id)
        except Exception:
            pass
