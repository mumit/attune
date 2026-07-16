# Security architecture

This document is the normative security design for Attune. It defines the
properties that must remain true as Attune evolves from a single-principal,
self-hosted assistant into a hosted service. Feature designs, implementation
plans, tests, deployment manifests, and security reviews should cite the
requirement identifiers in this document.

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** express requirement
strength. A control described as a hosted requirement is not a claim that the
current self-hosted runtime already implements it.

## 1. Scope and assurance posture

Attune handles high-value data and credentials: Gmail and Calendar content,
Google OAuth refresh tokens, Slack and Google Chat installations, memories,
approval state, model requests, and audit history. The security design assumes:

- a model can be manipulated or behave incorrectly;
- every email, event, chat message, MCP result, and memory can contain hostile
  instructions;
- an authenticated tenant can deliberately probe tenant boundaries;
- channel callbacks can be forged, modified, delayed, duplicated, or replayed;
- a dependency, worker, support account, or cloud identity can be compromised;
- a bearer token can leak despite preventive controls; and
- infrastructure and provider failures will occur during security-sensitive
  workflows.

Security does not depend on a system prompt, model refusal, hidden chain of
thought, or prompt-injection detector. Those mechanisms can improve quality and
telemetry, but they are not authorization controls.

The assurance program SHOULD map controls and evidence to:

- [OWASP ASVS 5.0](https://owasp.org/www-project-application-security-verification-standard/)
  for web and API verification;
- the [OWASP Top 10 for LLM and Generative AI applications](https://genai.owasp.org/llm-top-10/);
- the [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)
  and [Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf);
- the [NIST Secure Software Development Framework](https://csrc.nist.gov/pubs/sp/800/218/final);
- [OAuth 2.0 Security Best Current Practice](https://www.ietf.org/rfc/rfc9700.html);
  and
- Google's restricted-scope verification and annual
  [CASA security-assessment requirements](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
  when hosted Attune accesses restricted Gmail data.

Compliance reports do not replace adversarial product testing.

## 2. Current and target security boundaries

### 2.1 Current self-hosted runtime

The current runtime represents one principal. Its credential files, `.env`,
SQLite databases, JSON state, JSONL audit log, and Qdrant collections belong to
that principal and are protected primarily by host and filesystem isolation.
The current design already provides useful structural controls:

- external Workspace content is treated as untrusted;
- natural-language interaction selects from bounded read capabilities;
- free-form mutations are refused;
- connector sending is disabled by default;
- Slack actors are denied unless allowlisted;
- approvals use durable workflows and authorized actors;
- OAuth, MCP, channel, Chat-app, and model credentials have separate roles; and
- the public republisher does not hold Workspace, model, memory, or workflow
  credentials.

Local files and a mutable JSONL log are not sufficient hosted-service controls.
A hosted deployment MUST use the target controls below rather than treating a
shared filesystem or process boundary as tenant isolation.

### 2.2 Hosted target

Hosted Attune separates the public control plane and event ingress from queued,
tenant-scoped execution:

```text
user / administrator ─── authenticated web control plane ─┐
Slack / Google Chat ──── verified event ingress ──────────┼─> durable queue
Google provider events ─ verified event ingress ──────────┘        │
                                                                    v
                                                        policy enforcement
                                                                    │
                                                                    v
                                                       stateless worker cell
                                                         │       │       │
                                                   secret broker  model  tenant data
                                                         │
                                                 provider APIs
```

The model is outside the trusted computing base for identity, authorization,
tenant selection, secret access, and side-effect enforcement.

## 3. Non-negotiable invariants

These requirements apply to every deployment mode unless explicitly marked
hosted-only.

- **SEC-001 — Model non-authority.** A model MUST NOT authenticate an actor,
  select or derive a tenant, grant a capability, change policy, authorize an
  action, retrieve a credential, or directly invoke a provider mutation.
- **SEC-002 — Deterministic mediation.** Every provider read or write MUST pass
  trusted identity, tenant, capability, argument, rate, and policy validation
  implemented outside the model.
- **SEC-003 — Deny by default.** Missing identity, tenant, route, capability,
  scope, actor, destination, or policy configuration MUST refuse the operation.
- **SEC-004 — Untrusted content.** Provider content, channel content, MCP
  results, memories, conversation history, model output, and tool output MUST
  remain untrusted across component boundaries.
- **SEC-005 — Tenant derivation.** Hosted tenant identity MUST come from an
  authenticated server-side session, verified installation, or signed internal
  job. It MUST NOT be trusted merely because it appears in a URL, request body,
  model argument, cache key, or unsigned queue message.
- **SEC-006 — Secret exclusion.** Credentials and encryption material MUST NOT
  enter prompts, model output, ordinary logs, analytics, client responses,
  support bundles, or audit event bodies.
- **SEC-007 — Exact approval.** Approval MUST bind one authenticated actor to
  one tenant, one canonical action, one connector, one destination, one source
  version, and one expiry. Approval MUST be single-use and replay-safe.
- **SEC-008 — No silent authority growth.** Neither model behavior, successful
  history, memory, provider content, nor deployment configuration may expand
  the maximum permitted risk tier. Only an explicit, authenticated policy
  workflow can change an autonomy grant.
- **SEC-009 — Auditable effects.** Security-relevant reads, secret access,
  policy changes, approvals, refusals, and provider effects MUST produce an
  attributable audit event without recording secret or unnecessary content.
- **SEC-010 — Revocability.** The system MUST be able to disable all writes, a
  capability, a tenant, a connector, a workload, or a provider without a full
  redeployment.
- **SEC-011 — No shared mutable local state in hosted workers.** Hosted workers
  MUST be stateless between jobs; durable state MUST use tenant-aware services.
- **SEC-012 — Security before availability.** Provider timeouts, duplicate
  events, partial failures, stale source state, and audit failures MUST NOT turn
  a denied or approval-required action into an allowed action.

## 4. Assets and data classification

| Class | Examples | Minimum handling |
|---|---|---|
| C0 Public | product documentation, public keys | integrity controls |
| C1 Internal | deployment metadata, non-sensitive telemetry | authenticated access, encryption in transit |
| C2 Customer confidential | subjects, snippets, calendar metadata, summaries, preferences | tenant isolation, encryption at rest/in transit, retention limits |
| C3 Restricted | message bodies, attachments, private meeting details, durable memories | data minimization, narrowly authorized processing, access audit, approved model handling |
| C4 Secrets | OAuth refresh tokens, Slack tokens, signing secrets, MCP/model credentials, encryption keys | dedicated secret system, no model/log exposure, rotation and revocation |

Derived data retains the classification of its source unless a documented
transformation proves otherwise. Embeddings are not anonymized merely because
they are difficult to read directly.

The data inventory MUST record source, purpose, fields, classification,
storage, subprocessors, retention, deletion behavior, region, and control owner.

## 5. Identity and authorization

Attune has distinct identities for the Attune account, organization, principal,
Google authorization, channel installation, channel actor, approver, workload,
support operator, and model/provider. Linking two identities MUST be explicit,
authenticated, and audited.

### 5.1 User sessions and connector linking

- **SEC-100.** Hosted authentication MUST use a well-maintained OIDC provider
  or passkeys. Local password storage SHOULD be avoided unless required.
- **SEC-101.** OAuth authorization-code flows MUST use transaction-specific
  `state`; OIDC MUST validate `nonce`; PKCE with `S256` SHOULD be used even for
  confidential web clients.
- **SEC-102.** Redirect URIs MUST be exact allowlisted HTTPS endpoints in
  production. Tokens and authorization codes MUST NOT enter URLs, referrers, or
  analytics.
- **SEC-103.** Connector callbacks MUST be bound to the authenticated browser
  session and intended principal. Account substitution and login-CSRF tests are
  mandatory.
- **SEC-104.** Session identifiers MUST rotate after authentication and
  privilege changes. Cookies MUST be Secure, HTTP-only, and use an appropriate
  SameSite policy. State-changing web requests require CSRF protection.
- **SEC-105.** Connector replacement, exports, deletion, autonomy changes, and
  high-impact approvals MUST require recent authentication. Production
  administrators MUST use phishing-resistant MFA.
- **SEC-106.** Sessions, connectors, and channel installations MUST be visible
  and independently revocable by the user or authorized administrator.
- **SEC-107.** A credential-bearing OAuth callback MUST use a dedicated public
  service and identity with no tenant, database, secret, queue, or provider
  authority. Load-balancer request logging MUST be disabled for its backend;
  platform and Cloud Armor request logs MUST be excluded by dedicated service
  and backend identities before routing is exposed; and the response MUST
  immediately replace the browser URL with a credential-free location.
  Canonical content-free security events remain mandatory; request-log
  suppression is not audit suppression. Provider redirect registration MUST
  occur only after the route has converged globally and synthetic non-retention
  evidence has passed; configuration order is part of the control. The callback
  MUST require the exact provider authorization-response issuer, reject missing
  or duplicate authority fields, and scrub rather than forward non-authoritative
  response extensions.
- **SEC-108.** OAuth transaction resolution MUST require independent state and
  browser-binding secrets, resolve tenant/principal/connector/redirect/scope
  authority from a short-lived canonical record, and atomically lease it before
  provider exchange. The exchange runtime MUST have no direct transaction-table
  access; a dedicated memberless function owner may cross forced RLS only
  through fixed lease/finalize functions. Finalization MUST re-prove the browser
  binding, be terminal, and remove the live PKCE verifier from the current row.
  The exchange workload MUST NOT receive application log-writer authority.
- **SEC-109.** Hosted sign-in and provider connector consent MUST use separate
  OAuth clients, secrets, redirects, and validation paths. Login identity MUST
  NOT authorize provider data access, and a connector ID token MUST NOT create
  an Attune session.
- **SEC-110.** Hosted sessions MUST use independent high-entropy opaque and CSRF
  values, store only hashes, bind one active tenant and principal, cap absolute
  lifetime, and support immediate revocation. Email and domain claims MUST NOT
  establish tenant membership. Zero or ambiguous membership MUST fail closed.
- **SEC-111.** Browser identity API keys MUST be restricted to the exact
  application and provider-handler origins and only the identity APIs the
  client uses. They are public project identifiers, not authorization.
  Administrative verification MUST select or redact exact non-secret fields
  because a complete Identity Platform configuration can contain password-hash
  configuration. OAuth client secrets MUST remain in provider configuration or
  the connector-secret path, never Terraform state, logs, support output, or
  operator chat.
- **SEC-112.** Hosted browser sign-in MUST keep provider credentials transient,
  exchange only a freshly verified token for an independent application
  session, and remove provider state after exchange. Browser authentication code
  MUST be version- and integrity-locked, built into the reviewed image, served
  from the application origin, and constrained by an exact Content Security
  Policy. A development bootstrap MAY expose sign-in before membership exists
  only when zero membership fails closed and no connector authority is active.
- **SEC-113.** Initial tenant membership MUST use a distinct operator workload
  and database role with no direct table access. A fixed memberless function
  owner MAY create a tenant only atomically with its first principal, MUST
  serialize concurrent ceremonies, MUST make exact replay idempotent, and MUST
  reject conflicting slugs or cross-tenant identity reuse. The raw provider
  subject, expected email, and subject hash MUST NOT enter Terraform, job
  arguments, images, or logs. A one-time secret version MAY carry only the
  locally derived subject hash and MUST be destroyed after successful use.
- **SEC-114.** A connector-consent start MUST derive tenant and principal only
  from a CSRF-authorized application session. Pending connector, install intent,
  and OAuth transaction creation MUST be atomic and serialized per
  tenant/principal/provider. The browser MUST NOT choose connector identifiers,
  provider, redirect, scopes, capability, expiry, or credential intent. An
  active connector MUST NOT be silently replaced. Initial consent SHOULD use
  the minimum read-only provider scopes; every scope escalation requires a
  distinct reviewed capability and user ceremony.
- **SEC-115.** Provider-exchange diagnostics MUST be limited to fixed,
  content-free stage identifiers. Authorization codes, tokens, provider
  responses, account identifiers, scopes, state, nonce, binding material, and
  exception text MUST NOT enter logs. An operated synchronous consent chain
  SHOULD keep its callback, exchange, broker, and mandatory audit dependency
  warm rather than extending credential-bearing request timeouts.
- **SEC-116.** A browser-initiated connector test MUST be bound to a valid
  application session and CSRF token. Tenant, principal, connector, scopes,
  capability, provider operation, and dispatch destination MUST be resolved
  from canonical server-side state. The browser MUST receive only an opaque job
  reference and a bounded public state; provider data, mailbox counters,
  connector identifiers, credentials, and provider errors MUST remain behind
  the worker and secret-broker boundaries. A job identifier is a reference, not
  authority: status reads MUST rebind it to the session principal and active
  connector. Test failure MUST NOT expand authority or silently replace a
  connector, and repeated provider failures MUST page through content-free
  telemetry.
- **SEC-117.** Browser-initiated connector disconnection MUST require a valid
  application session, same-origin request, CSRF proof, and an explicit
  destructive confirmation value. Tenant, principal, provider, connector,
  credential, capability, and intent authority MUST be resolved server-side.
  The control plane MAY send only a one-use opaque revoke intent to the private
  secret broker. Revocation MUST atomically disable the active local credential
  and connector, be safe to retry after an ambiguous response, preserve the
  independent account membership, and emit content-free allowed/observed audit
  events. The product MUST distinguish local Attune disconnection from upstream
  provider-grant revocation and MUST NOT let provider unavailability delay
  immediate local withdrawal.
- **SEC-118.** Hosted policy and autonomy changes MUST require a same-origin,
  CSRF-authorized application session authenticated within the previous ten
  minutes. The browser MUST review a fixed, versioned profile and MUST NOT
  submit identity, policy documents, risk tiers, capabilities, grants, or
  resource references. A memberless function owner MUST apply the exact policy
  and grants atomically under tenant context only after content-free pre-effect
  audit is durable. Direct policy/grant mutation by the ordinary control-plane
  role, missing or ambiguous state, audit outage, stale authentication, and
  external modification MUST fail closed. See the [hosted policy
  ceremony](hosted-policy.md).
- **SEC-119.** Hosted channel preference changes MUST require a same-origin,
  CSRF-authorized session authenticated within ten minutes and mandatory
  content-free pre/post audit. The browser MAY submit only a fixed schema and
  bounded Slack/Google Chat interaction/brief selections; tenant, principal,
  app, installation, token, destination, allowlist, ingress, and provider
  authority MUST remain server-derived. Preference MUST advance onboarding no
  further than `authorized`; only verified installation, destination binding,
  ingress, and test delivery may produce `validated`. The ordinary control
  plane MUST NOT directly mutate preferences, and validated routes MUST NOT be
  silently retargeted. See the [hosted channel ceremony](hosted-channels.md).
- **SEC-120.** Hosted channel installation MUST prove provider ingress,
  platform-app identity, owner actor, owner-only destination, and bounded test
  delivery as separate facts. Google Chat linking MUST consume a one-use
  high-entropy code only from a verified `DIRECT_MESSAGE` event. Slack linking
  MUST use one-use browser-bound OAuth, verify the app/team/bot/installer and
  exact granted scopes, and retain bot credentials only through the encrypted
  private broker. Public ingress MUST NOT receive tenant or provider authority;
  a private broker MUST derive domain-separated HMAC references with a
  broker-only 256-bit key and resolve the tenant. Link consumption MUST use a
  short database claim: a durable pre-effect audit is written before canonical
  installation/destination mutation, audit failure releases the claim, and
  replay cannot consume it. The ordinary control plane MUST NOT mutate installations,
  destination bindings, or validation state. See the
  [hosted channel installation design](hosted-channel-installation.md).

### 5.2 Authorization model

- Authorization MUST be checked at both service and storage boundaries.
- Object identifiers, including UUIDs, are references rather than proof of
  authority.
- Administrative and support roles MUST be separate from tenant execution
  roles and use just-in-time elevation.
- Support access to customer data MUST be disabled by default, time-bounded,
  purpose-bound, approved, and visible in the security audit.
- Service-to-service calls MUST use short-lived workload identity with audience
  restriction rather than static shared API keys where the platform supports it.

## 6. Tenant isolation

Hosted durable objects MUST carry an immutable tenant identifier, including
connector grants, channel installations, events, jobs, retries, conversations,
memories, vectors, approvals, checkpoints, autonomy grants, audits, usage,
exports, and deletion markers.

- **SEC-200.** Relational storage MUST enforce tenant isolation with database
  row-level security or an equivalently strong database boundary in addition
  to application checks.
- **SEC-201.** Vector queries MUST inject an authenticated tenant/principal
  filter inside the storage adapter. A model-supplied filter is insufficient.
- **SEC-202.** Cache keys, object paths, deduplication keys, locks, metrics, and
  rate-limit keys MUST include the verified tenant where applicable.
- **SEC-203.** Internal jobs MUST be authenticated, integrity-protected,
  purpose-bound, deduplicated, and authorized again when consumed.
- **SEC-204.** Ordinary workers MUST NOT perform wildcard cross-tenant queries
  or bulk secret retrieval.
- **SEC-205.** Database migration, export, support, and incident tooling MUST
  have separate identities and explicit bulk-access controls.
- **SEC-206.** Higher-assurance tenant cells MAY use distinct data-plane
  identities, databases, regions, and encryption keys, but must preserve the
  same application authorization checks.
- **SEC-207.** Hosted producers MUST NOT enqueue worker tasks or use the task
  delivery identity directly. A dedicated dispatch boundary MUST authenticate
  the producer, resolve a tenant-bound canonical intent without trusting a
  caller-supplied tenant, restrict purpose and destination, use deterministic
  task identity, and enforce infrastructure-controlled queue routing.
- **SEC-208.** A hosted audit writer MUST NOT trust a caller-supplied tenant or
  free-form event. Tenant-scoped workloads MUST first persist an idempotent
  audit intent under the storage boundary; privileged writers MUST resolve that
  intent server-side and atomically append it. Fixed-purpose infrastructure
  brokers MAY create intents only by resolving canonical state.
- **SEC-209.** A cross-tenant `SECURITY DEFINER` function under forced RLS MUST
  have a dedicated `NOLOGIN BYPASSRLS` owner with no members, no superuser or
  role/database-creation authority, and only its reviewed table/function
  privileges. Runtime and IAM login roles MUST remain `NOBYPASSRLS`, MUST NOT
  own these functions, and MUST NOT receive the owner's direct table access.

Automated isolation tests MUST attempt every operation with another tenant's
IDs, installations, approval nonces, queue records, vector filters, cache
entries, export jobs, and connector references. See the
[OWASP API authorization guidance](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/).

## 7. Secrets and cryptographic controls

- **SEC-300.** Hosted C4 material MUST use a dedicated secret store and MUST
  NOT be stored as ordinary application configuration or plaintext database
  fields.
- **SEC-301.** Tenant credentials MUST be envelope-encrypted with protected key
  encryption keys. Key access and secret access MUST use distinct least-
  privilege workload identities where practical.
- **SEC-302.** A secret broker MUST authorize tenant, workload, connector,
  operation, and destination before releasing a credential or performing an
  operation on its behalf.
- **SEC-303.** Secret decrypt/read events MUST be audited and monitored for
  volume, workload, region, and tenant anomalies.
- **SEC-304.** Secrets MUST be redacted by construction at logging, exception,
  tracing, analytics, and support-export boundaries; regex cleanup alone is not
  sufficient.
- **SEC-305.** Rotation and revocation procedures MUST be automated and tested.
  Production secrets MUST never be copied to development or test environments.
- **SEC-306.** Encryption keys MUST have documented ownership, rotation,
  recovery, regional placement, and destruction procedures.
- **SEC-307.** A credential installation or revocation boundary MUST accept an
  opaque one-time intent rather than caller-authoritative tenant, connector,
  provider, capability, or key fields. Its workload identity and exact audience
  MUST be verified before parsing secret material, and the protected effect
  MUST fail closed unless content-free pre-effect audit is durable.

The hosted connector-vault implementation follows the envelope-encryption and
opaque-intent contract in [`secret-broker.md`](secret-broker.md): a fresh
AES-256-GCM DEK per credential version, tenant/connector/provider/version-bound
associated data, KMS-wrapped DEKs, route-specific control-plane or worker
authentication, content-free intent-based audit, and no caller-authoritative
tenant field. Its first provider-use capabilities are fixed read-only Gmail
profile and Calendar primary-calendar operations: no caller URL or user ID, no
redirects, bounded responses, and no access-token release. Gmail omits the
email address; Calendar returns no provider data. A composite verification job
creates a separate two-minute one-use intent and audit trail for each operation
and succeeds only when both succeed. It remains unavailable to normal hosted
jobs until the test-identity, egress, paging, and end-to-end evidence gates are
satisfied. Its dormant worker executor accepts one canonical connector UUID,
derives stable job-bound idempotency keys, creates a two-minute
use intent, and calls only the typed broker client. Terraform requires the fixed
dispatch broker and a notification channel before registering the route.
Credential-use leases are serialized and limited
per tenant and capability in PostgreSQL, and fixed content-free failure markers
feed a Cloud Monitoring alert.

## 8. Agent and capability containment

### 8.1 Typed capability gateway

The model may produce only a versioned intent and typed arguments. Trusted code
maps that proposal to a registered capability and constructs provider requests.
There is no arbitrary HTTP, shell, SQL, Python, MCP-tool, or provider-method
capability available to the model.

For every execution, the gateway MUST validate:

1. authenticated actor and verified tenant;
2. connector ownership and granted provider scopes;
3. enabled capability and maximum risk tier;
4. typed, normalized, size-bounded arguments;
5. permitted data scope, recipients, destinations, and time range;
6. rate, concurrency, and cost limits;
7. approval and recent-authentication requirements;
8. source freshness and resource version; and
9. idempotency and replay state.

- **SEC-400.** Model output MUST be schema-validated and treated as untrusted.
- **SEC-401.** Provider request arguments MUST be reconstructed from trusted
  types; model-produced raw requests MUST be rejected.
- **SEC-402.** User-configurable URLs MUST NOT be reachable from shared hosted
  workers without URL policy, DNS-rebinding protection, egress enforcement, and
  isolation. Hosted model routes SHOULD use approved endpoints.
- **SEC-403.** Rendered model/provider content MUST be contextually escaped for
  HTML, Markdown, Slack blocks, Google Chat cards, logs, and filenames.
- **SEC-404.** Prompt-injection classifiers MAY raise risk or produce telemetry,
  but MUST NOT be the control that makes an unsafe capability safe.
- **SEC-405.** System prompts MUST contain no credentials or controls whose
  secrecy is required for authorization.

The first deterministic admission slice is implemented as described in the
[hosted capability-gateway contract](capability-gateway.md). It accepts only an
exact, versioned proposal; reconstructs arguments through a trusted schema; and
atomically resolves active tenant, principal, policy/grant, risk ceiling,
connector ownership, and scopes under forced RLS. It is not yet wired to a
model planner or dispatch producer. Rate/cost limits, freshness, idempotency,
content-free audit, and approval/recent-authentication enforcement remain
activation gates, so this slice authorizes no provider effect.

The first fixed R0 profile and recent-authenticated activation boundary are
implemented separately in [`hosted-policy.md`](hosted-policy.md). Policy
activation remains dormant until migration, audit, UI, and live owner evidence
are complete, and gateway-to-dispatch integration remains disabled.

### 8.2 Risk tiers

| Tier | Examples | Maximum default behavior |
|---|---|---|
| R0 | bounded search, summarize, agenda | automatic read |
| R1 | reversible Attune-owned state, private preparation | automatic only when contained and reversible |
| R2 | Gmail draft, label, private tentative hold | explicit approval by default |
| R3 | send mail, external meeting mutation, broad channel post | recent-authenticated approval of exact action |
| R4 | deletion, bulk action, sharing/security change, access grant | dedicated non-model administrative workflow |

- **SEC-410.** Earned autonomy MUST remain inside the maximum product risk tier.
- **SEC-411.** Public hosted beta MUST NOT autonomously send email, mutate an
  external meeting, delete provider data, change sharing/security, or perform
  bulk writes.
- **SEC-412.** Every new write capability requires a threat model, abuse cases,
  negative tests, audit schema, rollback analysis, and explicit security owner.

## 9. Approval and effect integrity

Approval payloads MUST contain only an opaque, random reference to canonical
server-side state. Provider/channel payload fields are not executable action
arguments.

- **SEC-500.** The canonical proposal MUST bind tenant, principal, approver,
  capability, connector, destination, exact action hash, source/resource
  version, policy version, creation time, expiry, and originating surface.
- **SEC-501.** Approval references MUST be high-entropy, single-use, short-lived,
  and atomically consumed. Duplicate callbacks return the recorded result.
- **SEC-502.** Callback identity and installation MUST match the bound approver
  and tenant. Editing creates and approves a new canonical action hash.
- **SEC-503.** Immediately before a write, Attune MUST reauthorize, refetch
  relevant state, check source/version freshness, and evaluate policy again.
- **SEC-504.** Timeout, stale state, changed recipient/destination, audit failure,
  or ambiguous provider result MUST fail closed or enter explicit reconciliation;
  it MUST NOT silently retry a non-idempotent write.
- **SEC-505.** R3 actions require recent web authentication or an equivalently
  strong step-up ceremony; possession of a channel session alone is insufficient.
- **SEC-506.** Ambiguous effect intake MUST atomically bind a canonical job to a
  tenant-bound, content-free reconciliation record with a fixed reason. Workers
  MAY open this record but MUST NOT resolve, delete, or silently requeue it.
  Resolution MUST refetch authoritative provider state through an authenticated,
  audited, provider-specific workflow.

## 10. Memory and retrieval security

- **SEC-600.** Every memory has tenant/principal, provenance, creator, source
  class, confidence, timestamps, and deletion state.
- **SEC-601.** External content cannot automatically become user policy,
  autonomy, credentials, tool definitions, or an unattributed preference.
- **SEC-602.** Sensitive durable memories require explicit teaching or
  confirmation under a documented policy.
- **SEC-603.** Retrieved memory remains untrusted context and cannot override
  capability or authorization policy.
- **SEC-604.** Deletion MUST propagate to relational records, vector records,
  caches, derived summaries, and exports. Backup expiry and restore suppression
  MUST prevent deleted tenants or credentials from being resurrected.
- **SEC-605.** Memory poisoning, cross-tenant retrieval, inference leakage, and
  adversarial embedding tests are required for memory or model changes.

## 11. Data minimization and model handling

- Fetch raw provider content only when needed for a bounded task.
- Prefer transient processing or short-TTL encrypted storage for raw bodies and
  attachments; Attune SHOULD NOT become a second permanent mailbox.
- Do not embed every message by default. Classify and purpose-limit durable
  memory.
- Send the minimum necessary fields to the selected model route.
- Managed model providers MUST have reviewed retention, training, access,
  residency, incident, and deletion terms. Customer data MUST NOT train a
  general model without separate explicit consent and policy approval.
- Channel delivery MUST account for destination visibility. Private Workspace
  data cannot be silently posted to a broader surface.
- User-facing controls MUST support inspection, correction, export,
  disconnection, revocation, retention, and deletion.

Google consent MUST be incremental where feasible. Read access needed for the
core service is explained at connection time; compose or Calendar-write scopes
are requested only when their features are enabled. Google's
[user-data policy](https://developers.google.com/terms/api-services-user-data-policy)
requires minimum permissions and contextual, transparent disclosure.

## 12. Public ingress and channel security

Public ingress holds no Workspace, model, memory, or workflow credential. It
authenticates and normalizes provider events, enforces strict limits, enqueues a
minimal event, and returns promptly.

- **SEC-700.** Slack HTTP events and interactions MUST verify the signature over
  the raw body using the signing secret, compare in constant time, enforce the
  timestamp window, and deduplicate/reject replays. See Slack's
  [request verification guide](https://docs.slack.dev/authentication/verifying-requests-from-slack/).
- **SEC-701.** Google Chat, Google provider callbacks, and Pub/Sub delivery MUST
  use the strongest documented provider verification, including expected
  audience/issuer and channel tokens where applicable.
- **SEC-702.** Ingress MUST apply TLS, body/header limits, content-type checks,
  strict schemas, event-type allowlists, rate limits, deduplication, and safe
  parser configuration before queueing.
- **SEC-703.** Duplicate, delayed, reordered, and malformed events MUST be safe.
  Notification payloads are reconciliation signals rather than commands.
- **SEC-704.** Shared hosted Slack uses verified HTTP Events API ingress; local
  Socket Mode remains a self-hosted transport and is not a tenant-isolation
  mechanism.

## 13. Setup and deployment assistant

The conversational setup experience explains choices and gathers intent. A
deterministic, versioned provisioner performs configuration and infrastructure
changes. The model and user-provided free text are never converted directly
into shell commands, infrastructure definitions, IAM policies, URLs, or secret
values.

- **SEC-800.** Every setup step MUST declare its required inputs, privileges,
  resources, secret outputs, validation, idempotency key, rollback behavior, and
  repair behavior.
- **SEC-801.** Provisioning MUST use reviewed typed APIs or declarative
  infrastructure with a pinned version. Free-form model output MUST NOT enter a
  command interpreter or template expression.
- **SEC-802.** Before an external change, the assistant MUST show a bounded plan
  containing resources, identities, permissions, public exposure, regions,
  estimated ongoing services, and destructive replacements. Material plan
  changes require renewed confirmation.
- **SEC-803.** Cloud access SHOULD use short-lived workload federation or an
  interactive provider session. Attune MUST NOT request or retain a standing
  organization-owner key merely to simplify deployment.
- **SEC-804.** Provisioning credentials MUST be separate from runtime
  credentials. The deployed runtime MUST NOT retain the ability to create IAM
  grants, change its security boundary, or redeploy itself.
- **SEC-805.** Setup state MUST be resumable and integrity-protected. It MUST
  distinguish not-started, authorized, applied, validated, failed, rolled-back,
  and externally modified states instead of assuming that a timed-out step did
  not succeed.
- **SEC-806.** Secret inputs MUST use secret-aware controls and destinations;
  they MUST NOT be echoed in prompts, command arguments visible to process
  listings, terminal transcripts, generated plans, or setup telemetry.
- **SEC-807.** `attune init` and hosted signup MUST validate the real deployed
  capability and send a bounded test through the selected path. A successful
  resource-creation API call alone is not successful setup.
- **SEC-808.** Repair and uninstall MUST operate only on resources recorded as
  owned by that setup, revalidate current ownership, preview destructive
  effects, preserve required audit/deletion markers, and refuse ambiguous
  resources.
- **SEC-809.** Deployment providers, templates, migrations, and setup schema
  changes require the same supply-chain, review, negative-test, and rollback
  controls as runtime code.
- **SEC-810.** Hosted onboarding state MUST be versioned, tenant-bound, and
  stored server-side without credentials, free text, provider identifiers, or
  caller-authoritative resource references. Starting or changing onboarding
  requires a CSRF-authorized application session. The service MUST derive the
  tenant, principal, existing connectors, and completed capabilities from
  canonical state; the browser MAY receive only bounded step names and status
  values. Ambiguous ownership and unsupported schema versions fail closed.

The local setup target may use a narrowly defined subprocess adapter for
Docker or service-manager commands. Arguments MUST be constructed as arrays
from validated types, commands MUST be allowlisted, and secrets MUST be passed
through protected files or standard input rather than interpolated shell text.

## 14. Network and infrastructure controls

- Public, control-plane, worker, secret, data, build, and administrative
  identities MUST be separate and least-privileged.
- Databases, vector stores, queues, and administrative endpoints MUST not be
  publicly reachable.
- Worker egress MUST be deny-by-default and limited to required internal
  services and approved provider/model endpoints.
- Calls to internal Cloud Run HTTPS origins MUST traverse the private VPC using
  all-traffic egress so internal-ingress provenance is preserved. Environments
  without an approved egress gateway or Cloud NAT MUST fail closed for arbitrary
  internet destinations.
- The initial GCP provider boundary uses no Cloud NAT. Private Google Access and
  exact private DNS zones expose only `oauth2.googleapis.com`,
  `www.googleapis.com`, `gmail.googleapis.com`, and
  `secretmanager.googleapis.com` through the `private.googleapis.com` VIP;
  wildcard Google API DNS is prohibited. The additional hosts are restricted
  in code to signing-certificate retrieval and the platform OAuth-client-secret
  read. Because the VIP can serve more Google APIs,
  fixed code paths, TLS hostname verification, disabled redirects and ambient
  proxies, canonical capability checks, and route-specific IAM remain
  independent controls. Each network change requires a credential-free live
  probe before connector authorization.
- Production, staging, development, and security-test environments MUST use
  separate projects/accounts, credentials, OAuth clients, and customer data.
- Infrastructure changes MUST be declarative, reviewed, logged, and applied by
  a restricted deployment identity.
- Workloads SHOULD be immutable, non-root, read-only where practical, resource-
  bounded, and automatically patched/replaced rather than repaired manually.
- Administrative access MUST use short-lived identity, phishing-resistant MFA,
  just-in-time elevation, session recording where appropriate, and audited
  break-glass procedures.
- Backups MUST be encrypted, access-controlled, restoration-tested, and covered
  by deletion/retention rules.

## 15. Audit, detection, and incident response

Hosted audit storage MUST be append-only and tamper-evident. The audit event
records identifiers and decisions, not raw message bodies, access tokens, or
hidden model reasoning.

The infrastructure audit export MUST include only reviewed Cloud Audit log
classes. It MUST NOT retain all application, load-balancer, or request logs;
OAuth callbacks necessarily carry short-lived authorization codes in their
query string. Callback request logs require an explicit non-retention boundary
before the route is exposed, while canonical content-free Attune events remain
in the hash-chained application audit.

The development boundary assigns the exact callback path to a dedicated
credential-free Cloud Run scrubber. Its load-balancer backend logging is off;
the `_Default` sink excludes both that service's platform request logs and its
backend's Cloud Armor/load-balancer request logs by resource identity; and the
retained audit sink contains Cloud Audit classes only. Backend logging disable
does not by itself suppress Cloud Armor `requests` entries. The exclusion is
protected from Terraform destruction. Synthetic values must be absent from both
request-log planes and all project-log search before the deployed exchange is
connected to an enabled public callback.

Log verification MUST NOT place an authorization code, token, state value, or
synthetic marker in a server-side logging query: Data Access audit records the
query filter. Verification uses a narrow timestamp-only query and searches the
returned data locally. Only explicit non-secret synthetic markers may be used
in a controlled callback test; real credentials are never searched for.

The implemented hosted boundary uses the tenant-bound transactional outbox and
private intent-only writer specified in [`audit-writer.md`](audit-writer.md).
The writer accepts no tenant or event fields over HTTP and has no direct table
or free-form append authority.

At minimum record:

- actor, tenant, installation, connector, workload, and correlation ID;
- normalized capability and bounded arguments or their safe digest;
- evidence/source identifiers and resource versions;
- policy, capability, model, and prompt-template versions;
- approval identity, action hash, expiry, and decision;
- secret-access metadata;
- provider request id/result and reconciliation state; and
- refusal, error, policy-change, export, deletion, and emergency-control events.

Alerts MUST cover repeated tenant-boundary failures, abnormal bulk reads,
unusual secret access, approval replay, new workloads/regions, autonomy changes,
webhook verification failures, audit interruption, exports, deletion, and cost
or request spikes.

Emergency controls MUST disable globally or selectively: all writes, a
capability, tenant, connector, provider/model route, workload identity, or job
consumer. These controls and connector revocation MUST be exercised in incident
tabletops and production-safe game days.

## 16. Secure development and supply chain

- Security-sensitive changes require threat-model updates and independent
  review from someone other than the author.
- CI MUST run unit/integration tests, secret scanning, static analysis,
  dependency and license checks, IaC checks, and container/image scanning as
  applicable.
- Dependencies and images MUST be pinned through reviewed lock/digest
  mechanisms, with automated vulnerability triage and patch policy.
- Releases SHOULD generate an SBOM and signed build provenance; deployment
  SHOULD verify artifact identity. See the
  [SLSA build track](https://slsa.dev/spec/v1.2/build-track-basics).
- Build and release identities MUST be separate, least-privileged, and protected
  by branch rules and two-person review.
- Production data and secrets MUST NOT be used in development, CI, model
  evaluation, or support reproduction.
- New models, prompts, connectors, parsers, channels, memory providers, and
  capabilities require regression and adversarial evaluation before promotion.
- The project MUST publish a vulnerability-reporting route, response process,
  researcher safe-harbor policy, and supported-version policy before public
  hosted launch.

## 17. Required adversarial test program

The red-team suite MUST exercise at least:

1. direct and indirect prompt injection in email, calendar, Slack, Chat, MCP,
   attachments, memory, and conversation history;
2. cross-tenant ID substitution across every API and storage adapter;
3. vector-filter, cache-key, queue-job, export, and approval cross-tenant attacks;
4. forged, modified, stale, duplicate, reordered, and replayed callbacks;
5. approval theft, actor substitution, action mutation, and time-of-check/time-
   of-use changes;
6. OAuth login CSRF, account mix-up, redirect manipulation, code replay, and
   connector substitution;
7. SSRF, DNS rebinding, credential forwarding, and unsafe redirects through
   configurable URLs;
8. secret leakage through prompts, responses, errors, logs, traces, metrics,
   audit, analytics, and support bundles;
9. memory poisoning, provenance confusion, deletion failure, and restore
   resurrection;
10. model output injection into every rendered or parsed context;
11. compromised worker, workload identity, support user, and malicious insider;
12. webhook floods, parser bombs, attachment bombs, model loops, and cost
    exhaustion;
13. malicious or compromised dependency, image, build, and deployment artifact;
14. provider timeout, ambiguous write result, audit outage, queue replay, and
    regional failure; and
15. global and tenant-specific kill switch, token revocation, backup restore,
    deletion, and incident recovery.

The expected security result is a deterministic refusal, contained failure, or
authorized exact effect. A model saying that it would refuse is not evidence.

## 18. Security review and launch gates

### 18.1 Every material feature

A pull request or design review for a new data source, model route, memory
behavior, channel, capability, or write path MUST include:

- data-flow and trust-boundary changes;
- assets, actors, abuse cases, and affected SEC requirements;
- required provider scopes and why narrower scopes do not suffice;
- data retention, model/subprocessor, and deletion impact;
- authorization and tenant-isolation tests;
- prompt-injection and malformed-output tests;
- audit, detection, rollback, revocation, and incident behavior; and
- named security owner and residual risks.

### 18.2 Hosted private alpha

Before real external customer data:

- security architecture and data inventory reviewed;
- secret broker, tenant-aware storage, deterministic capability gateway, and
  emergency write-disable implemented;
- no shared SQLite, JSON, JSONL, or local Qdrant state used as hosted tenant
  isolation;
- automated cross-tenant and adversarial side-effect tests passing;
- production access, logging, backup, deletion, and incident procedures tested;
- model-provider and infrastructure subprocessors approved; and
- no unresolved critical or high-severity security findings.

### 18.3 Hosted public beta

In addition to alpha gates:

- applicable Google verification and CASA assessment complete or explicitly
  satisfied for the permitted audience;
- external application and cloud penetration test complete;
- applicable ASVS controls mapped to repeatable evidence;
- zero unauthorized effects or cross-tenant disclosure in the defined
  adversarial regression suite;
- signed artifacts/SBOM, vulnerability disclosure, security contact, and patch
  process operational;
- restore, revocation, global write-disable, compromised-token, malicious-
  insider, and deletion exercises complete; and
- all accepted risks have an owner, rationale, expiry, and compensating control.

### 18.4 Ongoing operation

Security review is continuous. Repeat independent testing after material trust-
boundary changes and at least as often as provider or regulatory obligations
require. Re-run agentic adversarial evaluations for model, prompt, tool,
connector, and policy changes. Track security requirements as product tests,
not a one-time document checklist.

## 19. Decision record

Security exceptions MUST be written as time-bounded decision records containing
the affected requirement, reason, customer exposure, alternatives considered,
compensating controls, owner, approval, expiry, and removal plan. An exception
cannot be created or renewed by the model or setup assistant.
