# Operated SaaS on Google Cloud

GCP is Attune's first operated SaaS platform. This is a platform decision, not
a change to the portable self-hosted product: `attune init --target local` and
polling deployments continue to work without hosted Attune.

The normative requirements remain in
[`security-architecture.md`](security-architecture.md). This document maps them
to the first GCP implementation.

## Trust boundaries and services

| Boundary | GCP implementation | Holds customer credentials? | Public? |
|---|---|---:|---:|
| Web control plane | Cloud Run behind external HTTPS load balancing and Cloud Armor | No | Yes |
| Provider/channel ingress | Dedicated Cloud Run service with verified Slack, Chat, Calendar, and Pub/Sub handlers | Signing material only where verification requires it | Yes |
| Durable dispatch | Cloud Tasks with a dedicated OIDC dispatch identity | No | No |
| Dispatch broker | Private Cloud Run service and the only Cloud Tasks enqueuer | No | No (internal ingress and IAM) |
| OAuth exchange | Private Cloud Run service; function-only transaction lease and fixed broker call | Transient authorization code only | No (internal ingress and callback IAM) |
| Tenant worker | Private Cloud Run service, one authenticated job envelope per request | No | No (internal ingress and task-delivery IAM) |
| Secret broker | Private Cloud Run service with the only connector-vault KMS identity | Yes | No (internal ingress and IAM) |
| Relational/vector data | Private-IP Cloud SQL PostgreSQL with IAM authentication, RLS, and `vector` | No | No |
| Audit writer | Private intent-only service writing canonical events to PostgreSQL and retained Cloud Storage | No | Implemented in development |
| Retention scheduler | Cloud Scheduler OAuth call to one Cloud Run job; distinct non-database identity | No | No |
| Protocol-retention executor | Bounded Cloud Run job with function-only database authority | No | No |
| Images | Artifact Registry with provenance and vulnerability policy gates | No | No |

Every service has a distinct user-managed service account. Google recommends
per-service identities and Google-signed OIDC tokens for Cloud Run
service-to-service calls. Cloud Tasks likewise sends OIDC tokens to authenticated
Cloud Run handlers. Request headers that merely resemble Cloud Tasks metadata
are not identity.

## Data model

The hosted service does not mount or share `.env`, SQLite, JSON, JSONL, or a
local Qdrant volume. PostgreSQL owns accounts, installations, connectors,
policies, jobs, approvals, audit metadata, and vector rows. Every customer row
contains an immutable tenant identifier; RLS derives access from a transaction-
local server setting established from a verified internal job or session.
Application queries must not accept an arbitrary tenant id as authority.

The first hosted vector implementation is PostgreSQL `vector`, not shared
Qdrant. This reduces the number of privileged data systems and lets relational
and vector access use the same transaction, RLS, backup, export, and deletion
boundary. The existing memory interface remains the application abstraction.

### Implemented development schema

`deploy/gcp/data` now supplies checksum-pinned migrations and a private Cloud
Run migration job. The schema covers every durable tenant object class in the
security architecture, forces PostgreSQL RLS, uses composite tenant foreign
keys, keeps vectors in the same tenant boundary, reconciles least-privilege
runtime roles, and provides a hash-chained append-only audit path. The job
connects through private networking with automatic IAM database authentication
and then verifies the live controls.

RLS consumes a transaction-local tenant selected by deterministic trusted code.
It does not authenticate that selection: a shared database role with a fully
compromised session is not contained merely because a GUC and RLS exist.
Purpose-bound signed jobs, service authorization, secret-broker policy,
revocation, and—where warranted—separate tenant cells remain required layers.
No customer data is authorized by the existence of the schema.

Hosted repositories now require a typed `TenantContext` for every durable
object class, including provider events, jobs and retries, checkpoints,
conversations, approvals, memory and vectors, autonomy, usage, exports,
deletion, and audit. Idempotency collisions are checked, claims and sequence
allocation are atomic, vector predicates include both tenant and principal,
deletion updates relational and vector records together, and approval
consumption binds actor, proposed action, source and policy versions,
connector, destination, and expiry. Tenant authority is never accepted from a
model response or provider payload.

## Credential flow

The transaction and callback contract is specified in
[`oauth-transaction.md`](oauth-transaction.md).

1. The authenticated control plane creates an OAuth transaction bound to the
   browser session, intended tenant, PKCE verifier, exact redirect URI, state,
   and expiry. Pending connector, install intent, and transaction are created
   atomically; the browser chooses none of their authority.
2. The public callback scrubber removes the credential-bearing browser URL and
   hands only the bounded code, state, and callback binding to a private OAuth
   exchange service. That service leases canonical transaction authority using
   both stored hashes and has no direct transaction-table access.
3. The exchange sends the code plus canonical PKCE, nonce, redirect, scope,
   principal, and connector bindings to one fixed secret-broker operation. The
   broker envelope-encrypts the credential with the connector-vault KMS
   key, stores tenant-bound versioned ciphertext in PostgreSQL, and returns an
   opaque connector reference. It never returns the refresh token to the
   control plane or worker.
4. A worker presents a signed internal job and exact capability to the broker.
   Policy is rechecked; the broker either performs the provider operation or
   issues a narrowly bounded, short-lived access path.
5. Access, refusal, rotation, replacement, and revocation produce content-free
   audit events.

Secret Manager holds static platform credentials such as OAuth client and Slack
signing material; it is not a per-customer token database. The foundation
creates empty platform-secret containers only. Tenant credentials use the
connector vault described above. No secret value may enter Terraform state,
Cloud Run environment variables, plans, build logs, or support bundles.

## Ingress flow

Public handlers authenticate the provider over the raw request, enforce size
and timestamp limits, normalize only identifiers needed for reconciliation,
deduplicate, enqueue, and return promptly. Gmail publishes to the dedicated
topic; its eventual push subscription must use a service account and an exact
OIDC audience. Calendar and channel notifications are signals to fetch current
provider state, never executable instructions.

Internal Cloud Tasks requests use a minimal versioned identifier envelope.
The worker verifies the exact HTTPS audience, Google issuer, dispatch service
account, token lifetime, canonical identifiers, allowlisted purpose, and exact
body schema before loading canonical state from PostgreSQL. Provider content
and executable arguments never travel as task authority, and duplicate
delivery is contained by the atomic job claim.

OIDC authenticates Cloud Tasks delivery; it does not make arbitrary body fields
an Attune-signed authorization statement. The worker dispatch core consequently
rebinds the exact purpose and capability while atomically claiming canonical
database state, requires a content-free audit event before execution, and sends
ambiguous executor or audit outcomes to reconciliation rather than blind retry.
The development worker is deployed only with the content-free deterministic
`platform.smoke` capability after queue target routing, producer permissions,
and the private audit-writer path were installed. Higher-assurance cells may
also use producer signatures or one-time job capabilities as an additional
boundary.

The approved broker contract is documented in
[`dispatch-broker.md`](dispatch-broker.md). Producers persist a tenant-bound
job and dispatch intent, then invoke the broker with only the opaque intent ID.
The broker verifies the producer identity, leases canonical routing data
through a narrow database function, and creates a deterministically named task.
It is the only workload with queue-enqueue and delivery-identity permissions.

The Google-managed Gmail publisher receives only `roles/pubsub.publisher` on
that topic. If legacy Domain Restricted Sharing blocks the external system
principal, operators must use the documented, audited project-scoped
break-glass procedure in the foundation README and restore the constraint
immediately. Public topic access and permanent policy exceptions are not
acceptable substitutes.

## Deployment order and gates

1. **Foundation:** apply `deploy/gcp/foundation` in development and staging;
   verify private networking, IAM, CMEK recovery, backup restore, queues, and
   audit retention. No customer data is allowed.
2. **Hosted schema and dispatch:** the development schema, RLS, tenant-context
   transaction helper, PostgreSQL vector storage, durable object model, and
   tamper-evident audit path now exist. All durable repositories plus the
   authenticated envelope and fail-closed dispatch core exist. The durable
   dispatch boundary, exclusive broker IAM, tenant-bound audit outbox, and
   private audit-writer service are implemented in development. The strict
   dispatch-broker service, fixed jobs-queue override, and deterministic smoke
   worker are deployed in development. The brokered synthetic round trip has
   live evidence across canonical state, pre-effect audit, Cloud Tasks, worker
   claim/execution, and post-effect audit. Ambiguous worker outcomes now open a
   durable content-free reconciliation record atomically with job state.
   Provider capability executors, ingress queue routing, authenticated
   resolution operations, and adversarial provider evidence remain before this
   gate is complete.
3. **Secret broker and OAuth exchange:** private install/revoke service, serialized encrypted
   lifecycle, exact workload authentication, intent-only audit, and live KMS
   evidence are implemented in development. The first fixed, read-only,
   response-minimized Gmail profile operation is deployed. The content-free
   Calendar primary-read and composite Workspace verifier are deployed behind
   the same broker boundary and were exercised with the dedicated development
   identity on 2026-07-16. Live evidence includes principal-bound dispatch,
   separate Gmail and Calendar one-use intents, provider responses `200` and
   `204`, durable pre/post audit, worker success, and content-free browser
   verification. The standalone Gmail dispatch route was then disabled.
   Its no-NAT, exact-host private Google API boundary is declarative, and the
   credential-free egress probe passed in development on 2026-07-14. It must be
   repeated after material network or image changes. Write reconciliation and
   broader operational alerting remain. The durable
   per-tenant/capability use limit and a content-free use-anomaly alert are
   implemented in development. The private OAuth exchange, function-only
   transaction lease/finalize database boundary, callback-only invoker grant,
   and fixed broker exchange operation are deployed and exercised in
   development.
   The callback activation path and authenticated connector-start boundary are
   implemented and were activated for development evidence on 2026-07-15 with
   a separate broker-only client-secret version, exact redirect registration,
   and callback non-retention evidence. The synchronous consent chain uses one
   warm control-plane, callback, exchange, secret-broker, and audit-writer
   instance while active; dormant environments retain a zero-instance floor.
   Production authorization still requires the remaining adversarial,
   operational, verification, and launch-gate evidence.
4. **Control plane:** OIDC/passkey login and explicit connector identity links.
   Google Identity Platform sign-in, the tenant-bound opaque session store, and
   one exact development tenant/principal mapping were activated and verified
   on 2026-07-15. Email/domain membership inference remains forbidden. The
   signed-in page now exposes a separately gated Google Workspace connection
   journey; while its gate is false, it creates no connector authority.
   The principal-bound, CSRF-protected Google Workspace disconnect ceremony is
   implemented: it revokes the local credential and connector through a
   one-use private-broker intent while preserving Attune membership. It does
   not yet claim upstream Google grant revocation. The complete development
   disconnect/reload/reconnect/reload journey was exercised on 2026-07-15:
   the private broker returned 204 for the fixed revoke operation, both
   mandatory audit writes returned 200, the subsequent OAuth exchange returned
   204, and fresh Gmail/Calendar verification returned 200/204. Production signup,
   invitation/tenant-selection, and broader account-management ceremonies
   remain. Follow the separate [hosted sign-in
   operator ceremony](identity-platform.md); it deliberately uses a different
   OAuth client from Workspace connector consent.
5. **Ingress and workers:** provider verification, replay resistance,
   reconciliation, deterministic capabilities, and kill switches.
6. **Operations:** load balancer/WAF, alerts, SLOs, backups/restores, export,
   deletion, incident response, support controls, and supply-chain enforcement.
   The first expired-protocol retention slice is active in development: an
   independent identity can invoke only the bounded retention job on a daily
   schedule, while the executor alone has its fixed database function. It was
   deployed paused-first and activated after the exact OAuth invocation,
   aggregate output, paging, database verifier, IAM isolation, and Terraform
   convergence were evidenced. This does not implement customer conversation,
   memory, export, erasure, or backup-suppression lifecycle controls.
7. **Assurance:** tenant-isolation suite, red team, independent penetration
   test, Google OAuth verification/CASA evidence, and launch-gate review.

Production is blocked until every launch gate in `security-architecture.md` is
evidenced. Successfully applying Terraform is not successful onboarding.

The first capability-gateway admission slice now exists but is deliberately
not deployed. It validates the exact versioned model proposal and resolves
active policy/grant, risk ceiling, connector ownership, and provider scopes in
one forced-RLS tenant transaction. It creates no task and performs no provider
operation. See [`capability-gateway.md`](capability-gateway.md) for the
remaining per-execution gates required before integration with the exclusive
dispatch producer.

The next prerequisite is implemented behind a separate default-off gate: the
fixed private-alpha R0 policy ceremony. It requires web authentication within
ten minutes, same-origin CSRF proof, content-free pre-effect audit, and the one
function-owned database mutation described in
[`hosted-policy.md`](hosted-policy.md). Migration
`0019_hosted_read_only_policy.sql` must pass boundary verification before the
edge paths are exposed. Enabling the UI still does not connect the gateway to a
model planner, dispatch producer, worker, or provider effect.

Development rollout evidence was collected on 2026-07-16 UTC from commit
`5ba3668`. Migration 0019 applied exactly once and the live verifier again
reported 28 tenant tables forced through RLS. The new control plane was first
deployed with hosted policy disabled, then the reviewed edge gate was enabled.
After Cloud Armor convergence, an unauthenticated policy read reached
application authorization and returned 401; the signed-in owner saw the exact
R0 automatic and excluded actions. No confirmation was submitted and no policy
or autonomy grant was created during rollout. Both Terraform roots were empty
after deployment.

The owner then completed the separate confirmation ceremony on 2026-07-16
UTC. The application refused the stale session with 409, accepted a freshly
authenticated confirmation with 200, completed both mandatory private audit
writes, and returned only after rereading `validated` policy state. Deployment
automation did not submit the confirmation.

The next default-off onboarding slice is the effect-free channel preference
ceremony in [`hosted-channels.md`](hosted-channels.md). Migration 0020 and edge
priority `886` permit a recently authenticated owner to record independent
Slack/Google Chat interaction and brief choices. The result is only
`authorized`; provider app installation, exact destination binding, verified
ingress and test delivery remain mandatory before `validated`.

Development rollout evidence was collected on 2026-07-16 UTC from commit
`1585ded`. Migration 0020 applied exactly once and the live verifier reported
29 tenant tables forced through RLS. The reviewed control-plane image was
deployed first with the feature gate false, then a second plan enabled only the
gate and exact edge path. The activation changed two resources in place and
created or destroyed none. After Cloud Armor convergence, unauthenticated
access reached application authorization and returned 401; both Terraform
roots were empty after deployment. No channel preference, installation,
destination, ingress, or message was created during rollout.

The owner then completed the separate preference ceremony on 2026-07-16 UTC.
The application rejected stale-session attempts with 409, accepted a freshly
authenticated PUT with 200, and completed both mandatory private audit writes.
Canonical readback showed Google Chat and Slack selected for interaction and
briefs with the channel step `authorized`. Provider installation and exact
owner-only destination verification remain required.

The next channel-installation state slice was deployed dormant-first on
2026-07-16 UTC from commit `27cda78`. Migration 0021 applied exactly once in
execution `attune-development-database-migrate-rlc6q`, and the live verifier
reported 31 tenant tables forced through RLS. Control-plane digest
`sha256:7a084cd8776ce1b2130bf5d55287ee19f50ac8491e5ba2c23144699ae0176089`
was deployed with its setup gate explicitly false. Health returned 200, Cloud
Armor continued to deny the installation-status path with 403, and both
Terraform roots converged empty. No link, destination, provider credential,
ingress, or message was created. The private channel broker and verified
provider ingress remain prerequisites to enabling edge priority `887`.

The following implementation slice separates Google Chat ingress from channel
authority. A dedicated ingress identity verifies Google at the exact public
audience and can invoke only the internal channel broker. A distinct broker
identity owns the broker-only HMAC secret and three fixed database functions,
but has no direct table privileges. The broker uses a short claim and durable
pre-effect audit before creating the canonical installation and owner-DM
binding. Both the private broker and public route default off; deployment of an
unrouted ingress backend is separate from provider-route activation.

Development exercised that separation on 2026-07-16 UTC. Migration 0022 used
digest
`sha256:386ceb843a33de4594c1b438a941bfa8823d500ecf50ef6ceb5079fd9ca2f7aa`
and execution `attune-development-database-migrate-tbd9h`. The private broker
is Ready at digest
`sha256:b5df7b42ea722ae621671fbc6cd05a66a2af29034aa09ec7e2c89daaec2b63ba`
with only the ingress identity as invoker. The Google Chat ingress is Ready at
digest
`sha256:abd3ff681cf4f576f00bcdc7ed509de7f3e3ddd3e0c85d22ab7acfac2411ad94`,
but its backend remains default-deny and absent from the URL map. The intended
public endpoint returned 403, all activation gates remained false, and runtime
and edge Terraform converged empty. No tenant channel state or provider
message was created.

The next slice adds asynchronous destination verification without granting the
control plane provider authority. Migration 0023 stores the raw Chat space
only as tenant- and destination-bound AEAD ciphertext with a KMS-wrapped DEK.
The control plane may invoke the broker with only its canonical destination
UUID; the broker fixes the Chat endpoint, `chat.bot` scope, message text, and
response validation in code. Its Cloud Run policy therefore has exactly two
invokers with route-specific application checks: ingress for link consumption
and control plane for delivery testing. A pre-route development binding is
reported as `needs_relink` and requires an exact-match owner-DM adoption code.

## Operator workflow

The operated platform is provisioned by a restricted platform identity from
reviewed infrastructure changes. End users never run Terraform or receive GCP
roles. Their eventual journey is sign in, connect Google, optionally connect
Slack or enable Google Chat, select destinations and policy, run bounded live
tests, and activate Attune. Hosted onboarding reuses the versioned setup-state
concept, but stores only server-side, tenant-bound progress and opaque resource
references—not `.env` files or credentials.

The first versioned onboarding slice stores one typed,
owner-principal-bound state per tenant. It has fixed Workspace, channels,
policy, and activation steps using the resumable statuses from SEC-805. A
signed-in user explicitly starts it through a same-origin CSRF-protected route
with an empty body. The database seeds Workspace as validated only from the
canonical active Google connector. The browser cannot submit tenant/principal
IDs, step states, resource references, provider choices, or arbitrary details.
Later channel and activation ceremonies will advance this record through fixed
server-side operations rather than a generic step-update endpoint.

Development activation evidence was collected on 2026-07-16 UTC. Migration
`0018_hosted_onboarding.sql` applied successfully and the migration verifier
reported 28 tenant tables forced through RLS. The edge then converged from its
default deny to application authorization: an unauthenticated onboarding read
received 401, while the signed-in owner received 200. The owner started setup
through the empty-body route (201), reloaded the page, and recovered the same
state. Workspace was `validated` from the canonical verified connector;
channels, policy, and activation remained `not_started`. Data and edge
Terraform plans were both empty after deployment. This evidence authorizes the
development slice only; it does not authorize hosted production signup or a
generic browser-controlled state transition.

## GCP implementation references

- [Cloud Run service identities](https://cloud.google.com/run/docs/securing/service-identity)
  and [authenticated service-to-service calls](https://cloud.google.com/run/docs/authenticating/service-to-service)
- [Cloud Tasks HTTP targets with OIDC](https://cloud.google.com/tasks/docs/creating-http-target-tasks)
- [Cloud SQL private IP](https://cloud.google.com/sql/docs/postgres/configure-private-ip),
  [IAM database authentication](https://cloud.google.com/sql/docs/postgres/iam-authentication),
  and [row-level security](https://cloud.google.com/sql/docs/postgres/data-privacy-strategies)
- [Secret Manager CMEK](https://cloud.google.com/secret-manager/docs/cmek)
  and [Cloud Storage Bucket Lock](https://cloud.google.com/storage/docs/bucket-lock)
- [authenticated Pub/Sub push](https://cloud.google.com/pubsub/docs/authenticate-push-subscriptions)
