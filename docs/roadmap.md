# Roadmap

A full 2026-07-18 review of the implementation is recorded in
[`current-state.md`](current-state.md); its
[gap analysis](gap-analysis.md) and the phased
[future-state plan](future-state.md) add the product-intelligence dimension
to the hosted assurance sequence below.

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
require fixed server-side ceremonies. Step 6's admission core (exact
proposals, tenant-scoped policy/grant and connector resolution, risk
ceilings) is now wired to the real dispatch spine for one capability,
`google.gmail.draft.create` at R2 -- implemented and tested, not deployed;
see the Phase 5 stage 3 paragraph below. Rate/cost/concurrency budgets, live
provider source-freshness re-verification, and admission/approval-decision
audit remain before that capability's own activation gate can pass; this
does not skip the remaining work or assurance gates in steps 2–5, and no
other capability or write surface is wired.

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
environments remain paused by default. Customer content retention and an
owner-initiated tenant deletion ceremony are now designed and implemented
(migration 0046; see `docs/data-lifecycle.md`'s "Content retention and
tenant deletion design" section and the matching decisions.md entry) but not
yet active: both remain behind independent default-off gates
(`ATTUNE_ENABLE_CONTENT_RETENTION`, `ATTUNE_HOSTED_DELETION_ENABLED`), a
registry-driven executor walks every classified relation for the
right-to-be-forgotten path behind a 14-day grace period, and the gated
real-PostgreSQL suite proves per-tenant isolation, RLS, one-use claims, and
an end-to-end multi-tenant deletion -- but neither executor's Cloud Run job
is deployed yet, neither has passed the paused-first activation ceremony the
protocol-retention executor already did, and the independent cross-tenant
restore-suppression ledger this design depends on for backup/PITR recovery
remains unbuilt. Support repair, customer-visible audit, adversarial
assurance, and external security review remain later independent slices.

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

Stage 1 of converging hosted onto the local product intelligence
(`docs/future-state.md` Phase 5 item 1; `docs/gap-analysis.md` G8/G18) is
implemented and tested but not deployed: migration 0042 adds forced-RLS
`attune.importance_signals` and `attune.attention_items`, registered in the
reviewed lifecycle inventory as customer content and granted only to
`attune_worker`; `attune.hosted.intelligence.PostgresImportanceProfile` and
`PostgresAttentionStore` satisfy the exact local `ImportanceProfile`/
`AttentionStore` protocol shapes, importing the same tier-rule engine
(`orchestrator.importance.assess_from_signals`) local triage and briefs
already use, with sender/channel/thread references stored as keyed HMAC
digests rather than plaintext. No executor constructs either class yet, no
HMAC key is provisioned outside tests, and no hosted behavior changes: this
mirrors the capability gateway's own "tested, non-deployed admission core"
status above. Wiring an executor to actually read/write these stores, and
extending the same pattern to correlation/brief assembly, are later
independent slices.

Stage 2 of the same effort — hosted conversational memory retrieval plus
explicit teach/inspect/forget commands on the shared conversation executor
(Google Chat, Slack, and web) — is implemented and tested behind a
default-off gate, `ATTUNE_ENABLE_HOSTED_MEMORY`, and not deployed: a third
fixed model-gateway task (`embed`) joins `classify`/`converse` with the same
worker-credential-free discipline, the tenant/principal filter is injected
by the storage adapter from `TenantContext` (SEC-201) rather than by the
model or message text, memory commands are recognized deterministically
before any classifier call, and the two-step forget confirmation's turn-scoped
state rides in the already-durable `conversation_turns.provenance` column
rather than any shared worker process state (SEC-011). See
[`hosted-memory.md`](hosted-memory.md) for the design and
[`decisions.md`](decisions.md) for the dated record. Gate-off behavior is
pinned as byte-identical to pre-stage-2 conversation handling; there is no
hosted approval workflow or signal-capture path yet, both deliberately out
of scope.

Stage 3 of the same effort — wiring the dormant typed capability gateway
into the dispatch spine, then introducing the first hosted write capability
(`docs/future-state.md` Phase 5 item 3; `docs/gap-analysis.md` G17; roadmap
step 6's remaining half) — is implemented and tested behind a default-off
gate, `ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY`, and not deployed. One
capability is registered, `google.gmail.draft.create` v1, at product risk
tier **R2** per the security architecture's own risk-tier table (not the R1
this stage's plan initially proposed — see `decisions.md` for why the
normative table governs). A new migration (0043) adds an immutable,
append-only `attune.capability_admissions` table and turns the previously
dormant `attune.approvals` (migration 0001) into a real privilege boundary:
direct `UPDATE` is revoked from every runtime role, and a new one-use,
actor-bound SECURITY DEFINER function (`attune.claim_capability_approval`,
owned by a new memberless role) is the only decide/consume path. The
approval surface is web-conversation-only, using a deterministic grammar
mirroring the memory command grammar exactly; admission, approval, and
dispatch stay three separate steps, and dispatch reuses the existing,
unmodified dispatch producer and broker client rather than a new one. Gate-
off (and every non-web surface) behavior is pinned as byte-identical to
pre-stage-3 mutation refusal. Full detail, including precisely which
section-8.1 execution-checklist items this slice does and does not satisfy,
is in [`capability-gateway.md`](capability-gateway.md); the dated design
record is in [`decisions.md`](decisions.md). No worker deployment sets the
gate on, the fixed R0 policy grants no tenant R2 authority, and no OAuth
flow requests the scope this capability requires — no production tenant can
exercise it. Rate/cost/concurrency budgets, live Gmail thread source-
freshness re-verification before dispatch, and content-free audit of the
admission/approval-decision steps themselves (distinct from the job's own
claim/execute audit, which is unchanged) are genuine remaining gates before
this capability's activation gate can pass.

Stage 4 of the same effort — the hosted proactive brief job
(`docs/future-state.md` Phase 5 item 4; `docs/gap-analysis.md` G12), closing
out Phase 5 — is implemented and tested behind a default-off gate,
`ATTUNE_ENABLE_HOSTED_BRIEF`, and not deployed. The worker executor
(`channel.brief.deliver`) assembles a proactive "what matters now" spine by
importing `brief.build_spine` directly (the exact pure ranking/rendering
function local triage and briefs already use, renamed from a private helper
with no logic change), fed by bounded Gmail/Calendar reads through the
existing secret-broker routes and stage 1's tenant-scoped
`PostgresImportanceProfile`/`PostgresAttentionStore` (the attention store is
empty in production today — no executor writes to it yet — the seam is
wired regardless, matching stage 1's own documented posture). A new
migration (0044) adds `attune.hosted_brief_deliveries`, keyed
`(tenant_id, job_id, destination_id)` so one job can fan out to every ACTIVE
destination whose stored preference includes briefs, and Google Chat/Slack
delivery claim/complete function pairs mirroring the existing conversation-
reply delivery functions exactly, except sourcing rendered brief text from
this new table (never a live worker parameter) and matching
`brief_channels` rather than `interaction_channels`. An owner-facing
control-plane route, `POST /v1/brief/run`, requires the same ordinary
session/CSRF bar as `POST /v1/conversation/messages` (not the destructive-
ceremony recency gate) and is idempotent per tenant per principal per UTC
hour by construction (the dispatch idempotency key folds in the current
hour) — recurring scheduling without an owner click remains future operator
work, mirroring the retention job's own separate-scheduler-identity
pattern. The draft-and-approve capability's approve/reject decisions now
also record an importance signal (keyed on the hashed thread reference,
since no Gmail read in that flow ever resolves a real sender) and a raw
action-signal hosted-memory write when the memory gate is on, closing the
signal-capture loop stage 3 left open; a pre-existing gap in
`build_turn_provenance` (a draft-capability provenance key it should have
allowed since stage 3, never caught because no stage-3 test exercised the
real repository) was fixed alongside it. Hosted nudges and hygiene-action
proposals (the local product's Phase 3 breadth) remain explicitly out of
scope for this phase — Phase 5's brief/nudge item closes with briefs only.
The dated design record, including what was and wasn't reused from
`brief.py`, is in [`decisions.md`](decisions.md); the delivery flow and its
gates are also described in [`hosted-channels.md`](hosted-channels.md).

Phase 6's hosted onboarding item (`docs/future-state.md` Phase 6;
`docs/gap-analysis.md` G19's "no production signup" half) has its production
signup half implemented and tested behind a new default-off gate,
`ATTUNE_HOSTED_SIGNUP_ENABLED`, and not deployed. A verified Identity
Platform subject with zero Attune membership can call a new,
authenticated-but-sessionless `POST /v1/signup` to create its own tenant
(or learn it already has one), reusing the exact login token-verification
code path rather than a parallel one. Migration 0045 adds
`attune.provision_hosted_signup_tenant` -- a new function, deliberately not
a grant on the operator-only `provision_initial_identity`, since that
function's caller-supplied slug parameter would hand a general web caller a
slug oracle; the new function takes no slug at all and is owned by the
same existing memberless executor role, so the migration adds no new role,
table grant, or schema privilege. Signup never mints a session itself --
the client performs the ordinary sign-in flow afterward -- and inviting a
second member into an existing tenant remains out of scope. The full
design, including the abuse-throttle posture and what remains operator
work (the Cloud Armor edge rule, a live probe, abuse monitoring), is in
[`hosted-signup.md`](hosted-signup.md); the dated decision record is in
[`decisions.md`](decisions.md).

## Later

- richer calendar negotiation and follow-up workflows
- temporal/entity memory evaluation and optional Graphiti migration path
- additional channel adapters behind the same routing interface
- voice as a separate front door, without coupling it to a model provider
