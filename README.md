# Attune

Attune is a memory-aware assistant for Gmail, Google Calendar, Google Chat,
and Slack. It drafts and triages work, prepares briefs, detects scheduling
conflicts, and earns autonomy through explicit, audited grants.

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
attune init
attune doctor
attune run
```

`attune init` edits an existing `.env` in place: current values become defaults,
comments and unknown keys are preserved, legacy names are migrated, and a
`.env.bak` backup is written. Use `attune init --fresh` only when you explicitly
want a new configuration.

See [Getting started](docs/getting-started.md), [Design](docs/design.md), and
[Deployment](docs/deployment.md). MCP server implementers should use the
[versioned Workspace contract](docs/mcp-contract.md).

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Attune is MIT licensed.
