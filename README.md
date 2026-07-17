# Attune

Attune is a memory-aware assistant for Gmail, Google Calendar, Google Chat,
and Slack. It answers natural-language Workspace questions from live data,
drafts and triages work, prepares briefs, detects scheduling conflicts, and
earns autonomy through explicit, audited grants.

## Why the name

Attune describes the product's purpose: adapting to a principal's context,
preferences, and working rhythm. The Python distribution and command are both
`attune`.

## Architecture choices

- Any OpenAI-compatible `/chat/completions` provider works through the official
  `openai` SDK. Configure a base URL, bearer credential, and model IDs; there is
  no provider-specific client.
- Workspace access is selectable: direct Google OAuth is the default, while an
  MCP Streamable HTTP backend provides a useful credential and policy boundary.
- One instance acts for one principal. There is no organization-specific or
  “personal versus corporate” mode.
- Hosting is portable. Polling is the simplest transport; Google Pub/Sub is an
  explicitly Google-specific advanced option.
- Slack and Google Chat are optional, independently routable surfaces. Briefs
  and notifications can go to several channels; approvals go to one channel to
  avoid duplicate decisions.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,orchestrator,memory,google,slack,mcp]"
cp .env.example .env
attune init --target local
attune brief
attune run
```

The local target edits the environment, displays its deterministic deployment
plan, starts the pinned Qdrant service on `127.0.0.1:6333`, and runs the full
Doctor battery. Setup progress is resumable from
`~/.attune/setup-state.json`; the state contains no configuration values or
credentials. Use `--yes` only when you have already reviewed the displayed
local plan and want non-interactive application.

After setup, `attune status` reads only the secret-free progress record;
`attune status --check` also runs live diagnostics. `attune repair` previews
and reapplies the same owned local plan, then validates it again. Repair refuses
to infer or modify resources when no matching setup record exists.

`attune init` edits an existing `.env` in place: current values become defaults,
comments and unknown keys are preserved, legacy names are migrated, and a
`.env.bak` backup is written. Use `attune init --fresh` only when you explicitly
want a new configuration. Without `--target local`, initialization remains
configuration-only.

See [Getting started](docs/getting-started.md), the complete
[configuration reference](docs/configuration.md), the
[user journey](docs/user-journey.md), [Design](docs/design.md), and
[Deployment](docs/deployment.md). Operators building the managed service should
start with the [GCP operated-service architecture](docs/hosted-gcp.md); the
[hosted sign-in guide](docs/identity-platform.md) documents the separate Google
Identity Platform and Workspace consent clients. External reviewers should
begin with the [security review guide](docs/security-review.md), which maps
the implemented architecture, service inventory, cryptography, and channel
trust model to code and evidence. The normative
[security architecture](docs/security-architecture.md) defines trust boundaries,
control requirements, red-team cases, and hosted launch gates; the
[hosted data-lifecycle contract](docs/data-lifecycle.md) defines retention,
export, deletion, and backup restore suppression; the
[hosted customer-export boundary](docs/customer-export.md) defines fixed
scopes, dedicated identities, encrypted temporary objects, and the
recent-authenticated download ceremony; the approved
[dispatch-broker contract](docs/dispatch-broker.md) defines exclusive task
authority and queue delivery, while the
[audit-writer contract](docs/audit-writer.md) defines the intent-only path to
hosted hash-chained audit events, and the
[secret-broker contract](docs/secret-broker.md) defines connector credential
encryption and use. The
[capability-gateway contract](docs/capability-gateway.md) defines how untrusted
model proposals become typed, tenant-bound admission without becoming provider
requests, and the [hosted policy ceremony](docs/hosted-policy.md) defines the
recent-authenticated fixed R0 owner choice. The [hosted channel preference
ceremony](docs/hosted-channels.md) keeps Slack/Google Chat interaction and brief
choices independent without treating a preference as an installed route. MCP server
implementers should use the [versioned Workspace contract](docs/mcp-contract.md).

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Attune is MIT licensed.
