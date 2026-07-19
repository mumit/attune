"""Owner-initiated tenant deletion (right-to-be-forgotten) repository.

Mirrors ``hosted_policy.py``: a thin wrapper that calls the sole fixed
SECURITY DEFINER functions under tenant context. All authority (recent
session, one-active-request, grace math) lives in
``0046_tenant_content_lifecycle.sql``; this module contributes no additional
authorization decisions.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .repositories import ConnectionFactory
from .tenant import TenantContext, tenant_transaction


@dataclass(frozen=True)
class TenantDeletionRequest:
    id: UUID
    status: str
    requested_at: datetime
    grace_expires_at: datetime
    created: bool


@dataclass(frozen=True)
class TenantDeletionCancellation:
    cancelled: bool
    status: str


class PostgresTenantDeletionRequests:
    """Call the request/cancel functions under tenant context."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def request(
        self, context: TenantContext, *, principal_id: UUID, session_id: UUID
    ) -> TenantDeletionRequest:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT deletion_request_id, request_status, requested_at,
                           grace_expires_at, created
                      FROM attune.request_tenant_deletion(%s, %s)
                    """,
                    (principal_id, session_id),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("tenant deletion request returned no state")
        return TenantDeletionRequest(*row)

    def cancel(
        self, context: TenantContext, *, principal_id: UUID, session_id: UUID
    ) -> TenantDeletionCancellation:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT cancelled, request_status
                      FROM attune.cancel_tenant_deletion_request(%s, %s)
                    """,
                    (principal_id, session_id),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("tenant deletion cancellation returned no state")
        return TenantDeletionCancellation(*row)

    def read(self, context: TenantContext, *, principal_id: UUID):
        """Plain RLS-scoped read of the tenant's most recent request, if any.

        Ordinary SELECT under tenant context -- ``attune_control_plane`` is
        granted SELECT on ``deletion_requests`` directly; only mutation
        requires a SECURITY DEFINER function.
        """

        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT id, status, requested_at, grace_expires_at,
                           cancelled_at, completed_at, failure_code
                      FROM attune.deletion_requests
                     WHERE requested_by = %s
                     ORDER BY requested_at DESC
                     LIMIT 1
                    """,
                    (principal_id,),
                )
                return cursor.fetchone()
