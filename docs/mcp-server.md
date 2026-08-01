# Attune as an MCP server

Attune is a workspace-connector *client* of MCP (see
[`mcp-contract.md`](mcp-contract.md)) and, separately, an MCP *server*
(`src/attune/mcp_server/`, Phase P8, build prompt 34): a second process other
agents can query for what a principal's Attune instance already knows,
without those agents each rebuilding triage, importance, and memory
themselves. The two roles use different contracts and are not interchangeable
— a change to one does not imply a change to the other.

## Why a server, not just a client

Slack, Calendly, Cal.com, Granola, Glean, and Google Workspace all now expose
MCP servers. For a one-principal product, being the memory-and-importance
layer other agents query is a stronger position than competing as another
front door — see `docs/landscape-2026.md` for the fuller competitive
argument and `docs/plan-2026-h2.md`'s Phase P8.

## A separate, credential-free process

`mcp_server/server.py`'s `AttuneMcpServer` depends only on:

- `mcp_server/reader.py`'s `RuntimeReadPort` protocol (a read-only view onto
  brief/importance/memory/playbook state — never the connector, model
  client, or OAuth credentials directly),
- `mcp_server/auth.py`'s `AgentAllowlist` (public agent ids and token
  hashes, never a secret) and a pluggable token verifier,
- `mcp_server/proposals.py`'s proposal store, and
- an audit log.

Its constructor holds no parameter shaped like a credential —
`settings`/`connector`/`api_key`/`oauth`/`google`/`slack`/`model`/`mem0`/
`qdrant` are all structurally absent. `CLAUDE.md`'s "the runtime holding
user credentials exposes no public port" rule binds the credential-holding
runtime, not this process — this is deliberately the one process that CAN
take a public listener, because it holds nothing worth stealing on its own.
The same boundary shape the stateless republisher and the hosted dispatch
brokers already hold, applied to a new surface.

## Read tools first, write gated behind Tasks

`mcp_server/reader.py` defines six read-only tools — `attune.brief`,
`attune.what_matters`, `attune.importance`, `attune.memory.search`,
`attune.pending`, `attune.playbook.show` — as bounded, documented response
shapes (capped list length and text size, matching
`hosted/google_provider.py`'s data-minimization discipline) built from view
dataclasses, never Attune's internal `Brief`/`Bullet`/`MemoryRecord` types
directly. No read tool takes a principal/tenant argument — one Attune
instance has exactly one principal, so there is no selection surface for a
calling agent's arguments to attack.

`attune.propose` produces a **task, never an effect**. `mcp_server/
gateway.py`'s `McpCapabilityGateway` mirrors `hosted/capability_gateway.py`'s
shape (immutable registry, strict envelope parsing, frozen bounded
arguments) without importing it — that module is the hosted, multi-tenant,
Postgres-bound admission gate; this one serves a single self-hosted instance
with no tenant/connector/Postgres dependency, matching the existing
layering (hosted depends on core self-hosted primitives, never the
reverse). A curated, deliberately small registry (`mail.draft_reply`,
`mail.add_label` today) maps a dotted MCP-facing name to an
`orchestrator.capabilities` `Action`/`Domain`/`RiskTier`. Admission produces,
at most, an `AdmittedProposal`; `mcp_server/server.py` persists it as an
`mcp_server/tasks.py` `Task` in `INPUT_REQUIRED` state
(`mcp_server/proposals.py`'s `JsonMcpProposalStore`) and returns it — no
connector, no draft-approve graph, no effect runs at proposal time.

Task vocabulary is A2A-compatible on purpose: `TaskState` names
`input_required`/`auth_required` exactly as A2A's eight-state task lifecycle
does (`docs/landscape-2026.md` §10) — MCP + A2A has crystallized as the
two-layer reference model, and an MCP host that also speaks A2A shouldn't
need a second vocabulary to reason about Attune's approval gate.

**Resolving a proposal today only records the human's decision** —
`attune mcp-server proposals list/approve/reject` (`cli/mcp_cmd.py`)
resolves an `McpProposal` to `COMPLETED`/`REJECTED` directly, since
`orchestrator/pending.py`'s existing `PendingApprovals` is keyed by a live
LangGraph workflow thread id and an MCP-originated proposal has neither. It
does **not** yet drive a real connector effect through
`orchestrator.capabilities`' `apply` functions — wiring an approved MCP
proposal into the same dispatch path a card's approval already uses is a
deliberately deferred next step, not an oversight.

## Authorization

OAuth 2.1 with Resource Indicators (RFC 8707), fail-closed at every step.
`mcp_server/auth.py`'s `authorize_request` verifies the bearer token (a
pluggable `TokenVerifier`, mirroring `hosted/task_envelope.py`'s
`_verify_claims` pattern), requires the token's `aud` claim to equal this
deployment's own resource identifier exactly (refusing a token minted for a
different MCP resource even if it verifies cleanly — the standard's answer
to token-passthrough), then requires the `sub` (agent id) AND the token's
own hash to match an allowlist entry.

A calling agent is not the principal: it gets its own identity, its own
allowlist, and its own audit actor type (`actor_type="mcp_agent"`, distinct
from hosted's `"principal"`/`"workload"`). Every invocation, success or
refusal, is audited with that actor type and the agent id.

Every `BoundedArguments` contract is an exact allowlist of field names, and
`FORBIDDEN_ARGUMENT_KEYS` (`rung`, `actor`, `tenant_id`, `principal_id`,
`risk_tier`, `scope`, ...) is checked independently as a second,
capability-registration-independent layer — nothing an agent sends selects
a tenant, an actor, or a rung, structurally, not by runtime value-sniffing.

## What this surface deliberately does not do

- **No meeting capture.** Meeting context is consumed over MCP (read-only,
  into the attention store) when a principal already uses a consented
  capture tool — Attune does not add its own recording capability. See
  `docs/decisions.md`'s "Meeting context over MCP: consume, don't capture"
  entry.
- **No browser automation.** Declined for now — see `docs/decisions.md`'s
  "Declining browser automation, for now" entry for the full reasoning and
  the conditions that would revisit it.
- **No live effect from an approved MCP proposal yet.** See above.

## Tests

`tests/test_mcp_server.py` runs the whole server end to end from in-memory
fakes and a tmp-path-backed proposal store — no network, no live
credentials. It proves the credential-free claim both structurally (the
constructor's parameter names) and functionally, asserts no effect occurs
until a human resolves a proposal, asserts the distinct `mcp_agent` actor
type and allowlist refusal (including a wrong resource indicator or a token
hash not on the allowlist), and parametrizes over the same
prompt-injection/authority-escalation attack shapes build prompt 27's
injection suite already exercises for the rest of the product.
