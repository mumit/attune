# Attune

Attune is a memory-aware assistant for Gmail, Google Calendar, Google Chat,
and Slack. It answers natural-language Workspace questions from live data,
drafts and triages work, prepares briefs, detects scheduling conflicts, and
earns autonomy through explicit, audited grants.

## Choose how you run Attune

Attune ships as one product with two deployment modes. Full detail, exact
commands, and the tradeoffs between them are in the
[deployment modes guide](docs/modes.md); this table is the short version.

| Mode | Who it's for | How you run it |
|---|---|---|
| Self-hosted, polling (the default) | One person, on their own machine, VM, or server | `attune init --quick` → `attune doctor` → `attune run` |
| Self-hosted, Google Pub/Sub push | The same one person, wanting lower-latency push instead of polling | The polling setup above, plus Pub/Sub and the stateless republisher |
| Hosted multi-tenant, as a customer | Someone signing in to an operator-run instance | Sign in with Google in a browser — nothing to install |
| Hosted multi-tenant, as the operator | Whoever runs the service on GCP for others | Terraform (foundation → data → runtime → edge), then gate-by-gate activation |

Self-hosted is the only mode that is fully built and runnable today; hosted
multi-tenant is a development-stage platform gated behind default-off
activation flags and is not publicly operated. See
[the modes guide](docs/modes.md) for what that means in practice.

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

This is the self-hosted, polling path. For the Pub/Sub push variant or the
hosted multi-tenant service, see the [deployment modes guide](docs/modes.md).

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

Use `attune init --quick` for the shortest path through the wizard: it asks
only the essential questions (workspace backend, data directory, mailbox,
internal domains, LLM base/key/default model, embedding model/key) and
defaults everything else, printing the follow-up commands for channels and
Google credentials. Add `--recommended` (with `--quick` or the full wizard)
to fill the documented mixed-provider model routing from
[`configuration.md`](docs/configuration.md) as editable defaults. Use
`attune init --google-setup` for a guided, resumable checklist through the
Google Cloud Console ceremony, with exact copy-paste values and no silent
cloud mutations.

## Where to go next

- **I want a personal assistant on my own machine** — start with the
  [self-hosted install guide](docs/install/self-hosted.md) and the
  [deployment modes guide](docs/modes.md). The complete
  [configuration reference](docs/configuration.md) documents every setting,
  the [user journey](docs/user-journey.md) describes day-to-day use, and
  [Deployment](docs/deployment.md) covers always-on hosting and the
  Google Pub/Sub push variant.
- **I operate the hosted service** — start with the
  [hosted operator runbook](docs/install/hosted-operator.md), which
  operationalizes the [GCP operated-service architecture](docs/hosted-gcp.md)
  and the operator section of the
  [deployment modes guide](docs/modes.md). The
  [hosted sign-in guide](docs/identity-platform.md) documents the separate
  Google Identity Platform and Workspace consent clients; the
  [hosted policy ceremony](docs/hosted-policy.md) defines the
  recent-authenticated fixed R0 owner choice, and the
  [hosted channel preference ceremony](docs/hosted-channels.md) keeps
  Slack/Google Chat interaction and brief choices independent without
  treating a preference as an installed route. The
  [hosted data-lifecycle contract](docs/data-lifecycle.md) defines
  retention, export, deletion, and backup restore suppression; the
  [hosted customer-export boundary](docs/customer-export.md) defines fixed
  scopes, dedicated identities, encrypted temporary objects, and the
  recent-authenticated download ceremony. Internally, the approved
  [dispatch-broker contract](docs/dispatch-broker.md) defines exclusive task
  authority and queue delivery, the
  [audit-writer contract](docs/audit-writer.md) defines the intent-only path
  to hosted hash-chained audit events, the
  [secret-broker contract](docs/secret-broker.md) defines connector
  credential encryption and use, and the
  [capability-gateway contract](docs/capability-gateway.md) defines how
  untrusted model proposals become typed, tenant-bound admission without
  becoming provider requests.
- **I'm reviewing security** — begin with the
  [security review guide](docs/security-review.md), which maps the
  implemented architecture, service inventory, cryptography, and channel
  trust model to code and evidence. The normative
  [security architecture](docs/security-architecture.md) defines trust
  boundaries, control requirements, red-team cases, and hosted launch gates.
- **I'm implementing an MCP server** — use the
  [versioned Workspace contract](docs/mcp-contract.md).
- **I want the design history** — read [Design](docs/design.md), the
  [durable decisions record](docs/decisions.md), and the
  [roadmap](docs/roadmap.md), plus the point-in-time review trilogy: a full
  [current state](docs/current-state.md) review, the
  [gap analysis](docs/gap-analysis.md) against the product goal, and the
  [future-state plan](docs/future-state.md).

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Attune is MIT licensed.
