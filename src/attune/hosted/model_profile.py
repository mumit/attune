"""Tenant-bound, effect-free hosted model profile preference.

Mirrors ``hosted_channels.py`` exactly: a bounded owner preference -- one row
per tenant, a name from a fixed vocabulary -- with a plain read under
ordinary RLS and a SECURITY DEFINER function for the one mutation path
(``attune.set_tenant_model_profile``, migration 0047). The worker also reads
this table directly (ordinary SELECT, the same trust it already has for
``hosted_channel_preferences``) to resolve which profile to pass to the
model gateway when ``ATTUNE_ENABLE_TENANT_MODEL_PROFILES`` is on.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from uuid import UUID

from .model_gateway import PROFILES, STANDARD_PROFILE
from .repositories import ConnectionFactory
from .tenant import TenantContext, tenant_transaction


@dataclass(frozen=True)
class TenantModelProfile:
    schema_version: int
    profile: str
    revision: int


class PostgresTenantModelProfileRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def read(self, context: TenantContext) -> TenantModelProfile | None:
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT schema_version, profile, revision
                      FROM attune.tenant_model_preferences
                     WHERE tenant_id = %s
                    """,
                    (context.tenant_id,),
                )
                row = cursor.fetchone()
        return TenantModelProfile(*row) if row is not None else None

    def read_profile_name(self, context: TenantContext) -> str:
        """The resolved profile name a model caller should pass to the
        gateway: the stored preference, or the fixed default when the tenant
        has never set one -- never ``None``, never a raw endpoint."""
        current = self.read(context)
        return current.profile if current is not None else STANDARD_PROFILE

    def set(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        profile: object,
    ) -> TenantModelProfile:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        if not isinstance(profile, str) or profile not in PROFILES:
            raise ValueError("model profile is invalid")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT profile, revision
                      FROM attune.set_tenant_model_profile(%s, %s, %s)
                    """,
                    (principal_id, session_id, profile),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("model profile configuration returned no state")
        return TenantModelProfile(1, row[0], row[1])
