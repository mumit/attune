# Getting started

## 1. Install

Python 3.10+ is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,orchestrator,memory,google,slack,mcp]"
```

Only install the optional channel/backend extras you use in a production image.

## 2. Configure

```bash
cp .env.example .env
attune init
```

The initializer reads the existing file and offers its values as defaults. A
blank answer keeps the current value; `-` clears one. It preserves comments,
unknown variables, and secrets, creates `.env.bak`, writes atomically, and sets
owner-only permissions. `--fresh` intentionally starts a new managed file.

Configure an OpenAI-compatible base URL, bearer credential, and at least one
model. A single `ATTUNE_MODEL_DEFAULT` is enough; task-specific model variables
are optional.

Choose one workspace backend:

- `google_oauth`: default and best-supported. Create a Google OAuth desktop
  client, set `ATTUNE_GOOGLE_CREDENTIALS_FILE`, and complete consent.
- `mcp`: set a shared `ATTUNE_MCP_URL`, or Gmail and Calendar URLs separately.
  Add `ATTUNE_MCP_TOKEN` if the server requires bearer authentication. MCP is
  currently polling-only and requires servers exposing Attune's documented
  Gmail and Calendar [version-1 tool contract](mcp-contract.md).

## 3. Choose channels

Slack and Google Chat are independent and optional. Set explicit routes:

```dotenv
ATTUNE_BRIEF_CHANNELS=google_chat
ATTUNE_APPROVAL_CHANNEL=google_chat
ATTUNE_NOTIFICATION_CHANNELS=google_chat
ATTUNE_INTERACTION_CHANNELS=google_chat
```

Use `slack`, `google_chat`, both comma-separated for multi-destination routes,
or an empty value to disable a route. Approvals accept exactly one channel.

Slack interaction needs Socket Mode app and bot tokens plus an allowed-user
list. Google Chat needs a Chat app service-account credential for authored
messages, a target space, allowed users, and the verified republisher endpoint
for incoming messages and card actions. Direct Google user OAuth is kept
separate from the Chat app identity.

## 4. Validate and run

```bash
attune doctor
attune run
```

Doctor fails before the runtime starts when a selected channel lacks its bot or
app credential, proactive destination, actor allowlist, or required Chat
interaction subscription. An empty route remains a valid way to disable that
delivery or interaction behavior.

Polling is the portable default and requires no inbound application port.
Start with it. Move to `google_pubsub` only when lower latency justifies the
additional Google Cloud topics, subscriptions, watches, and republisher.

While running, idle polls are quiet except for a five-minute heartbeat. A
successful source change produces a count-only `poll activity` log. Gmail only
posts an approval for mail triaged as actionable; noise is audit-only. Calendar
only notifies immediately about conflicts, while ordinary appointments appear
in that day's scheduled brief.

Run tests with `pytest -q`.
