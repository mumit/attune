# GCP hosted runtime boundaries

This independent Terraform root deploys private hosted services after the
foundation and database migrations pass. It currently deploys the audit writer,
credential-mutation secret broker, and a deterministic worker with only the
content-free `platform.smoke` route. It also deploys the dispatch broker after
the jobs queue has its reviewed fixed routing override.

## Audit-writer boundary

The service accepts exactly one canonical audit-intent UUID. It never accepts a
tenant, actor, action, outcome, metadata, or event body over HTTP. Control-plane,
worker, secret-broker, and dispatch-broker identities may invoke the private
service with Google IAM; no public principal may invoke it.

The audit-writer identity has no direct table privilege and cannot call the old
free-form append function. Its only database authority is the
`write_audit_intent(uuid)` function. That function resolves the durable intent,
sets its tenant context, appends one hash-chained event, and marks the intent
written in one transaction. Replays return the existing event. Unknown IDs do
nothing. Audit failures return a generic error and callers must fail closed for
security-sensitive work.

## Secret-broker boundary

The secret broker accepts only an opaque credential-intent UUID, plus the
credential object for an install. Cloud Run IAM permits only the control-plane
service account to invoke it. A stable custom audience is checked again inside
the application, and caller-supplied tenant or connector authority is rejected.
The broker alone can use the connector credential KMS key and its narrow
database functions. It requires the private audit writer before and after a
mutation and fails closed on ambiguous results.

## Deterministic worker boundary

The worker accepts only the minimal versioned Cloud Tasks envelope at
`/v1/tasks/dispatch`. It verifies the exact task-delivery service account and
custom audience, then atomically rebinds tenant, job kind, and capability to
canonical PostgreSQL state. The initial `platform.smoke` executor accepts only
`{"probe":"dispatch-v1"}` and has no provider, model, network, secret, or
customer-content effect. Required audit failure or executor ambiguity moves the
job to reconciliation rather than retrying an uncertain effect.

## Dispatch-broker boundary

The broker accepts only an opaque dispatch-intent UUID from the exact control,
ingress, or worker identity. It resolves tenant, purpose, capability, delivery
ID, and task name through its narrow database functions, requires durable audit
before task creation, and can enqueue only the foundation jobs queue. Runtime
configuration contains only the `platform.smoke` route, while the queue itself
independently forces the worker target and delivery identity.

Services that invoke another internal Cloud Run service use Direct VPC
`ALL_TRAFFIC` egress. This is required because an internal service is addressed
through its HTTPS `run.app` origin: `PRIVATE_RANGES_ONLY` would bypass the VPC
and fail the callee's internal-ingress check. The audit writer and database
migrator need only private Cloud SQL and retain `PRIVATE_RANGES_ONLY`. The
development VPC has no Cloud NAT, so arbitrary internet egress remains denied.

## Development deployment

Apply `deploy/gcp/data` and successfully execute its migrator before deploying
this root. Build Linux/amd64 and deploy only by immutable digest:

```bash
export PROJECT_ID="your-development-project"
export REGION="northamerica-northeast1"
export REPOSITORY="attune-development"
export AUDIT_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/attune-audit-writer"
export BROKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/attune-secret-broker"
export WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/attune-worker"
export DISPATCH_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/attune-dispatch-broker"

docker buildx build --platform=linux/amd64 --push \
  -f deploy/audit-writer/Dockerfile -t "${AUDIT_IMAGE}:audit-writer-v1" .
docker buildx build --platform=linux/amd64 --push \
  -f deploy/secret-broker/Dockerfile -t "${BROKER_IMAGE}:secret-broker-v1" .
docker buildx build --platform=linux/amd64 --push \
  -f deploy/worker/Dockerfile -t "${WORKER_IMAGE}:worker-v1" .
docker buildx build --platform=linux/amd64 --push \
  -f deploy/dispatch-broker/Dockerfile -t "${DISPATCH_IMAGE}:dispatch-v1" .
gcloud artifacts docker images describe "${AUDIT_IMAGE}:audit-writer-v1" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.fully_qualified_digest)'
gcloud artifacts docker images describe "${BROKER_IMAGE}:secret-broker-v1" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.fully_qualified_digest)'
gcloud artifacts docker images describe "${WORKER_IMAGE}:worker-v1" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.fully_qualified_digest)'
gcloud artifacts docker images describe "${DISPATCH_IMAGE}:dispatch-v1" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.fully_qualified_digest)'
```

Copy the examples to ignored local files, put the returned digest in
`terraform.tfvars`, initialize the isolated state, and review a saved plan:

```bash
cd deploy/gcp/runtime
cp backend.hcl.example backend.hcl
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config=backend.hcl
terraform validate
terraform plan -out=runtime.tfplan
terraform apply runtime.tfplan
```

Verify that all services have internal ingress and reject unauthenticated
invocation. The audit-writer IAM policy must list only its four expected
workloads; the broker policy must list only the control plane. Verify the broker
custom audience; the worker policy must list only the task-delivery identity.
Verify that no runtime service account has a user-managed key. Do not place
tenant data, tokens, or credentials in Terraform variables, state, labels,
probes, or deployment logs.

Worker deployment does not enable delivery. Copy the worker output's URI
hostname and custom audience into the two nullable jobs-worker variables in the
foundation root, review the queue-only in-place plan, and apply it. Confirm the
queue override forces HTTPS, POST, `/v1/tasks/dispatch`, the task-delivery
identity, and the exact audience before adding the dispatch broker to this
runtime root. `enable_dispatch_broker` defaults to `false`; change it to `true`
only after the foundation queue-only apply is complete and a new saved runtime
plan shows the broker plus exactly three producer invoker grants.

For a release candidate, validate the live connector key using the exact
digest already reviewed in `terraform.tfvars`. This creates no tenant or
credential record: it generates a random 256-bit value in memory, verifies a
KMS wrap/unwrap with CRC32C integrity, clears the plaintext buffers, and prints
only pass/fail. The job is intentionally ephemeral so the KMS-capable identity
does not gain another standing execution surface:

```bash
# Use the secret_broker.image value from `terraform output -json` for IMAGE.
export IMAGE="northamerica-northeast1-docker.pkg.dev/PROJECT/REPOSITORY/attune-secret-broker@sha256:DIGEST"
export BROKER_SA="attune-ENVIRONMENT-secrets@PROJECT.iam.gserviceaccount.com"
export KMS_KEY="projects/PROJECT/locations/REGION/keyRings/attune-ENVIRONMENT/cryptoKeys/connector-credentials"

gcloud run jobs create "attune-ENVIRONMENT-kms-smoke" \
  --project="$PROJECT_ID" --region="$REGION" --image="$IMAGE" \
  --service-account="$BROKER_SA" \
  --set-env-vars="ATTUNE_CONNECTOR_KMS_KEY=$KMS_KEY" \
  --command=python --args=-m,attune.hosted.kms_smoke \
  --tasks=1 --max-retries=0 --task-timeout=120s --execute-now --wait
gcloud run jobs delete "attune-ENVIRONMENT-kms-smoke" \
  --project="$PROJECT_ID" --region="$REGION" --quiet
```

Do not retain the job, replace the random input with a real credential, or put
secret values in command arguments or environment variables.

After the dispatch broker is enabled, validate the complete synthetic path
from control-plane canonical state through broker audit, Cloud Tasks, queue
override, worker OIDC verification, atomic claim, deterministic execution, and
worker audit. The validation uses a reserved development-only tenant containing
no customer/provider content. Run the immutable dispatch image under the
control-plane identity with Direct VPC egress, then delete the job:

```bash
gcloud run jobs create "attune-ENVIRONMENT-dispatch-smoke" \
  --project="$PROJECT_ID" --region="$REGION" --image="$DISPATCH_IMAGE_DIGEST" \
  --service-account="$CONTROL_PLANE_SA" \
  --set-env-vars="ATTUNE_CLOUD_SQL_INSTANCE=$CLOUD_SQL_INSTANCE,ATTUNE_DB_NAME=attune,ATTUNE_DB_USER=$CONTROL_PLANE_DB_USER,ATTUNE_DISPATCH_BROKER_URL=$DISPATCH_BROKER_URL,ATTUNE_DISPATCH_BROKER_AUDIENCE=$DISPATCH_BROKER_AUDIENCE,ATTUNE_REGION=$REGION" \
  --network="$VPC_NETWORK" --subnet="$VPC_SUBNETWORK" \
  --vpc-egress=all-traffic \
  --command=python --args=-m,attune.hosted.dispatch_smoke \
  --tasks=1 --max-retries=0 --task-timeout=120s --execute-now --wait
gcloud run jobs delete "attune-ENVIRONMENT-dispatch-smoke" \
  --project="$PROJECT_ID" --region="$REGION" --quiet
```

Success prints only `PASS brokered dispatch round trip`. Retain the content-free
execution/audit evidence, not the temporary job.
