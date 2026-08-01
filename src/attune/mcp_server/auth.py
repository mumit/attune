"""OAuth 2.1 with Resource Indicators for calling agents (build prompt 34,
task 4): "authorization is not optional."

A calling agent is **not** the principal — CLAUDE.md's boundary rule applies
here exactly as it does to every other credential surface (workspace OAuth,
MCP server auth, Chat app auth, channel tokens, model credentials are
already separate boundaries; a calling agent's identity is one more). It
gets its own identity, its own allowlist, and its own audit actor type
(``mcp_agent`` — see :mod:`attune.mcp_server.server`).

Token verification is injected (``TokenVerifier``), mirroring
``hosted/task_envelope.py``'s ``_verify_claims`` pattern used by the
dispatch broker: this module owns the fail-closed *authorization* decision
(unknown agent refused, wrong resource refused), never the cryptographic
*verification* of a bearer token, which is a pluggable, swappable concern
(a real deployment verifies a JWT against its authorization server's JWKS;
tests inject a fake).

Resource Indicators (RFC 8707, referenced by the MCP authorization spec)
are the concrete mechanism: a token's ``aud`` claim must name THIS MCP
server's own canonical resource identifier, or the token is refused
regardless of whose signature verifies. This is the standard's answer to
token-passthrough — a token minted for a different MCP resource must not
work here just because the same authorization server issued it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class AgentAuthorizationError(Exception):
    """Fail-closed authorization refusal. ``code`` is a stable, non-
    reflected reason -- never includes the raw token or untrusted claims."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AgentIdentity:
    """A calling agent's verified identity for THIS request -- distinct
    from the principal (the human Attune instance serves) at every layer:
    its own id, its own scopes, and (see ``server.py``) its own audit
    actor type."""

    agent_id: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class TokenVerifier(Protocol):
    def __call__(self, token: str) -> Mapping[str, Any]:
        """Verify a bearer token and return its claims (``sub``, ``aud``,
        ``scope``). Raises on an invalid/expired/unverifiable token. Never
        returns a value for a token that fails verification -- there is no
        "verified but with an empty identity" state."""
        ...


@dataclass(frozen=True)
class AllowedAgent:
    """One entry in the calling-agent allowlist. ``token_hash`` is a
    SHA-256 digest, never the raw token -- the allowlist is safe to log,
    diff in a PR, and hold in memory without itself becoming a secret."""

    agent_id: str
    token_hash: str
    scopes: frozenset[str]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AgentAllowlist:
    """Exact-match, fail-closed registry: an agent not listed here is
    unavailable, the same posture ``hosted.capability_gateway.CapabilityRegistry``
    holds for capabilities and ``dispatch_broker_service``'s
    ``expected_callers`` holds for producer identities."""

    def __init__(self, agents: "tuple[AllowedAgent, ...]"):
        by_id: dict[str, AllowedAgent] = {}
        for agent in agents:
            if agent.agent_id in by_id:
                raise ValueError("duplicate agent_id in allowlist")
            by_id[agent.agent_id] = agent
        self._by_id = by_id

    def resolve(self, *, agent_id: str, token_hash: str) -> "AllowedAgent | None":
        entry = self._by_id.get(agent_id)
        if entry is None or entry.token_hash != token_hash:
            return None
        return entry

    def __len__(self) -> int:
        return len(self._by_id)


def authorize_request(
    token: str,
    *,
    resource: str,
    allowlist: AgentAllowlist,
    verifier: "TokenVerifier",
) -> AgentIdentity:
    """The single fail-closed gate every MCP tool call passes through
    before touching a reader, a proposal store, or the audit log.

    Order matters and each step refuses outright rather than degrading:
    1. The token must verify at all (``verifier`` raises otherwise).
    2. Its ``aud`` claim must equal ``resource`` exactly (RFC 8707 Resource
       Indicator match) -- a token minted for a different MCP resource is
       refused even if it verifies cleanly, which is the whole point of
       Resource Indicators.
    3. Its ``sub`` (agent id) must be on the allowlist, AND the token's own
       hash must match what the allowlist recorded for that agent id --
       an agent id alone is never sufficient; a stolen/forwarded token for
       a different, unlisted agent must not resolve to a listed one.
    """
    try:
        claims = verifier(token)
    except Exception as exc:  # noqa: BLE001 -- any verifier failure is a refusal
        raise AgentAuthorizationError("token_invalid") from exc

    if not isinstance(claims, Mapping):
        raise AgentAuthorizationError("token_invalid")

    audience = claims.get("aud")
    if audience != resource:
        raise AgentAuthorizationError("resource_mismatch")

    agent_id = claims.get("sub")
    if not isinstance(agent_id, str) or not agent_id:
        raise AgentAuthorizationError("token_invalid")

    entry = allowlist.resolve(agent_id=agent_id, token_hash=hash_token(token))
    if entry is None:
        raise AgentAuthorizationError("agent_not_allowed")

    scope_claim = claims.get("scope", "")
    if not isinstance(scope_claim, str):
        raise AgentAuthorizationError("token_invalid")
    token_scopes = frozenset(scope_claim.split())
    # The effective scope is the INTERSECTION of what the token claims and
    # what the allowlist grants that agent -- neither side alone is
    # authoritative. A compromised authorization server minting an
    # over-scoped token still can't exceed what this deployment's operator
    # explicitly allowlisted for that agent.
    return AgentIdentity(agent_id=entry.agent_id, scopes=token_scopes & entry.scopes)
