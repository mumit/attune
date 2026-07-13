# Deployment Guide (personal and TELUS)

This is the concrete "how to actually run this" companion to `docs/design.md`
(architecture) and `docs/decisions.md` (why things are shaped the way they
are). Read `CLAUDE.md`'s non-negotiable rules first — this guide implements
them, it doesn't relitigate them.

There are two tracks:

- **Track A — quickstart (poll mode, the default).** No Pub/Sub, VM, Cloud
  Run, or webhook infrastructure: the assistant polls Gmail and Calendar on
  a timer, outbound-only. You still need a Google Cloud project to enable the
  Workspace APIs and create an OAuth desktop client. Anything that can run
  Docker + Python works. Start with Slack; Google Chat has additional app-auth
  requirements described in §12.
- **Track B — hardened push deployment (GCP).** Pub/Sub push ingestion fed
  by watches, the republisher on Cloud Run, Secret Manager, one GCP project
  per deployment. Lower latency, lower API-call volume, and the posture the
  TELUS deployment should use. Everything from §2 onward is Track B.

**Status: partially live-exercised.** On 2026-07-12, Track A completed OAuth,
Fuel iX, Gmail, Calendar, Qdrant, and Slack validation and generated a real
terminal brief. Slack posting, approval-to-draft, the always-on loop, and the
one-week reliability bar remain unverified; Track B remains unexercised.

---

## Track A — quickstart (poll mode)

The short version is the README quickstart; the operational notes:

1. `pip install -e "packages/bearer-openai" -e
   "packages/aidedecamp[orchestrator,memory,google,slack]"`.
2. `docker compose -f packages/aidedecamp/deploy/compose.yml up -d` —
   Qdrant only; Mem0 runs in-process inside the assistant.
3. Create the Google OAuth project/client in §4, then run `aidedecamp init`.
   It writes `.env` (chmod 0600), which later CLI commands load automatically,
   and can run the Google
   OAuth consent flow for a consumer Gmail account. Poll mode and a single
   `ADC_DATA_DIR` are the defaults it writes.
4. `aidedecamp doctor` until everything relevant is PASS/SKIP, then
   `aidedecamp brief`, then `aidedecamp run`.
5. Always-on options, in increasing effort: a tmux session; the systemd unit
   in §9 (drop the Pub/Sub-specific parts); or the compose `assistant`
   profile (`--profile assistant up -d --build`), which mounts all state in
   the `adc_data` volume and points Mem0 at the `qdrant` service.

What Track A gives up relative to Track B: event latency is the poll
cadence (`ADC_POLL_SECONDS`, default 120s, floor 30s) instead of push; and
**Google Chat approval buttons don't work without the republisher**, and
proactive Cards v2 currently need a separate Chat app-auth credential that the
runtime does not yet configure (§12). Use Slack for the first deployment.
Gmail and Calendar watch renewals, Pub/Sub quotas, and the republisher do not
exist in a Slack-based Track A deployment.

---

## Track B — hardened push deployment (GCP)

**Change from `docs/design.md` §4.6**: that section assumed personal ran on a
home server and only TELUS ran on GCP. Both deployments now run on GCP —
personal and TELUS each get their own **separate GCP project**, keeping the
"two fully separate deployments, same codebase" property design 4.6 wanted,
just with cloud infrastructure on both sides instead of one home server + one
VM. This is a decision, not a code change; see `docs/decisions.md`.

---

## 1. Shape of one deployment

Each deployment (personal, TELUS) is:

- **One GCP project**, isolated from the other deployment's project. No
  shared Pub/Sub topics, no shared service accounts, no shared anything.
- **One Compute Engine VM** running `aidedecamp run` (via systemd) plus a local
  Qdrant container for memory (`packages/aidedecamp/deploy/compose.yml`).
- **One thin, stateless Cloud Run service** — the Calendar webhook and Chat
  interaction republisher. Gmail notifications and Chat message events reach
  Pub/Sub directly; Calendar notifications and card clicks require synchronous
  HTTPS responses, so rule 5 keeps them off the VM.
- **Secret Manager** for `FUELIX_TOKEN`, Google OAuth credentials, and Slack
  tokens.
- **Pub/Sub topics + subscriptions** for Gmail, Chat, and (indirectly, via
  the republisher) Calendar.

Run every step in this guide **twice**, once per GCP project, with
deployment-specific values substituted (project id, Slack workspace, Chat
space, calendar owner). Nothing here is shared between the two.

---

> **Shortcut:** `aidedecamp init` (interactive wizard) writes `.env` and can
> run the Google OAuth consent flow for you, and `aidedecamp doctor` validates
> each credential/resource below with a fix hint per failure — use them
> alongside this guide rather than assembling everything by hand.

## 2. Prerequisites

- A GCP project per deployment, billing enabled. (`gcloud projects create
  aidedecamp-personal` / `aidedecamp-telus` or equivalent — TELUS's project
  will likely go through TELUS's own project-creation process, not a
  personal `gcloud` login.)
- `gcloud` CLI authenticated against the right account for each project.
- A Fuel iX bearer token (`FUELIX_TOKEN`) for whichever gateway this
  deployment talks to.
- For TELUS: sign-off from TELUS IT on whichever `ConnectorMode` you end up
  needing (`mcp` vs `direct_oauth`) and on the OAuth scopes below — this is
  the governance step design 4.7 flagged; don't skip it and don't assume it's
  a formality.
- A Slack workspace (if using the Slack channel) where you can install an
  app, and/or a Google Chat space (if using the Chat channel).

Set the active project for the rest of this guide:

```bash
export PROJECT_ID=aidedecamp-personal   # or aidedecamp-telus
gcloud config set project "$PROJECT_ID"
```

---

## 3. Enable APIs

```bash
gcloud services enable \
  gmail.googleapis.com \
  calendar-json.googleapis.com \
  chat.googleapis.com \
  workspaceevents.googleapis.com \
  pubsub.googleapis.com \
  secretmanager.googleapis.com \
  compute.googleapis.com \
  run.googleapis.com \
  iam.googleapis.com
```

For Track A with Slack, enabling only Gmail, Calendar, and the service-usage
dependencies is sufficient; the remaining APIs support Chat or Track B.

`workspaceevents.googleapis.com` is what backs Chat's proactive message
ingestion (`ingestion/chat_events.py`); `chat.googleapis.com` backs the Cards
v2 send/receive path (`channels/gchat.py`).

---

## 4. Google Workspace access and OAuth

For a first personal setup, use the screenshot-free, click-by-click sequence in
[`getting-started.md`](getting-started.md). This section is the security and
deployment reference behind it.

### What is supported now

The production-wired connector is `direct_oauth`, using one OAuth authorized-
user credential for Gmail, Calendar, and read-side Chat APIs. The MCP adapter
exists and is tested behind an injected transport, but `build_runtime()` does
not construct that transport; do not set `ADC_CONNECTOR_MODE=mcp` in a live
deployment yet.

Service-account JSON can be parsed, but mailbox impersonation is not
implemented: the loader never calls `with_subject(...)`. Likewise, the VM's
Application Default Credentials identify the VM service account, not a human
mailbox. Consumer Gmail and Workspace deployments should both use per-user
OAuth today. Domain-wide delegation needs a code change and its own tests
before this guide can recommend it.

### Create the OAuth client

1. Create or select a Google Cloud project. Poll mode avoids GCP runtime
   infrastructure, not this API/OAuth control plane.
2. Enable Gmail API and Google Calendar API. Enable Google Chat API and Google
   Workspace Events API only if you are evaluating Chat.
3. In **Google Auth Platform**, configure Branding, Audience, and Data Access.
   For a personal account choose External and add your Google account as a
   test user. For Workspace, prefer Internal when the project belongs to the
   same organization and ask the administrator to trust the app/scopes.
4. Add the exact scopes below in Data Access, then create a **Desktop app**
   OAuth client and download its client-secret JSON.
5. Run `aidedecamp init`, point it at that downloaded JSON, and answer `y` when
   it offers to run the browser consent flow. The resulting
   `~/.aidedecamp/google_authorized_user.json` is the credential configured by
   `ADC_GOOGLE_CREDENTIALS_FILE`.
6. Set `ADC_USER_ID` to the authorized mailbox's full email address. This is
   required for safe reply targeting and useful quiet-thread detection.

The requested scopes are:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/calendar.events
```

`gmail.compose` is a Google **restricted** scope and technically authorizes
both draft management and sending. Aide-de-camp still refuses send in its
connector unless a separately reviewed `send_enabled` path is introduced;
the current runtime only creates drafts. `calendar.events` is required because
an approved conflict proposal creates a tentative hold with `events.insert`.

An External app left in **Testing** receives refresh tokens that expire after
seven days for these scopes. That is useful for a smoke test, not an always-on
deployment. Before relying on it, move the OAuth app to the appropriate
production/internal posture and satisfy Google's current verification,
restricted-scope, data-use, and security-assessment requirements. For a
corporate account, administrator policy can still block or limit the app.

Authoritative references: [Google OAuth production readiness](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview),
[Gmail scope classifications](https://developers.google.com/workspace/gmail/api/auth/scopes),
and [Google Chat authentication](https://developers.google.com/workspace/chat/authenticate-authorize).

---

## 5. Service account for the VM itself

Separate from the Gmail/Calendar/Chat credential above: the Compute Engine
VM needs its own service account with least-privilege IAM, per design 4.6:

```bash
gcloud iam service-accounts create aidedecamp-runtime \
  --display-name="Aide-de-camp runtime"

# Pub/Sub: pull from subscriptions, nothing else
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:aidedecamp-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/pubsub.subscriber"

# Secret Manager: read secrets, nothing else
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:aidedecamp-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

This infrastructure identity is deliberately separate from the authorized
Workspace user credential in §4.

---

## 6. Secrets

```bash
printf '%s' "$FUELIX_TOKEN_VALUE" | gcloud secrets create fuelix-token --data-file=-
printf '%s' "$SLACK_BOT_TOKEN_VALUE" | gcloud secrets create slack-bot-token --data-file=-
printf '%s' "$SLACK_APP_TOKEN_VALUE" | gcloud secrets create slack-app-token --data-file=-
gcloud secrets create google-credentials --data-file=./oauth-credentials.json
```

Grant the runtime service account access to each (`secretAccessor` role,
already granted at project level above — narrow to per-secret bindings if you
want tighter scoping).

At VM startup, secrets are pulled and written to a local path (or exported as
env vars) by the startup script — see §10. Rotating the Fuel iX token is then
`gcloud secrets versions add fuelix-token --data-file=-` plus a service
restart, matching the workflow `CLAUDE.md` rule 6 describes.

---

## 7. Pub/Sub topics and subscriptions

Four ingestion paths, four topic/subscription pairs. Gmail and Chat messages
publish directly to their topic (Google's own watch/subscribe APIs do this);
Calendar notifications and Chat card-interactions have no such option, so
those two topics are published to by the thin republisher (§8), not by
Google directly — see `docs/decisions.md` for why Chat interactions need
this same treatment despite Chat's *message* ingestion not needing it.

```bash
for name in gmail chat calendar chat-interaction; do
  gcloud pubsub topics create "aidedecamp-${name}"
  gcloud pubsub subscriptions create "aidedecamp-${name}-sub" \
    --topic="aidedecamp-${name}" \
    --ack-deadline=60
done
```

Grant Gmail's own service account publish rights on its topic (Google
requires this explicitly — `gmail-api-push@system.gserviceaccount.com`):

```bash
gcloud pubsub topics add-iam-policy-binding aidedecamp-gmail \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

If evaluating Chat events, also grant Chat's delivery identity permission on
the Chat topic:

```bash
gcloud pubsub topics add-iam-policy-binding aidedecamp-chat \
  --member="serviceAccount:chat-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

Map these to config:

| Topic | Env var |
|---|---|
| `aidedecamp-gmail` | `ADC_GMAIL_PUBSUB_TOPIC` |
| `aidedecamp-gmail-sub` | `ADC_GMAIL_PUBSUB_SUBSCRIPTION` |
| `aidedecamp-chat` | `ADC_CHAT_PUBSUB_TOPIC` |
| `aidedecamp-chat-sub` | `ADC_CHAT_PUBSUB_SUBSCRIPTION` |
| `aidedecamp-chat-interaction` | `ADC_CHAT_INTERACTION_PUBSUB_TOPIC` |
| `aidedecamp-chat-interaction-sub` | `ADC_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION` |
| `aidedecamp-calendar` | `ADC_CALENDAR_PUBSUB_TOPIC` |
| `aidedecamp-calendar-sub` | `ADC_CALENDAR_PUBSUB_SUBSCRIPTION` |

---

## 8. The republisher (Cloud Run) — Calendar webhook + Chat interactions

One small, stateless Cloud Run service handles **both** inbound webhooks this
deployment needs, for the same underlying reason: each needs a synchronous
HTTP response, so neither can follow the Pub/Sub-pull pattern Gmail/Chat
messages use. It holds no credentials, no memory, and no Fuel iX token.

**Not part of the installable `aidedecamp` package** — it lives at
`packages/aidedecamp/deploy/republisher/` (own `main.py`,
`requirements.txt`, `Dockerfile`, `test_main.py`), deployed independently,
the same way `deploy/compose.yml` is infrastructure rather than
application code.

**`/calendar-webhook`** — design 4.6's flagged exception: Calendar push
notifications only deliver via HTTPS POST, no Pub/Sub option. No request
verification is needed here: the notification carries almost no payload (just
headers), and the main process only ever treats it as "go re-check your sync
token" — never as a direct command. If this route is abused, the blast
radius is "the main process runs an extra, harmless reconciliation pass."

1. Accept a POST, read the `X-Goog-Channel-ID` / `X-Goog-Resource-ID` /
   `X-Goog-Resource-State` / `X-Goog-Message-Number` headers (the exact shape
   `ingestion/calendar_sync.py::decode_calendar_headers` expects as input).
2. Publish `{"channel_id": ..., "resource_id": ..., "resource_state": ...,
   "message_number": ...}` as JSON onto `aidedecamp-calendar`, waiting for
   publish confirmation (`future.result()`) before acking.
3. Return HTTP 200.

**`/chat-interaction`** — Google Chat's approve/reject buttons also need a
synchronous response, but resuming the paused workflow needs the checkpointer
and memory store, which this service must never hold (rule 5). So it doesn't
resume anything: it verifies the request, republishes the decoded click, and
returns an immediate placeholder ack; `dispatcher.handle_chat_interaction`
(pulled via `ADC_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION` on the main VM) does
the actual resume and posts the *real* confirmation back to the space
afterward. **This route does need request verification** — without it,
anyone who found this service's public URL could forge an approve/reject
decision on someone else's pending draft:

1. Verify `Authorization: Bearer <token>` is a Google-signed OIDC ID token
   whose `email` claim is `chat@system.gserviceaccount.com`
   (`verify_chat_request`, via `google.oauth2.id_token.verify_oauth2_token`).
   Reject with 403 otherwise. This uses Chat's **"HTTP endpoint URL"**
   Authentication Audience mode (§12) — the `aud` claim must equal this
   route's exact URL, e.g. `https://<service>.run.app/chat-interaction`, set
   as `CHAT_APP_AUDIENCE`. (Chat's other mode, "Project Number," uses a
   different JWT-based check against the numeric project number instead —
   not implemented here; "HTTP endpoint URL" is the right choice for a
   service that isn't using Cloud Run's own IAM-based auth, i.e. this one.)
   Confirmed against
   [Google's current docs](https://developers.google.com/workspace/chat/verify-requests-from-chat);
   **not yet exercised against a live Chat app.**
2. If the click is the **edit** button: return the dialog-open response
   directly, synchronously — opening a dialog never touches the graph, so
   there's nothing to protect by routing it through Pub/Sub.
3. If the click is **approve/reject**: publish the raw event JSON onto
   `aidedecamp-chat-interaction`, return `{"text": "⏳ Processing your
   response..."}` as an immediate placeholder.
4. Anything else: return 200, do nothing.

Test it (own dependency set, deliberately excluded from the main `pytest`
run via `norecursedirs = deploy` in both `pytest.ini` and the root
`pyproject.toml` — CI installs only `aidedecamp[dev]`, not Flask, so
`pytest packages/aidedecamp` must never try to collect this service's tests):

```bash
cd packages/aidedecamp/deploy/republisher
pip install -r requirements.txt pytest
pytest test_main.py
```

Deploy it once, note its HTTPS URL, and use it for both integrations:
`ADC_CALENDAR_WEBHOOK_ADDRESS=<url>/calendar-webhook` (the `address` field
`ensure_calendar_watch` registers with Google), and the Chat app's
interactivity endpoint (§12) is `<url>/chat-interaction`. `CHAT_APP_AUDIENCE`
must be that exact `<url>/chat-interaction` string — it has to match what
you configure as the Chat app's Connection settings URL exactly, since that's
the `aud` claim Google's ID token carries.

Give the service its own identity and publisher access only to the two topics
it writes:

```bash
gcloud iam service-accounts create aidedecamp-republisher
for topic in aidedecamp-calendar aidedecamp-chat-interaction; do
  gcloud pubsub topics add-iam-policy-binding "$topic" \
    --member="serviceAccount:aidedecamp-republisher@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/pubsub.publisher"
done
```

```bash
gcloud run deploy aidedecamp-republisher \
  --source=packages/aidedecamp/deploy/republisher \
  --service-account="aidedecamp-republisher@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="CALENDAR_PUBSUB_TOPIC=projects/${PROJECT_ID}/topics/aidedecamp-calendar,CHAT_INTERACTION_PUBSUB_TOPIC=projects/${PROJECT_ID}/topics/aidedecamp-chat-interaction,CHAT_APP_AUDIENCE=https://aidedecamp-republisher-xxxxx.run.app/chat-interaction" \
  --allow-unauthenticated \
  --region=us-central1
```

(`--allow-unauthenticated` because neither Google's Calendar webhook caller
nor its Chat interaction caller is a GCP identity you can IAM-gate the usual
way — `/chat-interaction`'s own JWT check above is the real authentication
for that route; `/calendar-webhook` deliberately has none, per its own risk
analysis.)

---

## 9. Compute Engine VM

A small VM is enough (design 4.6): `e2-small` or `e2-medium` runs the app
process and a local Qdrant container comfortably.

```bash
gcloud compute instances create aidedecamp-vm \
  --machine-type=e2-medium \
  --service-account="aidedecamp-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --scopes=cloud-platform \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=20GB
```

On the VM:

```bash
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv docker.io docker-compose-plugin git

git clone <this-repo> /opt/aidedecamp
cd /opt/aidedecamp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e "packages/bearer-openai"
pip install -e "packages/aidedecamp[dev,memory,orchestrator,slack,google]"

# Sanity check before proceeding — same invocation CI runs, entirely offline.
pytest packages/aidedecamp packages/bearer-openai -q

# Memory substrate (Qdrant)
docker compose -f packages/aidedecamp/deploy/compose.yml up -d
```

Materialize the authorized-user JSON and a root-readable `.env` on the VM.
The service's working directory is `/opt/aidedecamp`, so `aidedecamp run`
loads this file automatically:

```bash
gcloud secrets versions access latest --secret=google-credentials > /opt/aidedecamp/google-credentials.json
install -m 600 /dev/null /opt/aidedecamp/.env
printf 'FUELIX_TOKEN=%s\n' "$(gcloud secrets versions access latest --secret=fuelix-token)" >> /opt/aidedecamp/.env
printf 'SLACK_BOT_TOKEN=%s\n' "$(gcloud secrets versions access latest --secret=slack-bot-token)" >> /opt/aidedecamp/.env
printf 'SLACK_APP_TOKEN=%s\n' "$(gcloud secrets versions access latest --secret=slack-app-token)" >> /opt/aidedecamp/.env
# Append the non-secret ADC_* settings from §10, including the credential path.
```

---

## 10. Environment variables — deployment reference

Append these to the `.env` created above. They are grouped by purpose;
`ADC_*` is this project's convention, distinct from Google's own env vars.

**Core / identity**
```
ADC_DEPLOYMENT=personal              # or telus
ADC_CONNECTOR_MODE=direct_oauth      # the only production-wired mode today
ADC_USER_ID=owner@example.com        # authorized mailbox + memory principal
FUELIX_TOKEN=<from Secret Manager>
ADC_INGESTION_MODE=poll              # push only after §§7–8 are complete
ADC_DATA_DIR=/opt/aidedecamp/data
```

**Memory**
```
ADC_QDRANT_HOST=localhost
ADC_QDRANT_PORT=6333
```

**Audit + state (persisted to local disk on the boot persistent disk — back
this directory up, it's the only copy)**
```
ADC_AUDIT_LOG_PATH=/opt/aidedecamp/data/audit.log.jsonl
ADC_DB_PATH=/opt/aidedecamp/data/aidedecamp.db
ADC_GMAIL_WATCH_STATE_PATH=/opt/aidedecamp/data/gmail_watch_state.json
ADC_CHAT_SUBSCRIPTION_STATE_PATH=/opt/aidedecamp/data/chat_subscription_state.json
ADC_CALENDAR_WATCH_STATE_PATH=/opt/aidedecamp/data/calendar_watch_state.json
ADC_CALENDAR_SYNC_STATE_PATH=/opt/aidedecamp/data/calendar_sync_state.json
```

**Google credentials**
```
ADC_GOOGLE_CREDENTIALS_FILE=/opt/aidedecamp/google-credentials.json
GOOGLE_PROJECT_ID=<project id>
```

**Gmail / Chat / Calendar ingestion**
```
ADC_GMAIL_PUBSUB_TOPIC=projects/<project>/topics/aidedecamp-gmail
ADC_GMAIL_PUBSUB_SUBSCRIPTION=projects/<project>/subscriptions/aidedecamp-gmail-sub
ADC_CHAT_PUBSUB_TOPIC=projects/<project>/topics/aidedecamp-chat
ADC_CHAT_PUBSUB_SUBSCRIPTION=projects/<project>/subscriptions/aidedecamp-chat-sub
# Chat card-click interactions (approve/reject only) — the async half of the
# approval flow the republisher's /chat-interaction route feeds (§8).
ADC_CHAT_INTERACTION_PUBSUB_TOPIC=projects/<project>/topics/aidedecamp-chat-interaction
ADC_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION=projects/<project>/subscriptions/aidedecamp-chat-interaction-sub
ADC_CALENDAR_PUBSUB_TOPIC=projects/<project>/topics/aidedecamp-calendar
ADC_CALENDAR_PUBSUB_SUBSCRIPTION=projects/<project>/subscriptions/aidedecamp-calendar-sub
ADC_CALENDAR_WEBHOOK_ADDRESS=https://aidedecamp-republisher-xxxxx.run.app/calendar-webhook
ADC_CALENDAR_ID=primary
```

**Channels**
```
SLACK_APP_TOKEN=<from Secret Manager>       # xapp-...
SLACK_BOT_TOKEN=<from Secret Manager>       # xoxb-...
ADC_SLACK_CHANNEL=D0123456789               # owner-only DM preferred
ADC_SLACK_ALLOWED_USERS=U0123456789          # empty means deny-all
ADC_CHAT_SPACE=spaces/AAAAxxxxxxx           # where Chat briefs/approvals post proactively
ADC_CHAT_ALLOWED_USERS=users/123456789       # empty means deny-all
# Required for Chat, or for Slack C/G channels, after checking membership.
ADC_ACK_DESTINATION_VISIBILITY=1
```

Leave `ADC_SLACK_CHANNEL`/`ADC_CHAT_SPACE` unset to run without that
channel's proactive posting — `build_runtime()` only constructs a channel
when its config is present (see `runtime.py`).

`.env.example` is the exhaustive setting inventory, including cadence, logging,
conversation, nudge, and per-file state overrides. Prefer `ADC_DATA_DIR` over
setting each state path individually.

---

## 11. Slack app setup

Slack is the recommended first channel because Socket Mode keeps both messages
and approval interactions outbound-only and uses one bot credential.

1. [Slack app management](https://api.slack.com/apps) → Create New App → From
   scratch.
2. **Socket Mode**: enable it, generate an app-level token with the
   `connections:write` scope → this is `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → Bot Token Scopes: `chat:write`, `im:history`,
   `im:read`, `im:write` (for `message.im` DMs — `channels/slack.py`'s
   conversational handler), plus whatever's needed for the approval buttons
   (`chat:write` covers posting blocks). Install to workspace → this produces
   `SLACK_BOT_TOKEN`.
4. **Event Subscriptions** → Subscribe to bot events → `message.im` (matches
   the filter in `channels/slack.py`'s registered handler).
5. **App Home**: enable the Messages tab so the owner can DM the app.
6. **Interactivity & Shortcuts**: enable it (Socket Mode delivers these too;
   no Request URL needed).
7. Reinstall the app after changing scopes or event subscriptions. Copy the
   bot token (`xoxb-...`) to `SLACK_BOT_TOKEN` and the app token (`xapp-...`)
   to `SLACK_APP_TOKEN`.
8. Copy the owner's member ID (`U...`) from **Profile → More → Copy member
   ID** into `ADC_SLACK_ALLOWED_USERS`. Empty means deny-all.
9. Prefer the owner's DM conversation (`D...`) for `ADC_SLACK_CHANNEL`. Open a
   DM with the app, then use Slack's `conversations.open` API/tester with the
   owner's `U...` id to obtain the returned `channel.id`. If a
   channel is used, verify membership and set
   `ADC_ACK_DESTINATION_VISIBILITY=1`; allowlists stop actions, not reading.

The app-level token needs only `connections:write`. The bot scopes above are
the current minimum used by this code. See Slack's
[`connections:write` reference](https://docs.slack.dev/reference/scopes/connections.write/)
and Socket Mode documentation when the console labels move.

---

## 12. Google Chat app setup

### Current limitation

Do not enable Google Chat for the first live deployment. The runtime currently
uses the authorized-user credential from §4 for proactive `messages.create`.
Google generally permits only text with user authentication; stable Cards v2
and interactive widgets require Chat **app authentication** with a service
account and the `chat.bot` scope. The configuration has no separate Chat app
credential yet, so approval cards are not a supported live path even though
their rendering, interaction decoding, and republisher flow are covered by
offline tests.

The remaining implementation work is explicit: add a dedicated
`ADC_CHAT_APP_CREDENTIALS_FILE`, use it only for proactive Chat sends, retain
the user credential for polling/Workspace Events, and validate the two-identity
flow against a live Chat app. Do not reuse a Chat service-account credential
for Gmail or Calendar user data.

The user credential must then be reauthorized with the optional
`chat.messages` and `chat.spaces.readonly` scopes in `SCOPES_CHAT`; the Chat app
credential uses `chat.bot` separately.

### Configuration to prepare after that gap closes

1. GCP Console → APIs & Services → Google Chat API → Configuration.
2. App name, avatar, description. Interactive features: **on**.
3. Connection settings → HTTP endpoint URL: `<republisher-url>/chat-interaction`
   (§8's republisher, not the Calendar route). There's no Socket-Mode
   equivalent for Chat card interactivity, so this has to be a real HTTP
   endpoint — but it's the republisher's endpoint, not the credential-holding
   VM's: the republisher verifies the request, republishes approve/reject
   clicks onto `aidedecamp-chat-interaction`, and answers edit clicks
   directly. It never touches the checkpointer or memory itself (see
   `docs/decisions.md` for the full reasoning).
4. **Authentication Audience**: set to **"HTTP endpoint URL"** (the other
   option, "Project Number," uses a different verification path this service
   doesn't implement). This makes the `aud` claim on Google's signed request
   equal the URL from step 3 exactly — set that same string as the
   republisher's `CHAT_APP_AUDIENCE` env var (§8).
5. Permissions: whichever spaces/users should be able to add the app.
6. Note the space id (`spaces/AAAAxxxxxxx`) for `ADC_CHAT_SPACE` — get it via
   the Chat API (`spaces.list`) or from the space's URL once the app is
   added to it.

7. Create the Chat app's service account, authorize it with `chat.bot`, and add
   the Chat app itself to the destination space. App membership and user
   membership are distinct. This credential will belong in the dedicated
   setting described above once implemented.

`verify_chat_request`'s shape (audience = endpoint URL, check the `email`
claim) is confirmed against
[Google's current docs](https://developers.google.com/workspace/chat/verify-requests-from-chat)
but the entire dual-credential Chat path remains unverified end to end (§15).

---

## 13. Watch bootstrap and renewal

In push mode, `Runtime.run()` registers configured watches/subscriptions at
startup and its scheduler renews them daily. No separate cron job is required.
For diagnostics or a deliberate forced re-registration, run:

```python
from aidedecamp.runtime import build_runtime

rt = build_runtime()
rt.renew_gmail_watch(force=True)
rt.renew_chat_subscription(force=True)   # only if ADC_CHAT_SPACE is set
rt.renew_calendar_watch(force=True)      # only if ADC_CALENDAR_WEBHOOK_ADDRESS is set
```

Poll mode creates none of these watches. Its persisted high-water marks are
initialized on the first tick, so existing mailbox and calendar history is
baselined rather than replayed.

---

## 14. Running the process

`systemd` unit (`/etc/systemd/system/aidedecamp.service`):

```ini
[Unit]
Description=Aide-de-camp
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=/opt/aidedecamp
ExecStart=/opt/aidedecamp/.venv/bin/aidedecamp run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aidedecamp
journalctl -u aidedecamp -f    # tail logs
```

`__main__.py` calls `build_runtime().run()`, which starts the Gmail/Chat/
Calendar Pub/Sub pull loops on daemon threads and blocks the main thread on
Slack's Socket Mode connection (or just waits, if Slack isn't configured) —
see `runtime.py`'s docstring.

---

## 15. Verifying the deployment

Rough end-to-end smoke test, in order:

1. `journalctl -u aidedecamp -f` shows the process started without raising
   (a missing/invalid secret or scope typically fails loudly at `build_app`/
   `build_runtime` construction time).
2. Send yourself a test email. In Track A, wait one poll interval and confirm a
   draft approval card appears in Slack. In Track B, inspect logs for the
   Gmail pull loop; do not consume its subscription with an `--auto-ack` test
   command while the runtime is processing it.
3. DM the Slack bot with something conversational → confirm a reply comes back
   via `_converse`.
4. Ask for "the morning brief" in Slack → confirm `assemble_brief` output.
5. Create two overlapping calendar events. Track A should detect them on a
   poll tick. Track B should show the Calendar republisher publish and the
   runtime reconcile. Confirm one conflict notification and at most one hold
   approval card for the pair.
6. Approve a drafted reply from **Slack** → confirm the capture-signal write
   lands in Mem0 (`memory/signals.py`), and that the audit log
   (`ADC_AUDIT_LOG_PATH`) has a matching `draft_approve` entry.
7. **Deferred until §12's app-auth gap is implemented:** approve a drafted
   reply from Chat → click Approve, confirm the card
   immediately shows "⏳ Processing your response...", then confirm the real
   "✅ Approved — draft accepted." follows within the pull loop's poll window
   — this is the async hand-off (`/chat-interaction` → Pub/Sub →
   `Runtime.process_chat_interaction`) working end to end, not just the
   synchronous path `handle_interaction` covers in tests. **This is also the
   only real test of `verify_chat_request`** — if step 7 doesn't produce the
   "Processing" ack (whatever Chat's UI shows for that failure isn't
   confirmed here), check the republisher's Cloud Run logs for a 403, which
   means either the Authentication Audience mode/URL (§12) or
   `CHAT_APP_AUDIENCE` doesn't match what Chat is actually sending.

---

## 16. Ongoing maintenance

- **Watch/subscription renewal**: the built-in scheduler runs it daily in push
  mode. Monitor `watch_renewed`/`watch_renew_failed` audit events and process
  logs; a stopped runtime cannot renew its own watches.
- **Secret rotation**: `gcloud secrets versions add <name> --data-file=-` +
  `systemctl restart aidedecamp`. `FUELIX_TOKEN` specifically: a 401 raises
  `TokenRejectedError` in logs rather than retrying — that's your signal to
  rotate, not a bug to work around (rule 6).
- **Google's agent-tool quota/tiering** (`CLAUDE.md`'s "Still open"): confirm
  the actual watch-renewal + pull cadence here against current Google quota
  docs before relying on this in daily use — this was flagged as unverified
  during design and hasn't been checked against a real deployment yet.
- **Disk backup**: `ADC_DB_PATH` (LangGraph checkpoints), the four
  `*_STATE_PATH` files, and the audit log are the only copies of this
  deployment's state — back up the VM's data directory, not just the code.

---

## 17. Cost shape (rough, personal-scale usage)

- e2-medium VM: ~$25-30/mo running continuously.
- Cloud Run republisher: effectively free at personal-mailbox volume
  (occasional invocations, well within the free tier).
- Pub/Sub: free tier covers personal-scale message volume comfortably.
- Secret Manager: a few cents/month for a handful of secrets.
- Fuel iX usage: billed separately per the gateway's own terms, not GCP.

TELUS-scale volume (a busier mailbox, more calendar churn) may cross free
tiers on Pub/Sub/Cloud Run — check actual usage after a week or two rather
than pre-optimizing.
