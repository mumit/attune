"""``attune.propose`` admission (build prompt 34, tasks 2-4): converts an
untrusted MCP tool argument blob into, at most, a durable, human-reviewable
:class:`~attune.mcp_server.tasks.Task` — never an executed effect.

Deliberately mirrors ``hosted/capability_gateway.py``'s shape (immutable
registry, strict envelope parsing, frozen bounded arguments) rather than
importing it: that module is the hosted, multi-tenant, Postgres-backed
plane's admission gate, bound to a ``TenantContext``; this one serves a
single self-hosted instance's one principal (``docs/design.md``) and has no
tenant, connector, or Postgres dependency at all. The self-hosted
``orchestrator.capabilities`` module already depends on neither, and this
module follows the same layering: hosted code may depend on core
self-hosted primitives (see that module's own docstring, "RiskTier's
canonical home is ... orchestrator.autonomy"), never the reverse.

The constraint the build prompt states directly -- "nothing an agent sends
selects a tenant, an actor, or a rung" -- is enforced twice, structurally,
not by inspecting values at runtime for badness:

1. Every :class:`BoundedArguments` contract is an EXACT allowlist of field
   names; an unlisted key (``rung``, ``actor``, ``tenant_id``, ...) makes
   the whole proposal invalid, full stop.
2. :data:`FORBIDDEN_ARGUMENT_KEYS` is checked independently, before any
   per-capability contract runs, as a second, capability-independent
   layer -- so a new capability registered later without perfectly
   disjoint field names still can't accidentally admit one of these.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ..orchestrator.autonomy import Action, Domain, RiskTier, max_rung_for_risk_tier

MAX_PROPOSAL_BYTES = 16_384

# Defense in depth (see module docstring): no bounded argument contract may
# ever admit a field with one of these names, regardless of what a specific
# capability's allowlist declares.
FORBIDDEN_ARGUMENT_KEYS = frozenset(
    {
        "rung", "autonomy", "grant", "actor", "actor_id", "identity",
        "role", "scope", "scopes", "tenant", "tenant_id", "principal",
        "principal_id", "connector", "connector_id", "policy",
        "policy_version", "risk", "risk_tier", "url", "endpoint",
    }
)


class CapabilityDenied(Exception):
    """Fail-closed admission result. ``code`` is stable and never includes
    reflected untrusted content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StringField:
    name: str
    min_length: int = 1
    max_length: int = 4000


@dataclass(frozen=True)
class BoundedArguments:
    """An exact allowlist of string fields -- a capability's entire
    argument surface. Extra keys, missing keys, wrong types, or an
    out-of-bounds length all deny the whole proposal."""

    fields: "tuple[StringField, ...]"

    def reconstruct(self, value: "Mapping[str, Any]") -> "Mapping[str, Any]":
        allowed = {f.name for f in self.fields}
        if set(value) != allowed:
            raise CapabilityDenied("arguments_invalid")
        if allowed & FORBIDDEN_ARGUMENT_KEYS:
            # A capability registration bug, not untrusted input -- caught
            # here so it fails loudly at registry-construction-adjacent
            # call sites rather than silently admitting a forbidden field.
            raise CapabilityDenied("arguments_invalid")
        out: dict[str, Any] = {}
        for f in self.fields:
            raw = value[f.name]
            if not isinstance(raw, str) or not f.min_length <= len(raw) <= f.max_length:
                raise CapabilityDenied("arguments_invalid")
            out[f.name] = raw
        return out


@dataclass(frozen=True)
class McpProposableCapability:
    """One capability an MCP agent may request via ``attune.propose`` --
    deliberately a small, explicit, curated subset of
    ``orchestrator.capabilities``' full registry, named with a dotted
    MCP-facing string distinct from the internal ``Action``/``Domain``
    pair it maps to."""

    name: str
    action: Action
    domain: Domain
    risk_tier: RiskTier
    arguments: BoundedArguments


class McpCapabilityRegistry:
    """Immutable, exact-name registry -- unknown or unregistered
    capabilities are unavailable, never partially matched."""

    def __init__(self, capabilities: "tuple[McpProposableCapability, ...]"):
        by_name: dict[str, McpProposableCapability] = {}
        for cap in capabilities:
            if cap.name in by_name:
                raise ValueError(f"duplicate MCP capability registration: {cap.name!r}")
            by_name[cap.name] = cap
        self._by_name = MappingProxyType(by_name)

    def get(self, name: str) -> "McpProposableCapability | None":
        return self._by_name.get(name)


def build_default_mcp_capability_registry() -> McpCapabilityRegistry:
    """The initial, deliberately small curated set (build prompt 34): a
    mail reply draft and an add-label hygiene action. Both are already
    PROPOSE-gated in ``orchestrator.capabilities`` -- ``attune.propose``
    grants no additional autonomy beyond what a human approves; see
    ``docs/decisions.md``."""
    return McpCapabilityRegistry(
        (
            McpProposableCapability(
                name="mail.draft_reply",
                action=Action.DRAFT_REPLY,
                domain=Domain.MAIL,
                risk_tier=RiskTier.R2,
                arguments=BoundedArguments(
                    (
                        StringField("thread_id", 1, 256),
                        StringField("body", 1, 4000),
                    )
                ),
            ),
            McpProposableCapability(
                name="mail.add_label",
                action=Action.ADD_LABEL,
                domain=Domain.MAIL,
                risk_tier=RiskTier.R1,
                arguments=BoundedArguments(
                    (
                        StringField("thread_id", 1, 256),
                        StringField("label", 1, 80),
                    )
                ),
            ),
        )
    )


def _bounded_json_size(value: "Mapping[str, Any]") -> int:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise CapabilityDenied("proposal_invalid") from error
    return len(encoded)


def parse_propose_request(value: object) -> "tuple[int, str, Mapping[str, Any]]":
    """Strict envelope parsing, mirroring
    ``hosted.capability_gateway._parse_proposal``'s shape: exact top-level
    keys, a bounded serialized size, and no coercion of any field."""
    if not isinstance(value, dict) or set(value) != {"version", "capability", "arguments"}:
        raise CapabilityDenied("proposal_invalid")
    if _bounded_json_size(value) > MAX_PROPOSAL_BYTES:
        raise CapabilityDenied("proposal_invalid")
    version = value["version"]
    capability = value["capability"]
    arguments = value["arguments"]
    if (
        type(version) is not int
        or version != 1
        or not isinstance(capability, str)
        or not 1 <= len(capability) <= 120
        or not isinstance(arguments, dict)
    ):
        raise CapabilityDenied("proposal_invalid")
    return version, capability, arguments


@dataclass(frozen=True)
class AdmittedProposal:
    capability: str
    contract_version: int
    action: Action
    domain: Domain
    risk_tier: RiskTier
    arguments: "Mapping[str, Any]"


class McpCapabilityGateway:
    """Validates an untrusted ``attune.propose`` request against the
    curated registry. Produces, at most, an :class:`AdmittedProposal` --
    never executes anything. What happens with an admitted proposal
    (persisting it as a pending :class:`~attune.mcp_server.tasks.Task`) is
    :mod:`attune.mcp_server.server`'s job, not this gateway's."""

    def __init__(self, *, registry: McpCapabilityRegistry):
        self._registry = registry

    def admit(self, proposal: object) -> AdmittedProposal:
        version, capability, untrusted_arguments = parse_propose_request(proposal)
        definition = self._registry.get(capability)
        if definition is None:
            raise CapabilityDenied("capability_unavailable")
        # The ceiling this capability may EVER be granted to -- checked
        # here as a structural sanity assertion on the registry itself,
        # not a per-request decision (an MCP proposal never carries or
        # selects a rung; see the module docstring).
        max_rung_for_risk_tier(definition.risk_tier)
        arguments = definition.arguments.reconstruct(untrusted_arguments)
        return AdmittedProposal(
            capability=definition.name,
            contract_version=version,
            action=definition.action,
            domain=definition.domain,
            risk_tier=definition.risk_tier,
            arguments=arguments,
        )
