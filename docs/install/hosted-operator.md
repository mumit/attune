# Install: hosted multi-tenant operator runbook

*This is the canonical, ordered stand-up procedure for Attune's hosted
multi-tenant platform — see [`../modes.md`](../modes.md) for how this compares
to self-hosted, and [`../hosted-gcp.md`](../hosted-gcp.md) for the normative
architecture and trust-boundary reference this runbook operationalizes. This
document narrates *order and commands*; it does not restate architecture,
security contracts, or ceremony internals already specified elsewhere — every
step below points at the doc that owns those details.*

**Read this before running anything:** per [`../roadmap.md`](../roadmap.md)
and [`../security-review.md`](../security-review.md) §8, the hosted platform
is a **development-stage system**. Applying every step in this runbook
produces a working development deployment with live evidence for many
ceremonies — it does not produce a publicly operable product. Production
activation is blocked until every launch gate in
[`../security-architecture.md`](../security-architecture.md) is evidenced.
Successfully applying Terraform is not successful onboarding, and completing
this runbook is not a launch.

## 0. Prerequisites

Before any `terraform apply`:

- **GCP org/project/billing.** A dedicated GCP project with billing enabled
  and organization policies permitting the resources this platform creates
  (private networking, Cloud SQL, Cloud Run, Cloud Armor, KMS, Secret
  Manager, Cloud Tasks, Artifact Registry).
- **Domain + DNS.** A domain you control for the control plane and public
  ingresses (e.g. `dev.attune.example.org` in the development evidence
  throughout this repo's docs), delegated so you can point it at the external
  HTTPS load balancer created by `deploy/gcp/edge`.
- **Two Google OAuth clients**, per [`../identity-platform.md`](../identity-platform.md):
  one Web application client for Identity Platform sign-in (identity scopes
  only), and a second, separate Web application client for the Workspace
  connector consent journey (Gmail/Calendar scopes, redirect
  `https://<domain>/oauth/google/callback`). Never reuse either client ID or
  secret for the other purpose — `identity-platform.md`'s "Development
  operator ceremony" section is the exact console walkthrough for both.
- **Identity Platform enablement.** The foundation Terraform enables
  `identitytoolkit.googleapis.com`, but Terraform deliberately does not
  initialize Identity Platform, manage its generated browser key, or manage
  its Google provider — that is a one-time, separately reviewed operator
  ceremony (`identity-platform.md` steps 1–6) because the provider resource
  needs a client secret that would otherwise persist in Terraform state.
- **Platform Slack app**, if you intend to offer Slack: follow
  [`slack-app.md`](slack-app.md)'s "Hosted platform app" section to register
  the one platform-wide OAuth app (not Socket Mode). This is a prerequisite
  for, not a replacement of, the Slack activation ceremony in step 4 below.
- **Google Chat platform app**, if you intend to offer Chat: a platform-owned
  Chat app is configured directly in the Google Chat API console per
  [`../hosted-channel-installation.md`](../hosted-channel-installation.md)'s
  Google Chat section — there is no separate install doc for it, since (unlike
  Slack) the console mechanics are Chat-API-specific rather than shared with
  self-hosted setup.
- **Container build/registry.** Artifact Registry (created by the foundation
  root) plus a way to build and push the 15 service images under `deploy/`
  (see the image inventory in step 3) with provenance/vulnerability policy
  gates, and pin each by immutable `@sha256:` digest — every Cloud Run
  resource in this codebase references images only by digest, never a mutable
  tag.

## 1. Terraform: foundation → data → runtime → edge

Apply strictly in this order; each root's `README.md` (`deploy/gcp/<root>/README.md`)
has the exact `terraform init/plan/apply` invocation for that root — the
pattern is identical across all four:

```bash
cd deploy/gcp/<root>
cp backend.hcl.example backend.hcl        # edit state bucket
cp terraform.tfvars.example terraform.tfvars  # edit image digests, non-secret labels
terraform init -backend-config=backend.hcl
terraform fmt -check
terraform validate
terraform plan -out=<root>.tfplan
terraform show <root>.tfplan               # review before applying
terraform apply <root>.tfplan
terraform plan -detailed-exitcode          # must exit 0: no drift
```

### Foundation (`deploy/gcp/foundation`)

Creates private networking, IAM/workload identities, KMS/CMEK keys
(including the dedicated `connector-credentials` and `customer-export` keys),
Secret Manager containers (empty — platform secrets are populated later, out
of Terraform), Cloud Tasks queues, Artifact Registry, and audit-log retention.
**No customer data is allowed at this stage**, and the root's own `README.md`
documents this as a fail-closed gate. This root has no `enable_*` feature
variables — every variable here is sizing/identity
(`project_id`, `region`, `environment`, `sql_tier`, `database_version`,
`backup_retention_count`, `audit_retention_days`, `lock_audit_retention`,
`export_bucket_policy_admin_members`, `jobs_worker_target_host`,
`jobs_worker_oidc_audience`, `labels`).

### Data (`deploy/gcp/data`)

Deploys the private, operator-executed migrator job (§2 below) and the
private initial-identity-provisioning job (dormant without a one-time secret
version; never run by Terraform itself). Its two feature gates:

| Variable | Default | Gates |
|---|---|---|
| `enable_protocol_retention_schedule` | `false` | The independently-authenticated daily protocol-retention Cloud Scheduler job |
| `enable_export_cleanup_schedule` | `false` | The ten-minute customer-export cleanup Cloud Scheduler job |

Leave both `false` on first apply — everything in this root is paused/dormant
by default; day-2 operations (§5) covers when to flip them.

### Runtime

Deploys the dispatch broker, secret broker, workers, model gateway, channel
broker, and audit writer — each dormant-first (its feature flag off) until
its own negative/adversarial tests pass. Ten `enable_*`/`*_enabled` gates
live here, all defaulting `false`:

| Variable | Gates |
|---|---|
| `enable_channel_broker` | Deploys the private channel broker |
| `enable_dispatch_broker` | Deploys dispatch (after the jobs-queue fixed override) |
| `enable_export_writer` | Deploys the private customer-export writer |
| `enable_model_gateway` | Deploys the private fixed-task model gateway (alone: no conversation activation) |
| `enable_google_chat_conversation` | Registers the worker's bounded Google Chat conversation route |
| `enable_slack_conversation` | Registers the worker's bounded Slack conversation route |
| `enable_web_conversation` | Registers the worker's bounded hosted web conversation route |
| `slack_channel_enabled` | Configures the broker's Slack installation routes (needs the platform Slack app from §0) |
| `enable_google_gmail_profile` | Registers the fixed Gmail profile worker route |
| `enable_google_workspace_verification` | Registers the composite Gmail+Calendar verification route |

Also `oauth_min_instance_count` (default `0`; the rollout evidence sets it to
`1` only after OAuth activation) and the SLO threshold variables
(`slo_5xx_error_threshold` default `5`, `slo_alert_window_seconds` default
`300`, `slo_worker_conversation_p95_latency_ms` default `15000`) — these last
three are unconditional infrastructure, not gates; see §5.

### Edge

Deploys the public control plane and provider ingresses behind Cloud Armor,
admitting only exact paths, activated last per capability. Seventeen
`enable_*`/`deploy_*` gates live here, all defaulting `false`, plus four
`*_provider_ready` attestation booleans (also default `false` — these are
operator sign-off flags proving out-of-band provider configuration is done,
not deploy toggles themselves):

| Variable | Gates |
|---|---|
| `enable_identity_sign_in` | Staged Identity Platform session routes |
| `enable_google_workspace_oauth` | The separate Google Workspace connector-consent journey |
| `enable_hosted_onboarding` | The tenant-bound versioned onboarding-state API |
| `enable_hosted_policy` | The recent-authenticated fixed R0 read-only policy ceremony |
| `enable_hosted_channels` | The recent-authenticated channel-preference ceremony |
| `enable_hosted_channel_setup` | The channel-installation setup boundary |
| `enable_hosted_channel_lifecycle` | The channel disconnect/replacement ceremony |
| `enable_customer_exports` | Account export requests and owner-bound status |
| `deploy_customer_export_download` | Deploys the download service behind an unrouted, default-deny backend |
| `deploy_google_chat_ingress` | Deploys Google Chat ingress behind an unrouted backend |
| `enable_google_chat_ingress` | Routes the exact Google Chat event endpoint |
| `enable_google_chat_conversation` | Routes verified owner-DM Chat messages into hosted conversation |
| `deploy_slack_ingress` | Deploys Slack ingress behind an unrouted backend |
| `enable_slack_ingress` | Routes the exact Slack event endpoint |
| `enable_slack_conversation` | Routes verified owner-DM Slack messages into hosted conversation |
| `enable_hosted_slack_install` | Exposes hosted Slack installation (needs runtime's Slack channel + deployed Slack ingress) |
| `enable_hosted_web_conversation` | Exposes the web conversation message/turn-poll routes |

Cloud Armor rule priorities (`deploy/gcp/edge/main.tf`, hardcoded, not
variables) occupy `880`–`893` plus `900` for named security rules, with
catch-all default-deny rules at `1000`/`2147483647` per backend. When you add
a new onboarding-ceremony rule, the next free priority in the reviewed range
is what the ceremony docs (e.g. `hosted-signup.md` §7) already reserve — check
the specific ceremony doc for its assigned number before picking one.

Apply all four roots with every gate at its `false` default on first pass.
Confirm `terraform plan -detailed-exitcode` is `0` (empty) after each root
before moving to the next.

## 2. The migrator job and boundary verifier

`src/attune/hosted/migrate.py` is packaged into `deploy/migrator/Dockerfile`,
whose `ENTRYPOINT` is exactly:

```text
python -m attune.hosted.migrate
```

It is a Cloud Run **Job**, not a service: it accepts no command-line
arguments at all (`main()` raises `ValueError` if any `argv` is given) and is
driven entirely by environment variables set in `deploy/gcp/data/main.tf`:
`ATTUNE_CLOUD_SQL_INSTANCE`, `ATTUNE_DB_USER` (both required), `ATTUNE_DB_NAME`
(defaults `attune`), and `ATTUNE_DB_ROLE_BINDINGS` (a required JSON object
mapping each of the 14 fixed runtime roles to a distinct Cloud SQL IAM login).

Run it:

```bash
gcloud run jobs execute attune-<environment>-database-migrate \
  --project="$PROJECT_ID" --region="$REGION" --wait
terraform plan -detailed-exitcode
```

What it does, in order:

1. Opens a Cloud SQL Python Connector connection with automatic IAM database
   authentication (no password, key, or database URL ever passed).
2. **Applies migrations**: loads packaged SQL files matching
   `^[0-9]{4}_[a-z0-9_]+\.sql$`, takes a session-scoped PostgreSQL advisory
   lock so concurrent runs serialize, and for each migration either confirms
   its already-recorded SHA-256 checksum matches the packaged file exactly
   (a changed historical migration file is a hard `RuntimeError`, never
   silently reapplied) or applies and records it transactionally.
3. **Binds runtime roles**: reconciles all 14 fixed database roles
   (`attune_control_plane`, `attune_channel_broker`, `attune_dispatch_broker`,
   `attune_worker`, `attune_secret_broker`, `attune_audit_writer`,
   `attune_oauth_exchange`, `attune_identity_provisioner`, `attune_retention`,
   `attune_export`, `attune_export_cleanup`, `attune_export_download`,
   `attune_content_retention`, `attune_deletion`) to exactly the Cloud SQL IAM
   login named in `ATTUNE_DB_ROLE_BINDINGS`, revoking stale members.
4. **Verifies the database boundary**: a long, read-only sequence of
   assertions — every tenant table has RLS enabled *and* forced; every
   runtime/function-owner role is unprivileged and matches its exact expected
   table/schema privilege set; `pgcrypto`/`vector` live in the isolated
   `attune_ext` schema; `PUBLIC` has zero grants; append-only audit triggers
   exist and are enabled; every privileged `SECURITY DEFINER` function has
   `search_path` pinned, the correct owner, and no `PUBLIC EXECUTE`; every
   runtime role maps to exactly one IAM member — then rolls back (the
   verifier itself makes no changes).
5. On success, prints exactly:

   ```text
   hosted database boundary verified; <N> migration(s) applied; <M> tenant tables forced through RLS
   ```

   and exits `0`. Any failed assertion raises an uncaught exception (nonzero
   exit, traceback on stderr) — there is no partial-success mode. Re-running
   the job is safe and idempotent: it should apply zero migrations while
   repeating every live security check.

Confirm the printed migration count and RLS table count against what the
change you're applying expects, then confirm `terraform plan -detailed-exitcode`
returns `0` before proceeding.

## 3. Service deployment at fixed digests

Every hosted service is a `gunicorn` process fronting one Flask app module,
built from its own Dockerfile and deployed by digest — never a mutable tag:

| Service | Dockerfile | App module |
|---|---|---|
| Control plane | `deploy/control-plane/Dockerfile` | `attune.hosted.control_plane_app:app` |
| OAuth callback | `deploy/oauth-callback/Dockerfile` | `app:app` (bare module, no OAuth logic — see below) |
| OAuth exchange | `deploy/oauth-exchange/Dockerfile` | `attune.hosted.oauth_exchange_app:app` |
| Secret broker | `deploy/secret-broker/Dockerfile` | `attune.hosted.secret_broker_app:app` |
| Dispatch broker | `deploy/dispatch-broker/Dockerfile` | `attune.hosted.dispatch_broker_app:app` |
| Worker | `deploy/worker/Dockerfile` | `attune.hosted.worker_app:app` |
| Model gateway | `deploy/model-gateway/Dockerfile` | `attune.hosted.model_gateway_app:app` |
| Channel broker | `deploy/channel-broker/Dockerfile` | `attune.hosted.channel_broker_app:app` |
| Google Chat ingress | `deploy/google-chat-ingress/Dockerfile` | `attune.hosted.google_chat_ingress_app:app` |
| Slack ingress | `deploy/slack-ingress/Dockerfile` | `attune.hosted.slack_ingress_app:app` |
| Audit writer | `deploy/audit-writer/Dockerfile` | `attune.hosted.audit_service:app` |
| Customer-export writer | `deploy/export-writer/Dockerfile` | `attune.hosted.export_writer_app:app` |
| Customer-export download | `deploy/export-download/Dockerfile` | `attune.hosted.export_download_app:app` |
| Migrator (Cloud Run Job, not a service) | `deploy/migrator/Dockerfile` | `python -m attune.hosted.migrate` |

(`deploy/Dockerfile` and `deploy/republisher/Dockerfile` are not hosted
multi-tenant services — the former is the self-hosted always-on process
`python -m attune`, the latter is the self-hosted Pub/Sub variant's republisher
described in [`../deployment.md`](../deployment.md); neither belongs to this
runbook.)

The established pattern from every rollout recorded in
[`../hosted-gcp.md`](../hosted-gcp.md) and `roadmap.md` is: build and push each
image, pin its digest in `terraform.tfvars`, deploy with the relevant gate(s)
explicitly `false`, confirm `terraform plan -detailed-exitcode` is `0` and a
basic health probe passes, *then* a second, separately reviewed plan flips
only the gate(s) for that capability — never combine an image rollout with a
gate flip in the same plan.

## 4. Activation ceremonies, in dependency order

Each ceremony below is a *ceremony*, not a deploy: apply Terraform, verify an
empty plan and negative/adversarial tests, *then* flip the flag and verify
again. This section gives the order this platform's own development history
actually exercised these in (per `roadmap.md` and the dated entries in
`hosted-gcp.md`), the flag(s) each needs, what evidence must precede flipping
it, and a pointer to the doc that owns the ceremony's actual steps — this
runbook never restates the ceremony itself.

Where an order below is this document's own recommendation rather than a
hard dependency the code enforces, it is marked **(recommended order)**.

### 4.1 Identity: sign-in

- **Flags:** `enable_identity_sign_in` (edge), plus the operator ceremony in
  §0 (Identity Platform provider configuration, browser key restriction).
- **Evidence required first:** provider settings and authorized domains
  independently reviewed; separate client IDs/redirects verified; Cloud Armor
  route/rate rules and content-free logging in place.
- **Owning doc:** [`../identity-platform.md`](../identity-platform.md)
  "Activation gates". Development evidence: sign-in activated and verified
  2026-07-15.

### 4.2 Membership: operator provisioning (today) or signup (not yet active)

Today, membership is granted by a private, one-purpose operator job, not by a
customer-facing route:

- **Mechanism:** `attune-<env>-identity-provision` Cloud Run Job (dormant
  without a one-time secret version; never run by Terraform). Pipe the
  selected Identity Platform subject's SHA-256 hash into a one-version
  CMEK-backed secret, execute the job, then destroy the secret version. Exact
  commands are in `deploy/gcp/data/README.md`'s "Initial development identity
  ceremony" section.
- **Owning doc:** `identity-platform.md` "Staged development activation".
  Development evidence: first tenant/principal mapping activated 2026-07-15.

Production self-service signup (`POST /v1/signup`) is designed and
implemented behind `ATTUNE_HOSTED_SIGNUP_ENABLED` but is **not wired into
Terraform at all** — no `enable_*` variable exists in any of the four roots
for it. It also requires `ATTUNE_HOSTED_SIGNUP_REGION`, which
`control_plane_app.py` reads with no default (`os.environ[...]`) — deploying
with signup enabled today, before that variable is wired, would crash the
control plane at startup. Before enabling this in any environment: author the
missing Terraform wiring for both variables, apply migration 0045, author the
Cloud Armor edge rule at the next free priority (`hosted-signup.md` §7
reserves `894`), and complete the live probe and abuse-monitoring checks in
`hosted-signup.md` §11. None of this is done in this codebase today.

### 4.3 Workspace connect + verification route

- **Flags:** `enable_google_workspace_oauth` (edge), plus
  `enable_google_gmail_profile` / `enable_google_workspace_verification`
  (runtime worker routes) and the secret broker/OAuth exchange deployment
  from step 1's runtime apply.
- **Evidence required first:** the no-NAT, exact-host private Google API
  boundary's credential-free egress probe (repeat after material network or
  image changes); separate Gmail and Calendar one-use intents; durable
  pre/post audit.
- **Owning doc:** [`../hosted-gcp.md`](../hosted-gcp.md) "Deployment order and
  gates" §3 and "Credential flow"; [`../oauth-transaction.md`](../oauth-transaction.md)
  for the transaction/callback contract. Development evidence: Gmail profile
  operation and the composite verifier exercised 2026-07-16; callback
  activation 2026-07-15; egress probe 2026-07-14.

### 4.4 Onboarding state + policy ceremony

- **Flags:** `enable_hosted_onboarding` (edge) before `enable_hosted_policy`
  (edge) **(recommended order** — onboarding state is what the policy
  ceremony advances**)**.
- **Evidence required first:** migration `0019_hosted_read_only_policy.sql`
  applied and boundary-verified; control-plane deployed with the policy gate
  false first; private audit-writer invocation and recent-session negative
  tests pass.
- **Owning doc:** [`../hosted-policy.md`](../hosted-policy.md) "Deployment and
  activation order". Development evidence: rollout 2026-07-16, owner
  confirmation 2026-07-16.

### 4.5 Channel preference

- **Flag:** `enable_hosted_channels` (edge), Cloud Armor priority `886`.
- **Evidence required first:** migration `0020` applied and verified; empty
  data plan.
- **Owning doc:** [`../hosted-channels.md`](../hosted-channels.md) "Deployment
  order". Development evidence: rollout and owner ceremony both 2026-07-16.

### 4.6 Channel installation (Google Chat, then Slack)

- **Flags, Google Chat:** `deploy_google_chat_ingress` → `enable_google_chat_ingress`
  (edge) → `enable_hosted_channel_setup` (edge) → `enable_channel_broker`
  (runtime). **Flags, Slack:** `deploy_slack_ingress` → `enable_slack_ingress`
  (edge) → `slack_channel_enabled` (runtime) → `enable_hosted_slack_install`
  (edge, needs the runtime Slack channel and deployed ingress first).
- **Evidence required first:** each public ingress deployed independently
  with callback routes blocked at the edge, then verified provider
  signatures/audiences, replay limits, body limits, content-free logging;
  one platform-owned provider app (§0) with immutable callback/audience;
  a real owner-DM link and one explicit fixed-content test per provider
  before enabling the next provider.
- **Owning doc:** [`../hosted-channel-installation.md`](../hosted-channel-installation.md)
  "Activation gates" and its Google Chat/Slack implementation sections.
  Development evidence: Google Chat link+delivery-test complete and
  disconnect/relink lifecycle verified 2026-07-16; Slack activated
  2026-07-17, live lifecycle regression 2026-07-17/18. Slack's explicit
  mutation-refusal probe remains outstanding (exercised for Google Chat only).

### 4.7 Conversation (Google Chat, Slack, then web)

- **Flags:** `enable_google_chat_conversation` (runtime + edge, together),
  `enable_slack_conversation` (runtime + edge, together),
  `enable_web_conversation` (runtime) + `enable_hosted_web_conversation`
  (edge). `enable_model_gateway` (runtime) must be deployed first for any of
  these.
- **Evidence required first:** dormant model gateway with a dedicated service
  identity and secret grant; cross-tenant, replay, route-substitution,
  prompt-injection, SSRF, redirect, oversized-body, and duplicate-delivery
  tests; model-provider retention/training/residency review; a saved
  Terraform activation plan with no unrelated changes; live owner-DM general,
  Gmail, Calendar, and mutation-refusal tests.
- **Owning doc:** [`../hosted-conversation.md`](../hosted-conversation.md)
  "Activation gates" and "The browser surface". Development evidence: Google
  Chat conversation live 2026-07-16; Slack conversation live 2026-07-17; web
  conversation live 2026-07-18-dated evidence (migration 0041).

### 4.8 Memory gate

- **Flag:** `ATTUNE_ENABLE_HOSTED_MEMORY` (worker). **Not wired in Terraform
  in any root** — there is no `enable_hosted_memory` variable anywhere under
  `deploy/gcp`. Setting this today requires a direct, out-of-band Cloud Run
  environment-variable edit, which is itself a departure from this
  platform's own "activation is a reviewed Terraform ceremony, not an
  out-of-band edit" norm — do not do this outside a deliberately reviewed
  exception.
- **Owning doc:** [`../hosted-memory.md`](../hosted-memory.md). Status:
  implemented and tested behind the default-off gate; **not deployed**, per
  that document's own header.

### 4.9 Briefs

- **Flag:** `ATTUNE_ENABLE_HOSTED_BRIEF` (control plane + worker, together).
  **Not wired in Terraform.**
- **Owning doc:** [`../hosted-channels.md`](../hosted-channels.md) "Proactive
  brief delivery". Status: implemented and tested; **not deployed**.
  Recurring scheduling (firing the job on a timer rather than an owner click)
  is separate future operator work even once deployed.

### 4.10 Model profiles and metering

- **Flags:** `ATTUNE_ENABLE_TENANT_MODEL_PROFILES` (model gateway + control
  plane + worker, together) and `ATTUNE_ENABLE_MODEL_USAGE_METERING` (worker +
  control plane, independently). **Neither is wired in Terraform**; the
  gateway's premium-route environment variables
  (`ATTUNE_MODEL_PREMIUM_CLASSIFY`, `ATTUNE_MODEL_PREMIUM_CONVERSE`,
  `ATTUNE_MODEL_PREMIUM_EMBED`) and the Cloud Armor rule for
  `/v1/model-profile` and `/v1/usage` also remain unauthored.
- **Owning doc:** [`../hosted-model-profiles.md`](../hosted-model-profiles.md)
  "Deployment order". Status: implemented and tested; **not deployed**.

### 4.11 Draft-and-approve capability (typed capability gateway)

- **Flag:** `ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY` (worker). **Not wired in
  Terraform.** No worker deployment sets this gate on; the fixed R0 policy
  grants no tenant R2 authority, and no OAuth flow requests the scope this
  capability requires — no production tenant can exercise it even in
  principle today.
- **Owning doc:** [`../capability-gateway.md`](../capability-gateway.md) and
  `roadmap.md`'s Phase 5 stage 3 paragraph. Status: implemented and tested;
  **not deployed**.

### 4.12 Retention, deletion, export

- **Protocol retention (the one fully-activated retention slice):**
  `enable_protocol_retention_schedule` (data root). Deployed paused-first,
  activated to a daily schedule only after authenticated-path, paging,
  IAM-isolation, and Terraform-convergence evidence. This is the one
  retention/deletion gate that **is** live in development — flip it only
  after repeating that same evidence chain in your environment.
- **Content retention:** `ATTUNE_ENABLE_CONTENT_RETENTION` (job entrypoint
  gate). **Not wired in Terraform, and no Cloud Run Job resource for it
  exists yet** — there is nothing to `terraform apply` for this today; the
  job entry point refuses to open a database connection unless the gate is
  `"true"`, but there is no deployed job to set that on.
- **Tenant deletion:** `ATTUNE_HOSTED_DELETION_ENABLED` (control-plane routes
  + job entrypoint gate). **Not wired in Terraform, and no Cloud Run Job
  resource for it exists yet**, same as content retention.
- **Customer export:** `enable_customer_exports` / `deploy_customer_export_download`
  (edge, both wired) and `enable_export_writer` (runtime, wired) plus
  `enable_export_cleanup_schedule` (data, wired) — these **are** wired and
  deployable, following the exact sequence in
  [`../customer-export.md`](../customer-export.md) "Deployment order" /
  "Required evidence before production activation". The private alpha
  exposes only the `account` scope; every other scope stays server-side
  disabled regardless of these gates.
- **Owning doc:** [`../data-lifecycle.md`](../data-lifecycle.md) "Delivery
  sequence" ties all four together and is the authoritative status summary —
  read it before touching any of these four gates.

## 5. Day-2 operations

- **Migrations on upgrade.** Re-run the migrator job (§2) after every image
  build that adds a migration. It is idempotent — a repeat run applies zero
  new migrations while repeating every live boundary check — so it is safe to
  run on every deploy, not only when you know a migration changed. Watch for
  a checksum-mismatch failure, which means a historical migration file was
  edited after being applied; that is a hard stop, never auto-resolved.
- **Retention and cleanup schedules.** `enable_protocol_retention_schedule`
  and `enable_export_cleanup_schedule` (data root) are the two schedules wired
  today; both default paused. Content-retention and tenant-deletion have no
  scheduler infrastructure yet (§4.12). When you do enable a schedule, follow
  the same paused-first-then-authenticated-path evidence chain the protocol
  retention job already completed, documented in `data-lifecycle.md`
  "Delivery sequence" item 2.
- **SLO dashboard and alerts.** Unconditional infrastructure — it activates
  with the ordinary `terraform apply` for `runtime` and `edge`, tied only to
  whichever flag gates the underlying service's own existence, with no
  separate monitoring toggle. Populate `alert_notification_channels` in both
  roots or nothing pages anyone. See
  [`../hosted-gcp.md`](../hosted-gcp.md#slo-monitoring) for the full metric/alert/
  dashboard inventory and threshold variables
  (`slo_5xx_error_threshold`, `slo_alert_window_seconds`,
  `slo_control_plane_p95_latency_ms` (edge),
  `slo_worker_conversation_p95_latency_ms` (runtime)). No uptime-check or
  synthetic-monitoring infrastructure exists to extend.
- **Incident paging.** Every alert policy created above pages through
  `alert_notification_channels`; there is no separate paging system in this
  codebase. Reconciliation for ambiguous dispatch/execution outcomes is
  described in [`../reconciliation.md`](../reconciliation.md) — an operator
  resolves a `failed` reconciliation or deletion-request record manually;
  there is no automated retry for a genuinely ambiguous effect.
- **Backup posture.** Cloud SQL backups are created by the foundation root
  (`backup_retention_count` variable). The independent, cross-tenant
  restore-suppression ledger that `data-lifecycle.md` requires before any
  restore can admit traffic is **not yet built** — do not perform a restore
  into a traffic-serving environment until that ledger and its procedure
  exist; `data-lifecycle.md` "Account deletion and restore suppression" is
  explicit that a missing or unverifiable ledger blocks the restore
  unconditionally, never falling back to activating the snapshot.
- **Tenant support boundaries.** An operator provisions infrastructure and
  Terraform roots, not per-customer credentials or content. There is no
  operator UI or CLI path that reads tenant table rows directly — every
  cross-tenant operation in this codebase is a fixed, narrowly-scoped
  `SECURITY DEFINER` function owned by a memberless role, invoked only by the
  specific workload identity that owns that ceremony (see `hosted-gcp.md`
  "Trust boundaries and services" for the full service inventory). Support
  repair for a stuck ceremony (e.g. a `failed` deletion request, an
  unresolved reconciliation record) is explicitly **future work**, not a
  capability this runbook can give you today — see `data-lifecycle.md` and
  `reconciliation.md`'s own "remaining gate" language.

## 6. Honest status

What has **development-environment live evidence** today, per the dated
entries in `roadmap.md` and `hosted-gcp.md` (all dated 2026-07-14 through
2026-07-20, all in one shared development project, none in a
production/customer-facing environment):

- Foundation, data-boundary migration/verification, dispatch broker, secret
  broker, OAuth exchange.
- Identity sign-in and one operator-provisioned tenant/principal mapping.
- Hosted onboarding state, the R0 read-only policy ceremony, channel
  preferences.
- Google Chat and Slack channel installation, conversation, and lifecycle
  (disconnect/relink) — Slack's mutation-refusal probe still outstanding.
- Web browser conversation.
- Protocol-retention's paused-first-then-daily-schedule activation.
- Customer export's writer/download/cleanup implementation (not yet activated
  end to end at the edge; the private alpha ships only the `account` scope).
- SLO-grade request/task metrics, alerts, and one dashboard (unconditional
  infrastructure, not a gate).

What is **implemented and tested but has never been exercised outside
development, has no Terraform wiring, or both** — do not represent any of
these as available to a customer or as one `terraform apply` away from being
so:

- Production self-service signup (`ATTUNE_HOSTED_SIGNUP_ENABLED`) — no
  Terraform variable, and a second required environment variable
  (`ATTUNE_HOSTED_SIGNUP_REGION`) is also unwired.
- Hosted conversational memory (`ATTUNE_ENABLE_HOSTED_MEMORY`).
- The draft-and-approve capability gateway (`ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY`).
- Hosted proactive briefs (`ATTUNE_ENABLE_HOSTED_BRIEF`).
- Per-tenant model profiles and usage metering
  (`ATTUNE_ENABLE_TENANT_MODEL_PROFILES`, `ATTUNE_ENABLE_MODEL_USAGE_METERING`).
- Content-retention and tenant-deletion executors — neither has a deployed
  Cloud Run Job, let alone a scheduler, regardless of their own gate values.
- The independent, cross-tenant backup-restore-suppression ledger
  `data-lifecycle.md` requires before any restore is safe.

What has **never been attempted in any environment, development included**,
per `security-architecture.md`'s launch gates and `hosted-gcp.md`'s closing
line: the tenant-isolation adversarial suite, an independent penetration
test, Google OAuth verification/CASA evidence, and formal launch-gate review.
None of these exist yet in any form. Production is blocked on all of them.
