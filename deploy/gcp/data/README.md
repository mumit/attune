# Hosted data boundary

This Terraform root deploys Attune's private, operator-executed PostgreSQL
migration job after the no-customer-data foundation exists. The job creates the
hosted schema and verifies its database controls. It does not deploy an
application, public endpoint, connector credential, tenant, or customer record.

This root also declares a separate private initial-identity provisioning job.
It is dormant without a one-time secret version and is never run by Terraform.
It does not broaden the migration job: the migrator remains unable to accept
runtime arguments and never creates customer records.

## Security model

- The migration image is Linux/amd64, runs as UID/GID 65532, and is referenced
  in Terraform only by an immutable Artifact Registry digest.
- The job uses Direct VPC egress to reach private-IP Cloud SQL. It has no HTTP
  endpoint and this root grants nobody permission to execute it.
- Its dedicated service account has only Cloud SQL client/login and log-writer
  IAM roles. Its IAM database user is deliberately assigned
  `cloudsqlsuperuser` because Cloud SQL requires that role to install supported
  extensions such as `vector`.
- No key, password, token, or database URL is accepted. Automatic IAM database
  authentication uses the job's short-lived workload identity.
- The migrator accepts no command-line overrides. Migrations are ordered,
  SHA-256 recorded, serialized by a PostgreSQL advisory lock, transactional,
  and refuse changed checksums.
- Runtime database roles are fixed, `NOLOGIN`, non-superuser, and
  `NOBYPASSRLS`. Each role is reconciled to exactly one foundation IAM database
  user; stale members are revoked.
- Initial membership has its own IAM login and `NOLOGIN NOBYPASSRLS` database
  role. That role has schema usage and execute permission on one fixed function,
  with no direct table privileges. Its memberless function owner can select and
  insert only tenants and principals.
- Cross-tenant `SECURITY DEFINER` functions are owned by distinct
  memberless `NOLOGIN BYPASSRLS` roles for dispatch, audit writing, vault
  mutation, and one-time OAuth transaction exchange. No IAM or runtime login
  is a member. Each owner has only the table privileges required by its fixed
  functions; temporary migrator membership and schema-create authority are
  revoked inside the migration transaction.
- Every tenant table enables and forces RLS. Missing transaction-local tenant
  context raises an error rather than returning an ambiguous empty result.
- Audit events can only be appended through the tenant-checking hash-chain
  function; triggers reject update, delete, and truncate.

The transaction tenant setting is a storage guard, not authentication. A
caller must derive `TenantContext` from a verified session, installation, or
signed job before opening a database transaction. A model argument, URL field,
or unsigned queue payload is never sufficient. Shared-role RLS primarily
contains programming mistakes; a fully compromised workload holding a valid
database session could attempt to change its session setting. Production
therefore also requires deterministic service authorization, signed
purpose-bound jobs, secret-broker checks, workload revocation, and adversarial
tests. Higher-assurance tenants may require distinct workload/data identities.

## Schema

The migrations currently create tenant-bound records for:

- tenants, principals, installations, connectors, and policies;
- provider events, jobs, retries, workflow checkpoints, and approvals;
- conversations and turns;
- memories and variable-dimension `vector` embeddings;
- autonomy grants and content-free usage records;
- export jobs and deletion/restore-suppression markers;
- durable dispatch intents and broker-only lease/finalize functions; and
- tenant-bound audit intents, hash-chained audit events, and per-tenant audit
  heads; and
- immutable encrypted connector credential versions plus one-time installation,
  use, and revocation intents leased only through secret-broker functions; and
- content-free job reconciliation records opened atomically with the canonical
  job's transition out of a lease; and
- short-lived OAuth transactions bound to tenant, principal, pending connector,
  canonical `google.oauth.install` credential intent, state, browser binding,
  OIDC nonce, PKCE verifier, redirect URI, and scopes; and
- eight-hour-maximum opaque identity sessions bound to tenant, principal, and
  independent token/CSRF hashes.

The OAuth exchange IAM database user is bound to a dedicated unprivileged
runtime role. It has schema use and execute rights on exactly the lease and
finalize functions, but no direct table privilege. Leasing requires both
independent 256-bit hashes, resolves tenant authority from canonical storage,
and serializes concurrent callbacks. Finalization requires the callback-binding
hash again, is terminal, and clears the live PKCE verifier value. The control
plane can only select and insert its tenant-visible transaction rows.

The install-intent migration deliberately adds its required column while OAuth
is still disabled; any pre-existing transaction makes the migration fail
instead of being guessed or backfilled. Its composite foreign key is installed
`NOT VALID` to avoid an RLS-bypassing historical table scan, but PostgreSQL
enforces it for every subsequent insert and update. The insert trigger also
independently verifies the canonical connector and requested install intent.
The non-login function owner receives schema `CREATE` only within the migration
transaction while its lease function is replaced and ownership transferred;
that privilege is revoked before commit and verified absent afterward.

Identity session creation searches across tenants only inside a fixed
`SECURITY DEFINER` function and inserts a session only when the verified issuer
and subject hash map to exactly one active principal in one active tenant. Read,
CSRF authorization, and revocation are separate fixed functions. The control
plane can execute them but has no direct session-table privilege; the memberless
function owner retains only the exact tenant/principal reads and session-table
select/insert/update rights.

Credential installation and rotation atomically supersede the prior active
version, insert the new encrypted envelope, update the connector reference, and
consume the intent. Revocation atomically marks both credential and connector
revoked while retaining the opaque credential reference for audit lineage.
Only one installation or revocation intent can hold a live lease for a
connector, keeping the predicted credential version stable while AES-GCM binds
that version into authenticated data.

Dispatch audit is two-phase without being ambiguous: the broker must write a
canonical `allowed` audit intent while the dispatch lease is active before it
can create a task, then write the `observed` or `failed` result from finalized
canonical state. Audit failure before creation leaves the lease recoverable and
creates no task.

The hosted Python boundary provides repositories for every durable object
class: provider events, jobs/retries, workflow checkpoints, conversations,
approvals, memories/vectors, autonomy grants, usage, exports, deletion markers,
dispatch intents, and audit intents. Every tenant-scoped method requires a
`TenantContext`; none accepts a tenant
embedded in payload or model output. Idempotency collisions are checked,
leases and sequence allocation are atomic, checkpoints use expected versions,
vector search injects tenant and principal predicates, deletion marks both
relational and vector rows, and approvals atomically bind and consume the
expected actor, action hash, source version, policy version, connector,
destination, and expiry.

The versioned Cloud Tasks envelope contains only tenant, canonical job ID,
delivery ID, and an allowlisted purpose. Verification requires an exact HTTPS
audience, Google issuer, verified task-dispatch service-account email, bounded
token lifetime, canonical UUID text, and an exact body schema. Provider
content and executable arguments are fetched from PostgreSQL only after
verification; duplicate delivery loses the atomic job claim.

Cloud Tasks OIDC authenticates the Google-managed delivery and exact dispatch
service account; it does not sign arbitrary body fields on behalf of Attune.
The dispatch core therefore binds purpose and capability again inside the
atomic database claim and refuses to execute when the audit boundary is
unavailable. Live provider-executor activation remains prohibited until its queue has
fixed target routing and least-privilege producers, the private audit writer
exists, and the executor passes deterministic capability and ambiguous-effect
review. Deploying a generic handler before those controls would turn an
identifier envelope into unintended authority. The private intent-only audit
writer, credential-mutation secret broker, dispatch broker, fixed jobs-queue
route, and content-free deterministic smoke worker are now deployed from
`deploy/gcp/runtime`. The composite fixed Gmail/Calendar verifier exists behind
a disabled-by-default runtime gate. Development activation produced
authenticated provider, audit, and browser evidence on 2026-07-16; every new
environment must reproduce the operational gates before activation.

Migration `0018_hosted_onboarding.sql` was applied in development on
2026-07-16 UTC before its edge gate was enabled. The migration job reported one
new migration and verified 28 tenant tables forced through RLS. The subsequent
data plan was empty. This ordering is mandatory for rebuilds and new
environments: deploy and execute the immutable migrator, require successful
boundary verification, and only then enable hosted onboarding at the edge.

The fixed read-only policy ceremony adds migration
`0019_hosted_read_only_policy.sql`. It creates the recent-session authorization
function and memberless policy function owner, revokes direct policy/grant
mutation from the ordinary control-plane role, and exposes one exact idempotent
R0 activation function. New environments must run and verify this migration
before setting the edge root's `enable_hosted_policy` gate. The disposable
PostgreSQL suite covers cross-tenant refusal, direct-mutation denial, exact
document/grant creation, idempotency, recent-auth expiry, and external-change
detection.

Development rollout evidence was collected on 2026-07-16 UTC from commit
`5ba3668`. Immutable migrator digest
`sha256:9b39090eb54f83926055bc0ff5036ed5a43425cf50a2c6670061f7c684ad8b41`
was applied to both private jobs. Execution
`attune-development-database-migrate-rtw9v` applied exactly one migration and
reported 28 tenant tables forced through RLS; the following data plan was
empty. No policy or autonomy grant was created by the migration.

Migration `0020_hosted_channel_preferences.sql` adds the forced-RLS preference
record and fixed recent-session configuration function. It stores only
canonical Slack/Google Chat purpose choices and advances channels to
`authorized`; it creates no app, destination, credential, ingress, or message.
Apply and verify it before enabling the separate edge gate.

Development rollout evidence was collected on 2026-07-16 UTC. Immutable
migrator digest
`sha256:9720b34f541a5bcc7e0a2e9a30a91058e8248e3dd5db12e3db4b09253365634a`
was applied to both private jobs. Execution
`attune-development-database-migrate-pcpm9` applied exactly one migration and
reported 29 tenant tables forced through RLS; the following data plan was
empty. The migration created no channel preference or provider authority.

Migration `0021_hosted_channel_installation_state.sql` adds forced-RLS setup
transactions and owner-DM destination bindings, plus the fixed
recent-session setup-start function. Direct installation mutation remains
revoked from the ordinary control-plane role, and no provider callback
consumer or runtime consume grant exists in this slice.

Development rollout evidence was collected on 2026-07-16 UTC from commit
`27cda78`. Immutable migrator digest
`sha256:d240b09386c35d79d664a4d66dcb13dd8efd2a696c2427dbc5d4ec8ffd8a0c83`
was applied to both private jobs. Execution
`attune-development-database-migrate-rlc6q` applied exactly one migration and
reported 31 tenant tables forced through RLS; the following data plan was
empty. No setup attempt, link, destination, provider credential, or message
was created by the migration.

Migration `0022_google_chat_link_broker.sql` adds the one-use Google Chat link
claim/consume boundary, memberless function owner, broker-only runtime role,
and pre-effect audit intent. Two initial development executions failed before
the migration transaction committed: one exposed missing temporary membership
in the function-owner role and one exposed missing temporary `CREATE` on the
`attune` schema during ownership transfer. Both rolled back completely. The
migrator now grants those capabilities only to the migration identity and the
fixed functions revoke them from runtime callers.

Final development evidence used immutable migrator digest
`sha256:386ceb843a33de4594c1b438a941bfa8823d500ecf50ef6ceb5079fd9ca2f7aa`.
Execution `attune-development-database-migrate-tbd9h` applied exactly one
migration and reported 31 tenant tables forced through RLS. No setup attempt,
claim, installation, destination, credential, or message was created.

Migration `0023_google_chat_delivery_test.sql` adds the encrypted destination
route vault, fixed delivery-test claim/completion functions, and explicit
adoption path for pre-route `pending_test` bindings. Existing routes are never
inferred from hashes: canonical readback reports `needs_relink`, and a fresh
owner-DM code may attach a route only when installation, actor, and destination
references all match. The migration adds one forced-RLS tenant table, bringing
the verifier total to 32. Apply it before deploying broker or control-plane
code that exposes the delivery-test action.

Connector rows hold only opaque credential references. Credential ciphertext
arrives with the separate connector-vault/secret-broker phase. No secret value
belongs in these migrations, Terraform state, Cloud Run environment variables,
or job logs.

## Local isolation gate

Install the optional hosted dependencies, then run the disposable PostgreSQL
16/pgvector suite:

```bash
python -m pip install -e '.[dev,hosted]'
scripts/test-hosted-db.sh
```

The script uses a digest-pinned pgvector image and a random loopback port. It
tests missing context, cross-tenant IDs and writes, vector searches, pooled
connection reset, exact role membership, append-only audit, idempotency, and
migration checksum tampering. It contains synthetic records only and deletes
the container on exit.

## Build and deploy

Use a reviewed build system capable of producing Linux/amd64 images. The
following developer workflow is acceptable only in the development project;
staging and production require the provenance, vulnerability, signing, and
promotion gates listed below.

```bash
export PROJECT_ID="your-development-project"
export REGION="northamerica-northeast1"
export REPOSITORY="attune-development"
export TAG="schema-v1"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/attune-migrator"

gcloud auth configure-docker "${REGION}-docker.pkg.dev"
docker buildx build --platform=linux/amd64 --push \
  -f deploy/migrator/Dockerfile -t "${IMAGE}:${TAG}" .
gcloud artifacts docker images describe "${IMAGE}:${TAG}" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.fully_qualified_digest)'
```

Put that `@sha256:` reference—not the tag—in the ignored `terraform.tfvars`.
Then review and apply the separate `data` state:

```bash
cd deploy/gcp/data
cp backend.hcl.example backend.hcl
cp terraform.tfvars.example terraform.tfvars
# Edit state bucket, immutable image digest, and non-secret labels.
terraform init -backend-config=backend.hcl
terraform fmt -check
terraform validate
terraform plan -out=data.tfplan
terraform show data.tfplan
terraform apply data.tfplan
```

The plan must have no public principal, secret value, mutable image tag, user-
managed key, public database route, or resource replacement. Execute only the
reviewed job and inspect its content-free result:

```bash
gcloud run jobs execute attune-development-database-migrate \
  --project="$PROJECT_ID" --region="$REGION" --wait
terraform plan -detailed-exitcode
```

Success reports the number of applied migrations and tenant tables forced
through RLS. The final Terraform plan must return exit code 0. Re-running the
job is safe and should apply zero migrations while repeating all live security
checks.

## Initial development identity ceremony

Run this only after the expected user has completed Google sign-in and received
the unprovisioned-membership response. It creates the first tenant and principal
only; it is not a general invitation or membership-management path.

Set the non-sensitive tenant slug in ignored `terraform.tfvars`, rebuild the
migrator image containing the reviewed migration and provisioner, update its
immutable digest, then apply foundation and data plans. The foundation plan adds
one service account, IAM database user, and empty CMEK-backed secret container.
The data plan updates the migrator image and adds one private job. Neither plan
contains an email, provider subject, subject hash, or secret version.

After applying, run the migration job first. Then select exactly the expected,
verified Google user from Identity Platform and stream only its locally hashed
subject into a new secret version:

```bash
export PROJECT_ID="attune-development-502421"
export REGION="northamerica-northeast1"
export EXPECTED_EMAIL="owner@example.com"
export BOOTSTRAP_SECRET="attune-development-identity-bootstrap"

gcloud run jobs execute attune-development-database-migrate \
  --project="$PROJECT_ID" --region="$REGION" --wait

VERSION="$(
  TOKEN="$(gcloud auth print-access-token)"
  curl --fail --silent --show-error \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "X-Goog-User-Project: ${PROJECT_ID}" \
    "https://identitytoolkit.googleapis.com/v1/projects/${PROJECT_ID}/accounts:batchGet?maxResults=100" |
  EXPECTED_EMAIL="$EXPECTED_EMAIL" python -c '
import hashlib, json, os, sys
users = json.load(sys.stdin).get("users", [])
expected = os.environ["EXPECTED_EMAIL"].casefold()
matches = [
    user for user in users
    if user.get("email", "").casefold() == expected
    and user.get("emailVerified") is True
    and any(
        provider.get("providerId") == "google.com"
        for provider in user.get("providerUserInfo", [])
    )
]
if len(matches) != 1:
    raise SystemExit("expected exactly one verified Google identity")
sys.stdout.write(hashlib.sha256(matches[0]["localId"].encode()).hexdigest())
' |
  gcloud secrets versions add "$BOOTSTRAP_SECRET" \
    --project="$PROJECT_ID" --data-file=- --format="value(name.basename())"
)"
```

Execute the fixed job with no overrides, then destroy the one-time version even
though it contains only a pseudonymous hash:

```bash
gcloud run jobs execute attune-development-identity-provision \
  --project="$PROJECT_ID" --region="$REGION" --wait
gcloud secrets versions destroy "$VERSION" \
  --secret="$BOOTSTRAP_SECRET" --project="$PROJECT_ID" --quiet
unset VERSION EXPECTED_EMAIL
```

The only successful job message is `initial identity mapping verified` plus an
idempotency boolean. It prints no tenant ID, principal ID, email, raw subject,
or hash. Verify that the secret has no enabled version, exactly one active
tenant/principal mapping exists for the expected subject hash, a fresh browser
sign-in issues an opaque Attune session, and all three Terraform roots return a
zero-change plan. Do not retain the Identity Platform API response or put it in
a support bundle.

## Google Chat destination lifecycle migration

Migration `0026_google_chat_destination_lifecycle.sql` adds no user data and
performs no disconnection during migration. It creates the memberless
`attune_channel_lifecycle_executor`, exposes one fixed recent-session function
to the control plane, and extends the existing broker-owned link function to
reuse only a revoked canonical destination after a fresh one-time proof. The
lifecycle function cancels outstanding setup claims, deletes the encrypted
route, revokes the destination and installation, clears delivery authority,
and returns onboarding to `authorized` in one transaction. Ordinary runtime
roles retain no direct mutation privilege.

Before applying, the saved data plan may update only the immutable migrator and
identity-provision job images. Run the migration job, require the verifier to
report all tenant tables forced through RLS and exact function-owner
privileges, then require an empty data plan. Keep the independent edge
`enable_hosted_channel_lifecycle` gate false until the new control-plane image
is Ready and the destructive owner ceremony is approved.

## Dormant protocol-retention job

Migration `0028_protocol_retention.sql` and this module add
`attune-<environment>-protocol-retention` with its own Cloud SQL IAM identity.
The identity has no table privileges and may call only
`attune.prune_expired_protocol_records(uuid, integer)`. A separate memberless
`BYPASSRLS` function owner can select/delete the four reviewed protocol tables
and insert content-free audit intents; it cannot log in and has no members.

Each invocation is transactionally bounded to the configured batch size
(default 500, maximum 1,000) per table and prunes only:

- OAuth transactions more than 24 hours past protocol expiry;
- Google Chat/Slack setup transactions more than 24 hours past expiry;
- expired or revoked identity sessions older than 24 hours when no setup
  transaction still references them; and
- processed provider events older than seven days.

The job is deliberately unscheduled. After applying migration 0028, verify an
empty or synthetic development run explicitly:

```bash
gcloud run jobs execute attune-development-protocol-retention \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --wait
```

The execution must succeed once, return only aggregate counts in a structured
`attune_protocol_retention` log, create
the expected per-tenant audit intents when synthetic expired records exist, and
leave recent records intact. Then run the database migrator again to prove the
role/privilege verifier remains clean. Do not add Cloud Scheduler until this
evidence is recorded and alerting for job failure and an accumulating expired
backlog exists. Scheduling this job does not activate conversation or memory
retention.

The executor runs at most `protocol_retention_max_batches` (default four,
maximum ten) per invocation. If every bounded batch remains saturated, it sets
only `backlog_possible=true`; it does not expose tenant identifiers. The data
root creates two paging policies using `alert_notification_channels`: any
error-severity job log, and a structured possible-backlog signal. Google Cloud
documents that a single JSON line on Cloud Run stdout becomes `jsonPayload`,
which is why the metric filters use the fixed `event` and boolean fields. An
empty channel list may create incidents but is not acceptable before scheduling.

Development evidence on 2026-07-16:

- Foundation apply created only the retention service account, Cloud SQL IAM
  user, and its logging, metrics, client, and instance-user grants (`6 added,
  0 changed, 0 destroyed`).
- The data apply used migrator manifest digest
  `sha256:4137a24dc9eaa09595b0732983a3853985fa37d946a56552def09f2a372a5b09`,
  created the dormant retention job, and updated the two existing operator jobs
  in place (`1 added, 2 changed, 0 destroyed`).
- Migration execution `attune-development-database-migrate-9bzpz` applied
  exactly migration 0028 and verified all 33 tenant tables plus the exact
  function-owner privilege policy.
- Manual execution `attune-development-protocol-retention-nvlk6` succeeded with
  zero OAuth, setup, session, and provider-event deletions and logged no
  identifiers or content.
- Verification execution `attune-development-database-migrate-8zdz7` applied
  zero migrations and passed the same boundary verifier. The foundation and
  data plans were both empty afterward.

This proves deployment, IAM login, the fixed function boundary, empty-run
behavior, and drift convergence. It does not replace the real-PostgreSQL
synthetic deletion/audit regression and does not satisfy the remaining live
synthetic-delete, alerting, or scheduling gates.

## Production gates

Before this job or schema is promoted beyond development:

1. build in a dedicated, non-developer release identity with dependency lock
   and verifiable provenance;
2. generate an SBOM, scan dependencies and the image, sign the digest, and
   enforce admission policy;
3. grant job execution only to a just-in-time release identity, without runtime
   argument overrides, with two-person approval;
4. rehearse forward migration, restore, and compensating rollback in staging;
5. run the isolation suite against staging through each real runtime IAM role;
6. verify backups, deletion suppression, and audit-chain export; and
7. retain migration plan, digest, execution, verifier output, and reviewer
   evidence in the change record.

Customer data remains prohibited until broker-mediated provider authorization,
identity-link, verified-ingress, capability-gateway, export/deletion, and
assurance gates pass.
