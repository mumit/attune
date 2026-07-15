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

For provider use, prefer broker-mediated fixed operations over returning access
tokens to workers. Any exception that releases a short-lived credential must be
capability-, workload-, connector-, destination-, and expiry-bound, documented,
and adversarially tested.

## Implementation status

The envelope-encryption core and substitution tests are implemented. The vault
schema, one-time intent functions, private service, rotation/revocation API,
broker-mediated Google operation, and live KMS evidence remain gated work. No
customer credential is authorized until those controls and the hosted launch
gates pass.
