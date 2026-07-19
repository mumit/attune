"""Hosted production signup: a sessionless, function-owned tenant ceremony.

See docs/hosted-signup.md for the full design record. In short: a verified
Identity Platform subject with zero Attune membership may call
``POST /v1/signup`` (control_plane_service.py) to create its own tenant, or
learn that it already has one. The mutation is entirely owned by
``attune.provision_hosted_signup_tenant`` (migration 0045); this module never
writes to ``attune.tenants``/``attune.principals`` directly.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .identity import VerifiedIdentity
from .repositories import ConnectionFactory
from .tenant import TenantContext

# Mirrors the 10-per-60-second Cloud Armor rule every other onboarding
# ceremony uses (docs/hosted-signup.md section 7); the two layers are meant
# to agree, not the application layer standing in for the edge control.
HOSTED_SIGNUP_THROTTLE_LIMIT = 10
HOSTED_SIGNUP_THROTTLE_WINDOW = timedelta(seconds=60)


class SignupThrottle:
    """In-process, best-effort per-key request limiter.

    This is scoped to one running control-plane instance's memory. It is
    not shared across Cloud Run's multiple instances and resets on every
    deploy or restart -- it is a defense-in-depth backstop, never a
    substitute for the authoritative, global Cloud Armor edge rule
    documented in docs/hosted-signup.md.
    """

    def __init__(
        self,
        *,
        limit: int = HOSTED_SIGNUP_THROTTLE_LIMIT,
        window: timedelta = HOSTED_SIGNUP_THROTTLE_WINDOW,
    ):
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("throttle limit must be a positive integer")
        if not isinstance(window, timedelta) or window <= timedelta(0):
            raise ValueError("throttle window must be a positive duration")
        self._limit = limit
        self._window = window
        self._attempts: dict[bytes, list[datetime]] = {}

    def allow(self, key: bytes, *, now: datetime) -> bool:
        """Record one attempt for ``key`` and report whether it is allowed."""
        if not isinstance(key, bytes) or not key:
            raise TypeError("throttle key must be non-empty bytes")
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("throttle time must be timezone-aware")
        history = [
            when for when in self._attempts.get(key, ()) if now - when < self._window
        ]
        if len(history) >= self._limit:
            self._attempts[key] = history
            return False
        history.append(now)
        self._attempts[key] = history
        return True


@dataclass(frozen=True)
class SignupResult:
    status: str  # "created" | "already_provisioned"
    tenant_id: UUID
    principal_id: UUID


class HostedSignupProvisioner(Protocol):
    def provision(
        self, subject_hash: bytes, issuer: str, *, region: str
    ) -> tuple[UUID, UUID, bool]: ...


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


class PostgresHostedSignupRepository:
    """The sole caller of ``attune.provision_hosted_signup_tenant``."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def provision(
        self, subject_hash: bytes, issuer: str, *, region: str
    ) -> tuple[UUID, UUID, bool]:
        if not isinstance(subject_hash, bytes) or len(subject_hash) != 32:
            raise ValueError("subject_hash must be exactly 32 bytes")
        if not isinstance(issuer, str) or not 1 <= len(issuer) <= 255:
            raise ValueError("issuer must be a bounded non-empty string")
        if not isinstance(region, str) or not 1 <= len(region) <= 64:
            raise ValueError("region must be a bounded non-empty string")
        with closing(self._connect()) as connection:
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(
                        "SELECT tenant_id, principal_id, created "
                        "FROM attune.provision_hosted_signup_tenant(%s, %s, %s)",
                        (subject_hash, issuer, region),
                    )
                    row = cursor.fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        if row is None:
            raise RuntimeError("hosted signup provisioning returned no row")
        return UUID(str(row[0])), UUID(str(row[1])), bool(row[2])


class HostedSignupService:
    """Provision a tenant for a verified identity and audit the outcome.

    Unlike other ceremonies in this codebase, signup cannot write a durable
    pre-effect audit row before mutating: there is no tenant to scope it to
    until the provisioning function itself creates or resolves one. The
    durable, subject-hash-keyed audit trail therefore begins only after the
    function returns a tenant context (see docs/hosted-signup.md section 8).
    """

    def __init__(
        self,
        provisioner: HostedSignupProvisioner,
        audit: PostgresAuditProducerRepository,
        writer: AuditWriterClient | AuditWriter,
        *,
        region: str,
    ):
        if not isinstance(region, str) or not 1 <= len(region) <= 64:
            raise ValueError("region must be a bounded non-empty string")
        self._provisioner = provisioner
        self._audit = audit
        self._writer = writer
        self._region = region

    def provision(self, identity: VerifiedIdentity) -> SignupResult:
        if not isinstance(identity, VerifiedIdentity):
            raise TypeError("identity must be verified")
        tenant_id, principal_id, created = self._provisioner.provision(
            identity.subject_hash, identity.issuer, region=self._region
        )
        status = "created" if created else "already_provisioned"
        context = TenantContext(tenant_id)
        idempotency_key = hashlib.sha256(
            b"attune-hosted-signup-v1:" + tenant_id.bytes + b":" + status.encode("ascii")
        ).digest()
        intent = self._audit.request(
            context,
            idempotency_key=idempotency_key,
            actor_type="principal",
            actor_ref_hash=identity.subject_hash,
            action="hosted_signup.provision",
            outcome="observed",
            target_type="tenant",
            target_ref_hash=hashlib.sha256(tenant_id.bytes).digest(),
            metadata={"created": created},
        )
        if not self._writer.write(intent.id):
            raise RuntimeError("hosted signup audit is unavailable")
        return SignupResult(status=status, tenant_id=tenant_id, principal_id=principal_id)
