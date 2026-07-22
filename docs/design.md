# Attune design

Attune is a durable, memory-aware assistant that observes a principal's
workspace, prepares useful work, and acts only within earned authority.

## Principles

1. Memory is product behavior, not a transcript archive. Preferences and
   relationships must be inspectable, correctable, and scoped to one principal.
2. Autonomy is earned. Draft-first workflows, explicit grants, human approval,
   durable checkpoints, and append-only audit records are core controls.
3. Provider and hosting choices are configuration. Product concepts do not
   encode a particular company, model gateway, or cloud deployment target.
4. Credentials have narrow roles. Workspace OAuth, MCP server auth, Chat app
   auth, channel tokens, and model credentials are separate boundaries.
5. Incoming notifications are signals, not commands. Credential-bearing
   runtime processes expose no public listener.
6. The model is not a security principal. Identity, tenant selection,
   authorization, capability limits, approvals, and provider effects are
   enforced deterministically outside the model.

The normative [security architecture](security-architecture.md) assigns stable
requirements to these boundaries. New data sources, channels, memory behavior,
model routes, and write capabilities must satisfy its feature-review checklist.

## Components

```text
Gmail / Calendar ── OAuth or MCP connector ─┐
Google Chat / Slack ─ optional channels ────┼─> dispatcher -> durable workflows
polling / Google Pub/Sub ─ transports ──────┘                    │
                                               OpenAI-compatible LLM + memory
                                                               │
                                                      audit + checkpoints
```

The OpenAI Python SDK is sufficient for compatible Chat Completions gateways:
`base_url`, `api_key`, and model identifiers are ordinary configuration. The
SDK emits bearer authentication, so a separate bearer wrapper adds no value.
Task names select configurable models; no provider catalog is compiled in.

## Workspace access

`google_oauth` is the default backend. It is direct, well understood, and
supports both polling and Google Pub/Sub event ingestion.

`mcp` is a real alternative using Streamable HTTP. Its advantage is boundary
placement: a managed server can own consent, credentials, tool allowlists,
policy, and centralized auditing. It does not automatically improve model
reasoning or Workspace semantics, and it adds an operational dependency. MCP
currently uses connector polling so behavior is backend-neutral.
The adapter targets a small, [versioned Gmail and Calendar tool
contract](mcp-contract.md), not a named MCP vendor.

## Instance and deployment model

One Attune instance represents one principal. It owns a single memory namespace,
credential set, audit log, and state directory. Deployment targets—local host,
VM, container platform, or cloud—are operational choices. When isolation is
required, deploy separate instances instead of adding named profiles to code.

This describes the current self-hosted runtime. A future hosted service does not
make local state multi-tenant: it separates control/event ingress from queued,
tenant-scoped execution and replaces SQLite, JSON, JSONL, and local Qdrant state
with explicitly tenant-aware durable services. Hosted workers remain stateless
between jobs.

Polling is the portable default. `google_pubsub` explicitly names the advanced
Google-specific transport. Gmail and Chat Workspace Events can publish to pull
subscriptions. Calendar and Chat app callbacks use the stateless republisher,
which verifies where required and hands events to Pub/Sub without model,
workspace-user, memory, or workflow credentials.

## Channels and routing

Slack and Google Chat are peers and are optional. Briefs and notifications may
use zero, one, or several channels; approvals use zero or one; interactions may
use zero, one, or several.

Both channels normalize authenticated human messages into one bounded
natural-language planner. The planner can select live Gmail reads, live
Calendar reads, a fresh brief, or memory-informed conversation. It is not an
unrestricted tool loop and cannot execute free-form mutations. Workspace
writes remain explicit durable workflows governed by autonomy and approval.

Model output is an untrusted proposal. Trusted code binds the authenticated
actor and tenant, validates a registered typed capability, constructs provider
arguments, and enforces the maximum risk tier. Successful history cannot grant
authority beyond that product-defined ceiling.

Google Chat app messages and card actions arrive through its verified callback;
proactive messages use a separate app identity — a dedicated service account
by default, or (for organizations that disallow creating IAM service-account
keys) a dedicated OAuth user credential obtained the same way as the
principal's Gmail/Calendar credential but scoped to Chat and saved to its own
file. Either way, Google Workspace OAuth is not reused as Chat app
authentication: the Chat identity is always a second, distinct credential.
Sender allowlists and destination visibility acknowledgement are mandatory
safety controls.

## Workflow and data model

LangGraph provides resumable workflows and human-in-the-loop checkpoints.
SQLite stores local workflow/retry state, Mem0 and Qdrant hold long-term memory,
and a JSONL audit trail records decisions and effects. Source cursors advance
only after durable handling or durable retry recording. Approval actions are
idempotent and actor-authorized.

The earned-autonomy ladder remains: observe, draft, act-with-notification, and
act. Graduation is task-scoped, based on track record, and always reversible.
