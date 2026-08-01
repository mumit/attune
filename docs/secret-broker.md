# Hosted connector vault and secret broker

The secret broker is the only hosted identity permitted to use the connector
credential KMS key. Workloads receive opaque connector references, not stored
refresh tokens or encryption material.

## Cryptographic contract

Each credential version receives a new random 256-bit data-encryption key and
96-bit nonce. Credential JSON is encrypted locally with AES-256-GCM. The
authenticated associated data binds the ciphertext to its tenant, connector,
provider, format, and credential version, so moving any stored component to a
different record fails authentication. Cloud KMS wraps the DEK; plaintext DEKs
are never persisted and KMS CRC32C request/response integrity is verified.

The database will store ciphertext, nonce, wrapped DEK, exact KMS key resource,
format version, credential version, lifecycle status, and timestamps. It must
never store plaintext credentials. Rotation writes a new immutable version;
revocation disables use immediately and is independently auditable.

## Request boundary

The broker will not accept a caller-supplied tenant as authority. Tenant-scoped
workloads first create one-time, expiring credential intents under forced RLS.
The private broker accepts the opaque intent ID, resolves tenant, connector,
operation, and policy from canonical state, and atomically consumes the intent.
Credential installation may carry secret material only in the authenticated
request body for that one-time intent; secret values are excluded from logs,
errors, traces, audit metadata, environment variables, and Terraform state.

The deployed GCP boundary has two independent authentication checks. Cloud Run
IAM permits invocation only by the control-plane and worker service accounts,
and every route independently permits exactly one of those callers. The
application verifies the Google-signed token's issuer, exact custom audience,
verified service-account email, subject, and bounded lifetime. The body is
limited to 70 KB and is exactly `{intent_id, credential}` for installation or
`{intent_id}` for revocation and provider use. The intent must be a canonical UUID; tenant,
connector, provider, capability, KMS resource, and destination fields are not
accepted from the caller. In particular, there is no caller-authoritative
tenant field.

| Route | Exact caller | Canonical intent | Effect |
|---|---|---|---|
| `/v1/credentials/install` | control plane | `control_plane/install` | encrypt and version a credential |
| `/v1/credentials/revoke` | control plane | `control_plane/revoke/google.oauth.disconnect` | revoke the active Google credential and connector |
| `/v1/oauth/google/exchange` | control plane | `control_plane/install` (capability `google.oauth.install`) | exchange an authorization code for a credential, encrypt, and version it |
| `/v1/providers/google/gmail/profile` | worker | `worker/use/google.gmail.profile.read` | return bounded mailbox counters and history ID |
| `/v1/providers/google/calendar/primary` | worker | `worker/use/google.calendar.primary.read` | prove primary-calendar readability; return no content |
| `/v1/providers/google/gmail/threads` | worker | `worker/use/google.gmail.threads.read` | return a bounded (≤10), fields-masked list of Gmail thread summaries for a caller-supplied query |
| `/v1/providers/google/gmail/drafts/create` | worker | `worker/use/google.gmail.draft.create` | create a Gmail draft on a thread — the one write route this broker exposes |
| `/v1/providers/google/calendar/events` | worker | `worker/use/google.calendar.events.read` | return a bounded (≤25) list of Calendar event summaries in a caller-supplied window |

The original provider operations (`gmail/profile`, `calendar/primary`) were
intentionally narrow and read-only; the broker has since grown a bounded
Gmail-threads read, a bounded Calendar-events read, and one write route
(`gmail/drafts/create`, gated off by default via
`ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY` — see
[`capability-gateway.md`](capability-gateway.md)) — the discipline (no URL,
Google user ID, or provider argument accepted from the caller; every
provider request shape and bound is fixed server-side) is unchanged, but
"read-only" no longer describes every route this broker exposes. The broker
accepts no URL, Google user ID, query, or provider argument beyond the
bounded fields each route above validates. It exchanges the
refresh token only at `https://oauth2.googleapis.com/token`, disables HTTP
redirects and ambient proxy environment variables, and calls only Gmail's
fixed `users/me/profile`, `threads`, and `drafts` endpoints or Calendar's
fixed `calendars/primary` and `events` endpoints — one fixed Google endpoint
per route, never a caller-supplied URL.
All responses have strict status, type, timeout, and 32 KB size checks. The worker
receives only `history_id`, `messages_total`, and `threads_total`; the Gmail
profile's email address and the OAuth access token never leave the broker.
The Calendar ID and timezone are validated but never leave the broker.
Credentials with a caller-controlled token endpoint are rejected, and each
operation requires its own exact granted scope and one-use capability intent.

Google disconnection is principal-bound before the broker call. The control
plane resolves the one active Google connector under a tenant transaction and
creates or reuses an unexpired `google.oauth.disconnect` intent. It sends only
that intent UUID over authenticated service-to-service transport. The broker
accepts the exact control-plane identity and capability, writes the mandatory
allowed/observed audit pair, and invokes the database's serialized revocation
function. The function marks both the active credential and connector revoked
and consumes the intent atomically. A retry after that commit observes no active
connector and succeeds without issuing a second mutation.

This boundary revokes Attune's locally stored authority. It does not currently
call Google's token-revocation endpoint. That residual is explicit: upstream
provider failure must not block immediate local disconnection, and a reliable
provider-side ceremony needs independent retry and evidence semantics before it
can be claimed.

In GCP, the application subnet has Private Google Access but no Cloud NAT.
Exact private DNS zones resolve the reviewed Google hosts, including
`oauth2.googleapis.com`, `gmail.googleapis.com`, and the exact
`www.googleapis.com` host used for Calendar primary reads, to Google's
`private.googleapis.com` VIP. There is no
wildcard Google API zone. TLS still verifies the public hostnames, and the
broker still fixes each path in code; DNS alone is not treated as provider
authorization. A credential-free ephemeral job must prove both endpoints
return their expected unauthenticated refusals before a Google credential is
authorized.

Before encryption or revocation, the broker creates a tenant-bound,
content-free `allowed` audit intent through its forced-RLS database identity and
requires the private audit writer to persist it. It records the observed result
the same way after the effect. Writer timeout, identity-token failure, HTTP
error, malformed response, database failure, KMS failure, or ambiguous mutation
fails closed. Audit contains only the action, outcome, workload actor type, and
a one-way connector reference; credential values and provider content never
enter the audit contract.

Provider use is broker-mediated; access tokens are not returned to workers.
Every accepted use requires a fresh, expiring worker intent for the exact
registered capability, durable content-free audit before decrypt, durable audit
after the provider result, and atomic finalization of the intent. Provider or
credential failures are recorded without response bodies or secret-bearing
exception text. Because this first operation is read-only, a post-effect audit
or finalization failure may safely retry after the lease expires. Any future
exception that releases a short-lived credential must be
capability-, workload-, connector-, destination-, and expiry-bound, documented,
and adversarially tested.

The database lease function also enforces a durable limit of 60 credential-use
leases per tenant and exact capability in a rolling minute. A transaction-level
advisory lock serializes each bucket across broker instances, and runtime roles
cannot modify intent counters or timestamps. A hash collision can delay an
unrelated bucket but cannot add quota. Denied/limited, provider-failed, and
unavailable use results emit the same fixed content-free anomaly marker; a
Cloud Logging metric opens an incident after more than five markers in five
minutes. Notification channels are deployment configuration and are mandatory
before customer credentials.

## Implementation status

The envelope-encryption core, immutable forced-RLS vault schema, one-time
intent functions, serialized installation/revocation lifecycle, private HTTP
adapter, authenticated audit client, production composition root, non-root
container, and private Cloud Run service are implemented and deployed in the
development project. The service uses
its dedicated IAM database identity, the connector KMS key, and the private
audit writer; its runtime environment contains resource identifiers but no
credential values. A synthetic live wrap/unwrap passed under the broker identity
and verified KMS CRC32C integrity; the ephemeral validation job was removed.
The fixed Gmail and Calendar broker operations and their composite deterministic
worker executor are implemented, tested, and active in development. On
2026-07-16 the dedicated identity produced successful `200` and `204` provider
results, durable pre/post audit, worker success, and content-free browser
verification; the standalone Gmail dispatch route was then disabled. The
worker accepts only a canonical connector UUID and creates separate two-minute
tenant-bound use intents. Its exact-host private egress boundary is
declarative, and its credential-free live probe passed in development on
2026-07-14; repeat the probe after each material network or image change. A new
environment still requires a dedicated non-production identity, verified
paging, and full intent-to-ciphertext-to-provider-to-audit evidence before
customer credentials are authorized.
