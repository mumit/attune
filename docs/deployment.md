# Deployment Guide — GCP (personal and TELUS)

This is the concrete "how to actually run this" companion to `docs/design.md`
(architecture) and `docs/decisions.md` (why things are shaped the way they
are). Read `CLAUDE.md`'s non-negotiable rules first — this guide implements
them, it doesn't relitigate them.

**Status: unexercised.** Every step below is derived from the code and from
Google's documented APIs, but nothing here has been run against a real GCP
project yet. Treat this as a detailed first draft to execute and correct
against reality, not a verified runbook. Update it as you go — a deployment
guide that drifts from what actually works is worse than none.

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
- **One Compute Engine VM** running `python -m aidedecamp` (via systemd) plus
  a local Qdrant container for memory (`deploy/mem0-compose.yml`).
- **One thin, stateless Cloud Run service** — the Calendar webhook
  republisher (the one source needing a real inbound HTTPS endpoint; rule 5
  keeps that off the VM). Gmail and Chat don't need this — they deliver via
  Pub/Sub directly.
- **Secret Manager** for `FUELIX_TOKEN`, Google OAuth credentials, and Slack
  tokens.
- **Pub/Sub topics + subscriptions** for Gmail, Chat, and (indirectly, via
  the republisher) Calendar.

Run every step in this guide **twice**, once per GCP project, with
deployment-specific values substituted (project id, Slack workspace, Chat
space, calendar owner). Nothing here is shared between the two.

---

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

`workspaceevents.googleapis.com` is what backs Chat's proactive message
ingestion (`ingestion/chat_events.py`); `chat.googleapis.com` backs the Cards
v2 send/receive path (`channels/gchat.py`).

---

## 4. Google credentials — the one genuinely different step per deployment

This is the step most likely to trip you up, because **personal Gmail and a
TELUS Workspace account need different credential types**, and
`credentials.py` supports both, but you have to pick correctly.

### Personal (consumer Gmail account)

Consumer Gmail has no domain-wide delegation — a service account cannot be
granted access to a personal `@gmail.com` inbox. You need a real **OAuth user
credential**: a one-time human authorization that produces a refresh token.

1. GCP Console → APIs & Services → OAuth consent screen. External, testing
   mode is fine for a single-user personal deployment.
2. Create an OAuth 2.0 Client ID (type: Desktop app, simplest for a one-time
   local authorization flow).
3. Run the authorization flow once (`google-auth-oauthlib`'s
   `InstalledAppFlow`, or any standard OAuth helper script) against the
   scopes in `credentials.py::SCOPES_DEFAULT`, producing a JSON file shaped
   like:
   ```json
   {"type": "authorized_user", "client_id": "...", "client_secret": "...", "refresh_token": "..."}
   ```
   (`credentials.py` detects this via the absence of `"type":
   "service_account"` and loads it through
   `google.oauth2.credentials.Credentials.from_authorized_user_info`.)
4. Store that JSON in Secret Manager (§6), never in the repo or on local disk
   outside the secrets flow.

### TELUS (Workspace account)

Two paths, depending on what TELUS IT approves:

- **Domain-wide delegation (preferred if approved)**: create a service
  account, grant it domain-wide delegation in the Workspace Admin console for
  exactly `SCOPES_DEFAULT`, and configure it to impersonate the specific
  mailbox this deployment acts as. With this, the VM's attached service
  account identity can resolve credentials via
  `google.auth.default()` directly — no credentials file needed at all,
  `google_credentials_file` stays unset.
- **Per-user OAuth (if domain-wide delegation is refused)**: same flow as
  personal above, just against the TELUS Workspace account instead of a
  personal Gmail account. This is exactly why `connectors/base.py`'s
  interface and `ConnectorMode` exist as a config choice — a TELUS "no" on
  one auth path is a config change, not a redesign.

Either way, confirm the actual scope list against current Google docs before
requesting it — `SCOPES_DEFAULT` in `credentials.py` is:

```
gmail.readonly, gmail.compose, calendar.readonly, chat.messages, chat.spaces.readonly
```

`gmail.send` is deliberately **not** in this list (rule 4 — send is refused
structurally; only add it as a separate, reviewed change alongside
`send_enabled=True` and an autonomy grant).

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

If using domain-wide delegation for TELUS (§4), this is also the service
account you grant delegation to — one identity, not two.

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
the same way `deploy/mem0-compose.yml` is infrastructure rather than
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

Test it (own dependency set, not part of the main `pytest` run):

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

```bash
gcloud run deploy aidedecamp-republisher \
  --source=packages/aidedecamp/deploy/republisher \
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
pip install -e "packages/bearer-openai[dev]"
pip install -e "packages/aidedecamp[dev,memory,orchestrator,slack,google]"

# Memory substrate (Qdrant)
docker compose -f packages/aidedecamp/deploy/mem0-compose.yml up -d
```

Pull secrets into environment at boot (a startup script, not committed env
files):

```bash
export FUELIX_TOKEN=$(gcloud secrets versions access latest --secret=fuelix-token)
export SLACK_BOT_TOKEN=$(gcloud secrets versions access latest --secret=slack-bot-token)
export SLACK_APP_TOKEN=$(gcloud secrets versions access latest --secret=slack-app-token)
gcloud secrets versions access latest --secret=google-credentials > /opt/aidedecamp/google-credentials.json
```

---

## 10. Environment variables — full reference

Set these (directly, or via the secret-pull script above feeding a
`systemd` `EnvironmentFile`). Grouped by what they configure; `ADC_*` prefix
is this project's own convention, distinct from Google's own env vars.

**Core / identity**
```
ADC_DEPLOYMENT=personal              # or telus
ADC_CONNECTOR_MODE=direct_oauth      # or mcp, per §4's decision
ADC_USER_ID=me                       # or an explicit email; also the Gmail API "me" alias
FUELIX_TOKEN=<from Secret Manager>
```

**Memory**
```
ADC_MEM0_URL=http://localhost:8000   # only used if running the standalone Mem0 server; the
                                      # in-process library path (default) talks to Qdrant directly
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
# Omit entirely for TELUS-with-domain-wide-delegation — ADC via the VM's
# service account resolves it instead (§4).
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
ADC_SLACK_CHANNEL=C0123456789               # where briefs/approvals post proactively
ADC_CHAT_SPACE=spaces/AAAAxxxxxxx           # where Chat briefs/approvals post proactively
```

Leave `ADC_SLACK_CHANNEL`/`ADC_CHAT_SPACE` unset to run without that
channel's proactive posting — `build_runtime()` only constructs a channel
when its config is present (see `runtime.py`).

---

## 11. Slack app setup

1. https://api.slack.com/apps → Create New App → From scratch.
2. **Socket Mode**: enable it, generate an app-level token with the
   `connections:write` scope → this is `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → Bot Token Scopes: `chat:write`, `im:history`,
   `im:read`, `im:write` (for `message.im` DMs — `channels/slack.py`'s
   conversational handler), plus whatever's needed for the approval buttons
   (`chat:write` covers posting blocks). Install to workspace → this produces
   `SLACK_BOT_TOKEN`.
4. **Event Subscriptions** → Subscribe to bot events → `message.im` (matches
   the filter in `channels/slack.py`'s registered handler).
5. **Interactivity & Shortcuts**: enable it (Socket Mode delivers these too;
   no Request URL needed).
6. Invite the bot to `ADC_SLACK_CHANNEL` if it's a channel (not needed for
   DMs).

---

## 12. Google Chat app setup

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

`verify_chat_request`'s shape (audience = endpoint URL, check the `email`
claim) is confirmed against
[Google's current docs](https://developers.google.com/workspace/chat/verify-requests-from-chat)
— what's still unverified is only whether it works against an actual live
Chat app end to end (§15, step 7).

---

## 13. First-run bootstrap

Before starting the long-running process, register the watches/subscriptions
once (idempotent — safe to re-run):

```python
from aidedecamp.runtime import build_runtime

rt = build_runtime()
rt.renew_gmail_watch(force=True)
rt.renew_chat_subscription(force=True)   # only if ADC_CHAT_SPACE is set
rt.renew_calendar_watch(force=True)      # only if ADC_CALENDAR_WEBHOOK_ADDRESS is set
```

Schedule this to re-run daily (systemd timer or cron calling a tiny wrapper
script) — Gmail/Chat/Calendar watches all expire and `ensure_*` renews
proactively at <48h remaining, but only if something actually calls it on a
schedule. This confirms `docs/decisions.md`'s existing note: missing this
step is the single most common way this class of integration silently goes
quiet.

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
EnvironmentFile=/opt/aidedecamp/aidedecamp.env
ExecStart=/opt/aidedecamp/.venv/bin/python -m aidedecamp
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
2. Send yourself a test email → confirm a Pub/Sub message lands (`gcloud
   pubsub subscriptions pull aidedecamp-gmail-sub --auto-ack`) and a draft
   approval card appears in Slack/Chat within the pull loop's poll window.
3. DM the Slack bot / message the Chat space with something conversational →
   confirm a reply comes back via `_converse`.
4. Ask for "the morning brief" in either channel → confirm `assemble_brief`
   output comes back.
5. Create two overlapping calendar holds → confirm the republisher fires,
   the Pub/Sub message lands on `aidedecamp-calendar-sub`, and a conflict
   notification posts (`dispatcher.handle_calendar_notification`).
6. Approve a drafted reply from **Slack** → confirm the capture-signal write
   lands in Mem0 (`memory/signals.py`), and that the audit log
   (`ADC_AUDIT_LOG_PATH`) has a matching `draft_approve` entry.
7. Approve a drafted reply from **Chat** → click Approve, confirm the card
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

- **Watch/subscription renewal**: the daily cron/timer from §13. Missing this
  is silent — no error, ingestion just stops.
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
