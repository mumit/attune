"""Fail-closed core for connector credential mutation and fixed provider use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol
from uuid import UUID

from .google_provider import GoogleProvider, ProviderFailure
from .google_oauth import GoogleAuthorizationCodeProvider
from .vault import LeasedCredentialIntent, PostgresSecretBrokerRepository
from .vault_crypto import EnvelopeCipher

GOOGLE_GMAIL_PROFILE_ACTION = "credential.use.google.gmail.profile.read"
GOOGLE_CALENDAR_PRIMARY_ACTION = "credential.use.google.calendar.primary.read"
GOOGLE_GMAIL_THREADS_ACTION = "credential.use.google.gmail.threads.read"
GOOGLE_CALENDAR_EVENTS_ACTION = "credential.use.google.calendar.events.read"
GOOGLE_OAUTH_INSTALL_ACTION = "credential.install.google.oauth"
GOOGLE_OAUTH_DISCONNECT_ACTION = "credential.revoke.google.oauth"


class SecretAudit(Protocol):
    def record(
        self,
        intent: LeasedCredentialIntent,
        *,
        action: str,
        outcome: str,
    ) -> bool: ...


@dataclass(frozen=True)
class SecretBrokerResult:
    status_code: int
    body: Mapping[str, Any] | None = None


class SecretBroker:
    def __init__(
        self,
        *,
        vault: PostgresSecretBrokerRepository,
        cipher: EnvelopeCipher,
        audit: SecretAudit,
        google: GoogleProvider | None = None,
        google_oauth: GoogleAuthorizationCodeProvider | None = None,
    ):
        self._vault = vault
        self._cipher = cipher
        self._audit = audit
        self._google = google
        self._google_oauth = google_oauth

    def google_oauth_exchange(
        self,
        intent_id: UUID,
        *,
        authorization_code: str,
        pkce_verifier: str,
        nonce_hash: bytes,
        redirect_uri: str,
        scopes: tuple[str, ...],
    ) -> SecretBrokerResult:
        intent = self._lease(intent_id, "control_plane", "install")
        if intent is None:
            return SecretBrokerResult(404)
        if (
            intent.provider != "google"
            or intent.capability != "google.oauth.install"
            or self._google_oauth is None
        ):
            return SecretBrokerResult(404)
        if not self._record(
            intent, action=GOOGLE_OAUTH_INSTALL_ACTION, outcome="allowed"
        ):
            return SecretBrokerResult(503)
        try:
            credential = self._google_oauth.exchange(
                authorization_code=authorization_code,
                pkce_verifier=pkce_verifier,
                nonce_hash=nonce_hash,
                redirect_uri=redirect_uri,
                scopes=scopes,
            )
            version = (intent.credential_version or 0) + 1
            encrypted = self._cipher.encrypt(
                credential,
                tenant_id=intent.tenant.tenant_id,
                connector_id=intent.connector_id,
                provider=intent.provider,
                credential_version=version,
            )
            stored = self._vault.store(intent.id, encrypted, granted_scopes=tuple(scopes))
        except ProviderFailure:
            self._record(intent, action=GOOGLE_OAUTH_INSTALL_ACTION, outcome="failed")
            try:
                self._vault.finalize(
                    intent.id, producer_kind="control_plane", outcome="failed"
                )
            except Exception:
                pass
            return SecretBrokerResult(502)
        except Exception:
            return SecretBrokerResult(503)
        if stored is None or stored[1] != version:
            return SecretBrokerResult(503)
        return SecretBrokerResult(
            204
            if self._record(
                intent, action=GOOGLE_OAUTH_INSTALL_ACTION, outcome="observed"
            )
            else 503
        )

    def install(
        self, intent_id: UUID, credential: Mapping[str, Any]
    ) -> SecretBrokerResult:
        intent = self._lease(intent_id, "control_plane", "install")
        if intent is None:
            return SecretBrokerResult(404)
        if not self._record(intent, action="credential.install", outcome="allowed"):
            return SecretBrokerResult(503)
        version = (intent.credential_version or 0) + 1
        try:
            encrypted = self._cipher.encrypt(
                credential,
                tenant_id=intent.tenant.tenant_id,
                connector_id=intent.connector_id,
                provider=intent.provider,
                credential_version=version,
            )
            stored = self._vault.store(intent.id, encrypted)
        except Exception:
            return SecretBrokerResult(503)
        if stored is None or stored[1] != version:
            return SecretBrokerResult(503)
        return SecretBrokerResult(
            204
            if self._record(intent, action="credential.install", outcome="observed")
            else 503
        )

    def revoke(self, intent_id: UUID) -> SecretBrokerResult:
        intent = self._lease(intent_id, "control_plane", "revoke")
        if intent is None:
            return SecretBrokerResult(404)
        if intent.provider != "google" or intent.capability != "google.oauth.disconnect":
            return SecretBrokerResult(404)
        if not self._record(
            intent, action=GOOGLE_OAUTH_DISCONNECT_ACTION, outcome="allowed"
        ):
            return SecretBrokerResult(503)
        try:
            revoked = self._vault.revoke(intent.id)
        except Exception:
            return SecretBrokerResult(503)
        if not revoked:
            return SecretBrokerResult(503)
        return SecretBrokerResult(
            204
            if self._record(
                intent, action=GOOGLE_OAUTH_DISCONNECT_ACTION, outcome="observed"
            )
            else 503
        )

    def google_gmail_profile(self, intent_id: UUID) -> SecretBrokerResult:
        intent = self._lease(intent_id, "worker", "use")
        if intent is None:
            return SecretBrokerResult(404)
        if (
            intent.provider != "google"
            or intent.capability != "google.gmail.profile.read"
            or intent.encrypted is None
            or intent.credential_version is None
            or self._google is None
        ):
            return self._finish_failure(
                intent,
                action=GOOGLE_GMAIL_PROFILE_ACTION,
                status_code=404,
                outcome="denied",
            )
        if not self._record(
            intent, action=GOOGLE_GMAIL_PROFILE_ACTION, outcome="allowed"
        ):
            return SecretBrokerResult(503)
        try:
            credential = self._cipher.decrypt(
                intent.encrypted,
                tenant_id=intent.tenant.tenant_id,
                connector_id=intent.connector_id,
                provider=intent.provider,
                credential_version=intent.credential_version,
            )
            profile = self._google.gmail_profile(credential)
        except ProviderFailure:
            return self._finish_failure(
                intent,
                action=GOOGLE_GMAIL_PROFILE_ACTION,
                status_code=502,
                outcome="failed",
            )
        except Exception:
            return self._finish_failure(
                intent,
                action=GOOGLE_GMAIL_PROFILE_ACTION,
                status_code=503,
                outcome="failed",
            )
        if not self._record(
            intent, action=GOOGLE_GMAIL_PROFILE_ACTION, outcome="observed"
        ):
            return SecretBrokerResult(503)
        try:
            finalized = self._vault.finalize(
                intent.id, producer_kind="worker", outcome="consumed"
            )
        except Exception:
            finalized = False
        return (
            SecretBrokerResult(200, profile.response())
            if finalized
            else SecretBrokerResult(503)
        )

    def google_calendar_primary(self, intent_id: UUID) -> SecretBrokerResult:
        intent = self._lease(intent_id, "worker", "use")
        if intent is None:
            return SecretBrokerResult(404)
        if (
            intent.provider != "google"
            or intent.capability != "google.calendar.primary.read"
            or intent.encrypted is None
            or intent.credential_version is None
            or self._google is None
        ):
            return self._finish_failure(
                intent,
                action=GOOGLE_CALENDAR_PRIMARY_ACTION,
                status_code=404,
                outcome="denied",
            )
        if not self._record(
            intent, action=GOOGLE_CALENDAR_PRIMARY_ACTION, outcome="allowed"
        ):
            return SecretBrokerResult(503)
        try:
            credential = self._cipher.decrypt(
                intent.encrypted,
                tenant_id=intent.tenant.tenant_id,
                connector_id=intent.connector_id,
                provider=intent.provider,
                credential_version=intent.credential_version,
            )
            self._google.calendar_primary(credential)
        except ProviderFailure:
            return self._finish_failure(
                intent,
                action=GOOGLE_CALENDAR_PRIMARY_ACTION,
                status_code=502,
                outcome="failed",
            )
        except Exception:
            return self._finish_failure(
                intent,
                action=GOOGLE_CALENDAR_PRIMARY_ACTION,
                status_code=503,
                outcome="failed",
            )
        if not self._record(
            intent, action=GOOGLE_CALENDAR_PRIMARY_ACTION, outcome="observed"
        ):
            return SecretBrokerResult(503)
        try:
            finalized = self._vault.finalize(
                intent.id, producer_kind="worker", outcome="consumed"
            )
        except Exception:
            finalized = False
        return SecretBrokerResult(204 if finalized else 503)

    def google_gmail_threads(
        self, intent_id: UUID, *, query: str, limit: int
    ) -> SecretBrokerResult:
        intent = self._lease(intent_id, "worker", "use")
        if intent is None:
            return SecretBrokerResult(404)
        if (
            intent.provider != "google"
            or intent.capability != "google.gmail.threads.read"
            or intent.encrypted is None
            or intent.credential_version is None
            or self._google is None
        ):
            return self._finish_failure(
                intent, action=GOOGLE_GMAIL_THREADS_ACTION,
                status_code=404, outcome="denied",
            )
        if not self._record(intent, action=GOOGLE_GMAIL_THREADS_ACTION, outcome="allowed"):
            return SecretBrokerResult(503)
        try:
            credential = self._cipher.decrypt(
                intent.encrypted, tenant_id=intent.tenant.tenant_id,
                connector_id=intent.connector_id, provider=intent.provider,
                credential_version=intent.credential_version,
            )
            threads = self._google.gmail_threads(
                credential, query=query, limit=limit
            )
        except ProviderFailure:
            return self._finish_failure(
                intent, action=GOOGLE_GMAIL_THREADS_ACTION,
                status_code=502, outcome="failed",
            )
        except Exception:
            return self._finish_failure(
                intent, action=GOOGLE_GMAIL_THREADS_ACTION,
                status_code=503, outcome="failed",
            )
        return self._finish_success(
            intent,
            action=GOOGLE_GMAIL_THREADS_ACTION,
            body={"threads": [thread.response() for thread in threads]},
        )

    def google_calendar_events(
        self,
        intent_id: UUID,
        *,
        time_min: datetime,
        time_max: datetime,
        limit: int,
    ) -> SecretBrokerResult:
        intent = self._lease(intent_id, "worker", "use")
        if intent is None:
            return SecretBrokerResult(404)
        if (
            intent.provider != "google"
            or intent.capability != "google.calendar.events.read"
            or intent.encrypted is None
            or intent.credential_version is None
            or self._google is None
        ):
            return self._finish_failure(
                intent, action=GOOGLE_CALENDAR_EVENTS_ACTION,
                status_code=404, outcome="denied",
            )
        if not self._record(intent, action=GOOGLE_CALENDAR_EVENTS_ACTION, outcome="allowed"):
            return SecretBrokerResult(503)
        try:
            credential = self._cipher.decrypt(
                intent.encrypted, tenant_id=intent.tenant.tenant_id,
                connector_id=intent.connector_id, provider=intent.provider,
                credential_version=intent.credential_version,
            )
            events = self._google.calendar_events(
                credential, time_min=time_min, time_max=time_max, limit=limit
            )
        except ProviderFailure:
            return self._finish_failure(
                intent, action=GOOGLE_CALENDAR_EVENTS_ACTION,
                status_code=502, outcome="failed",
            )
        except Exception:
            return self._finish_failure(
                intent, action=GOOGLE_CALENDAR_EVENTS_ACTION,
                status_code=503, outcome="failed",
            )
        return self._finish_success(
            intent,
            action=GOOGLE_CALENDAR_EVENTS_ACTION,
            body={"events": [event.response() for event in events]},
        )

    def _finish_success(
        self,
        intent: LeasedCredentialIntent,
        *,
        action: str,
        body: Mapping[str, Any],
    ) -> SecretBrokerResult:
        if not self._record(intent, action=action, outcome="observed"):
            return SecretBrokerResult(503)
        try:
            finalized = self._vault.finalize(
                intent.id, producer_kind="worker", outcome="consumed"
            )
        except Exception:
            finalized = False
        return SecretBrokerResult(200, body) if finalized else SecretBrokerResult(503)

    def _finish_failure(
        self,
        intent: LeasedCredentialIntent,
        *,
        action: str,
        status_code: int,
        outcome: str,
    ) -> SecretBrokerResult:
        if not self._record(intent, action=action, outcome=outcome):
            return SecretBrokerResult(503)
        try:
            finalized = self._vault.finalize(
                intent.id, producer_kind="worker", outcome="failed"
            )
        except Exception:
            finalized = False
        return SecretBrokerResult(status_code if finalized else 503)

    def _lease(
        self, intent_id: UUID, producer_kind: str, operation: str
    ) -> LeasedCredentialIntent | None:
        try:
            intent = self._vault.lease(intent_id, producer_kind=producer_kind)
        except Exception:
            return None
        if intent is None or intent.operation != operation:
            return None
        return intent

    def _record(
        self,
        intent: LeasedCredentialIntent,
        *,
        action: str,
        outcome: str,
    ) -> bool:
        try:
            return self._audit.record(intent, action=action, outcome=outcome)
        except Exception:
            return False
