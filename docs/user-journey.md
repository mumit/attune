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
6. Configure delivery and interaction channels, then activate briefs.
   Capability upgrades such as Gmail draft
   creation are separate, explicit consent and policy changes.

Closing or denying the second screen leaves the Attune account signed in and
unconnected. Retrying creates a fresh ten-minute transaction. A completed
connector is verified instead of silently starting a replacement. A temporary
verification failure does not discard or replace the connector; a later
signed-in visit safely retries the fixed check.

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

Attune plans a capped Gmail search, fetches metadata for at most ten matching
threads and details for at most three, and answers only from those live
results.

> What is on my calendar tomorrow morning?

Attune resolves “tomorrow morning” in `ATTUNE_TIMEZONE`, performs a live
Calendar read for that bounded window, and summarizes the returned events.
This behavior is identical with direct Google OAuth and MCP because both
implement the same internal Workspace connector.

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
