# Install: self-hosted, single-principal Attune

*This is the complete setup runbook for self-hosted, single-principal
Attune — see [`../modes.md`](../modes.md) for how this compares to the Google
Pub/Sub push variant and the hosted multi-tenant service.*

This is the shortest path to one working Attune instance for one person. Start
with polling and Slack Socket Mode: both are outbound-only, so a workstation,
home server, or ordinary VM can run them without Pub/Sub, Cloud Run, or a public
webhook. Once this works, [`../deployment.md`](../deployment.md) covers
always-on service-ification (systemd, containers) and the Google Cloud Pub/Sub
push variant — read this doc first regardless of which of those you end up on,
since both build on the same install sequence.

## Prerequisites checklist

- A machine you control: a workstation, home server, or ordinary VM. Python
  3.12 recommended, 3.10+ supported.
- A model gateway account: any OpenAI-compatible `/chat/completions` and
  embeddings provider (base URL + API key + model IDs).
- A Google account for Workspace access — either your own (for direct OAuth)
  or an MCP server you trust that already owns Google credentials.
- Optional: a Slack workspace where you can install an app, if you want Slack
  briefs/approvals/interaction.
- Optional: Docker, if you want `attune init` to provision local Qdrant for
  you rather than pointing at one you already run.

Choose exactly one Workspace backend before you start:

- **Direct Google OAuth** gives Attune Gmail and Calendar access using a Google
  authorized-user credential. It is the best-supported path and is required for
  Google Pub/Sub ingestion.
- **MCP** delegates Google credentials, provider API calls, and server-side
  policy to one or more remote MCP services. Attune connects over Streamable
  HTTP and uses polling.

## 1. Install this checkout

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,orchestrator,memory,google,slack,mcp]"
```

For a smaller production installation, include only the backend/channel extras
you use. Confirm that Python imports this checkout:

```bash
python -c 'import attune; print(attune.__file__)'
```

The result should contain this repository's `src/attune` path. `attune doctor`
also checks this when run from a checkout.

## 2. Configure and provision (guided, recommended)

Create the editable environment and let the initializer configure, provision,
and validate the local substrate:

```bash
cp .env.example .env
attune init --target local
```

`attune init --quick` (combine with `--target local` if you also want local
Qdrant) asks only the essential questions — workspace backend, data directory,
mailbox, internal domains, LLM/embedding base URL, key, and model — and prints
the follow-up commands for channels and Google credentials. Add
`--recommended` to fill in the documented mixed-provider model routing from
[`../configuration.md`](../configuration.md) as editable defaults.

The wizard retains existing values as defaults, obtains Google consent when
requested, and then displays an exact local deployment plan. If accepted, it
starts a version-pinned Qdrant container bound only to `127.0.0.1:6333` and
runs the full Doctor battery. It passes no Attune environment or credentials to
the Qdrant container.

Progress is recorded atomically with mode `0600` in
`ATTUNE_DATA_DIR/setup-state.json`. That file contains statuses, resource names,
and a one-way configuration digest — not environment values or secrets. A failed
or interrupted apply is safe to retry with the same command. If the plan is
declined, configuration remains saved and no container command is run.

Inspect or repair the resulting installation without re-entering configuration:

```bash
attune status
attune status --check
attune repair
```

`status` never prints environment values. `repair` displays the fixed plan,
reapplies only the recorded Attune Compose project, and reruns Doctor. It
refuses to infer resource ownership when no matching state record exists.
Status also reports the installation as stale when `.env` or the packaged plan
has changed since the last successful apply; repair records and validates the
new digests.

The remaining sections below explain each underlying choice and can be
followed manually instead of (or after) the guided path. Skip the manual
Qdrant start and final `attune doctor` when the guided setup has already
completed successfully.

### Manual alternative: memory storage

Attune runs Mem0 in-process and stores vectors in Qdrant. With Docker installed:

```bash
docker compose -f deploy/compose.yml up -d
docker compose -f deploy/compose.yml ps
```

The `qdrant` service should be running on port 6333. There is no separate Mem0
server to configure. The guided local target (`--target local`) performs this
step using the packaged, pinned Compose definition instead.

### Manual alternative: model configuration

Attune uses the official OpenAI Python SDK against an OpenAI-compatible Chat
Completions API. A custom base URL still uses the configured API key as an
`Authorization: Bearer` credential. Set at least these values in `.env`:

```dotenv
ATTUNE_LLM_BASE_URL=https://api.openai.com/v1
ATTUNE_LLM_API_KEY=...
ATTUNE_MODEL_DEFAULT=...

ATTUNE_EMBEDDING_BASE_URL=https://api.openai.com/v1
ATTUNE_EMBEDDING_API_KEY=...
ATTUNE_EMBEDDING_MODEL=text-embedding-3-small
ATTUNE_EMBEDDING_DIMENSIONS=1536
```

The chat and embedding endpoints may be different. Task-specific
`ATTUNE_MODEL_*` overrides are optional; the default model is used when an
override is blank.

## 3. Workspace access

### Direct Google OAuth

Follow [`google-workspace-oauth.md`](google-workspace-oauth.md) — the complete
Google Cloud Console ceremony (project, APIs, consent screen, scopes, Desktop
OAuth client) and the `attune init` consent flow that produces the
authorized-user credential Attune runs with. `attune init --google-setup`
offers this as a guided, resumable checklist and is also offered automatically
inside `attune init` the moment it reaches the Google credentials question with
no client file present. Return here once `ATTUNE_GOOGLE_CREDENTIALS_FILE`
points at the generated authorized-user JSON.

### Workspace access through MCP

MCP is appropriate when a managed service should own Google consent,
credentials, provider calls, policy, and its own audit boundary. It is not
inherently more capable than direct OAuth.

Attune does not ship a Google MCP server. Install or deploy a package/service
that:

1. authenticates to the intended Gmail mailbox and Calendar;
2. exposes MCP **Streamable HTTP** (not only stdio);
3. implements Attune's Gmail and Calendar
   [version-1 tool contract](../mcp-contract.md); and
4. is reachable from the Attune host over TLS, optionally using one bearer
   token for server authentication.

The same endpoint can expose both logical services:

```dotenv
ATTUNE_WORKSPACE_BACKEND=mcp
ATTUNE_INGESTION_MODE=poll
ATTUNE_MCP_URL=https://workspace-mcp.example.com/mcp
ATTUNE_MCP_TOKEN=...
```

Or use separate services:

```dotenv
ATTUNE_WORKSPACE_BACKEND=mcp
ATTUNE_INGESTION_MODE=poll
ATTUNE_MCP_GMAIL_URL=https://gmail-mcp.example.com/mcp
ATTUNE_MCP_CALENDAR_URL=https://calendar-mcp.example.com/mcp
ATTUNE_MCP_TOKEN=...
```

`attune init` prompts for the same values. `attune doctor` calls `tools/list`
on both logical services and reports missing contract tools. Contract v1 can
search/read Gmail, create Gmail drafts, modify labels, and read Calendar
events. It deliberately has no send tool and no Calendar hold-creation tool.

MCP is currently polling-only. Do not configure `google_pubsub`; watches and
provider notification credentials belong to the MCP service, not Attune.

## 4. Validate Workspace access

With Qdrant running:

```bash
attune doctor
attune brief
```

Before Slack is configured, the Slack check should be `SKIP`. In poll mode,
Pub/Sub should also be `SKIP`. Those are expected. The workspace, Gmail, and
Calendar rows should pass for direct OAuth; with MCP, the workspace capability
check passes and the provider-specific read checks are skipped.

## 5. Optional channels

### Slack

Follow [`slack-app.md`](slack-app.md)'s self-hosted section — the manifest
path (`attune slack manifest`) is fastest; a manual nine-step walkthrough is
also there. Return here once `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, and
`ATTUNE_SLACK_ALLOWED_USERS` are set.

### Google Chat

Google Chat's interactive path (card clicks, the verified HTTP interaction
route) needs the stateless republisher and is documented as part of the
Google Cloud Pub/Sub push variant:
[`../deployment.md`](../deployment.md#9-optional-google-chat-app-setup). If you
only want polling self-hosted with Slack, skip this section entirely — Slack
alone is a complete, fully-outbound setup.

## 6. Validate and run

```bash
attune doctor
attune brief --post
attune run
```

`attune brief --post` is the simplest end-to-end test of Google/MCP reads,
model access, and proactive channel delivery.

## Verification: what green looks like

- `attune doctor` reports PASS for installation, environment, data directory,
  model routes, and workspace; PASS (or SKIP if unconfigured) for each channel;
  SKIP for Pub/Sub in poll mode.
- `attune brief` reads Gmail and the current day's Calendar events without
  error.
- `attune brief --post` reaches every configured brief channel (Slack DM,
  etc.).
- `attune run` starts cleanly; on the first polling run, Gmail and Calendar
  establish baselines and do not replay old history. Later changes are checked
  every `ATTUNE_POLL_SECONDS` (default 120, minimum 30). Idle polls are quiet
  except for a five-minute heartbeat. Activity produces count-only logs such
  as:

  ```text
  poll activity: gmail=changed, 0 actionable
  poll activity: calendar=1 changed, 0 conflict(s)
  ```

  Not every unread message produces channel output: mail triaged as noise is
  audit-only. Calendar sends an immediate notification only for conflicts;
  ordinary appointments appear in that day's scheduled brief.
- With `ATTUNE_INTERACTION_CHANNELS` configured, the owner can ask natural
  questions such as "Anything new to report?", "Did Sarah reply?", or "What is
  on my calendar tomorrow morning?" in Slack or Google Chat. Attune performs a
  bounded live Workspace read rather than answering those questions from memory
  alone. See the [user journey](../user-journey.md) for the complete
  interaction and approval flow.

## Common failures

`attune doctor` now prints each of these fixes inline, in the FAIL line
itself, so the table below is reference rather than something you need to
cross-check by hand.

| Doctor result | Meaning and fix |
|---|---|
| `installation FAIL` | This shell imported another installation. Run `pip install -e .` from this checkout. |
| `llm FAIL` | The API key, base URL, or one routed model is unavailable. Check the named `ATTUNE_MODEL_*` override. |
| `workspace FAIL` with Google OAuth | Point `ATTUNE_GOOGLE_CREDENTIALS_FILE` at the generated authorized-user JSON, not only the downloaded client JSON. See [`google-workspace-oauth.md`](google-workspace-oauth.md). |
| `gmail-read` or `calendar-read FAIL` | Enable the API, add the test user, include the required scopes, then rerun `attune init` to authorize again. |
| `workspace FAIL` with MCP | Check TLS/network/token settings and ensure `tools/list` includes every tool in `docs/mcp-contract.md`. |
| `qdrant FAIL` | Start Docker and run `docker compose -f deploy/compose.yml up -d`. |
| `slack FAIL: missing_scope` | Add the Slack scopes in [`slack-app.md`](slack-app.md) and reinstall the app. |
| `channels FAIL` | Set the destination, token, allowlist, and explicit route variables for every selected channel. |
| Slack configured with a display name | Use the owner's `U...` member ID for a DM, or a stable `D...`, `C...`, or `G...` conversation ID. |

## What's next

For an always-on server (systemd), containerized all-in-one deployment, or the
Google Cloud Pub/Sub push transport variant, continue with
[`../deployment.md`](../deployment.md).
