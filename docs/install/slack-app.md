# Install: Slack app

*This is the canonical Slack app creation runbook — see
[`../modes.md`](../modes.md) for how self-hosted and hosted multi-tenant differ,
and [`self-hosted.md`](self-hosted.md) for the complete self-hosted install
sequence this page is one step of.*

Slack is optional in every mode. Without it, `attune brief` still prints a
brief in the terminal, and hosted customers can use the built-in browser
conversation panel with no channel install at all.

The console mechanics below overlap heavily between self-hosted and the
hosted platform, so this is one document with two clearly separated sections
rather than two documents. **Self-hosted** creates one Socket Mode app for
your own instance, installed once, with tokens you copy into `.env`.
**Hosted platform** creates one OAuth app that the *operator* registers once
and that *customers* install into their own Slack workspace through a web
OAuth flow — you only do this section if you are standing up the multi-tenant
service.

## Self-hosted app (Socket Mode)

Slack Socket Mode carries events and button interactions over an outbound
WebSocket, so no Slack Request URL, public endpoint, load balancer, firewall
rule, or signing secret is needed. This is identical whether Attune runs on a
laptop, a home server, or a GCP VM.

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
the safer default. On a server or GCP VM, this is the complete Slack setup —
Socket Mode needs only outbound HTTPS/WebSocket connectivity.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `slack FAIL: missing_scope` | Add the four bot scopes above and reinstall the app. |
| `channels FAIL` | Set the destination, token, allowlist, and explicit route variables for every selected channel. |
| Slack configured with a display name | Use the owner's `U...` member ID for a DM, or a stable `D...`, `C...`, or `G...` conversation ID. |

## Hosted platform app (operator-installed, customer-installed)

This section is for the operator standing up the multi-tenant service
([`hosted-operator.md`](hosted-operator.md)), not for a self-hosted instance.
It differs from the section above in a structural way: the operator registers
**one** Slack app for the whole platform using ordinary OAuth (not Socket
Mode, since hosted distribution needs HTTPS ingress — Socket Mode apps cannot
be listed in the public Slack Marketplace), and each **customer** later
installs that app into their own workspace through a web OAuth flow described
in [`../hosted-channel-installation.md`](../hosted-channel-installation.md).
That document is the canonical source for the installation *ceremony*
(one-use OAuth state, callback binding, credential brokering, destination
binding, activation gates); this section is only the one-time console setup
of the app itself.

1. Open [Slack app management](https://api.slack.com/apps), choose **Create New
   App → From scratch**, and create the platform app (for example, named
   `Attune`).
2. Open **OAuth & Permissions → Bot Token Scopes** and add exactly:
   - `chat:write`
   - `im:write`
   - `im:history`
3. Under **OAuth & Permissions → Redirect URLs**, add the exact callback the
   control plane implements:

   ```text
   https://<your-domain>/v1/onboarding/channel-installations/slack/callback
   ```

4. Open **Event Subscriptions**, enable events, set the Request URL to the
   exact events path the Slack ingress service implements:

   ```text
   https://<your-domain>/v1/provider/slack/events
   ```

   and subscribe to the bot event `message.im` only. Slack verifies this URL
   with a signed `url_verification` handshake before accepting it.
5. Record the app's **Client ID**, **Client Secret**, and **Signing Secret**.
   Store the client secret in the secret the channel broker reads and the
   signing secret in the secret the Slack ingress service reads — never in
   the same secret, and never in Terraform state or a `.env` file. See
   `hosted-channel-installation.md`'s "Slack implementation" section for the
   exact grant boundary (only the private channel broker holds the client
   secret; only the Slack ingress identity holds the signing secret).
6. Do **not** install this app into any workspace yourself as the operator —
   installation is a per-customer ceremony driven by the control plane's OAuth
   start route, verified end to end in `hosted-channel-installation.md`.

This app registration is a prerequisite for, not a replacement of, the staged
activation gates in `hosted-channel-installation.md` and `roadmap.md`
(`ATTUNE_SLACK_CHANNEL_ENABLED`, `ATTUNE_HOSTED_SLACK_INSTALL_ENABLED`,
`ATTUNE_ENABLE_SLACK_CONVERSATION`) — creating the app does not itself enable
any customer-facing route.
