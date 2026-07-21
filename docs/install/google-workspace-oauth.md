# Install: Google Workspace OAuth (self-hosted)

*This is the canonical Google Cloud Console ceremony for self-hosted Attune's
direct-OAuth Workspace backend — see [`../modes.md`](../modes.md) for how
self-hosted fits alongside the hosted multi-tenant service, and
[`self-hosted.md`](self-hosted.md) for the complete self-hosted install
sequence this page is one step of.*

This produces two artifacts: a downloaded OAuth **client** JSON (from Google
Cloud Console) and, after a browser consent flow, a generated **authorized-user**
JSON that Attune uses at runtime. They are not the same file, and `attune doctor`
will `FAIL` a workspace check that points `ATTUNE_GOOGLE_CREDENTIALS_FILE` at the
former instead of the latter.

If your Workspace access instead goes through an MCP server, skip this page —
see `self-hosted.md`'s MCP section instead. This page's ceremony is unrelated
to the hosted platform's Identity Platform sign-in or Workspace-connector OAuth
clients described in [`../identity-platform.md`](../identity-platform.md); those
are separate Web application clients used by the operated multi-tenant service,
not the Desktop client this page creates.

## Guided checklist (recommended)

```bash
attune init --google-setup
```

This walks the entire ceremony below as a numbered, resumable checklist:
project creation, enabling the two APIs, OAuth consent screen branding,
choosing Internal or External+Testing, the exact scopes to paste (pulled live
from the code that uses them, so they can never drift), and Desktop OAuth
client creation. Every step only ever shows a URL or a copy-paste command and
waits for you to confirm or skip it; the two `gcloud services enable` steps
are the only ones Attune can run for you, and only after you confirm and only
with `gcloud` on PATH. Progress and your Internal/External+Testing answer are
recorded in secret-free state under `ATTUNE_DATA_DIR`, never in `.env` —
interrupt and rerun the same command any time. It is also offered
automatically inside `attune init` the moment the wizard reaches the Google
credentials question and no client file exists yet.

The remaining sections are the same ceremony written out as a manual runbook —
read them if you would rather drive the console yourself, or if the checklist
paused somewhere and you want the full context.

## Create the Google project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a project and record its **Project ID** (not its display
   name).
3. Open **APIs & Services → Library** and enable:
   - Gmail API
   - Google Calendar API

Polling does not require Pub/Sub, Compute Engine, Cloud Run, Google Chat, or
Google Workspace Events APIs. If you are following the Google Pub/Sub push
variant instead ([`../deployment.md`](../deployment.md) §3–§11), that guide
enables the additional services around this same OAuth ceremony — the project,
consent screen, scopes, and authorized-user credential here are identical
either way; push changes the transport, not the mailbox identity or scopes.

## Configure Google Auth Platform

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

### Testing-mode 7-day expiry

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

## Generate the authorized-user credential

Run:

```bash
attune init
```

For the workspace questions, use:

| Question | Answer |
|---|---|
| Workspace backend | `google_oauth` |
| Ingestion mode | `poll` (or `google_pubsub` for the push variant) |
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

The [configuration reference](../configuration.md) documents every key in
`.env.example`, including model recommendations and channel-routing examples.

## Optional Google Chat scopes

`attune init` currently requests only the core Gmail/Calendar scopes above. If
you later enable the optional user-authenticated Chat ingestion path described
in [`../deployment.md`](../deployment.md#9-optional-google-chat-app-setup),
regenerate the authorized-user file with the core scopes plus:

```text
https://www.googleapis.com/auth/chat.messages
https://www.googleapis.com/auth/chat.spaces.readonly
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `workspace FAIL` | Point `ATTUNE_GOOGLE_CREDENTIALS_FILE` at the generated authorized-user JSON, not only the downloaded client JSON. |
| `gmail-read` or `calendar-read FAIL` | Enable the API, add the test user, include the required scopes, then rerun `attune init` to authorize again. |
| `invalid_grant` on any workspace read | If your OAuth consent screen is in Testing mode, refresh tokens expire after 7 days — re-run `attune init` to re-authorize, and consider `attune init --google-setup` step 5 (Internal/Published). |

Return to [`self-hosted.md`](self-hosted.md) to continue the install sequence,
or to [`../deployment.md`](../deployment.md) if you are provisioning the Google
Cloud Pub/Sub push variant.
