# Attune developer guide

Attune is a one-principal, memory-aware assistant for Gmail, Google Calendar,
Google Chat, and Slack, shipped as two deployment planes — a self-hosted
local runtime and a tenant-scoped hosted service (`src/attune/hosted/`) —
that are converging onto one shared core rather than staying two full
reimplementations of the same product. Read `docs/design.md` before changing
architecture, check `docs/plan-2026-h2.md` for the current build-prompt
roadmap and phase status, and record durable design decisions in
`docs/decisions.md`.

Documentation upkeep is part of every change, not a separate ask: when a
change makes a statement in `docs/*.md` (including this file) stale,
incomplete, or contradictory, update it in the same pass — do not wait to
be asked. This applies whether the change is code or docs-only.

## Commands

```bash
pip install -e ".[dev]"
pytest -q
attune init
attune doctor
attune run
```

## Boundaries

- Keep the product provider-neutral. Model gateways are configured through the
  official OpenAI SDK using base URL, API key, and model identifiers.
- Keep one principal per instance. Do not add organization-named profiles.
- MCP is used in both directions, and they are not the same contract. As a
  *client*, MCP is a real workspace-connector alternative to direct OAuth
  (currently polling + Streamable HTTP); Direct OAuth stays the default.
  Attune is also an MCP *server* (`src/attune/mcp_server/`) — a credential-
  free process exposing read-only tools, with any write gated behind the
  Tasks propose/approve flow, never direct execution. Changes to either
  direction's required tools or envelopes must version `docs/mcp-contract.md`.
- Keep hosting portable. Name Google Pub/Sub when code is specifically tied to
  it; otherwise use backend-neutral concepts.
- Slack and Google Chat are optional peers. Respect brief, approval,
  notification, and interaction routes at every send/receive site. Selected
  routes must remain covered by Doctor's fail-fast local configuration check.
- The runtime holding user credentials exposes no public port. The standalone
  republisher is stateless and must not gain model, memory, or user OAuth access.
- Preserve human approval, actor allowlists, idempotency, durable checkpoints,
  retry-before-cursor semantics, append-only audit behavior, and — for every
  capability that performs an irreversible effect — either a compensating
  (undo) action or an explicit `irreversible=True` declaration.
- New actions register as one declarative descriptor in
  `orchestrator/capabilities.py`'s `Capability`/`CapabilityRegistry` (and,
  where hosted execution for that action exists, hosted's
  `CapabilityDefinition`) — never a bespoke per-action gate function, a new
  compiled graph, or a hand-rolled dispatch branch. This is the collapse
  build prompt 30 did; do not reintroduce the ceremony it replaced.
- Build once. A behavior needed on both planes shares its dataclasses and
  rule engine in core, with only storage/tenancy differing per plane —
  `hosted/intelligence.py` is the reference pattern, `docs/decisions.md`'s
  "Converge the two planes" entries record what's shared today and what
  remains genuinely plane-specific. Do not implement the same capability
  twice.

## Configuration

New variables use the `ATTUNE_` prefix. `.env.example` is the source-of-truth
inventory. `attune init` must remain an in-place, line-preserving editor: load
existing values as defaults, preserve comments/unknowns/secrets, migrate legacy
keys, create a backup, write atomically, and reserve `--fresh` for replacement.

Never read, print, commit, or overwrite a user's populated `.env` in tests or
tooling. Tests use explicit fake environment dictionaries and injected clients.

## Testing

All core behavior is offline-testable. Inject API services, model clients,
connectors, send functions, clocks, and persistence paths. The standalone
republisher has tests under `deploy/republisher`; run those from
that directory when changing webhook behavior.
