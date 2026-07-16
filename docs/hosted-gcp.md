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
   response-minimized Gmail profile operation is deployed. Its deterministic
   worker executor is deployed dormant and disabled by default in both worker
   and dispatch registries, and has no authorized-identity provider evidence.
   Its no-NAT, exact-host private Google API boundary is declarative, and the
   credential-free egress probe passed in development on 2026-07-14. It must be
   repeated after material network or image changes. A dedicated test identity,
   a verified paging channel, full end-to-end evidence,
   write reconciliation, and broader operational alerting remain. The durable
   per-tenant/capability use limit and a content-free use-anomaly alert are
   implemented in development. The private OAuth exchange, function-only
   transaction lease/finalize database boundary, callback-only invoker grant,
   and fixed broker exchange operation are deployed dormant in development.
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
   Production signup, invitation/tenant-selection, connector revocation, and
   account-management ceremonies remain. Follow the separate [hosted sign-in
   operator ceremony](identity-platform.md); it deliberately uses a different
   OAuth client from Workspace connector consent.
5. **Ingress and workers:** provider verification, replay resistance,
   reconciliation, deterministic capabilities, and kill switches.
6. **Operations:** load balancer/WAF, alerts, SLOs, backups/restores, export,
   deletion, incident response, support controls, and supply-chain enforcement.
7. **Assurance:** tenant-isolation suite, red team, independent penetration
   test, Google OAuth verification/CASA evidence, and launch-gate review.

Production is blocked until every launch gate in `security-architecture.md` is
evidenced. Successfully applying Terraform is not successful onboarding.

## Operator workflow

The operated platform is provisioned by a restricted platform identity from
reviewed infrastructure changes. End users never run Terraform or receive GCP
roles. Their eventual journey is sign in, connect Google, optionally connect
Slack or enable Google Chat, select destinations and policy, run bounded live
tests, and activate Attune. Hosted onboarding reuses the versioned setup-state
concept, but stores only server-side, tenant-bound progress and opaque resource
references—not `.env` files or credentials.

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
