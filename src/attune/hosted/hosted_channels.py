"""Tenant-bound, effect-free hosted channel preferences."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from uuid import UUID

from .repositories import ConnectionFactory
from .tenant import TenantContext, tenant_transaction

CHANNELS = frozenset({"google_chat", "slack"})


@dataclass(frozen=True)
class HostedChannelPreferences:
    schema_version: int
    revision: int
    interaction_channels: tuple[str, ...]
    brief_channels: tuple[str, ...]
    onboarding_revision: int
    status: str


def normalize_channels(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    if any(not isinstance(item, str) or item not in CHANNELS for item in value):
        raise ValueError(f"{name} contains an unsupported channel")
    normalized = tuple(sorted(set(value)))
    if len(normalized) != len(value):
        raise ValueError(f"{name} contains duplicate channels")
    return normalized


class PostgresHostedChannelRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def read(
        self, context: TenantContext, *, principal_id: UUID
    ) -> HostedChannelPreferences | None:
        if not isinstance(principal_id, UUID):
            raise TypeError("principal_id must be a UUID")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT preference.schema_version, preference.revision,
                           preference.interaction_channels,
                           preference.brief_channels, onboarding.revision,
                           onboarding.channels_status
                      FROM attune.hosted_channel_preferences AS preference
                      JOIN attune.hosted_onboarding_states AS onboarding
                        ON onboarding.tenant_id = preference.tenant_id
                       AND onboarding.owner_principal_id =
                           preference.owner_principal_id
                     WHERE preference.tenant_id = %s
                       AND preference.owner_principal_id = %s
                    """,
                    (context.tenant_id, principal_id),
                )
                return _preferences(cursor.fetchone())

    def configure(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        interaction_channels: object,
        brief_channels: object,
    ) -> HostedChannelPreferences:
        if not isinstance(principal_id, UUID) or not isinstance(session_id, UUID):
            raise TypeError("principal_id and session_id must be UUIDs")
        interaction = normalize_channels("interaction_channels", interaction_channels)
        briefs = normalize_channels("brief_channels", brief_channels)
        if not interaction and not briefs:
            raise ValueError("at least one channel purpose is required")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT schema_version, preference_revision,
                           interaction_channels, brief_channels,
                           onboarding_revision, channels_status
                      FROM attune.configure_hosted_channels(%s, %s, %s, %s)
                    """,
                    (principal_id, session_id, list(interaction), list(briefs)),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("hosted channel configuration returned no state")
        return _preferences(row)  # type: ignore[return-value]


def _preferences(row) -> HostedChannelPreferences | None:
    if row is None:
        return None
    return HostedChannelPreferences(
        row[0], row[1], tuple(row[2]), tuple(row[3]), row[4], row[5]
    )
