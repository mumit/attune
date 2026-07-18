# User journey

Attune is meant to feel like one assistant whether the principal uses Slack,
Google Chat, or both. The channel authenticates the human and carries the
response; the same bounded interaction layer, Workspace connector, memory, and
approval workflows operate behind it.

## 0. Sign up for hosted Attune

The operated service starts at the Attune hostname with **Continue with
Google**. Google sign-in identifies the Attune account; it does not grant access
to Gmail, Calendar, Chat, or other Workspace data. The browser keeps the Google
provider credential only in memory, exchanges the fresh Identity Platform token
for an independent Attune session, and discards the provider credential.

For a new account, Attune verifies the identity but does not infer membership
from the email address or domain. During development, the first test sign-in
therefore reports that membership is not provisioned. An operator binds the
exact Identity Platform subject to one tenant; the user then signs in again and
continues to the connector-consent journey. A production signup flow will
replace this development ceremony with an explicit tenant creation or invitation
step.

Connecting Google Workspace is a separate screen and OAuth client with explicit
Workspace scopes. A user can sign in to Attune without connecting Workspace,
and disconnecting Workspace does not silently end or transfer the Attune account.

The first hosted connection journey is:

1. Visit the Attune hostname and choose **Continue with Google**.
2. Complete identity-only sign-in. Attune creates its own eight-hour session;
   the transient browser provider session is discarded.
3. Choose **Connect Gmail and Calendar**. This opens a second Google consent
   ceremony for read-only Gmail and Calendar access. It does not reuse the
   sign-in client or silently request compose, send, Chat, or calendar-write
   authority.
4. Return to a credential-free Attune URL. The page reports connected, denied,
   or failed without exposing the authorization code or provider error.
5. Attune automatically performs one composite Workspace verification job. It
   uses two separately authorized, one-time broker operations: Gmail's fixed
   profile read and Calendar's fixed primary-calendar read. The browser receives
   only queued, running, succeeded, or failed—not mailbox counts, calendar
   metadata, a connector identifier, a Google account identifier, or provider
   error details. The page reports **Google Workspace is connected and
   verified** only after both private reads succeed.
6. Choose **Start guided setup**. Attune creates tenant-bound, resumable setup
   progress and marks Workspace complete from the already verified connector;
   it does not ask for configuration files or infrastructure credentials.
7. Review the fixed private-alpha policy. It permits only R0 read-only
   Workspace verification automatically and explicitly excludes sending,
   calendar changes, deletion, and sharing. Choose **Enable read-only policy**
   within ten minutes of sign-in; otherwise sign out and sign in again before
   confirming the authority change.
8. Choose Google Chat, Slack, or both independently for conversation and brief
   delivery. Saving records intent only; Attune clearly leaves the step pending
   installation and verified owner-only destination tests.
9. Install and verify the selected channel apps, then activate briefs.
   Capability upgrades such as Gmail draft
   creation are separate, explicit consent and policy changes.

At any later signed-in visit, choose **Disconnect Google Workspace** and confirm
the destructive action. Attune derives the connector from the current session;
the browser never sends a tenant, principal, connector, provider, or credential
identifier. A one-use private-broker authorization immediately marks Attune's
stored credential and connector revoked. The Attune account and membership stay
active, and **Connect Gmail and Calendar** becomes available again.

This action withdraws Attune's local ability to use the credential. It does not
claim to remove the upstream OAuth grant from the Google Account. A user who
also wants that provider-side grant removed should remove Attune in Google
Account's third-party connections. Provider-side revocation from Attune is a
separate future ceremony because a provider outage must never prevent immediate
local disconnection.

The guided setup card always shows the next four product steps—Workspace,
channels, policy, and activation. Closing the browser does not discard progress.
Only fixed Attune ceremonies can advance a step; neither browser requests nor
model-generated text can mark setup complete.

Policy confirmation is resumable and idempotent. Attune audits the fixed change
before creating the grant, derives the owner and tenant from the current
session, and never accepts a browser-supplied policy or risk tier. An unexpected
existing policy or grant set is shown as requiring repair instead of being
overwritten or adopted silently.

Channel choices are similarly resumable but deliberately stop at `authorized`.
They contain no provider token, app installation, destination, or allowlist.
Slack and Google Chat may be selected independently for interaction and briefs;
the step becomes `validated` only after the selected installations and exact
owner-only destinations pass bounded live tests.

For Google Chat, the owner generates a ten-minute link code in Attune and sends
it only in a direct message to the Attune Chat app. For Slack, the owner starts
installation from the setup page after recent authentication: Attune returns a
ten-minute, one-use Slack authorize link, the owner approves the fixed
`chat:write`, `im:write`, and `im:history` scopes in Slack's own consent
screen, and Slack returns the browser to Attune, which verifies the app, team,
installer, and scopes through a private broker and binds the verified
installer to a one-user DM. The browser never sees a Slack token, and Attune
never retains a Slack user token. Each provider then shows installation,
ingress, destination, and fixed-content test as separate checks. Shared
spaces and channels are not accepted by the initial hosted release. After
installation, Slack requires the same explicit fixed connection test as
Google Chat before its destination becomes active, and **Disconnect Slack**
is the same recent-authenticated, confirmed lifecycle ceremony: it deletes
the stored bot-token and route envelopes immediately, and reconnecting
requires a fresh installation plus a new delivery test.

After Google Chat says the one-time link succeeded, return to the setup page.
If the page says the delivery test remains, select **Send fixed connection
test**. The request has no editable message or destination. Attune sends one
sentence to the owner DM: `Attune connection test succeeded. No workspace data
was accessed.` Successful provider readback changes the destination to active.
If an older development binding says its encrypted route must be adopted,
generate a fresh link code and send it in the same owner DM first; Attune will
adopt only an exact app, owner, and DM match.

An active hosted destination hides link-code generation because linking is
complete; signing out does not reset tenant channel state. A new code is not a
conversation switch. In the current development hosted environment, an
ordinary owner-DM message receives a prompt `Working on it.` acknowledgement;
Attune then posts its answer as a second message in the same verified DM. The
durable natural-language path is active only for that owner binding. It
resolves tenant and destination server-side, dispatches replay safely, keeps
Workspace and model credentials behind private brokers, and records
content-free pre/post-effect audits. If the environment's independent
conversation gate is disabled, ordinary messages receive an explicit
unavailable response instead of misleading `/link` instructions.

To change or stop the Google Chat destination, sign in recently and choose
**Disconnect Google Chat**, then confirm the destructive action. Attune derives
the owner, installation, and DM from the session and canonical database state;
the browser sends none of those identifiers. Disconnection immediately stops
new message acceptance and outbound replies, cancels pending link/delivery
claims, removes the encrypted route, and returns the channel step to pending.
It does not delete conversation history under the separate retention policy.

Reconnection is intentionally a new proof, not an undo button. Generate a
fresh one-time link code, send it from the intended owner DM, and run the fixed
connection test again. Only then does the destination become active. This same
ceremony supports an intentional move to a different DM while preventing a
browser or model from silently retargeting Attune.

Closing or denying the second screen leaves the Attune account signed in and
unconnected. Retrying creates a fresh ten-minute transaction. A completed
connector is verified instead of silently starting a replacement. A temporary
verification failure does not discard or replace the connector; a later
signed-in visit safely retries the fixed check.

Once Workspace is connected and the read-only policy is active, the setup
page itself shows a bounded conversation panel: a signed-in owner can type a
message and converse with Attune right there, with no channel to install and
no destination to verify first. There is no push delivery -- the browser
polls for the stored assistant turn every two seconds, with a working
indicator and a note if a reply is taking a while. This is the same durable
acceptance, dispatch, and bounded read-only execution that Slack and Google
Chat use, just without a channel broker in between; see
[`hosted-conversation.md`](hosted-conversation.md#the-browser-surface).

## 1. Start the day

At `ATTUNE_BRIEF_TIME`, Attune reads recent unread Gmail, today's Calendar,
meeting context, and quiet threads, then posts the brief to every configured
`ATTUNE_BRIEF_CHANNELS` destination.

The principal can also ask naturally in an owner-only Slack DM or allowed
Google Chat space:

> Anything new to report?

> What needs my attention this morning?

These requests produce a fresh brief. They are not answered merely from
memory or the last polling cursor.

## 2. Ask a live Workspace question

The same conversation can narrow into Gmail or Calendar:

> Did Sarah send the launch plan?

The initial hosted release runs a fixed, capped Gmail search and returns
metadata summaries for at most ten matching threads; it does not expose
message bodies to the worker. Standalone Attune can use the richer connector
limits configured for that deployment. Both answer only from live results.

> What is on my calendar tomorrow morning?

The initial hosted release reads at most 25 events from the next seven days and
lets the bounded answer model select the requested portion. Standalone Attune
can resolve a narrower window in `ATTUNE_TIMEZONE`. Direct Google OAuth and MCP
still implement the same internal Workspace connector contract; the operated
hosted release currently uses brokered direct OAuth.

The hosted worker supplies an operator-confirmed IANA timezone and authoritative
current local datetime outside the untrusted conversation and Workspace data.
Words such as **today** and **tomorrow** are resolved only from that trusted
temporal context. Until each principal can confirm a profile timezone, an
operated deployment uses one explicit `hosted_timezone` value and must not infer
the date from earlier email, calendar, or conversation content.

Fetched subjects, snippets, bodies, event names, and attendees remain
untrusted external data. They can be summarized but cannot issue instructions
to Attune.

## 3. Continue the conversation

After a live answer, the principal can ask a follow-up:

> When is it due?

The recent conversation window lets Attune relate that question to its prior
answer. Short-term history is isolated by channel and user: a Slack exchange
does not unexpectedly appear in Google Chat. Durable memory is shared across
the instance, so explicitly taught preferences remain available everywhere.

Useful memory interactions include:

> Remember that Sarah prefers a short decision summary.

> What do you know about Sarah?

> Forget 2.

Deletion remains a two-step operation: Attune asks for `confirm forget` before
removing the selected memory.

## 4. Review prepared work

When Gmail ingestion finds an actionable message, Attune triages it and may
prepare a reply through the durable draft-and-approve workflow. The configured
`ATTUNE_APPROVAL_CHANNEL` receives one approval card. Approve, edit, or reject
there; an approved result becomes a Gmail draft for human review rather than a
silently sent message.

Free-form conversation does not bypass that workflow. For example:

> Move tomorrow's meeting to 3 PM.

Attune recognizes this as a write request, makes no change, and explains that
free-form chat is currently read-only. Writes require a capability with an
explicit autonomy policy and audited approval path.

## 5. Choose either interaction channel

For Slack interaction, Attune receives allowlisted owner DMs through Socket
Mode. Messages in ordinary Slack channels are ignored. Configure:

```dotenv
ATTUNE_SLACK_ALLOWED_USERS=U0123456789
ATTUNE_INTERACTION_CHANNELS=slack
```

For Google Chat interaction, Attune accepts only allowlisted human senders from
the configured space. App messages and card clicks use the verified
republisher/Pub/Sub handoff described in the deployment guide. Configure:

```dotenv
ATTUNE_CHAT_ALLOWED_USERS=users/123456789
ATTUNE_INTERACTION_CHANNELS=google_chat
```

Both can be enabled:

```dotenv
ATTUNE_INTERACTION_CHANNELS=slack,google_chat
```

Delivery routes remain independent. Briefs can go to both channels while
approvals use one channel, avoiding duplicate decisions.

## What the natural-language layer can do

| Request | Behavior |
|---|---|
| Overview, “what's new,” or “what needs attention” | Fresh Gmail/Calendar brief |
| Gmail question | Capped live Gmail search and evidence-grounded answer |
| Calendar or agenda question | Capped live Calendar window and evidence-grounded answer |
| Follow-up question | Uses recent history in that channel/user conversation |
| Memory or `autonomy` command | Uses the explicit inspect/teach/delete/status command path |
| General conversation | Answers from durable memory and recent conversation |
| Free-form Workspace mutation | Refuses without changing data |

If a live read fails, Attune reports the source and exception type and states
that nothing changed. It does not silently substitute a memory-only answer for
a failed Workspace lookup.
