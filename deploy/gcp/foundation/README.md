# GCP hosted foundation

This Terraform root creates the security substrate for the operated Attune
service. It does **not** deploy the current single-principal runtime and does
not admit customer data. Runtime services arrive only after tenant-aware
storage, the secret broker, ingress verification, and hosted migrations exist.

The configuration creates:

- a private VPC and private-service connection, plus exact private DNS zones
  for four fixed Google provider/platform hosts;
- CMEK-protected Cloud SQL for PostgreSQL with private IP, IAM database
  authentication, point-in-time recovery, deletion protection, and the
  standard Enterprise edition (development may use a shared-core tier);
- separate control-plane, credential-free OAuth-callback, ingress, worker,
  secret-broker, dispatch-broker, task-delivery, audit-writer, and initial
  identity-provisioning service accounts;
- Cloud Tasks queues and a Gmail-authorized Pub/Sub topic;
- CMEK-backed Secret Manager containers for static platform credentials,
  without secret versions, plus a separate connector-vault KMS key;
- Artifact Registry; and
- a versioned, CMEK-protected audit bucket with a retention policy, a
  Cloud-Audit-only log sink, and Data Access audit logging.

Only the dispatch-broker identity can enqueue either Cloud Tasks queue or use
the distinct task-delivery identity. Control-plane, ingress, and worker
identities must persist canonical dispatch state and invoke the broker; they
cannot create tasks or choose task targets directly. Queue routing is fixed
when the corresponding runtime service is deployed, before customer traffic.

The jobs queue is created with no HTTP target while the worker is absent. After
the private worker is deployed, set `jobs_worker_target_host` to the hostname
from the reviewed worker URI and `jobs_worker_oidc_audience` to the exact custom
audience from the runtime output, then reapply this root. Both values are
required together. Terraform forces HTTPS, POST, the exact
`/v1/tasks/dispatch` path, the task-delivery service account, its OIDC audience,
and `ALWAYS` URI enforcement. This staged second foundation apply is required
before the dispatch broker may be deployed; it is not an operator exception.

## Preconditions

Use a new billing-enabled project for each environment. Authenticate with an
interactive administrator session or Workload Identity Federation; do not
download a service-account key. The applying identity needs enough temporary
authority to enable APIs, create the declared resources, and bind the narrow
IAM roles. It must not be a runtime identity.

Terraform and both Google providers are version constrained in `versions.tf`.
Commit the generated `.terraform.lock.hcl` after initialization so provider
checksums are reviewed with the change.

The root uses a partial GCS backend. Bootstrap one private, region-local,
uniform-access, versioned state bucket outside this root, then copy
`backend.hcl.example` to the ignored `backend.hcl` and set its bucket name.
Terraform state can contain sensitive infrastructure metadata; never commit it
or keep the authoritative state only on an operator workstation.

## Review and apply

```bash
cd deploy/gcp/foundation
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
# Edit only non-secret project, region, environment, and label values.
terraform init -backend-config=backend.hcl
terraform fmt -check
terraform validate
terraform plan -out=foundation.tfplan
terraform show foundation.tfplan
terraform apply foundation.tfplan
```

Do not put OAuth, Slack, model, database, or encryption secrets in `.tfvars`.
Terraform creates empty Secret Manager resources for platform credentials and
the one-time identity-bootstrap handoff only. The secret broker is explicitly
denied access to the bootstrap container; only the initial identity provisioner
can read it.
Tenant connector credentials are later envelope-encrypted by the secret broker
with the dedicated connector KMS key and stored as tenant-bound ciphertext in
PostgreSQL; they do not enter Terraform or Cloud Run environment variables.

Terraform enables the Identity Toolkit API but deliberately does not manage the
Google Identity Platform provider resource. That resource requires the sign-in
OAuth client secret and would persist it in Terraform state. Initialize Identity
Platform and configure its Google provider through the documented secret-aware
operator ceremony instead. The sign-in OAuth client is distinct from the
Workspace connector client stored in the broker-only platform secret.

Production requires `lock_audit_retention = true`. Bucket Lock is permanent:
first validate retention, export, legal-hold, deletion, and incident procedures
in staging, then have two reviewers approve the production plan.

The retained sink deliberately exports only Cloud Audit activity, data-access,
policy, and system-event logs. It must never export all application or request
logs: OAuth authorization callbacks carry short-lived codes in their query
string, and an immutable audit bucket is not an acceptable destination for
those values. Canonical Attune security events use the separate content-free,
hash-chained audit path.

The OAuth-callback identity intentionally receives no project log-writer,
database, Secret Manager, KMS, queue, or provider permission. The private OAuth
exchange also receives no project log-writer because it transiently handles an
authorization code. Both retain content-free Monitoring permission. The
exchange adds only Cloud SQL client/login authority; its database role is
function-only and has no direct table access.

Cloud SQL and the audit bucket deliberately have deletion protection. Teardown
is an exceptional, separately reviewed workflow; `terraform destroy` is not the
data-deletion procedure.

## Fixed Google API egress

The application subnet has Private Google Access enabled and no Cloud NAT.
Terraform creates private zones for exactly `oauth2.googleapis.com`,
`www.googleapis.com`, `gmail.googleapis.com`, and
`chat.googleapis.com`, and `secretmanager.googleapis.com`, with apex A records pointing to Google's
`private.googleapis.com` VIP (`199.36.153.8/30`). It deliberately creates no
wildcard `*.googleapis.com` zone. The broker uses these names only for the
compiled-in token exchange, Google signing-certificate fetch, Gmail operation,
and platform OAuth-client-secret read respectively. Arbitrary internet
destinations continue to fail closed.

The foundation also enables `gmail.googleapis.com`, `chat.googleapis.com`, and
`calendar-json.googleapis.com` declaratively. Private DNS
reachability without project API activation is insufficient: Google accepts
OAuth consent and refresh-token exchange but rejects a provider read when its
project API is disabled. Keep API activation and exact private DNS in the same
reviewed foundation plan. Runtime authorization remains separate: the fixed
composite Gmail/Calendar verification route is disabled by default and requires
its own reviewed release gate.

The private VIP can serve other Google APIs, so DNS is one layer rather than
the complete authorization boundary. Exact broker URLs and paths, disabled
redirects and ambient proxies, TLS hostname verification, route-specific IAM,
canonical capabilities, and minimized responses remain mandatory. Adding a
provider hostname requires a reviewed Terraform change, a fixed application
operation, negative tests, and a credential-free egress probe. Do not add Cloud
NAT or broaden the zone to `googleapis.com` as an incident workaround.
The address range and routing model are defined in Google's
[Private Google Access documentation](https://cloud.google.com/vpc/docs/configure-private-google-access).

## Gmail publisher and domain restrictions

Gmail push requires the Google-managed
`gmail-api-push@system.gserviceaccount.com` principal to have exactly
`roles/pubsub.publisher` on the provider-events topic. An organization using
the legacy Domain Restricted Sharing constraint
(`constraints/iam.allowedPolicyMemberDomains`) may reject that one binding
because the principal is outside the allowed customer domain. Do not work
around this by making the topic public, broadening project IAM, or leaving the
constraint disabled.

Terraform normally creates this binding; do not also run `gcloud pubsub` during
a routine plan or apply. Use the following runbook only when an apply fails with
`FAILED_PRECONDITION: One or more users named in the policy do not belong to a
permitted customer`, and only after a security reviewer approves the temporary
exception. It applies to a project inheriting the legacy constraint with no
project-level override. If the project already has an explicit policy, stop and
have the organization-policy owner preserve and restore that policy instead of
using this procedure.

First obtain time-bounded `roles/orgpolicy.policyAdmin` through the normal
privileged-access process. The same operator also needs permission to change
the topic IAM policy. Record the current state and confirm there is no explicit
project override:

```bash
set -euo pipefail
export PROJECT_ID="your-project-id"
export TOPIC="$(terraform output -json foundation \
  | jq -r '.provider_events_topic | split("/")[-1]')"
export CONSTRAINT="iam.allowedPolicyMemberDomains"
export EVIDENCE_DIR="$(mktemp -d)"

gcloud org-policies describe "$CONSTRAINT" \
  --project="$PROJECT_ID" --effective --format=yaml \
  >"$EVIDENCE_DIR/domain-policy-effective-before.yaml"
gcloud pubsub topics get-iam-policy "$TOPIC" \
  --project="$PROJECT_ID" --format=yaml \
  >"$EVIDENCE_DIR/topic-iam-before.yaml"

if gcloud org-policies describe "$CONSTRAINT" \
  --project="$PROJECT_ID" --format=yaml \
  >"$EVIDENCE_DIR/domain-policy-project-before.yaml" 2>/dev/null; then
  echo "STOP: this project already has an explicit policy override" >&2
  exit 1
fi
```

Keep the same shell open for the rest of the procedure. Define restoration and
install its trap *before* changing policy, then create the project-scoped
`allowAll` policy in the temporary evidence directory. This deliberately
weakens only this project, and only for the time needed to admit the exact
Google-managed principal:

```bash
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" \
  --format='value(projectNumber)')"

cat >"$EVIDENCE_DIR/domain-policy-temporary.yaml" <<EOF
name: projects/${PROJECT_NUMBER}/policies/${CONSTRAINT}
spec:
  rules:
  - allowAll: true
EOF

OVERRIDE_APPLIED=0
restore_domain_policy() {
  if [[ "$OVERRIDE_APPLIED" == 1 ]]; then
    gcloud org-policies delete "$CONSTRAINT" \
      --project="$PROJECT_ID" --quiet
    OVERRIDE_APPLIED=0
  fi
}
trap restore_domain_policy EXIT HUP INT TERM

OVERRIDE_APPLIED=1
gcloud org-policies set-policy \
  "$EVIDENCE_DIR/domain-policy-temporary.yaml"
gcloud org-policies describe "$CONSTRAINT" \
  --project="$PROJECT_ID" --effective --format=yaml
```

Do not continue until the effective output shows `allowAll: true`. Add only the
required topic-level role, then immediately restore inheritance:

```bash
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

restore_domain_policy
trap - EXIT HUP INT TERM
```

Wait for policy propagation, then verify that the effective domain restriction
matches the saved policy, the narrow binding exists, and neither public
principal is present. Save these outputs with the reviewed change record; they
contain policy metadata but must not contain credentials or tokens.

```bash
gcloud org-policies describe "$CONSTRAINT" \
  --project="$PROJECT_ID" --effective --format=yaml \
  | tee "$EVIDENCE_DIR/domain-policy-effective-after.yaml"
diff -u "$EVIDENCE_DIR/domain-policy-effective-before.yaml" \
  "$EVIDENCE_DIR/domain-policy-effective-after.yaml"
gcloud pubsub topics get-iam-policy "$TOPIC" \
  --project="$PROJECT_ID" --format=json \
  | tee "$EVIDENCE_DIR/topic-iam-after.json" \
  | jq -e '
      ([.bindings[]
        | select(.role == "roles/pubsub.publisher")
        | .members[]?]
       | sort)
        == ["serviceAccount:gmail-api-push@system.gserviceaccount.com"]
      and
      ([.bindings[].members[]?
        | select(. == "allUsers" or . == "allAuthenticatedUsers")]
       | length) == 0'

terraform plan -detailed-exitcode
```

The final Terraform command must return exit code 0. Revoke the temporary
organization-policy role after verification. Repeat this procedure only for a
new topic/project where the legacy constraint blocks first admission—not for
normal updates. For a durable organization-wide solution, security
administrators should evaluate migration to the managed Domain Restricted
Sharing constraint, which supports specific principal exceptions. That
migration is an organization security change and is intentionally outside this
Terraform root.

The Pub/Sub binding remains managed by Terraform after it is admitted. A
subsequent zero-drift plan proves that the live binding matches the reviewed
configuration; it does not authorize weakening organization policy again.
Google documents both the required principal in the
[Gmail push guide](https://developers.google.com/workspace/gmail/api/guides/push)
and the legacy constraint's
[force-account-access sequence](https://cloud.google.com/resource-manager/docs/organization-policy/restricting-domains#forcing_account_access).

## Not created yet

- public load balancer, Cloud Armor, public DNS, or certificates;
- Cloud Run control-plane, ingress, worker, or secret-broker services;
- database schema, `vector` extension, row-security policies, or tenant data;
- secret versions, OAuth clients, channel applications, or customer links;
- a Pub/Sub push subscription, because its authenticated endpoint does not yet
  exist; or
- production alerts, SLOs, VPC Service Controls, and organization policies.

Those omissions are launch gates, not optional hardening. See
[`docs/hosted-gcp.md`](../../../docs/hosted-gcp.md).
