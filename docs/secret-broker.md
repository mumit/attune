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
IAM permits invocation only by the control-plane service account, and the
application verifies the Google-signed token's issuer, exact custom audience,
verified service-account email, subject, and bounded lifetime. The body is
limited to 70 KB and is exactly `{intent_id, credential}` for installation or
`{intent_id}` for revocation. The intent must be a canonical UUID; tenant,
connector, provider, capability, KMS resource, and destination fields are not
accepted from the caller. In particular, there is no caller-authoritative
tenant field.

Before encryption or revocation, the broker creates a tenant-bound,
content-free `allowed` audit intent through its forced-RLS database identity and
requires the private audit writer to persist it. It records the observed result
the same way after the effect. Writer timeout, identity-token failure, HTTP
error, malformed response, database failure, KMS failure, or ambiguous mutation
fails closed. Audit contains only the action, outcome, workload actor type, and
a one-way connector reference; credential values and provider content never
enter the audit contract.

For provider use, prefer broker-mediated fixed operations over returning access
tokens to workers. Any exception that releases a short-lived credential must be
capability-, workload-, connector-, destination-, and expiry-bound, documented,
and adversarially tested.

## Implementation status

The envelope-encryption core, immutable forced-RLS vault schema, one-time
intent functions, serialized installation/revocation lifecycle, private HTTP
adapter, authenticated audit client, production composition root, non-root
container, and private Cloud Run definition are implemented. The service uses
its dedicated IAM database identity, the connector KMS key, and the private
audit writer; its runtime environment contains resource identifiers but no
credential values. Broker-mediated Google operations, live end-to-end KMS
evidence, reconciliation/alerting, and the remaining hosted launch gates are
still required. No customer credential is authorized until those gates pass.
