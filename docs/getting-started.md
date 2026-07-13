# Personal Setup, Step by Step

This is the beginner path for one personal Google account and one Slack user.
It deliberately uses:

- Gmail and Calendar through Google OAuth
- Slack through Socket Mode
- poll mode, so there is no VM, Pub/Sub, Cloud Run, or public webhook
- a local Qdrant container for memory

Do not configure Google Chat yet. Its live card path still needs a separate
app-auth credential. `docs/deployment.md` covers the advanced push deployment
after this local path works.

## 1. Install this checkout

Python 3.12 is recommended. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install \
  -e "packages/bearer-openai" \
  -e "packages/aidedecamp[orchestrator,memory,google,slack]"
```

Confirm Python is importing the checkout you are standing in:

```bash
python -c 'import aidedecamp; print(aidedecamp.__file__)'
```

The result must contain this repository's path. `aidedecamp doctor` also
checks this when run from a checkout.

## 2. Start memory storage

Install and start Docker Desktop, then run:

```bash
docker compose -f packages/aidedecamp/deploy/compose.yml up -d
docker compose -f packages/aidedecamp/deploy/compose.yml ps
```

The `qdrant` service should be running. Mem0 runs inside Aide-de-camp; there is
no separate Mem0 server to configure.

## 3. Create the Google project

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Use the project picker at the top and choose **New Project**.
3. Give it a name such as `aidedecamp-personal`, create it, and make sure it is
   the selected project afterward. Record its **Project ID**, which is not
   always identical to the display name.
4. Open **APIs & Services → Library**. Find and enable:
   - Gmail API
   - Google Calendar API

You do not need Pub/Sub, Compute Engine, Cloud Run, or the Google Chat API for
this first setup.

## 4. Configure Google OAuth

Google's console calls this area **Google Auth Platform**.

1. Open **Google Auth Platform → Branding**. Enter an app name such as
   `Aide-de-camp`, your support email, and your contact email. Save it.
2. Open **Audience**. For a personal Gmail account choose **External**, leave
   the app in **Testing**, and add the same Gmail address under **Test users**.
3. Open **Data Access → Add or remove scopes**. Add these exact scopes:

   ```text
   https://www.googleapis.com/auth/gmail.readonly
   https://www.googleapis.com/auth/gmail.compose
   https://www.googleapis.com/auth/calendar.events
   ```

4. Open **Clients → Create Client → Desktop app**. Name it, create it, and
   download the JSON file. This downloaded file is the **OAuth client secret**,
   not the final account credential.

`gmail.compose` is classified by Google as restricted. It permits Gmail draft
management and technically permits sending, but Aide-de-camp's runtime only
creates drafts. An External app in Testing issues refresh tokens that expire
after seven days for these scopes. Testing is enough for the first smoke test;
an always-on deployment must later use an appropriate production or internal
OAuth posture and meet Google's current verification requirements.

## 5. Run the setup wizard

From the repository root with the virtual environment active:

```bash
aidedecamp init
```

Use these answers:

| Question | Answer |
|---|---|
| Deployment | `personal` |
| Connector mode | press Enter for `direct_oauth` |
| Ingestion mode | press Enter for `poll` |
| Data directory | press Enter for `~/.aidedecamp` |
| Google mailbox email | your full Gmail or Workspace address |
| Google Cloud project ID | the Project ID from step 3 |
| Fuel iX bearer token | your token; input is hidden |
| Google credentials JSON | path to the downloaded desktop-client JSON |
| Run Google consent flow | `y` |
| Slack tokens/settings | leave blank for now if Slack is not ready |
| Google Chat space | leave blank |
| Timezone | an IANA name such as `America/Vancouver` |
| Morning brief time | local `HH:MM`, such as `07:30` |

The browser consent flow creates
`~/.aidedecamp/google_authorized_user.json`. The wizard writes `.env` with
mode `0600`; later commands load it automatically. Never commit either file.

## 6. Validate Google and generate a brief

```bash
aidedecamp doctor
aidedecamp brief
```

Before Slack is configured, `slack` should be `SKIP`. In poll mode, `pubsub`
should also be `SKIP`. Those are expected, not failures. Python 3.10 produces
a `WARN` with its upgrade deadline; use 3.12 for a new environment. Every
other row should be `PASS`.

## 7. Create the Slack app

1. Open [Slack API Apps](https://api.slack.com/apps), choose **Create New
   App → From scratch**, select your workspace, and create it.
2. Open **Socket Mode**, enable it, and create an app-level token with the
   `connections:write` scope. Save the `xapp-...` value for `SLACK_APP_TOKEN`.
3. Open **OAuth & Permissions → Bot Token Scopes** and add:
   - `chat:write`
   - `im:history`
   - `im:read`
   - `im:write`
4. Open **Event Subscriptions**, enable events, and add the bot event
   `message.im`.
5. Open **App Home** and enable the Messages tab.
6. Open **Interactivity & Shortcuts** and enable it. Socket Mode does not need
   a Request URL.
7. Install or reinstall the app to the workspace. Save its `xoxb-...` Bot User
   OAuth Token for `SLACK_BOT_TOKEN`.
8. In Slack, open your profile, choose **More → Copy member ID**, and save the
   `U...` value for `ADC_SLACK_ALLOWED_USERS`.
9. Open a direct message with the app. The proactive destination must be the
   DM's `D...` conversation ID, not `#aide` or another display name.

First add the tokens and owner ID to `.env`:

```dotenv
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
ADC_SLACK_ALLOWED_USERS=U...
```

For the single owner ID configured above, this prints the DM ID without
printing either token:

```bash
python - <<'PY'
import os
from dotenv import load_dotenv
from slack_sdk import WebClient

load_dotenv()
result = WebClient(token=os.environ["SLACK_BOT_TOKEN"]).conversations_open(
    users=os.environ["ADC_SLACK_ALLOWED_USERS"]
)
print(result["channel"]["id"])
PY
```

Add the result to `.env`:

```dotenv
ADC_SLACK_CHANNEL=D...
```

Then validate and post a real brief:

```bash
aidedecamp doctor
aidedecamp brief --post
```

## 8. Start the assistant

```bash
aidedecamp run
```

On the first poll, Gmail and Calendar establish a baseline and do not replay
old history. New changes arrive after the configured poll interval, which
defaults to 120 seconds.

## Common failures

| Doctor result | Meaning and fix |
|---|---|
| `installation FAIL` | This shell imported another checkout. Re-run the editable install from step 1. |
| `fuelix FAIL` | The token is missing/rejected, the gateway is unreachable, or one routed model is not enabled. Check `FUELIX_TOKEN`; for a model-specific failure set the named `ADC_MODEL_*` override in `.env`. |
| `google-credentials FAIL` | The configured file is missing or malformed. Point at the generated authorized-user JSON, not merely the downloaded client JSON. |
| `gmail-read` or `calendar-read FAIL` | The API is disabled, the test user was not added, or consent lacks the listed scopes. Fix Google Auth Platform and rerun `aidedecamp init --force`. |
| `qdrant FAIL` | Start Docker Desktop and rerun the Compose command in step 2. |
| `slack FAIL: missing_scope` | Add the scopes in step 7 and reinstall the Slack app. |
| `env FAIL` mentioning `#aide` | Use the API's `D...`, `C...`, or `G...` conversation ID, not a display name. |

For push mode, a VM deployment, Secret Manager, or future Google Chat work,
continue with `docs/deployment.md` only after this path is green.
