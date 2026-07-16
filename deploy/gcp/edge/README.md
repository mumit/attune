# GCP hosted edge

This independent Terraform root creates the locked public HTTPS boundary for
the hosted control plane. With identity disabled, the service exposes only
`GET /healthz` and an unavailable root. Identity sign-in can be activated
independently. Google Workspace connector consent has a second default-off gate
that atomically creates a principal-bound transaction and connects the
credential-free callback scrubber to the private OAuth exchange. Customer
traffic remains unauthorized until the applicable gates and evidence pass.

The control-plane image contains a separately gated Identity Platform sign-in
page, verifier, and opaque session API. `enable_identity_sign_in = false` omits
every identity, asset, and session route from Cloud Armor and sets the
application flag false. Enabling it also requires
`identity_provider_ready = true` and the public restricted browser API key.
That attestation records an external review but does not replace the tests in
`docs/identity-platform.md`.

The edge uses a reserved global IPv4 address, global external Application Load
Balancer, Google-managed certificate, TLS 1.2+ policy, serverless NEG, and Cloud
Armor. Cloud Run accepts external traffic only from Cloud Load Balancing and
its default `run.app` URI is disabled. The Cloud Run invoker IAM check is
disabled because the load balancer cannot mint a Cloud Run identity token. This
also avoids an `allUsers` IAM grant, which domain-restricted-sharing policies
reject. Disabling the check is safe only in combination with both ingress
restrictions and the disabled default URI.

The locked shell policy permits only `/` and `/healthz` on the exact configured
host. When staged identity is enabled, a tighter rule adds only the public
configuration, two fixed assets, and exact session paths; the application
enforces the allowed HTTP methods and returns 405 for every other method. The browser bundle
pins Firebase Auth and its build tool in `package-lock.json`; the image rebuilds
the bundle in a digest-pinned Node stage and serves scripts only from Attune.
A distinct policy permits only `GET /oauth/google/callback` with a tighter
source-IP rate. The callback backend has load-balancer logging disabled, and a
protected project exclusion drops both Cloud Run platform request logs and
Cloud Armor/load-balancer request logs by the dedicated service/backend resource
identities. Disabling backend logging alone is insufficient because Cloud Armor
can still emit `requests` entries. The exclusion does not match on or inspect
the credential-bearing URL. While connector OAuth is off, the scrubber parses
no OAuth fields. When its separate gate is on, it bounds and normalizes only
code, state, and its callback-only binding cookie, then hands them to the
private exchange using the callback workload identity. It has no access-log,
database, secret, KMS, queue, or provider authority and redirects to a
credential-free result URL with HTTP 303. The foundation's immutable sink
exports Cloud Audit logs only.

When the runtime's reviewed Workspace verification gate is active, two
additional bounded paths are exposed: CSRF-protected `POST
/v1/connectors/google/test` and session-bound `GET
/v1/connectors/google/tests/JOB_UUID`. The control plane derives connector and
capability authority from its session and canonical database records, sends
only an opaque intent to the private dispatch broker, and returns no mailbox,
Calendar, or provider data. The runtime root must be applied first so the edge
consumes its authoritative gate and broker output.

While Workspace OAuth is active, the edge also permits only the exact
`DELETE /v1/connectors/google` path at a bounded rate. The application requires
same-origin and CSRF-bound session authorization plus the exact JSON
confirmation `{"confirmation":"disconnect"}`. It derives all connector
authority server-side and sends only a one-use revoke intent to the runtime's
private secret broker. The control-plane service receives the broker's private
URI and audience from runtime remote state; neither value grants access without
its exact workload identity. Applying the runtime root before the edge is
therefore also required for disconnection.

Development evidence on 2026-07-15 exercised the authenticated destructive
confirmation, durable disconnected state after reload, a fresh Google consent
exchange, and verified connected state after a second reload. Cloud Armor
priority `883` accepted only the exact disconnect path after global policy
convergence; an unauthenticated request reached the application and failed with
401 rather than the policy's default deny.

Hosted onboarding has its own default-off `enable_hosted_onboarding` gate. When
enabled, Cloud Armor priority `884` exposes only `GET /v1/onboarding` and
CSRF-protected empty-body `POST /v1/onboarding/start`; application methods and
session authorization remain authoritative. The browser receives no tenant,
principal, connector, provider, or resource identifiers. Apply migration
`0018_hosted_onboarding.sql` before activating this edge gate.

Development activation on 2026-07-16 UTC followed that order. Cloud Armor
priority `884` first propagated to the exact two paths; an unauthenticated read
then reached the application and failed with 401. A signed-session read
returned 200, the CSRF-protected empty-body start returned 201, and a reload
recovered the persisted state with the already verified Workspace step derived
as `validated`. The post-apply edge plan was empty.

Hosted policy review has an independent default-off `enable_hosted_policy`
gate. It requires hosted onboarding, injects only the private audit-writer URL,
and adds Cloud Armor priority `885` for exact GET
`/v1/onboarding/policy` and POST `/v1/onboarding/policy/confirm`. The
application remains authoritative for method, session, same-origin, CSRF, empty
body, and ten-minute recent-authentication checks. Apply and verify migration
`0019_hosted_read_only_policy.sql` before enabling this gate; do not infer
activation from a successful Terraform apply.

Development rollout on 2026-07-16 UTC first deployed control-plane digest
`sha256:ba9db1696aa534c206be9294caee8c1a821a40da5c5600a92ea753cf0738402e`
with this gate explicitly false. After migration 0019 passed, a second saved
plan enabled only the audit-writer binding and Cloud Armor priority `885`.
After global policy convergence, an unauthenticated review received 401 from
the application and the signed-in owner saw the fixed R0 review. The enable
button was deliberately not invoked as deployment evidence; policy activation
remains a distinct owner ceremony. The final edge plan was empty.

The owner completed that separate ceremony later on 2026-07-16 UTC. A stale
session was refused with 409; after fresh authentication, the confirmation
returned 200 through priority `885`, with both mandatory private audit-writer
requests returning 200. This is activation evidence for the development
tenant, not permission to auto-confirm policy during future deployments.

Hosted channel preferences use the independent default-off
`enable_hosted_channels` gate. It requires hosted onboarding, shares only the
private audit-writer URL, and adds Cloud Armor priority `886` for exact GET/PUT
`/v1/onboarding/channels`. Apply and verify migration 0020 first. Enabling the
gate exposes preference review/configuration only; it does not install Slack or
Google Chat, choose a destination, enable ingress, send a test, or validate the
channel step.

Development rollout on 2026-07-16 UTC first deployed control-plane digest
`sha256:a955271a12d185a734b0d130f54cff659f7e6d34862007fb3535fa7e7685d2af`
with the channel gate explicitly false. After migration 0020 passed, a second
saved plan enabled only the application gate and Cloud Armor priority `886`.
The apply changed two resources in place, created or destroyed none, and the
final edge plan was empty. After global policy convergence, an unauthenticated
request reached the application and returned 401. No owner preference was
submitted by deployment automation.

Hosted channel installation state has a further independent default-off
`enable_hosted_channel_setup` gate. It adds only the exact authenticated GET
`/v1/onboarding/channel-installations` and POST
`/v1/onboarding/channel-installations/google-chat/link` paths at Cloud Armor
priority `887`. Apply and verify migration 0021 first. Enabling this gate may
create a hash-only, expiring Google Chat link attempt; it does not enable a
provider callback, consume the link, create a destination, store a provider
credential, or send a test. Keep it false until the private channel broker and
verified Google Chat ingress have passed their separate activation gates.

Development rollout on 2026-07-16 UTC deployed control-plane digest
`sha256:7a084cd8776ce1b2130bf5d55287ee19f50ac8491e5ba2c23144699ae0176089`
with this setup gate explicitly false after migration 0021 passed. The saved
edge plan changed only the control-plane image and added the false environment
setting; it added or destroyed no resources. Health returned 200, the exact
installation-status path remained denied by Cloud Armor with 403, and the
following edge plan was empty. Priority `887` was not activated.

Verified Google Chat events use a separate two-stage edge gate. With
`deploy_google_chat_ingress=true` and `enable_google_chat_ingress=false`,
Terraform may create the no-default-URI Cloud Run service, serverless NEG,
backend, and default-deny Cloud Armor policy, but the public URL map contains
no Chat path. The service verifies Google's bearer token against the exact
`https://HOST/v1/provider/google-chat/events` audience and requires
`chat@system.gserviceaccount.com`; only a canonical human `MESSAGE` whose
top-level and message-level spaces agree on `DIRECT_MESSAGE` may reach the
private channel broker. Request logging is disabled on the dedicated backend.

Set `enable_google_chat_ingress=true` only after migration 0022, private broker
activation, a platform-owned Chat app configured with that exact endpoint and
audience, signature/audience negative tests, replay tests, paging, and an
explicit `google_chat_provider_ready=true` attestation. The saved activation
plan must add only the exact URL-map path and Cloud Armor allow rule. Never
route the provider path to the ordinary control-plane backend.

These controls establish URL non-retention; they do not by themselves activate
OAuth. The server-side transaction, PKCE exchange, callback-to-exchange
workload identity, and private broker handoff are implemented. A separate
reviewed Workspace OAuth client, broker-only secret version, exact redirect,
content-free live evidence, and adversarial tests remain activation gates.

## Build and apply

Build Linux/amd64, push, and resolve the immutable digest:

```bash
export PROJECT_ID="your-development-project"
export REGION="northamerica-northeast1"
export REPOSITORY="attune-development"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/attune-control-plane"
export CALLBACK_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/attune-oauth-callback"

docker buildx build --platform=linux/amd64 --push \
  -f deploy/control-plane/Dockerfile -t "${IMAGE}:locked-edge-v1" .
gcloud artifacts docker images describe "${IMAGE}:locked-edge-v1" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.fully_qualified_digest)'
docker buildx build --platform=linux/amd64 --push \
  -f deploy/oauth-callback/Dockerfile -t "${CALLBACK_IMAGE}:dormant-v1" .
gcloud artifacts docker images describe "${CALLBACK_IMAGE}:dormant-v1" \
  --project="$PROJECT_ID" \
  --format='value(image_summary.fully_qualified_digest)'
```

Copy the examples to ignored local files, set the reviewed image digest and
exact hostname, then use a saved plan:

```bash
cd deploy/gcp/edge
cp backend.hcl.example backend.hcl
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config=backend.hcl
terraform fmt -check
terraform validate
terraform plan -out=edge.tfplan
terraform show edge.tfplan
terraform apply edge.tfplan
terraform output -json edge
```

`runtime_state_prefix` must identify the already-applied runtime root because
the callback reads the private OAuth exchange URI and audience from remote
state. This is non-secret routing metadata.

## Workspace OAuth activation

Leave these values at their defaults during image and route rollout:

```hcl
enable_google_workspace_oauth = false
google_oauth_provider_ready    = false
google_oauth_client_id         = ""
```

First deploy the final control-plane and callback images with OAuth off and
repeat the synthetic callback non-retention test below after global route
convergence. Then create the separate Workspace web client, register only the
exact callback output, add its downloaded JSON as a Secret Manager version
using `docs/identity-platform.md`, and independently compare its public client
ID. Only after the complete evidence review should a saved edge plan set:

```hcl
enable_google_workspace_oauth = true
google_oauth_provider_ready    = true
google_oauth_client_id         = "PUBLIC_WORKSPACE_CLIENT_ID.apps.googleusercontent.com"
```

Terraform never receives the client secret. Preconditions require identity
sign-in to be enabled, and Cloud Armor exposes the connector-start and
connector-disconnect routes only while this separate gate is true. Enabling the gate also keeps one control-plane
and one callback instance warm. Set `oauth_min_instance_count = 1` in the
runtime root before this apply so the complete synchronous consent chain does
not cold-start serially.

Create exactly the output `A` record at the authoritative DNS provider. The
Google-managed certificate remains `PROVISIONING` until DNS points at the
reserved address and can take time to become active. Do not create an OAuth
client until HTTPS health, direct-URL denial, exact-host denial, Cloud Armor,
and callback-log non-retention have passed.

After applying, send a synthetic callback containing unmistakable fake values,
then prove the 303 strips them and neither request-log plane retained them:

```bash
curl -sS -D - -o /dev/null \
  'https://dev.attune.example.com/oauth/google/callback?code=ATTUNE_FAKE_CODE&state=ATTUNE_FAKE_STATE'
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="attune-development-oauth-callback" AND log_id("run.googleapis.com/requests")' \
  --freshness=15m --limit=10
gcloud logging read \
  'resource.type="http_load_balancer" AND resource.labels.backend_service_name="attune-development-oauth-callback"' \
  --freshness=15m --limit=10
```

Both reads must be empty. Also search all project logs for the two fake values.
Never use a real authorization code for this test.

Do not put even a synthetic marker into the `gcloud logging read` filter:
Data Access audit logs record that filter, making the search self-retaining.
Instead record a narrow start/end time, fetch that whole log window using only
the timestamps in the server-side filter, and search the returned JSON locally.
The callback marker must be absent. Real authorization codes, tokens, and state
values must never appear in an operator command or log query.

URL-map changes converge asynchronously across the global data plane. During
that interval the old shell backend can still deny—and log—the callback path.
Therefore the OAuth client MUST NOT exist or list this redirect URI until
query-free probes return 303 after a documented soak, multi-location synthetic
markers return 303, and every marker is absent from all project logs after the
normal ingestion window. This ordering prevents real authorization codes from
arriving while an older logged route is still serving.
