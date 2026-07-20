# Hosted channel conversation

This document specifies the operated Attune path from an authenticated channel
message to a bounded assistant response. It is deliberately separate from
channel linking. A verified destination proves where Attune may communicate;
it does not by itself authorize model use, Workspace reads, or conversation
storage.

The route is channel-parameterized: the Slack owner-DM surface reuses the
same acceptance, job, executor, and delivery state machine with Slack proofs
and the `channel.slack.converse` job kind
(see [`hosted-channel-installation.md`](hosted-channel-installation.md)).
This document narrates the Google Chat instance, which has live development
evidence.

## User contract

An owner sends an ordinary direct message to the Attune Google Chat app. Attune
acknowledges receipt promptly, processes the message asynchronously, and posts
one response to the same verified owner DM. The response can use a bounded live
Gmail or Calendar read, provide a fresh overview, refuse a free-form mutation,
or answer a general conversational question. It cannot execute a free-form
write.

`/link CODE` remains a setup command. Once a destination is active, no new link
code is needed for conversation. Signing out of the browser does not alter the
tenant's channel binding.

## Trust boundaries

```text
Google Chat
   │ provider-authenticated event, untrusted message text
   ▼
public Chat ingress
   │ exact audience/caller, owner-DM schema, bounded fields
   ▼
private channel broker
   │ HMAC refs; resolve exactly one active tenant binding; enqueue atomically
   ▼
private dispatch broker ── Cloud Tasks OIDC ──> tenant-scoped worker
                                                  │
                         ┌────────────────────────┴──────────────────────┐
                         ▼                                               ▼
              private secret broker                            private model gateway
              fixed Gmail/Calendar reads                       fixed model tasks only
                         │                                               │
                         ▼                                               ▼
                 Google APIs                                    configured model API
                         └────────────────────────┬──────────────────────┘
                                                  ▼
                                      private channel broker
                                      fixed verified-DM delivery
```

The public ingress has no database, Workspace, model, memory, provider-send, or
secret authority. The channel broker has no model or Workspace credential. The
worker never receives an OAuth refresh token or model API key. The secret
broker constructs fixed Google requests from a server-resolved credential-use
intent. The model gateway owns only the platform model secret and has no
database or Workspace access.

## Inbound acceptance

The ingress MUST:

- verify Google's service identity, issuer, and exact endpoint audience;
- require a human `MESSAGE` in a `DIRECT_MESSAGE` space;
- bind top-level and nested sender/space facts as specified in SEC-701B;
- accept only bounded UTF-8 message text and a canonical Google message
  resource name;
- treat message text as C3 untrusted data and never log it;
- route an exact `/link CODE` through the one-use setup ceremony;
- route every other message through the conversation acceptance ceremony; and
- return a prompt acknowledgement without waiting for Workspace or model I/O.

The broker derives keyed hashes for the app, actor, destination, and provider
message. A memberless `SECURITY DEFINER` function resolves those hashes against
exactly one active installation and owner-DM destination whose preference
selects Google Chat for interaction. Zero or multiple matches fail closed.
Browser identity, tenant ID, principal ID, installation ID, destination ID,
connector ID, and capability are never accepted from the provider request.

In one transaction the function deduplicates the provider message, creates or
resolves the conversation, appends the untrusted user turn once, creates the
fixed conversation job and ingress dispatch intent, and creates a content-free
pre-effect audit intent. A replay returns the same opaque dispatch intent and
does not append another turn or create another job.

## Durable execution

The only initial worker route is:

```text
purpose:    channel.google_chat.converse
capability: assistant.conversation.read
risk:       R0
```

Its payload contains only canonical server-generated UUIDs and the user-turn
sequence. The worker re-reads every object under forced RLS and requires the
job, event, conversation, principal, active destination, active read-only
policy, Google connector, and interaction preference to agree. A UUID is a
lookup key, not authority.

The planner is the existing bounded five-way classifier: brief, Gmail,
Calendar, write, or general. Model output cannot add a capability. Deterministic
heuristics prevent an obvious mutation from becoming a read and prevent an
obvious live-read question from silently becoming memory-only conversation.

The initial read limits are:

- at most ten Gmail thread metadata summaries from a query no longer than 300
  characters; message bodies are not returned in the initial hosted release;
- a Calendar window no longer than 31 days and at most 25 events;
- at most six recent conversation turns, each truncated before model use;
- a deterministic keyword router resolves brief, Gmail, Calendar, and write
  requests without a model call; only an ambiguous request falls through to
  one classifier call, and every accepted message gets exactly one answer
  call; and
- one outbound Chat response no longer than the provider limit configured in
  code.

The secret broker accepts only a canonical credential-use intent and a fixed
operation schema. It constructs all Google URLs and query parameters. It
returns bounded, allowlisted fields; refresh and access tokens never leave the
broker.

The model gateway accepts only `classify` or `converse`, a bounded array of
role/content messages, and no caller-selected model, URL, headers, tools,
response callback, or credential. Infrastructure fixes the OpenAI-compatible
base URL and model names. It rejects redirects and oversized responses and
returns only bounded assistant text. Model requests and responses are never
logged.

## Outbound delivery and idempotency

The worker stores the assistant turn before requesting outbound delivery. It
then asks the channel broker to deliver by canonical destination UUID and
conversation job UUID. The broker decrypts the active route, fixes the Google
Chat API origin and `chat.bot` scope, derives a deterministic request ID, and
validates the returned message resource. A retry therefore cannot intentionally
fan out to another destination and Google can deduplicate the provider create.

The worker cannot supply reply text, tenant identity, a provider route, or a
provider request ID to this boundary. A broker-owned, forced-RLS
`hosted_channel_deliveries` record binds the canonical job, assistant turn,
destination, and delivery state. A memberless `NOLOGIN BYPASSRLS` function
owner resolves that record for the broker; only the broker's login role may
claim or complete it. The broker reads the stored assistant turn itself and
uses the job UUID as Google's deterministic request ID. This keeps model output
out of the cross-tenant API and prevents destination or body substitution by a
compromised worker.

Credential-use idempotency keys include the durable job attempt. An actual
re-lease can therefore request a fresh two-minute intent, while a consumed
intent in the same attempt fails closed instead of replaying a provider call.

The job is successful only after provider acknowledgement and content-free
post-effect audit. Ambiguous model, Workspace, database, audit, or provider
outcomes enter reconciliation instead of being treated as safe retries.

Google Chat renders the ingress's synchronous response, so the owner sees
immediate feedback while the job above runs. Slack's Events API ignores that
response body, so the Slack ingress instead asks the channel broker to send
a fixed, provider-owned acknowledgment ("Working on it.") right after a
message is durably accepted and dispatched. The acknowledgment is audited
before and after the send and idempotent per provider message -- a retried
Slack event never sends it twice -- and its failure never affects the `200`
already returned to Slack (see the Slack implementation's "Acknowledgment"
section in
[`hosted-channel-installation.md`](hosted-channel-installation.md)).

## Data handling

- Provider message bodies and assistant replies are C3.
- The provider-event row stores deduplication and bounded routing facts, not a
  second copy of the message body.
- Conversation turns are retained only under the configured conversation
  retention policy and are excluded from application logs and audit metadata.
- Workspace result fields remain C2/C3 and are transient in the worker/model
  request unless an explicit user-visible turn contains a derived answer.
- Model prompts include provenance framing and the minimum selected source
  fields. Workspace content and conversation history remain untrusted data.
- Usage records contain counts and model-route labels, never prompt text.

## Activation gates

The feature defaults off. Development activation requires, in order:

1. reviewed migration and forced-RLS verification;
2. dormant model gateway with a dedicated service identity and secret grant;
3. bounded provider-read and model-gateway contract tests;
4. worker and broker image deployment with the conversation route disabled;
5. cross-tenant, replay, route-substitution, prompt-injection, SSRF, redirect,
   oversized-body, and duplicate-delivery tests;
6. model-provider retention/training/residency review and a populated model
   secret version;
7. paging, reconciliation, usage ceilings, and cost limits;
8. a saved Terraform activation plan with no unrelated changes;
9. live owner-DM general, Gmail, Calendar, mutation-refusal, and replay tests;
10. content-free audit verification and empty post-apply Terraform plans.

Passing link or fixed-content delivery tests does not satisfy these gates.

Development completed steps 1 through 4 on 2026-07-16 UTC. Migration 0025
applied under the dedicated migrator and reported 33 forced-RLS tenant tables.
The worker, secret-broker, and channel-broker conversation images then deployed
as Ready revisions while `enable_google_chat_conversation=false`; the saved
plan changed only those three services, its post-apply plan was empty, and no
worker-to-channel-broker invoker grant or dispatch route was activated.

The subsequent private-runtime activation used a separate saved plan: it added
the worker-only channel-broker invoker grant, registered the single fixed
conversation dispatch route, and supplied only the private model/channel
broker origins and audiences to the worker. A second saved edge plan changed
only the dedicated Google Chat ingress revision, adding the private dispatch
broker origin/audience and setting the conversation flag. Both roots converged
to empty plans.

Four live owner-DM journeys then traversed ingress, channel acceptance,
dispatch, Cloud Tasks, worker execution, model gateway, the applicable bounded
Workspace broker, canonical reply delivery, and content-free audit. General,
Gmail, Calendar, and mutation-refusal executions all completed; ingress and
broker calls returned 200/204, outbound delivery returned 200, and no error log
was emitted by any service in the chain. Replay, cross-tenant, substitution,
redirect, oversized-body, and duplicate-delivery behavior remains enforced by
the automated contract and isolated-PostgreSQL suites. The development feature
is therefore active for the verified owner DM; activation is not a production
readiness attestation.

## The browser surface

The browser is a third front door, not a fourth copy of the channel state
machine. A signed-in owner with an active read-only policy and an active
Google connector converses directly from the setup page. There is no
installation, preference, or destination ceremony for `web`: the ordinary
authenticated session is the whole route. No channel broker is involved, and
there is no delivery row -- the stored assistant turn is itself the delivery.
The browser polls for it.

Two decisions distinguish this surface from the destructive onboarding
ceremonies and from the Slack/Google Chat channel state machine, both dated
2026-07-18 in [`decisions.md`](decisions.md):

- the authenticated session is the route; and
- acceptance requires ordinary session, same-origin, and CSRF proofs rather
  than the ten-minute recency reserved for destructive ceremonies, with edge
  throttling sized for a polling browser tab rather than an infrequent
  ceremony.

### Acceptance

Migration 0041 adds a tenant-scoped `attune.accept_web_owner_message`
function, owned by a new memberless `attune_web_message_executor` role that
holds only the columns and functions it needs; the ordinary control-plane
role holds `EXECUTE` only. In one transaction it re-validates the caller's
session, principal, active policy, and active Google connector; deduplicates
the message per turn; appends the owner turn; and creates the fixed
`channel.web.converse` job and its ingress-attributed dispatch intent. Audit
attribution runs through a new `channel_message` producer kind, checked
against `session_user` membership the same way the existing channel-message
path is checked; dispatch intents stay attributed `ingress`, with the
`ingress` producer check widened to accept the control plane calling through
the executor. A replay returns the same opaque identifiers and does not
append a second turn or create a second job.

### Control-plane routes

Two routes exist behind the `hosted_web_conversation_enabled` gate:

- `POST /v1/conversation/messages` accepts a schema-versioned, 1-8,000
  character message under ordinary session, same-origin, and CSRF proofs (not
  recent-authentication). It returns `202` with the accepted user-turn
  sequence; the acceptance audit reaches the private audit writer before
  dispatch through the dispatch broker.
- `GET /v1/conversation/turns` returns bounded pages of canonical turns plus
  a `pending` flag derived from whether the newest turn's actor is the owner
  (an unanswered turn) rather than the assistant.

Neither route accepts a destination, provider, or delivery field from the
browser.

### Execution and delivery

The worker executes `channel.web.converse` on the shared bounded read-only
conversation executor -- the same routing, Gmail/Calendar reads, and model
classification the Google Chat and Slack executors use -- but the web
executor never calls a reply broker. It appends the assistant turn and stops;
there is no destination and no channel preference to check, only the active
policy and active Google connector re-checked at execution time, mirroring
the acceptance function's authority.

### Setup-page panel

The setup page shows a bounded composer only when the gate is enabled and the
owner has a connected Workspace and an active policy. It polls
`/v1/conversation/turns` every two seconds, shows a working indicator while a
turn is pending, and surfaces a "still working" note once a pending turn
passes 60 seconds. Rendering is text-only.

Three purely client-side additions (no server or protocol change) round out
the polling contract:

- **A genuine terminal state past five minutes.** A pending turn that has
  not resolved after five minutes gets an honest note -- "this is taking
  much longer than expected... your message was accepted and will still be
  answered; check back or send a follow-up" -- and the poll cadence drops
  from every two seconds to every fifteen. This is not an error state (the
  acceptance ceremony already made the turn durable; nothing has failed)
  and it never stops polling outright, it just stops polling aggressively
  for a reply that is already known to be unusually slow. The distinct
  error path (`GET /v1/conversation/turns` itself failing repeatedly) keeps
  its own separate message and still stops the indicator rather than
  claiming the turn is still in flight.
- **First-run hints.** An empty panel (zero turns, nothing pending) shows
  three clickable example prompts that prefill the composer and disappear
  once any turn exists. The wording is deliberately drawn from the routes
  this executor actually answers -- brief, Gmail, Calendar, general -- and
  never suggests a write the bounded executor refuses.
- **Opt-in browser notifications.** A control next to the panel requests
  the browser's `Notification` permission only on that explicit click,
  never automatically. When granted and the tab is hidden, a reply arriving
  through the existing poll shows a content-free `Notification("Attune
  replied")` -- no message text, matching the content-free discipline this
  document's data-handling section already applies to logs and audit
  fields -- and clicking it focuses the tab. A denied or unsupported
  browser removes the control and explains why instead of leaving it inert.

All three are advisory presentation only: they change what the page shows
and how fast it polls, never the acceptance, dispatch, execution, or
delivery contract described above.

### Activation gates and evidence

`enable_hosted_web_conversation` gates the edge: Cloud Armor priority `893`
admits only the exact `/v1/conversation/messages` and `/v1/conversation/turns`
paths, rate-limited at 60 requests per 60 seconds per IP -- wider than the
10-per-60-second onboarding-ceremony rules because a browser tab polling
every two seconds must not trip it. `enable_web_conversation` gates the
runtime worker environment, the model-gateway environment widening, and the
dispatch route.

Migration 0041 applied under execution
`attune-development-database-migrate-zsvpg` from migrator digest
`sha256:3ff0777cf29634b0467d14167871144b3e5f1253667d435bc3af4ed0cb8b585f`; the
boundary verifier passed with 41 migrations. The control plane deployed at
digest
`sha256:d4b4e097ee57a4b326fe415d9113901017facdf6e4c04f97ad13474029e25432`
(revision `00029-z6t`), the worker at digest
`sha256:bc29b66a05dbda0d6f9208dd42329e230ca0094e7aebe5f79f0d2f7d26193a54`
(revision `00016-dt9`), and the dispatch broker at revision `00012-l5z`.

Live probes: both conversation paths return an application-level `401
invalid_session` unauthenticated, a near-miss path stays edge-denied `403`,
and all Terraform plans converge empty. The owner then exercised a live
browser conversation end to end. With this, Google Chat, Slack, and the
browser all ride the same durable acceptance, dispatch, bounded read-only
execution, and audit spine.
