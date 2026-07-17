"""Fixed-scope database boundary for dormant hosted customer exports."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from .repositories import ConnectionFactory, _fixed_hash
from .tenant import TenantContext, tenant_transaction

ExportScope = Literal["account", "conversations", "memories", "activity"]
EXPORT_SCOPES = frozenset({"account", "conversations", "memories", "activity"})


@dataclass(frozen=True)
class CustomerExportRequest:
    id: UUID
    scope: ExportScope
    state: str
    created_at: datetime


@dataclass(frozen=True)
class CustomerExportStart(CustomerExportRequest):
    was_created: bool


@dataclass(frozen=True)
class CustomerExportStatus:
    id: UUID
    scope: ExportScope
    state: str
    created_at: datetime
    updated_at: datetime
    ready_at: datetime | None
    expires_at: datetime | None
    archive_bytes: int | None
    failure_code: str | None


class PostgresCustomerExportRequests:
    """Create only canonical, recent-session-bound export requests."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def request(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        scope: ExportScope,
        idempotency_key: bytes,
    ) -> CustomerExportRequest:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        if scope not in EXPORT_SCOPES:
            raise ValueError("unsupported customer export scope")
        _fixed_hash("idempotency_key", idempotency_key)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.request_customer_export(%s,%s,%s,%s)",
                    (principal_id, session_id, scope, idempotency_key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("customer export request was not created")
                return CustomerExportRequest(*row)

    def request_or_existing(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        scope: ExportScope,
        idempotency_key: bytes,
    ) -> CustomerExportStart:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        if scope not in EXPORT_SCOPES:
            raise ValueError("unsupported customer export scope")
        _fixed_hash("idempotency_key", idempotency_key)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.request_or_read_customer_export(%s,%s,%s,%s)",
                    (principal_id, session_id, scope, idempotency_key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("customer export request was not created")
                return CustomerExportStart(*row)

    def list(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        limit: int = 10,
    ) -> tuple[CustomerExportStatus, ...]:
        if not isinstance(principal_id, UUID):
            raise TypeError("principal_id must be a UUID")
        if not 1 <= limit <= 20:
            raise ValueError("customer export limit must be between 1 and 20")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.list_customer_exports(%s,%s)",
                    (principal_id, limit),
                )
                return tuple(CustomerExportStatus(*row) for row in cursor.fetchall())


@dataclass(frozen=True)
class ClaimedCustomerExport:
    tenant_id: UUID
    id: UUID
    requested_by: UUID
    scope: ExportScope
    lease_expires_at: datetime


class PostgresCustomerExportClaims:
    """Claim one opaque queued export through the dedicated executor role."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def claim(
        self,
        export_id: UUID,
        *,
        run_id: UUID,
        expected_tenant_id: UUID | None = None,
    ) -> ClaimedCustomerExport | None:
        if (
            not isinstance(export_id, UUID)
            or not isinstance(run_id, UUID)
            or (
                expected_tenant_id is not None
                and not isinstance(expected_tenant_id, UUID)
            )
        ):
            raise TypeError("export and tenant identifiers must be UUIDs")
        with closing(self._connect()) as connection:
            with closing(connection.cursor()) as cursor:
                if expected_tenant_id is None:
                    cursor.execute(
                        "SELECT * FROM attune.claim_customer_export(%s,%s)",
                        (export_id, run_id),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM "
                        "attune.claim_customer_export_for_tenant(%s,%s,%s)",
                        (expected_tenant_id, export_id, run_id),
                    )
                row = cursor.fetchone()
            connection.commit()
        return ClaimedCustomerExport(*row) if row is not None else None


@dataclass(frozen=True)
class ReservedCustomerExportObject:
    object_id: UUID
    requested_at: datetime


@dataclass(frozen=True)
class CompletedCustomerExport:
    id: UUID
    state: str
    expires_at: datetime


@dataclass(frozen=True)
class FailedCustomerExport:
    id: UUID
    state: str
    failure_code: str


class PostgresCustomerExportExecution:
    """Use only claim-bound projection and state-transition functions."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def reserve_object(
        self, export_id: UUID, *, run_id: UUID, proposed_object_id: UUID
    ) -> ReservedCustomerExportObject:
        _uuids(export_id, run_id, proposed_object_id)
        row = self._one(
            "SELECT * FROM attune.reserve_customer_export_object(%s,%s,%s)",
            (export_id, run_id, proposed_object_id),
        )
        return ReservedCustomerExportObject(*row)

    def records(
        self, export_id: UUID, *, run_id: UUID, expected_member: str
    ) -> tuple[Mapping[str, Any], ...]:
        _uuids(export_id, run_id)
        if expected_member not in {
            "account.jsonl", "conversations.jsonl", "memories.jsonl",
            "activity.jsonl",
        }:
            raise ValueError("invalid export member")
        with closing(self._connect()) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    "SELECT member_name, record "
                    "FROM attune.read_customer_export_records(%s,%s) "
                    "ORDER BY sort_key",
                    (export_id, run_id),
                )
                rows = cursor.fetchall()
            connection.commit()
        if any(row[0] != expected_member for row in rows):
            raise RuntimeError("customer export projection returned the wrong member")
        return tuple(row[1] for row in rows)

    def cleanup_objects(
        self, export_id: UUID, *, run_id: UUID
    ) -> tuple[UUID, ...]:
        _uuids(export_id, run_id)
        with closing(self._connect()) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    "SELECT object_id "
                    "FROM attune.list_customer_export_cleanup_objects(%s,%s)",
                    (export_id, run_id),
                )
                rows = cursor.fetchall()
            connection.commit()
        return tuple(row[0] for row in rows)

    def complete(
        self,
        export_id: UUID,
        *,
        run_id: UUID,
        object_id: UUID,
        object_generation: int,
        wrapped_dek: bytes,
        nonce: bytes,
        key_resource: str,
        archive_sha256: bytes,
        ciphertext_sha256: bytes,
        archive_bytes: int,
        ciphertext_bytes: int,
        encryption_format: int,
    ) -> CompletedCustomerExport:
        _uuids(export_id, run_id, object_id)
        row = self._one(
            "SELECT * FROM attune.complete_customer_export("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                export_id, run_id, object_id, object_generation, wrapped_dek,
                nonce, key_resource, archive_sha256, ciphertext_sha256,
                archive_bytes, ciphertext_bytes, encryption_format,
            ),
        )
        return CompletedCustomerExport(*row)

    def fail(
        self, export_id: UUID, *, run_id: UUID, failure_code: str
    ) -> FailedCustomerExport:
        _uuids(export_id, run_id)
        row = self._one(
            "SELECT * FROM attune.fail_customer_export(%s,%s,%s)",
            (export_id, run_id, failure_code),
        )
        return FailedCustomerExport(*row)

    def _one(self, query: str, parameters: tuple[Any, ...]) -> tuple[Any, ...]:
        with closing(self._connect()) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(query, parameters)
                row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("customer export transition returned no result")
        return row


def _uuids(*values: UUID) -> None:
    if not all(isinstance(value, UUID) for value in values):
        raise TypeError("export identifiers must be UUIDs")
