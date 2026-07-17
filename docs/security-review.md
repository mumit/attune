# Attune security review guide

This document is the entry point for an external security review. It
describes the system as implemented, maps every trust boundary to the code
and SQL that enforce it, and indexes the evidence a reviewer should expect.
The normative requirements live in
[`security-architecture.md`](security-architecture.md); this guide does not
restate them, it shows where each one is realized. Statements here describe
the repository at review time, not aspirations.

## 1. What Attune is

Attune is a one-principal, memory-aware assistant over Gmail, Google
Calendar, Google Chat, and Slack. It observes a principal's workspace,
prepares bounded work (briefs, answers, drafts), and acts only within earned,
human-approved authority. Two deployment forms share this repository:

- **Self-hosted single-principal runtime** (`src/attune/` outside `hosted/`):
  one instance, one principal, local SQLite/JSONL/Qdrant state, direct Google
  OAuth or MCP workspace access, optional Slack (Socket Mode) and Google Chat
  channels, LangGraph draft-and-approve workflows, append-only audit.
- **Hosted multi-tenant service** (`src/attune/hosted/`, `deploy/`): a set of
  small Cloud Run services over one PostgreSQL data core with forced
  row-level security, private brokers for every credential-bearing effect,
  and default-off activation gates for every capability. The hosted service
  is deployed in a development environment only; production activation is
  explicitly gated (see section 8).

The model is never a security principal. Identity, tenant selection,
authorization, capability limits, approvals, and provider effects are
enforced deterministically outside the model
([`design.md`](design.md) principle 6).

## 2. Hosted service inventory

Each service is a separate Cloud Run workload with its own service account.
"Public" means reachable through the edge load balancer; everything else
accepts only Google-signed ID tokens from the exact listed caller identities.

| Service | Entry module | Exposure | Holds | Callers |
|---|---|---|---|---|
| Control plane | `control_plane_app` | Public (session UI/API) | Session/CSRF secrets handling, no provider tokens | Browsers |
| Identity sign-in page | `web/hosted-identity` | Public | Nothing persistent (provider credential kept in browser memory only) | Browsers |
| Google Chat ingress | `google_chat_ingress_app` | Public (exact path, Cloud Armor) | Nothing; verifies Google bearer + audience | Google Chat |
| Slack ingress | `slack_ingress_app` | Public (exact path) | Slack signing secret only | Slack |
| Channel broker | `channel_broker_app` | Private | Channel-reference HMAC key, connector KMS access, Slack client secret, Google Chat app identity | Both ingresses, control plane, worker (distinct identities enforced) |
| Secret broker | `secret_broker_app` | Private | Connector vault decrypt authority, provider read operations | Worker |
| Dispatch broker | `dispatch_broker_app` | Private | Cloud Tasks enqueue authority | Control plane, ingresses |
| Model gateway | `model_gateway_app` | Private | Model API credential, fixed task catalog | Worker |
| Audit writer | `audit-writer` | Private | Append authority to the audit ledger | All producers |
| Worker | `worker_app` | Private (task dispatch only) | No provider or model credentials; stateless between jobs | Cloud Tasks dispatch identity |
| OAuth exchange / callback | `oauth_exchange_app`, `oauth_callback_app` | Callback public, exchange private | Google Workspace OAuth client secret (exchange only) | Google, control plane |
| Export writer / download | `export_writer_app`, `export_download_app` | Private / public one-time links | Export bucket write / bounded read | Worker, browsers with one-time grants |
| Migrator | `migrate` | Job | DDL under migration roles | Operator |
| Republisher | `deploy/republisher` | Public callback | No model, memory, or user OAuth access; publish-only | Google callbacks |

Key properties a reviewer should verify against
[`hosted-gcp.md`](hosted-gcp.md) and the Terraform under `deploy/gcp/`:

- No credential-bearing runtime exposes a public port except through the
  exact-path, default-deny edge policy.
- The channel broker refuses to start if any two of its four caller
  identities coincide (`channel_broker_service.create_app`).
- Workers receive job references, never tenant-chosen URLs, routes, tokens,
  or message bodies.

## 3. Data core and database authority

One PostgreSQL instance holds all tenant state. The controls, in code:

- **Forced RLS everywhere.** Every tenant-bearing relation is created with
  `FORCE ROW LEVEL SECURITY` and a `current_tenant_id()` policy. The
  migration verifier (`migrate.py`) fails if any tenant table is missing
  from the reviewed inventory, and the lifecycle inventory
  (`data_lifecycle.py`) requires an exact data-class and deletion-rule
  classification for every relation. As of migration 0038 there are 36
  tenant tables; `hosted_channel_credentials` is classified
  credential/crypto-erase.
- **Memberless function owners.** Cross-tenant operations (link/install
  consumption, message acceptance, delivery claims, lifecycle disconnect,
  export claims, retention) exist only as `SECURITY DEFINER` functions owned
  by memberless roles (`attune_channel_link_executor`,
  `attune_channel_message_executor`, `attune_channel_lifecycle_executor`,
  export and retention executors). Runtime roles hold `EXECUTE` on exact
  function signatures and no direct table privileges; the verifier pins the
  full expected privilege matrix
  (`migrate.py::FUNCTION_OWNER_TABLE_PRIVILEGES`).
- **One-use claim ceremonies.** Every secret-consuming transition (Google
  Chat link code, Slack OAuth state, delivery tests, conversation delivery,
  export claims) uses a short hash-bound claim with a pre-effect audit
  written through the private audit writer before any mutation; audit
  failure releases the claim without consuming the secret.
- **Idempotency and replay safety.** Message acceptance, job creation,
  delivery completion, and audit intents are keyed by deterministic hashes;
  replaying a provider event returns the original identifiers without new
  effects. This is exercised against real PostgreSQL in
  `tests/test_hosted_db.py`.

## 4. Cryptography inventory

| Material | Purpose | Custody | Notes |
|---|---|---|---|
| Connector KMS key | Wraps per-credential DEKs (AES-256-GCM envelopes) | KMS; unwrap only by secret broker and channel broker | Associated data binds tenant, object UUID, provider purpose, version |
| Google OAuth refresh tokens | Workspace reads | Encrypted vault rows; decrypt only in secret broker | Never returned to control plane or worker |
| Slack bot tokens | Channel delivery | `hosted_channel_credentials` envelopes; decrypt only in channel broker | User tokens refused at exchange; crypto-erase on disconnect |
| Channel routes (Chat space / Slack team+DM) | Reply delivery | `hosted_channel_routes` envelopes; decrypt only in channel broker | Destination UUID bound into AEAD associated data |
| Channel reference HMAC key | Pseudonymous provider identifiers | Secret Manager; channel broker only | Domain-separated per provider and reference kind |
| Slack signing secret | Ingress authentication | Secret Manager; Slack ingress only | v0 HMAC over raw body, 5-minute window |
| Slack client secret | OAuth code exchange | Secret Manager; channel broker only | Never in control plane, ingress, or browser |
| Identity session + CSRF tokens | Browser sessions | `__Host-` cookies, hashed server-side | 8-hour sessions; 10-minute recency for mutations |
| One-use secrets (link codes, OAuth state, export downloads) | Ceremonies | Stored as SHA-256 hashes only | Returned to the owner exactly once |
| Export encryption key (dormant) | Customer export archives | Separate KMS key; writer encrypts, cannot decrypt | See [`customer-export.md`](customer-export.md) |

Raw provider identifiers (space names, team/user/channel IDs, message IDs)
are never stored; only keyed HMAC references appear in the database and
audit metadata.

## 5. Channel trust model (Google Chat and Slack)

Both channels share durable state and differ only in proofs, per
[`hosted-channel-installation.md`](hosted-channel-installation.md):

| Fact | Google Chat proof | Slack proof |
|---|---|---|
| Provider ingress | Google bearer token, exact HTTPS audience | v0 HMAC signature over raw body, 5-minute window |
| Installation | Verified event for the platform-owned Chat app | One-use OAuth state + verified `oauth.v2.access` (fixed app ID, `bot` type, exact scopes) |
| Owner actor | Sender of the one-use `/link CODE` DM | Installing user from the bound OAuth flow |
| Destination | Exact `DIRECT_MESSAGE` space from the signed event | Exact one-user IM from `conversations.open` |
| Browser binding | n/a (code typed in provider DM) | Session cookie + tenant/principal recheck inside `consume_slack_install` |
| Credential | Platform Chat app service identity (no tenant row) | Encrypted bot token per destination |

Shared invariants: owner-DMs only; a destination becomes `active` only after
an explicit, audited fixed-content delivery test; conversation acceptance
requires the full stored fact set (active destination, matching HMAC
references, active installation/tenant/principal, interaction preference,
active Google connector, active policy, live route and credential);
disconnect is a recent-authenticated confirmed ceremony that crypto-erases
routes and credentials and fails ingress closed; reinstalling requires fresh
proof and a new delivery test.

The conversation itself is a bounded, read-only executor
(`google_chat_conversation_executor.py`, parameterized for Slack in
`slack_conversation_executor.py`): fixed model tasks through the private
gateway, deterministic routing with a mutation-refusal guard, brokered
bounded Gmail/Calendar reads under per-attempt credential intents,
authoritative server time injected outside untrusted content, and reply
delivery only by canonical destination and job references.

## 6. Identity, sessions, and onboarding

- Sign-in uses Google Identity Platform for identity only; Workspace consent
  is a separate OAuth client and ceremony
  ([`identity-platform.md`](identity-platform.md),
  [`user-journey.md`](user-journey.md)).
- Membership is never inferred from email or domain; an operator binds the
  identity subject to a tenant during the private alpha.
- All state-changing browser routes require same-origin + `Sec-Fetch-Site`,
  double-submit CSRF, and a session authenticated within ten minutes. The
  one exception—the Slack OAuth callback, which is inherently
  cross-site—substitutes the one-use state plus an in-database
  session-tenant-principal binding recheck (SEC-700A).
- Onboarding is a resumable, server-owned state machine
  (workspace → channels → policy → activation); every step transition is a
  fixed function with mandatory content-free audit
  ([`hosted-policy.md`](hosted-policy.md),
  [`hosted-channels.md`](hosted-channels.md)).

## 7. Audit, retention, and export

- Every effectful ceremony writes a pre-effect `allowed` intent through the
  private audit writer before mutation and an `observed`/`failed` outcome
  after; producer identity is enforced by database trigger against the
  session role ([`audit-writer.md`](audit-writer.md)).
- Audit metadata is content-free: fixed action names, schema versions, and
  hashed references only.
- Expired-protocol retention runs as a bounded, audited executor under a
  dedicated scheduler identity ([`data-lifecycle.md`](data-lifecycle.md)).
- Customer export is a claim-bound, encrypted, expiring pipeline with
  scope-limited projections and secret-negative structural validation
  ([`customer-export.md`](customer-export.md)).

## 8. Assurance posture: implemented vs deployed vs production

Reviewers must distinguish three states, tracked in
[`roadmap.md`](roadmap.md):

- **Implemented and tested, not deployed:** the entire Slack channel slice
  (installation, ingress, conversation, delivery, lifecycle; migration
  0038); the capability-gateway admission core.
- **Deployed in development with live evidence:** identity, Workspace
  connect/verify/disconnect, onboarding and R0 policy ceremonies, the full
  Google Chat journey (link, delivery test, replay-safe conversation,
  disconnect/relink), protocol retention, customer export end-to-end. The
  rollout notes in the `hosted-*` documents record immutable image digests,
  migration executions, negative probes, and empty Terraform plans for each
  activation.
- **Not yet built or gated for later:** production signup/tenant creation,
  capability-gateway dispatch integration and execution budgets,
  customer-visible audit UI, deletion/repair flows, adversarial isolation
  suites, independent penetration testing, and the alpha/beta launch gates
  (roadmap steps 6–10). Development activations are explicitly not
  production launch evidence.

## 9. How to verify

Offline (no network, no credentials; fakes injected everywhere):

```bash
pip install -e ".[dev]"
pytest -q
```

Database isolation and function-authority suite against real PostgreSQL
(pgvector image; applies all migrations, verifies forced RLS on every tenant
table, the exact privilege matrix, one-use ceremonies, replay rejection, and
cross-tenant refusal—including the full Slack journey):

```bash
docker run -d --rm --name attune-pg -e POSTGRES_PASSWORD=test \
  -p 55433:5432 pgvector/pgvector:pg16
ATTUNE_TEST_DATABASE_URL=postgresql://postgres:test@localhost:55433/postgres \
  pytest -q tests/test_hosted_db.py
```

Documentation consistency (environment-variable inventory, contract wording)
is itself tested in `tests/test_docs.py`.

## 10. Review artifact index

| Topic | Document |
|---|---|
| Normative security requirements and launch gates | [`security-architecture.md`](security-architecture.md) |
| Product design and principles | [`design.md`](design.md) |
| Durable design decisions | [`decisions.md`](decisions.md) |
| Hosted GCP mapping and Terraform | [`hosted-gcp.md`](hosted-gcp.md), `deploy/gcp/` |
| Identity and sessions | [`identity-platform.md`](identity-platform.md) |
| Workspace OAuth transactions | [`oauth-transaction.md`](oauth-transaction.md) |
| Secret broker and vault | [`secret-broker.md`](secret-broker.md) |
| Dispatch broker and worker | [`dispatch-broker.md`](dispatch-broker.md) |
| Channel preferences | [`hosted-channels.md`](hosted-channels.md) |
| Channel installation, Slack + Google Chat proofs, rollout evidence | [`hosted-channel-installation.md`](hosted-channel-installation.md) |
| Hosted conversation route | [`hosted-conversation.md`](hosted-conversation.md) |
| Capability gateway (admission core) | [`capability-gateway.md`](capability-gateway.md) |
| Audit writer | [`audit-writer.md`](audit-writer.md) |
| Data lifecycle and retention | [`data-lifecycle.md`](data-lifecycle.md) |
| Customer export | [`customer-export.md`](customer-export.md) |
| Reconciliation | [`reconciliation.md`](reconciliation.md) |
| Status and sequencing | [`roadmap.md`](roadmap.md) |
