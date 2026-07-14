# Attune developer guide

Attune is a one-principal, memory-aware assistant for Gmail, Google Calendar,
Google Chat, and Slack. Read `docs/design.md` before changing architecture and
record durable design decisions in `docs/decisions.md`.

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
- Keep OAuth and MCP as real workspace alternatives. Direct OAuth is the
  default; MCP currently requires polling and Streamable HTTP.
- Keep hosting portable. Name Google Pub/Sub when code is specifically tied to
  it; otherwise use backend-neutral concepts.
- Slack and Google Chat are optional peers. Respect brief, approval,
  notification, and interaction routes at every send/receive site.
- The runtime holding user credentials exposes no public port. The standalone
  republisher is stateless and must not gain model, memory, or user OAuth access.
- Preserve human approval, actor allowlists, idempotency, durable checkpoints,
  retry-before-cursor semantics, and append-only audit behavior.

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
