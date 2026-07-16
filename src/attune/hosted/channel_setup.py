"""Effect-free setup state for hosted channel installations."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .repositories import ConnectionFactory, _fixed_hash
from .tenant import TenantContext, tenant_transaction

MECHANISMS = {"google_chat": "link_code", "slack": "oauth"}


@dataclass(frozen=True)
class HostedChannelSetupTransaction:
    id: UUID
    preference_revision: int
    provider: str
    mechanism: str
    state: str
    expires_at: datetime


@dataclass(frozen=True)
class HostedChannelProviderState:
    provider: str
    selected: bool
    setup_state: str
    destination_state: str


class PostgresHostedChannelSetupRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def begin(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        provider: str,
        mechanism: str,
        secret_hash: bytes,
        expires_at: datetime,
    ) -> HostedChannelSetupTransaction:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        if MECHANISMS.get(provider) != mechanism:
            raise ValueError("unsupported channel setup mechanism")
        _fixed_hash("secret_hash", secret_hash)
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError("channel setup expiry must be timezone-aware")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT transaction_id, preference_revision, provider,
                           mechanism, state, expires_at
                      FROM attune.begin_hosted_channel_setup_v2(
                          %s, %s, %s, %s, %s, %s
                      )
                    """,
                    (
                        principal_id,
                        session_id,
                        provider,
                        mechanism,
                        secret_hash,
                        expires_at,
                    ),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("channel setup returned no state")
        return HostedChannelSetupTransaction(*row)

    def read(
        self, context: TenantContext, *, principal_id: UUID
    ) -> tuple[HostedChannelProviderState, ...]:
        if not isinstance(principal_id, UUID):
            raise TypeError("principal_id must be a UUID")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    WITH preference AS (
                        SELECT interaction_channels || brief_channels AS selected
                          FROM attune.hosted_channel_preferences
                         WHERE tenant_id = %s AND owner_principal_id = %s
                    ), providers(provider) AS (
                        VALUES ('google_chat'::text), ('slack'::text)
                    )
                    SELECT providers.provider,
                           providers.provider = ANY(COALESCE(
                               preference.selected, ARRAY[]::text[]
                           )) AS selected,
                           COALESCE(transaction.state, 'not_started'),
                           COALESCE(
                               CASE
                                   WHEN destination.status = 'pending_test'
                                    AND destination.route_version IS NULL
                                   THEN 'needs_relink'
                                   ELSE destination.status
                               END,
                               'not_started'
                           )
                      FROM providers
                      LEFT JOIN preference ON true
                      LEFT JOIN LATERAL (
                          SELECT setup.state
                            FROM attune.hosted_channel_setup_transactions setup
                           WHERE setup.tenant_id = %s
                             AND setup.owner_principal_id = %s
                             AND setup.provider = providers.provider
                           ORDER BY setup.created_at DESC
                           LIMIT 1
                      ) transaction ON true
                      LEFT JOIN attune.hosted_channel_destinations destination
                        ON destination.tenant_id = %s
                       AND destination.owner_principal_id = %s
                       AND destination.provider = providers.provider
                     ORDER BY providers.provider
                    """,
                    (
                        context.tenant_id,
                        principal_id,
                        context.tenant_id,
                        principal_id,
                        context.tenant_id,
                        principal_id,
                    ),
                )
                rows = cursor.fetchall()
        return tuple(HostedChannelProviderState(*row) for row in rows)

    def pending_destination_id(
        self, context: TenantContext, *, principal_id: UUID, provider: str
    ) -> UUID:
        if not isinstance(principal_id, UUID):
            raise TypeError("principal_id must be a UUID")
        if provider != "google_chat":
            raise ValueError("unsupported delivery-test provider")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT destination.id
                      FROM attune.hosted_channel_destinations destination
                      JOIN attune.hosted_channel_preferences preference
                        ON preference.tenant_id = destination.tenant_id
                       AND preference.owner_principal_id = destination.owner_principal_id
                     WHERE destination.tenant_id = %s
                       AND destination.owner_principal_id = %s
                       AND destination.provider = %s
                       AND destination.visibility = 'owner_dm'
                       AND destination.status = 'pending_test'
                       AND %s = ANY(
                           preference.interaction_channels || preference.brief_channels
                       )
                    """,
                    (context.tenant_id, principal_id, provider, provider),
                )
                rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError("canonical pending destination is unavailable")
        return rows[0][0]
