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
audited expired-protocol retention executor and manual Cloud Run job are live;
its empty execution, IAM boundary, migration verifier, and drift convergence
are verified. Its failure and backlog incidents now page through the verified
development channel. A separate, non-database scheduler identity now invokes
only that job; its daily development schedule was activated after paused-first
authenticated-path, paging, verifier, IAM, and convergence evidence. New
environments remain paused by default. Customer content retention and controls
are not active yet. Export/deletion, support repair, customer-visible audit,
adversarial assurance, and external security review remain later independent
slices.

The Slack half of the channel-installation design is now implemented and
tested but not deployed: migration 0038 adds the one-use OAuth-state claim
ceremony, the forced-RLS encrypted bot-token store, owner-DM acceptance, the
`channel.slack.converse` conversation route, delivery, and the extended
disconnect/reinstall lifecycle; the Python layer adds the signature-verified
Slack ingress service, the broker-held OAuth exchange with fixed app/scope
verification, browser-bound callback consumption, and the reused bounded
read-only conversation executor. All Slack stages are behind independent
default-off gates (`ATTUNE_SLACK_CHANNEL_ENABLED`,
`ATTUNE_HOSTED_SLACK_INSTALL_ENABLED`, `ATTUNE_ENABLE_SLACK_CONVERSATION`),
and the platform Slack app, Terraform substrate, edge activation, and live
owner ceremony remain future operator work recorded in
[`hosted-channel-installation.md`](hosted-channel-installation.md).

Slack installation, verified ingress, the fixed delivery test, and the
replay-safe durable conversation route are now live in development behind
their independent gates, exercised on 2026-07-17: migration 0038's dormant-first
deploy, staged activation through `enable_slack_ingress`,
`slack_channel_enabled`, and `enable_hosted_slack_install`, a live owner
install, a delivered fixed-content test, and a first bounded Calendar
conversation dispatched and answered through the private broker. Two
decisions from this rollout are recorded in
[`decisions.md`](decisions.md): each provider ingress runs its own workload
identity, with dispatch attribution now accepting multiple authorized emails
per producer kind; and internet egress exists only on a dedicated,
subnet-scoped Cloud NAT for Slack's ordinary internet API, while every other
workload keeps the no-NAT fail-closed posture. The live disconnect /
fail-closed refusal / reinstall / delivery-test / conversation-recovery
regression, exercised for Google Chat, has now also been exercised live for
Slack (2026-07-17/18), including a reinstall defect it found and fixed:
`consume_slack_install` collided with the tenant/provider/reference unique
constraint instead of reusing the revoked installation row, corrected by
migration 0039. The same window shipped the audited, idempotent "Working on
it." acknowledgment for Slack (migration 0040) and a deterministic-first
conversation routing change that skips the classify model call for
unambiguous requests on both channels, together cutting measured end-to-end
reply latency from roughly 15 seconds. The explicit mutation-refusal probe
over Slack remains outstanding; that path is covered by tests and was
exercised live over Google Chat.

The browser is now live in development as the product's own conversation
front door, distinct from the optional Slack/Google Chat peer channels: a
signed-in owner with an active policy and an active Google connector
converses directly from the setup page, with no installation, preference, or
destination ceremony and no channel-broker involvement. Migration 0041 added
the tenant-scoped `attune.accept_web_owner_message` acceptance function,
owned by the memberless `attune_web_message_executor`, with per-turn
idempotency and a new `channel_message` audit producer kind; the control
plane's `POST /v1/conversation/messages` and `GET /v1/conversation/turns`
routes require ordinary session, same-origin, and CSRF proofs rather than the
ten-minute recency reserved for destructive ceremonies. The worker executes
`channel.web.converse` on the shared bounded read-only conversation executor
with no reply broker; the stored assistant turn is the delivery, and the
setup-page panel polls for it every two seconds. Both the edge gate
(`enable_hosted_web_conversation`, Cloud Armor priority `893`, 60 requests per
60 seconds) and the runtime gate (`enable_web_conversation`) were exercised
in development: migration 0041 applied and verified 41 migrations, the
control plane and worker deployed at fixed digests, both conversation paths
returned an application-level 401 unauthenticated, a near-miss path stayed
edge-denied 403, all Terraform plans converged empty, and the owner exercised
a live browser conversation end to end. With this, Google Chat, Slack, and
the browser -- all three planned front doors -- now share the same durable
acceptance, dispatch, bounded read-only execution, and audit spine. Full
route shapes, the acceptance ceremony, and rollout evidence are in
[`hosted-conversation.md`](hosted-conversation.md#the-browser-surface); the
two rollout decisions are recorded in [`decisions.md`](decisions.md).

The first customer-export authority slice is implemented and deployed:
four server-defined scopes, recent-session binding, idempotent request,
one-use executor claim, atomic audit evidence, and function-only mutation. It
contains no ready/publish transition and grants no storage or KMS authority.
The deterministic archive builder is also implemented and adversarially tested
with fixed paths/schema/kinds, structural secret-negative validation, member
and archive digests, and record/byte/depth ceilings. A claim-bound positive
database projection is deployed in development with a current-owner check,
fixed fields, unreviewed nested-JSON exclusions, and a 100,000-record ceiling.
Its real-PostgreSQL owner/lease/secret-negative/archive tests, exact migration
verifier, and empty infrastructure plans are recorded. Authenticated archive
encryption plus a separate dormant export key and temporary bucket are also
implemented and deployed: the reserved writer has encrypt/create/delete but no
decrypt/read/list authority, inherited bucket readers are removed, the bucket
is empty, and an object-free policy-administrator path keeps Terraform
manageable. An exact-claim, idempotent encrypted-object completion transition
is deployed and live-verified in development. No writer can invoke it yet.
Cleanup, download, and UI remain.

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
