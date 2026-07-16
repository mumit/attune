"""Deterministic admission for untrusted hosted capability proposals.

The gateway is deliberately not an executor.  It converts a small, versioned
model proposal into trusted, tenant-bound authority that a producer may use to
create canonical server-side work.  It never accepts identity, connector,
policy, risk, route, URL, or provider-request fields from the proposal.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID

from .repositories import ConnectionFactory
from .tenant import TenantContext, tenant_transaction

_CAPABILITY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_DOMAIN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
MAX_PROPOSAL_BYTES = 16_384


class RiskTier(IntEnum):
    """Product risk tiers from the hosted security architecture."""

    R0 = 0
    R1 = 1
    R2 = 2
    R3 = 3
    R4 = 4


class CapabilityDenied(Exception):
    """Fail-closed admission result with no reflected untrusted content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ArgumentContract(Protocol):
    """Trusted schema that reconstructs bounded provider-neutral arguments."""

    def reconstruct(self, value: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class EmptyArguments:
    """Exact empty-object contract for capabilities with server-derived input."""

    def reconstruct(self, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if value:
            raise CapabilityDenied("arguments_invalid")
        return {}


@dataclass(frozen=True)
class CapabilityDefinition:
    """Infrastructure-owned definition; registry membership means enabled."""

    name: str
    version: int
    risk: RiskTier
    maximum_product_risk: RiskTier
    domain: str
    provider: str
    required_scopes: tuple[str, ...]
    arguments: ArgumentContract

    def __post_init__(self) -> None:
        if not _CAPABILITY.fullmatch(self.name) or len(self.name) > 120:
            raise ValueError("invalid capability name")
        if type(self.version) is not int or self.version != 1:
            raise ValueError("only capability contract version 1 is supported")
        if not isinstance(self.risk, RiskTier) or not isinstance(
            self.maximum_product_risk, RiskTier
        ):
            raise TypeError("risk tiers must be RiskTier values")
        if self.risk > self.maximum_product_risk:
            raise ValueError("capability exceeds its product risk ceiling")
        if not _DOMAIN.fullmatch(self.domain):
            raise ValueError("invalid capability domain")
        if not _PROVIDER.fullmatch(self.provider):
            raise ValueError("invalid capability provider")
        if (
            not self.required_scopes
            or len(self.required_scopes) > 32
            or len(set(self.required_scopes)) != len(self.required_scopes)
            or any(
                not isinstance(scope, str) or not 1 <= len(scope) <= 255
                for scope in self.required_scopes
            )
        ):
            raise ValueError("required scopes must be bounded and unique")


class CapabilityRegistry:
    """Immutable exact-name registry; unknown capabilities are unavailable."""

    def __init__(self, definitions: tuple[CapabilityDefinition, ...]):
        by_name: dict[str, CapabilityDefinition] = {}
        for definition in definitions:
            if definition.name in by_name:
                raise ValueError("capability definitions must be unique")
            by_name[definition.name] = definition
        self._definitions = MappingProxyType(by_name)

    def get(self, name: str) -> CapabilityDefinition | None:
        return self._definitions.get(name)


@dataclass(frozen=True)
class CapabilityAuthority:
    connector_id: UUID
    policy_version: int
    maximum_risk: RiskTier

    def __post_init__(self) -> None:
        if not isinstance(self.connector_id, UUID):
            raise TypeError("connector_id must be a UUID")
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ValueError("policy_version must be positive")
        if not isinstance(self.maximum_risk, RiskTier):
            raise TypeError("maximum_risk must be a RiskTier")


class AuthorityRepository(Protocol):
    def resolve(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        definition: CapabilityDefinition,
    ) -> CapabilityAuthority | None: ...


@dataclass(frozen=True)
class AuthorizedCapability:
    """Trusted, immutable admission result for downstream canonical work."""

    context: TenantContext
    principal_id: UUID
    connector_id: UUID
    capability: str
    contract_version: int
    risk: RiskTier
    policy_version: int
    arguments: Mapping[str, Any]


class TypedCapabilityGateway:
    """Validate an untrusted proposal against registry and durable authority."""

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        authority: AuthorityRepository,
    ):
        self._registry = registry
        self._authority = authority

    def authorize(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        proposal: object,
    ) -> AuthorizedCapability:
        if not isinstance(context, TenantContext) or not isinstance(principal_id, UUID):
            raise TypeError("verified tenant context and principal UUID are required")
        version, capability, untrusted_arguments = _parse_proposal(proposal)
        definition = self._registry.get(capability)
        if definition is None or definition.version != version:
            raise CapabilityDenied("capability_unavailable")
        try:
            reconstructed_arguments = definition.arguments.reconstruct(
                untrusted_arguments
            )
            arguments = _freeze_arguments(reconstructed_arguments)
        except CapabilityDenied:
            raise
        except Exception as error:
            raise CapabilityDenied("arguments_invalid") from error
        try:
            authority = self._authority.resolve(
                context,
                principal_id=principal_id,
                definition=definition,
            )
        except CapabilityDenied:
            raise
        except Exception as error:
            raise CapabilityDenied("authority_unavailable") from error
        if authority is None:
            raise CapabilityDenied("authority_unavailable")
        if definition.risk > authority.maximum_risk:
            raise CapabilityDenied("risk_exceeds_policy")
        return AuthorizedCapability(
            context=context,
            principal_id=principal_id,
            connector_id=authority.connector_id,
            capability=definition.name,
            contract_version=definition.version,
            risk=definition.risk,
            policy_version=authority.policy_version,
            arguments=arguments,
        )


class PostgresCapabilityAuthorityRepository:
    """Resolve principal, active policy/grant, and connector in one snapshot."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def resolve(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        definition: CapabilityDefinition,
    ) -> CapabilityAuthority | None:
        if not isinstance(principal_id, UUID):
            raise TypeError("principal_id must be a UUID")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT autonomy.maximum_risk, autonomy.policy_version,
                           array_agg(connector.id ORDER BY connector.created_at DESC)
                      FROM attune.tenants AS tenant
                      JOIN attune.principals AS principal
                        ON principal.tenant_id = tenant.id
                       AND principal.id = %s
                       AND principal.status = 'active'
                      JOIN attune.policies AS policy
                        ON policy.tenant_id = tenant.id
                       AND policy.active
                      JOIN attune.autonomy_grants AS autonomy
                        ON autonomy.tenant_id = tenant.id
                       AND autonomy.principal_id = principal.id
                       AND autonomy.capability = %s
                       AND autonomy.domain = %s
                       AND autonomy.policy_version = policy.version
                       AND autonomy.revoked_at IS NULL
                      JOIN attune.connectors AS connector
                        ON connector.tenant_id = tenant.id
                       AND connector.principal_id = principal.id
                       AND connector.provider = %s
                       AND connector.status = 'active'
                       AND connector.granted_scopes @> %s::text[]
                     WHERE tenant.id = %s AND tenant.status = 'active'
                     GROUP BY autonomy.id, autonomy.maximum_risk,
                              autonomy.policy_version
                    """,
                    (
                        principal_id,
                        definition.name,
                        definition.domain,
                        definition.provider,
                        list(definition.required_scopes),
                        context.tenant_id,
                    ),
                )
                rows = cursor.fetchall()
        if len(rows) != 1 or len(rows[0][2]) != 1:
            return None
        return CapabilityAuthority(
            connector_id=rows[0][2][0],
            policy_version=rows[0][1],
            maximum_risk=RiskTier(rows[0][0]),
        )


def _parse_proposal(value: object) -> tuple[int, str, Mapping[str, Any]]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "capability",
        "arguments",
    }:
        raise CapabilityDenied("proposal_invalid")
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise CapabilityDenied("proposal_invalid") from error
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise CapabilityDenied("proposal_invalid")
    version = value["version"]
    capability = value["capability"]
    arguments = value["arguments"]
    if (
        type(version) is not int
        or version != 1
        or not isinstance(capability, str)
        or len(capability) > 120
        or _CAPABILITY.fullmatch(capability) is None
        or not isinstance(arguments, dict)
    ):
        raise CapabilityDenied("proposal_invalid")
    return version, capability, arguments


def _freeze_arguments(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy JSON-compatible trusted arguments into immutable containers."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise CapabilityDenied("arguments_invalid") from error
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise CapabilityDenied("arguments_invalid")

    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise CapabilityDenied("arguments_invalid")
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        if item is None or type(item) in {str, int, float, bool}:
            return item
        raise CapabilityDenied("arguments_invalid")

    frozen = freeze(value)
    if not isinstance(frozen, Mapping):
        raise CapabilityDenied("arguments_invalid")
    return frozen
