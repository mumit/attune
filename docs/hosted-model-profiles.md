# Hosted model profile and usage metering

Hosted Attune lets the owner choose among a fixed set of OPERATOR-DEFINED
model profiles (`standard`, `premium`) for their own tenant's classify,
converse, and embed model calls, and lets them see the resulting usage. This
closes `docs/future-state.md` Phase 6's "per-tenant model configuration and
usage metering" bullet (hosted-review gaps #1/#2). Implemented and tested,
not deployed.

## Owner experience

After sign-in, the setup page's model-profile section is offered
optimistically, the same "no pre-session availability signal" pattern the
account-deletion section already uses: a 404 from `GET /v1/model-profile`
is the honest signal that `ATTUNE_ENABLE_TENANT_MODEL_PROFILES` is off. When
the gate is on, the owner sees their current profile (`standard` by
default) in a two-option selector and a `Save` button, plus a short summary
of their model usage over the last 30 days.

Choosing a profile never changes who can act on the tenant's behalf, what a
connector can reach, or what autonomy a grant confers -- it only selects
among operator-approved model routes for future calls. Saving is therefore
an ordinary bounded preference: the same session/CSRF bar as
`POST /v1/conversation/messages`, not the ten-minute recent-authentication
window `PUT /v1/onboarding/channels` reserves for a channel-authority
change.

## Security boundary

A tenant's profile choice is one of a fixed, reviewed vocabulary
(`standard`, `premium`) and never carries a base URL, API key, or model
string. The gateway's own configuration is the only place a profile name
ever resolves to a concrete model id per task; extending the vocabulary is
a reviewed migration and code change, never data.

`GET`/`PUT /v1/model-profile` require an ordinary (not recently
re-authenticated) session, same-origin, and CSRF. The request has exactly
two fields: schema version 1 and a profile name from the fixed vocabulary.
Vocabulary is validated server-side, in the audited service and the
database function, not merely by trusting the browser's own `<select>`
options. A content-free `allowed`/`observed`/`failed` audit (schema_version
only -- never the profile name, matching every other bounded-preference
ceremony's own posture) surrounds the mutation.

`attune.tenant_model_preferences` is a forced-RLS, one-row-per-tenant table.
The ordinary control-plane role may only `SELECT` it; every mutation goes
through the SECURITY DEFINER `attune.set_tenant_model_profile`, owned by a
new memberless `attune_model_profile_executor`, which independently
rechecks the caller's session, principal, and tenant status before writing.
The worker also reads this table directly (an ordinary `SELECT`, the same
trust it already has for `hosted_channel_preferences`) to resolve which
profile to pass to the model gateway -- the model itself never chooses a
profile, and neither does a provider event or the principal's own message
content; only the executor's own DB read may populate the gateway
envelope's `profile` field.

The gateway request envelope's optional `profile` field is accepted only
when the receiving gateway process's OWN `ATTUNE_ENABLE_TENANT_MODEL_PROFILES`
gate is on -- independent of the worker's gate, mirroring how
`ATTUNE_ENABLE_HOSTED_BRIEF` already gates both the control plane and the
worker route/executor together rather than trusting one side alone. With
the gate off anywhere in the chain, the field is absent or refused, and
`HostedModelGateway`'s fixed model routing resolves byte-identically to the
pre-profile configuration -- pinned by a dedicated test. An unrecognized
profile name always fails closed to an error, never a silent default to
the fixed config.

## Metering

Every classify/converse/embed call the worker makes is optionally metered,
behind its own independent gate, `ATTUNE_ENABLE_MODEL_USAGE_METERING`. The
model gateway extracts token usage defensively from the upstream
OpenAI-compatible provider's own response (a malformed or absent `usage`
field degrades to `None`, never breaking the actual completion) and always
reports it (nullable) in its own versioned response envelope;
`ModelGatewayClient` parses that field strictly, since it is now a trusted,
internal, versioned contract between two of Attune's own services. The
conversation executor records one accumulate call per model call --
success or failure -- through `attune.accumulate_model_usage`, a SECURITY
DEFINER function owned by a new memberless `attune_usage_meter_executor`.
The worker holds no direct `UPDATE` grant on `attune.model_usage_daily`: a
bare grant would let a compromised or buggy worker rewrite its own billing
history, so the only exposed operation is an atomic "add one request, add
these bounded token counts, add this bounded failure count" upsert.

`attune.model_usage_daily` stores exactly one aggregate row per (tenant,
task, profile, UTC day): request count, input/output token counts as the
provider reported them, and a bounded failure count -- never prompt or
response text, never a per-message row. A metering write failure is logged
and never breaks the model call, the same dual-write posture every other
best-effort write in this codebase already has; a genuine model-call
failure is never swallowed by this path.

`GET /v1/usage` (ordinary session, no CSRF needed for a read) returns the
tenant's own bounded 30-day window of these aggregates -- content-free by
construction, since the underlying table never stored anything else. This
is the customer-facing half of metering; the operator-facing half
(aggregating counts toward an actual invoice) remains unbuilt future work.

## Deployment order

1. Apply and verify `0047_tenant_model_profiles.sql`.
2. Deploy the model gateway, control plane, and worker with both gates
   explicitly false.
3. Require an empty data plan and negative gate-off tests: the profile and
   usage routes both return 404, and the gateway refuses a `profile` field
   on the wire.
4. Enable `ATTUNE_ENABLE_TENANT_MODEL_PROFILES` on the model gateway,
   control plane, and worker together; configure the gateway's premium
   route environment variables (`ATTUNE_MODEL_PREMIUM_CLASSIFY`,
   `ATTUNE_MODEL_PREMIUM_CONVERSE`, `ATTUNE_MODEL_PREMIUM_EMBED`) and the
   exact Cloud Armor rule admitting `/v1/model-profile`.
5. Confirm the byte-identical gate-off model routing pin still holds for
   any tenant that has never set a preference, then have a freshly
   authenticated owner save a profile and confirm the audited round trip.
6. Enable `ATTUNE_ENABLE_MODEL_USAGE_METERING` on the worker and control
   plane independently; confirm a real model call accumulates a row and
   `GET /v1/usage` reflects it within the bounded window.

Cloud Run/Terraform wiring for the new environment variables and the
Cloud Armor rule for `/v1/model-profile` and `/v1/usage` remain separate
operator work, mirroring how `ATTUNE_ENABLE_HOSTED_BRIEF` shipped
implemented-and-tested well before its own deployment evidence
(`hosted-channels.md`'s "Development rollout evidence" section shows what
that evidence looks like once produced).
