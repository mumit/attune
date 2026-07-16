# Hosted channel conversation

This document specifies the operated Attune path from an authenticated channel
message to a bounded assistant response. It is deliberately separate from
channel linking. A verified destination proves where Attune may communicate;
it does not by itself authorize model use, Workspace reads, or conversation
storage.

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
- one classifier call and one answer call per accepted message; and
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

The job is successful only after provider acknowledgement and content-free
post-effect audit. Ambiguous model, Workspace, database, audit, or provider
outcomes enter reconciliation instead of being treated as safe retries.

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
