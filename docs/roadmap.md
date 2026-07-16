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

Step 5 also has a live development fixed R0 policy ceremony: recent owner
authentication, content-free mandatory audit, exact function-owned policy and
grant creation, and resumable step advancement are implemented. Owner
activation was exercised on 2026-07-16. The channel and activation ceremonies
remain.

The next step-5 slice now records bounded Slack/Google Chat preferences behind
a default-off gate. It preserves independent interaction and brief choices and
advances only to `authorized`; live app installation, destination binding,
verified ingress/test delivery, replacement, and disconnection still remain.

The following slice is specified in
[`hosted-channel-installation.md`](hosted-channel-installation.md): a shared
forced-RLS installation/destination state machine, Google Chat owner-DM link
codes, Slack OAuth installation, private opaque route resolution, and explicit
fixed-content tests. Provider ingress and callback consumption remain
default-off until their separate activation gates pass.

The Google Chat portion now has its database broker boundary, private broker,
and verified ingress deployed in development. The platform-owned direct-message
app is restricted to the development owner, and its exact provider route is
active behind method/path Cloud Armor filtering and application-level Google
identity and audience verification. Unauthenticated, invalid-token,
wrong-method, and wrong-path live probes are denied, and the post-activation
Terraform plan is empty. Owner-DM linking and one-use replay rejection now
have live evidence. Encrypted route adoption and fixed-content delivery are
also live and verified. The replay-safe durable conversation route is now
active for the verified development owner DM. It resolves tenant and active
destination only from provider facts, dispatches through the private broker,
uses fixed model tasks and brokered bounded Gmail/Calendar reads, refuses
free-form writes, and delivers the stored response through the private channel
broker. General, Gmail, Calendar, and mutation-refusal journeys have live
end-to-end evidence with content-free audits and empty Terraform plans. This is
a development activation, not an operated-production launch gate. Google Chat
destination disconnect and deliberate replacement are implemented behind an
independent default-off gate and enabled in the development environment:
canonical ingress/delivery authority and the encrypted route are revoked
immediately, and reconnection requires a fresh owner-DM link plus fixed
delivery test. The edge and application activation plus the live owner
disconnect, fail-closed message refusal, fresh link, fixed delivery test, and
conversation recovery are verified. Relative dates are now grounded from an
authoritative server clock and `America/Vancouver`, with a live repeat of the
same Calendar question proving that prior email context no longer supplies the
date.
Workspace disconnect/reconnect was already live. The first retention safety
slice is implemented: the live database verifier now requires an exact,
reviewed lifecycle classification for every tenant-bearing relation. A bounded,
audited expired-protocol retention executor and manual Cloud Run job are
implemented but remain dormant pending live synthetic verification; customer
content retention and controls are not active yet. Slack installation/lifecycle,
export/deletion, support repair, customer-visible audit, adversarial assurance,
and external security review remain later independent slices.

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
