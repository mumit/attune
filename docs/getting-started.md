# Getting started, step by step

This is the shortest path to one working Attune instance for one person. Start
with polling and Slack Socket Mode: both are outbound-only, so a workstation,
home server, or ordinary VM can run them without Pub/Sub, Cloud Run, or a public
webhook.

Choose exactly one Workspace backend:

- **Direct Google OAuth** gives Attune Gmail and Calendar access using a Google
  authorized-user credential. It is the best-supported path and is required for
  Google Pub/Sub ingestion.
- **MCP** delegates Google credentials, provider API calls, and server-side
  policy to one or more remote MCP services. Attune connects over Streamable
  HTTP and uses polling.

Slack is optional. Without it, `attune brief` still prints a brief in the
terminal. Google Chat is a separate optional channel described in
[`deployment.md`](deployment.md).

## 1. Install this checkout

Python 3.12 is recommended; Python 3.10+ is supported. From the repository root:

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

### Guided local setup

For the shortest local path, create the editable environment and let the
initializer configure, provision, and validate the local substrate:

```bash
cp .env.example .env
attune init --target local
```

The wizard retains existing values as defaults, obtains Google consent when
requested, and then displays an exact local deployment plan. If accepted, it
starts a version-pinned Qdrant container bound only to `127.0.0.1:6333` and
runs the full Doctor battery. It passes no Attune environment or credentials to
the Qdrant container.

Progress is recorded atomically with mode `0600` in
`ATTUNE_DATA_DIR/setup-state.json`. That file contains statuses, resource names,
and a one-way configuration digest—not environment values or secrets. A failed
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

The remaining sections explain each underlying choice and can be followed
manually. Skip the manual Qdrant start and final `attune doctor` when the guided
setup has already completed successfully.

## 2. Start memory storage manually

Attune runs Mem0 in-process and stores vectors in Qdrant. With Docker installed:

```bash
docker compose -f deploy/compose.yml up -d
docker compose -f deploy/compose.yml ps
```

The `qdrant` service should be running on port 6333. There is no separate Mem0
server to configure. The guided local target performs this step using the
packaged, pinned Compose definition instead.

## 3. Configure models

Attune uses the official OpenAI Python SDK against an OpenAI-compatible Chat
Completions API. A custom base URL still uses the configured API key as an
`Authorization: Bearer` credential.

Copy the example, then set at least these values:

```bash
cp .env.example .env
```

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

## 4A. Direct Google OAuth

Skip to [4B](#4b-workspace-access-through-mcp) if an MCP service owns your
Google access.

### Guided checklist (recommended)

```bash
attune init --google-setup
```

This walks the Google Cloud Console ceremony below as a numbered, resumable
checklist: project creation, enabling the two APIs, OAuth consent screen
branding, choosing Internal or External+Testing, the exact scopes to paste
(pulled live from the code that uses them, so they can never drift), and
Desktop OAuth client creation. Every step only ever shows a URL or a
copy-paste command and waits for you to confirm or skip it; the two
`gcloud services enable` steps are the only ones Attune can run for you, and
only after you confirm and only with `gcloud` on PATH. Progress and your
Internal/External+Testing answer are recorded in secret-free state under
`ATTUNE_DATA_DIR`, never in `.env` — interrupt and rerun the same command any
time. It is also offered automatically inside `attune init` the moment the
wizard reaches the Google credentials question and no client file exists yet.

The remaining steps in this section are the same ceremony written out as a
manual runbook — read them if you would rather drive the console yourself, or
if the checklist paused somewhere and you want the full context.

### Create the Google project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project and record its **Project ID** (not its display
   name).
3. Open **APIs & Services → Library** and enable:
   - Gmail API
   - Google Calendar API

Polling does not require Pub/Sub, Compute Engine, Cloud Run, Google Chat, or
Google Workspace Events APIs.

### Configure Google Auth Platform

1. Open **Google Auth Platform → Branding**. Set the app name to `Attune`, add
   support/contact email addresses, and save.
2. Open **Audience**:
   - For a personal Google account, choose **External**, leave the app in
     **Testing**, and add the account under **Test users**.
   - For a Workspace-owned project, prefer **Internal** when organizational
     policy allows it. Your administrator can still restrict the app.
3. Open **Data Access → Add or remove scopes** and add exactly:

   ```text
   https://www.googleapis.com/auth/gmail.readonly
   https://www.googleapis.com/auth/gmail.compose
   https://www.googleapis.com/auth/calendar.events
   ```

4. Open **Clients → Create Client → Desktop app**, create the client, and
   download its JSON file.

The downloaded file is an OAuth client secret, not the account credential that
Attune uses at runtime. The consent flow below creates the authorized-user JSON.

`gmail.compose` is a Google restricted scope and technically permits draft
management and sending. Attune's connector creates drafts with it; sending
requires the separate `gmail.send` scope below AND an explicit autonomy
grant — by default, with neither, Attune only ever drafts and the human
sends from Gmail. `calendar.events` is needed because approving a conflict
proposal can create a tentative hold.

Add `https://www.googleapis.com/auth/gmail.modify` only if you intend to set
`ATTUNE_MAIL_LABELS_ENABLED=1` (Phase 3 stage 1's archive-proposal write
path, disabled by default). Without it, Attune only reads and drafts;
`attune doctor` reports whether the enabled flag and the connector agree.

Add `https://www.googleapis.com/auth/gmail.send` only if you intend to set
`ATTUNE_MAIL_SEND_ENABLED=1` (Phase 4 stage 2's SEND_REPLY write path,
disabled by default) AND plan to `attune autonomy grant send_reply ...`
yourself — the CLI refuses that grant outright while the flag is off, and
without the scope `send_reply` still structurally refuses even with a
grant in place (rule 4: no shortcuts). `attune doctor` reports whether the
enabled flag and the connector agree, the same way it does for
`ATTUNE_MAIL_LABELS_ENABLED`.

`ATTUNE_CALENDAR_WRITES_ENABLED=1` (Phase 3 stage 2's decline-invite/
reschedule write path, also disabled by default) needs no additional scope
beyond `calendar.events` above — the same scope tentative holds already
use. `attune doctor` reports whether this enabled flag and the connector
agree, the same way it does for `ATTUNE_MAIL_LABELS_ENABLED`.

An External app in Testing normally issues refresh tokens that expire after
seven days for these scopes. That is adequate for a smoke test, not an
always-on service. Use the appropriate Internal/Published and verification
posture before depending on it continuously. See Google's
[OAuth app state overview](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview)
and [Gmail scope classifications](https://developers.google.com/workspace/gmail/api/auth/scopes).

If you answered the guided checklist's Internal/External+Testing question
with External+Testing, `attune doctor`'s `google-oauth-app` check WARNs about
this every run (with the authorized-user file's approximate age) until you
switch to Internal or publish the app; a workspace/Gmail/Calendar read that
fails with `invalid_grant` also gets this hint appended in place.

### Generate the authorized-user credential

Run:

```bash
attune init
```

For the workspace questions, use:

| Question | Answer |
|---|---|
| Workspace backend | `google_oauth` |
| Ingestion mode | `poll` |
| Data directory | normally `~/.attune` |
| Google mailbox email | the complete Gmail/Workspace address |
| Google Cloud project ID | the Project ID above |
| Google credentials JSON | path to the downloaded desktop-client JSON |
| Run Google consent flow | `y` |

The browser consent flow writes
`~/.attune/google_authorized_user.json` (or the chosen data directory) and puts
that path in `ATTUNE_GOOGLE_CREDENTIALS_FILE`. Both `.env` and the authorized
credential are secrets; never commit them.

The resulting core configuration resembles:

```dotenv
ATTUNE_WORKSPACE_BACKEND=google_oauth
ATTUNE_INGESTION_MODE=poll
ATTUNE_USER_ID=owner@example.com
ATTUNE_DATA_DIR=~/.attune
GOOGLE_PROJECT_ID=my-project-id
ATTUNE_GOOGLE_CREDENTIALS_FILE=/home/me/.attune/google_authorized_user.json
```

The [configuration reference](configuration.md) documents every key in
`.env.example`, including model recommendations and channel-routing examples.

## 4B. Workspace access through MCP

MCP is appropriate when a managed service should own Google consent,
credentials, provider calls, policy, and its own audit boundary. It is not
inherently more capable than direct OAuth.

Attune does not ship a Google MCP server. Install or deploy a package/service
that:

1. authenticates to the intended Gmail mailbox and Calendar;
2. exposes MCP **Streamable HTTP** (not only stdio);
3. implements Attune's Gmail and Calendar
   [version-1 tool contract](mcp-contract.md); and
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

## 5. Validate Workspace access

With Qdrant running:

```bash
attune doctor
attune brief
```

Before Slack is configured, the Slack check should be `SKIP`. In poll mode,
Pub/Sub should also be `SKIP`. Those are expected. The workspace, Gmail, and
Calendar rows should pass for direct OAuth; with MCP, the workspace capability
check passes and the provider-specific read checks are skipped.

## 6. Create the Slack app

Slack Socket Mode carries events and button interactions over an outbound
WebSocket, so no Slack Request URL is needed.

### Manifest path (recommended)

```bash
attune slack manifest
```

Prints a ready-to-paste Slack app manifest with Socket Mode, the four bot
token scopes, the `message.im` event subscription, App Home's Messages tab,
and Interactivity already configured — exactly what the manual steps below
set up by hand. Only three steps remain manual because Slack's manifest
format has no field for them:

1. Paste the printed JSON at [Slack app management](https://api.slack.com/apps)
   → **Create New App → From an app manifest**, and select the workspace.
2. Under **Basic Information → App-Level Tokens**, generate a token with the
   `connections:write` scope (save the `xapp-...` value as `SLACK_APP_TOKEN`),
   then install the app and save the `xoxb-...` **Bot User OAuth Token** as
   `SLACK_BOT_TOKEN`.
3. In Slack, open your profile and choose **More → Copy member ID**. Put the
   `U...` value in `ATTUNE_SLACK_ALLOWED_USERS`. An empty allowlist denies all
   interactive users.

### Manual path

The same nine steps written out for the standard Slack app UI, if you would
rather not use a manifest:

1. Open [Slack app management](https://api.slack.com/apps), choose **Create New
   App → From scratch**, select the workspace, and create the app.
2. Under **Basic Information → App-Level Tokens**, generate a token with the
   `connections:write` scope. Save the `xapp-...` value as
   `SLACK_APP_TOKEN`.
3. Open **Socket Mode** and enable it.
4. Open **OAuth & Permissions → Bot Token Scopes** and add:
   - `chat:write`
   - `im:history`
   - `im:read`
   - `im:write`
5. Open **Event Subscriptions**, enable events, and subscribe to the bot event
   `message.im`.
6. Open **App Home** and enable the Messages tab so users can DM the app.
7. Open **Interactivity & Shortcuts** and enable it. Leave Request URL blank;
   Socket Mode delivers the interactions.
8. Install or reinstall the app to the workspace. Save the `xoxb-...` **Bot
   User OAuth Token** as `SLACK_BOT_TOKEN`.
9. In Slack, open your profile and choose **More → Copy member ID**. Put the
   `U...` value in `ATTUNE_SLACK_ALLOWED_USERS`. An empty allowlist denies all
   interactive users.

Slack documents that Socket Mode uses an app token with `connections:write`,
and that `message.im` needs `im:history`: see the
[Socket Mode guide](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)
and [`message.im` reference](https://docs.slack.dev/reference/events/message.im).

### Choose the owner-only DM destination

Use the same stable owner member ID (`U...`) for both the proactive destination
and interaction allowlist. Slack's `chat.postMessage` accepts a user ID and
opens the app's one-to-one conversation when needed, so no separate `D...`
lookup script is required. An existing `D...` conversation ID also works.

Why not store a display name? Names are easier to recognize and type, which is
useful in an interactive setup wizard. Slack display names are nevertheless
mutable and non-unique, username-based posting is deprecated, and resolving a
name requires listing workspace users with the additional `users:read` scope.
The stable `U...` ID is already required for Attune's allowlist, works directly
for an owner DM, and avoids both ambiguity and extra directory access. Shared
destinations still use stable `C...` or `G...` conversation IDs.

Configure Slack as the delivery and interaction surface:

```dotenv
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
ATTUNE_SLACK_CHANNEL=U0123456789
ATTUNE_SLACK_ALLOWED_USERS=U0123456789

ATTUNE_BRIEF_CHANNELS=slack
ATTUNE_APPROVAL_CHANNEL=slack
ATTUNE_NOTIFICATION_CHANNELS=slack
ATTUNE_INTERACTION_CHANNELS=slack
```

If you intentionally use a shared `C...` or `G...` destination, verify its
membership and set `ATTUNE_ACK_DESTINATION_VISIBILITY=1`. The owner-only DM is
the safer default.

## 7. Validate and run

```bash
attune doctor
attune brief --post
attune run
```

`attune brief --post` is the simplest end-to-end test of Google/MCP reads,
model access, and proactive Slack delivery.

On the first polling run, Gmail and Calendar establish baselines and do not
replay old history. Later changes are checked every `ATTUNE_POLL_SECONDS`
(default 120, minimum 30). Idle polls are quiet except for a five-minute
heartbeat. Activity produces count-only logs such as:

```text
poll activity: gmail=changed, 0 actionable
poll activity: calendar=1 changed, 0 conflict(s)
```

Not every unread message produces Slack output: mail triaged as noise is
audit-only. Calendar sends an immediate notification only for conflicts;
ordinary appointments appear in that day's scheduled brief.

With `ATTUNE_INTERACTION_CHANNELS` configured, the owner can now ask natural
questions such as “Anything new to report?”, “Did Sarah reply?”, or “What is
on my calendar tomorrow morning?” in Slack or Google Chat. Attune performs a
bounded live Workspace read rather than answering those questions from memory
alone. See the [user journey](user-journey.md) for the complete interaction and
approval flow.

## Common failures

`attune doctor` now prints each of these fixes inline, in the FAIL line
itself, so the table below is reference rather than something you need to
cross-check by hand.

| Doctor result | Meaning and fix |
|---|---|
| `installation FAIL` | This shell imported another installation. Run `pip install -e .` from this checkout. |
| `llm FAIL` | The API key, base URL, or one routed model is unavailable. Check the named `ATTUNE_MODEL_*` override. |
| `workspace FAIL` with Google OAuth | Point `ATTUNE_GOOGLE_CREDENTIALS_FILE` at the generated authorized-user JSON, not only the downloaded client JSON. |
| `gmail-read` or `calendar-read FAIL` | Enable the API, add the test user, include the required scopes, then rerun `attune init` to authorize again. |
| `workspace FAIL` with MCP | Check TLS/network/token settings and ensure `tools/list` includes every tool in `docs/mcp-contract.md`. |
| `qdrant FAIL` | Start Docker and run `docker compose -f deploy/compose.yml up -d`. |
| `slack FAIL: missing_scope` | Add the Slack scopes above and reinstall the app. |
| `channels FAIL` | Set the destination, token, allowlist, and explicit route variables for every selected channel. |
| Slack configured with a display name | Use the owner's `U...` member ID for a DM, or a stable `D...`, `C...`, or `G...` conversation ID. |

For an always-on server or Google Cloud push deployment, continue with
[`deployment.md`](deployment.md).
