# Hosted data boundary

This Terraform root deploys Attune's private, operator-executed PostgreSQL
migration job after the no-customer-data foundation exists. The job creates the
hosted schema and verifies its database controls. It does not deploy an
application, public endpoint, connector credential, tenant, or customer record.

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
- Cross-tenant `SECURITY DEFINER` functions are owned by three distinct
  memberless `NOLOGIN BYPASSRLS` roles for dispatch, audit writing, and vault
  mutation. No IAM or runtime login is a member. Each owner has only the table
  privileges required by its fixed functions; temporary migrator membership
  and schema-create authority are revoked inside the migration transaction.
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
  use, and revocation intents leased only through secret-broker functions.

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
unavailable. A live provider executor remains prohibited until its queue has
fixed target routing and least-privilege producers, the private audit writer
exists, and the executor passes deterministic capability and ambiguous-effect
review. Deploying a generic handler before those controls would turn an
identifier envelope into unintended authority. The private intent-only audit
writer, credential-mutation secret broker, dispatch broker, fixed jobs-queue
route, and content-free deterministic smoke worker are now deployed from
`deploy/gcp/runtime`. Provider capability executors and broker-mediated
provider operations remain required.

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
