# Hosted customer export boundary

This document defines the security and product contract for Attune-hosted
customer exports. An `export_jobs` row, bucket, or download button alone does
not constitute a working export.

## Customer journey

1. A signed-in owner chooses the server-defined account and preferences export.
   Other reviewed scopes remain disabled in the private alpha until their
   customer disclosures and controls are activated.
2. Attune requires a fresh web authentication and an exact confirmation. The
   browser supplies neither tenant identity, table names, object paths, nor
   retention duration.
3. The request page shows queued, generating, ready, failed, or expired. It does
   not expose internal errors, object identifiers, wrapping keys, or signed
   storage URLs.
4. When ready, the owner reauthenticates and downloads once through Attune.
   Attune streams the object with an attachment filename and no-store headers.
5. The object becomes unavailable immediately after the first successful
   download or after 24 hours, whichever occurs first. The owner may explicitly
   erase it sooner.

Google and Slack remain the source of truth for data Attune did not retain.
Attune does not silently refetch an entire mailbox, calendar, or channel to
manufacture an account export.

## Fixed scopes and exclusions

| Scope | Included | Always excluded |
| --- | --- | --- |
| `account` | tenant/principal profile, installation and connector descriptors, policy version/status, autonomy, onboarding, and channel preferences/destinations | policy document internals, connector credentials, route ciphertext, sessions, link/OAuth transactions, internal jobs |
| `conversations` | conversations and turns retained by Attune, including content and timestamps | unreviewed provenance objects, provider access tokens, raw task payloads, hidden model/tool authority |
| `memories` | active explicit memories, source class, confidence, and timestamps | unreviewed provenance objects, deleted memories, raw embedding vectors, and model-provider secrets |
| `activity` | customer-visible audit action/outcome descriptors and usage quantities | unreviewed metadata/attribute objects, audit-chain internals, identity hashes, security-only events, IP/device abuse evidence |

The schema-versioned manifest identifies Attune, tenant export scope, request
and generation timestamps, format versions, record counts, and a digest for
each payload member. Stable customer-facing identifiers may be included;
database implementation details and unrelated principals may not.

Migration `0030_customer_export_projections.sql` implements those positive
field lists as one claim-bound database function. It accepts only the export
job ID and exact unexpired lease run ID, derives tenant, owner, and scope from
the job, and refuses content unless the requesting principal is still the
canonical hosted-onboarding owner. It returns a fixed member name, stable sort
key, and exact schema-versioned record. Each scope is capped at 100,000 records
before any row is returned. The function owner has `SELECT` only on the tables
named by these four projections; the runtime export identity has no direct
table access.

The migration is deployed in the development database. Its exact runtime and
function-owner privilege inventory passed the live verifier, and no export
execution job, object storage, key authority, endpoint, or UI was introduced.

Arbitrary JSON policy documents, conversation/memory provenance, audit
metadata, and usage attributes are intentionally omitted. Their byte bounds
do not make their internal fields customer-safe. Adding any such field
requires a positive customer-facing schema, adversarial fixtures, and an
explicit projection migration; the archive's forbidden-key scan remains a
second fail-closed check, not a substitute for that review.

## Trust boundaries

- **Control plane:** authenticates the owner, enforces recent authentication,
  creates only a fixed request, and serves status. It cannot read export
  content, choose an object path, or unwrap an export key.
- **Dispatch broker:** queues only an opaque canonical export intent. Browser
  data cannot become worker arguments.
- **Export executor:** a dedicated identity claims one pending job through a
  fixed database function. Its database owner can read only the reviewed
  export projection and update only that job's state. It has no connector,
  secret-broker, queue-administration, or general storage-list authority.
- **Export crypto/storage writer:** creates a random per-attempt data-encryption
  key, encrypts the archive with authenticated context binding tenant, job,
  scope, object, and schema version, wraps the key with the export KMS key, and
  creates only a canonical opaque object name. Each retry has a distinct,
  durable object reservation so a late stale worker cannot overwrite or delete
  the winning attempt. A terminal failure is forbidden unless the writer has
  proved its current object absent; bucket lifecycle remains the backstop for
  a process that dies between a storage write and cleanup.
- **Download gateway:** after a second recent-auth ceremony, leases the exact
  one-time authorization, reads and authenticates the referenced object
  generation, then atomically consumes the grant immediately before returning
  the attachment. It never redirects to a public or long-lived bearer URL.
- **Cleanup executor:** deletes expired/consumed object generations and wrapped
  keys in bounded batches, then records content-free evidence. Bucket lifecycle
  is a backstop, not proof that application cleanup succeeded.

The storage bucket is separate from retained audit evidence, uses a separate
KMS key, uniform access, public-access prevention, versioning disabled, a
24-hour lifecycle ceiling, and provider-enforced deletion protection. No
principal receives bucket-wide read/list plus key-decrypt authority.

## State machine and concurrency

The allowed transitions are:

```text
requested -> running -> ready -> consumed
                           \-> expired
                    \-> failed
requested ---------> cancelled
```

Claims use a one-use lease and idempotency key. At most one active export per
owner and scope is allowed. A ready record binds the opaque object UUID, exact
storage generation, wrapped-key ciphertext, archive digest, byte size, and an
expiry no later than 24 hours after readiness. Download consumption is atomic;
parallel or replayed requests cannot both obtain plaintext. A failed,
cancelled, consumed, or expired job cannot return to ready.

An expired `running` claim may be reassigned, but its attempt record and opaque
object UUID remain durable. The replacement receives a new object UUID and
deletes known prior candidates without storage listing. Create-if-absent keeps
each attempt immutable. Completion and cleanup use the exact returned storage
generation. Reusing one object name is prohibited because a late expired
worker could otherwise race with and delete its replacement.

## Content and format safety

The archive is size- and record-bounded, deterministic JSON Lines plus a JSON
manifest, compressed before authenticated encryption. Text remains data: it is
never evaluated as templates, HTML, spreadsheet formulas, paths, or tool
instructions. Member names are fixed, UTF-8 is validated, control characters
are escaped, and archive extraction cannot create absolute paths or `..`
segments.

Generation applies a structural secret-negative policy before encryption and
again to a test decryption: forbidden column classes, OAuth/token/key field
names, connector ciphertext, route ciphertext, sessions, link secrets, raw
embeddings, internal task authority, and unreviewed tables fail the job closed.
Regex redaction is not the authorization boundary.

## Current implementation

Migrations `0029` through `0035` implement request authority, positive
projections, bounded archive generation, envelope encryption, immutable object
attempts, exact completion, failure recovery, abandoned/expired cleanup, and
the canonical task relationship. The private writer is deployed at concurrency
one. Only the task-dispatch identity can invoke it. The writer can wrap keys and
create/delete canonical objects, but cannot decrypt, read, or list.

Migration `0036_customer_export_control_plane.sql` adds the owner-facing
request and status boundary. A recent identity session is checked again in the
database. Double-clicks and concurrent requests adopt only the exact active
owner/scope row. Status is returned through a principal-bound function capped
at twenty rows; the control plane has no export-content access. The private
alpha exposes only the `account` scope in the web API and UI. The remaining
reviewed scopes stay server-side unavailable until their separate product
disclosures are enabled.

Migration `0037_customer_export_download.sql` adds a separate download role and
90-second one-time authorization. The opaque secret is returned once in a JSON
body and posted to a fixed same-origin endpoint; it never appears in a URL,
redirect, object name, or access-log field. The gateway leases the exact grant,
reads the exact immutable generation with CRC32C verification, unwraps the DEK,
authenticates every AES-GCM context field and digest, and only then atomically
marks the grant used and the export consumed. Failed read/decrypt attempts
release the exact lease without consuming the grant. Replays and parallel
claims cannot obtain a second plaintext response.

The download identity has only database function execution, object `get`, and
KMS decrypt. It cannot list, create, overwrite, or delete objects. The
delete-only cleanup identity has none of its read/decrypt authority. Consumed
objects become immediate exact-generation cleanup candidates; expired ready
objects use the same bounded queue. Cleanup destroys the object and wrapped DEK
only after deletion or verified absence. A distinct scheduler identity may
invoke only this cleanup job every ten minutes. The one-day bucket lifecycle is
still only a backstop.

The archive remains bounded to 50 MiB and 100,000 records. It uses fixed ZIP
members, deterministic JSON Lines, schema/version manifests, record and archive
digests, and structural rejection of credential, authorization, identity-hash,
route, session, claim, embedding, and audit-chain fields.

## Required evidence before production activation

- real-PostgreSQL cross-tenant, role, claim/replay, transition, and concurrency
  tests through the exact runtime identities;
- fixtures containing canary credentials and adversarial archive/text values,
  proving no forbidden field or path escapes;
- envelope-encryption substitution tests for tenant, job, scope, generation,
  and object context;
- partial-write, KMS failure, expired-worker retry, double-download, expiry,
  process-death orphan reconciliation, and cleanup tests;
- a synthetic development export whose decrypted manifest and payload are
  reviewed, followed by object/key cleanup and an empty infrastructure plan;
- paging for generation failure, cleanup failure, and expired-object backlog;
- a staging restore exercise proving a consumed/expired export cannot reappear;
  and
- independent security review before any production customer export.

The control must remain behind its default-off environment gate until the
development rollout ceremony below is complete. Production remains prohibited
until the independent review and recovery gates pass.
