"""Fixed owner-confirmed policy ceremony for hosted private alpha."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from uuid import UUID

from .repositories import ConnectionFactory
from .tenant import TenantContext, tenant_transaction

PROFILE = "private_alpha_read_only"
CAPABILITIES = ("google.workspace.connection.verify",)


@dataclass(frozen=True)
class HostedPolicyActivation:
    policy_version: int
    onboarding_revision: int
    status: str


class PostgresHostedPolicyRepository:
    """Call the sole fixed policy mutation function under tenant context."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def activate_read_only(
        self, context: TenantContext, *, principal_id: UUID, session_id: UUID
    ) -> HostedPolicyActivation:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT policy_version, onboarding_revision, policy_status
                      FROM attune.activate_hosted_read_only_policy(%s, %s)
                    """,
                    (principal_id, session_id),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("hosted policy activation returned no state")
        return HostedPolicyActivation(*row)
