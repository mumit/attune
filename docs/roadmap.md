# Roadmap

## Current foundation

- Attune package and CLI naming
- OpenAI-compatible SDK client with configurable task models
- Google OAuth and MCP workspace backends
- portable polling and advanced Google Pub/Sub ingestion
- independently configurable Slack and Google Chat routes
- editable, migration-aware `attune init`
- versioned, secret-free setup state and deterministic local Qdrant provisioning
- versioned generic MCP tool contract and capability diagnostics
- fail-fast validation for optional channel routes
- durable approvals, memory, audit, retries, and earned autonomy

## Near term

- exercise the Google Chat app callback and cards in a real test space
- add live MCP conformance fixtures for selected server packages
- use the [security architecture](security-architecture.md) feature-review
  checklist for every new connector, model route, memory behavior, and write
  capability

## Hosted foundation

The hosted path is gated by the security architecture rather than being a
deployment wrapper around the current local process. Work proceeds in this
order:

1. validate the no-customer-data GCP foundation in development and staging;
2. deploy the implemented private dispatch-broker service and Cloud Tasks
   adapter only after fixed queue routing, deterministic capability routes, and
   a live HTTP worker adapter exist on the tenant-aware data core;
3. extend the implemented encrypted connector vault and private install/revoke
   broker with broker-mediated provider operations, reconciliation, alerting,
   and full intent-to-audit live evidence;
4. authenticated control plane and explicit account/connector identity links;
5. extend versioned, resumable setup state into tenant-bound hosted onboarding;
6. deterministic typed capability gateway and risk-tier enforcement;
7. verified provider/channel ingress with replay-safe durable jobs;
8. customer-visible audit, retention, export, deletion, revocation, and repair;
9. adversarial isolation and side-effect regression suites; and
10. independent penetration testing, Google/CASA evidence, incident exercises,
   and the documented alpha/public-beta launch gates.

The first slice of step 5 is live in development: a signed-in owner can start
and resume a tenant-bound, versioned setup record whose Workspace status is
derived from canonical connector state. Channels, policy, and activation still
require fixed server-side ceremonies. Step 6 now has a tested, non-deployed
admission core for exact proposals, tenant-scoped policy/grant and connector
resolution, and risk ceilings. Its dispatch integration, execution budgets,
freshness/idempotency, audit, and approval gates remain; this does not skip the
remaining work or assurance gates in steps 2–5.

Step 5 also has a dormant fixed R0 policy ceremony: recent owner
authentication, content-free mandatory audit, exact function-owned policy and
grant creation, and resumable step advancement are implemented. Live owner
activation evidence and the channel/activation ceremonies remain.

The first platform mapping is [`hosted-gcp.md`](hosted-gcp.md), and the initial
declarative substrate is `deploy/gcp/foundation`. Applying that foundation does
not authorize customer data or constitute a hosted launch.

The current SQLite, JSON, JSONL, and local Qdrant implementation remains a
single-principal self-hosted substrate. It is not a hosted tenant boundary.

## Later

- richer calendar negotiation and follow-up workflows
- temporal/entity memory evaluation and optional Graphiti migration path
- additional channel adapters behind the same routing interface
- voice as a separate front door, without coupling it to a model provider
