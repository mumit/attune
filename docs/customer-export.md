# Hosted customer export boundary

This document defines the security and product contract for Attune-hosted
customer exports. An `export_jobs` row, bucket, or download button alone does
not constitute a working export.

## Customer journey

1. A signed-in owner chooses one server-defined scope: account and preferences,
   conversations, memories, or customer-visible activity. The page describes
   what is and is not included.
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
- **Download gateway:** after a second recent-auth ceremony, atomically consumes
  the download authorization, reads exactly the referenced object generation,
  unwraps and streams it, and schedules immediate erasure. It never redirects
  to a public or long-lived bearer URL.
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
requested -> running -> ready -> consumed -> expired
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

The dormant database reader and archive builder now implement the reviewed
projection and format boundary with fixed ZIP member
names, timestamps, modes, schema version, scope-specific record kinds, member
record counts and SHA-256 digests, and a whole-archive digest. It caps each
record at 2 MiB, the archive at 50 MiB, total records at 100,000, and nesting at
20 levels. It recursively normalizes field spelling before rejecting reviewed
credential, authorization, identity-hash, route, claim, and audit-chain keys.
No compute path connects them to an envelope key or object store, and there is
no completion transition, cleanup executor, or download path; customer export
therefore remains unavailable.

The next dormant substrate defines a separate export KMS key and temporary
bucket. The writer can encrypt/wrap and create or delete opaque objects, but
cannot decrypt, read, or list. The bucket is non-versioned, has uniform access,
enforced public-access prevention, disabled soft delete, a one-day lifecycle
backstop, and provider-enforced protection against accidental Terraform
destruction. No writer job or completion path is deployed, so the bucket must
remain empty.

The archive encryption format authenticates tenant ID, export job ID, fixed
scope, opaque object ID, plaintext digest and size, and format version with
AES-256-GCM. It uses a fresh random data key and nonce for every encryption,
wraps the data key with the separate export KMS key, verifies ciphertext and
plaintext digests, and refuses key/context/metadata substitution. Adversarial
tests cover each authenticated field. The development key, bucket, exact IAM,
emptiness, and empty Terraform plan are live verified; this still provides no
path that can generate or download an export.

Migration `0031_customer_export_completion.sql` defines the dormant handoff
from a live five-minute claim to `ready`. The export identity must present the
exact job and run IDs plus the opaque object UUID, positive immutable storage
generation, wrapped data key, 12-byte nonce, full KMS resource, plaintext and
ciphertext SHA-256 digests, bounded sizes with the exact 16-byte GCM overhead,
and format version 1. The database chooses `ready_at` and an expiry no later
than 24 hours, clears the lease, and atomically emits content-free audit
evidence. Exact retry is idempotent; any changed metadata or stale claim is
refused. The transition alone neither proves an object exists nor makes it
downloadable, so it remains unusable until the fail-closed writer and cleanup
paths are independently implemented and tested.

The completion migration is deployed in development. Its exact-lease,
idempotency, altered-metadata refusal, expiry, audit, role, and schema tests
pass against real PostgreSQL, the live migrator verifier passes, and both
infrastructure plans are empty. No export object was generated.

Migration `0032_customer_export_recovery.sql` and the dormant writer library
close the interrupted-execution boundary. The migration permits only expired
leases to be reclaimed, records one opaque object attempt per run, returns
known cleanup candidates through an exact-claim function, and exposes a fixed
failure transition with five content-free codes. The writer deletes prior
candidates, builds the positive projection, encrypts with a fresh DEK, uploads
with create-if-absent plus CRC32C, binds the returned generation at completion,
and deletes that exact generation if completion fails. An ambiguous upload is
deleted before failure is recorded. If deletion cannot be verified, the job
remains nonterminal and raises a cleanup incident. The storage adapter exposes
no read or list operation. Migration 0032 is deployed in development; the
writer library is not deployed or invocable. There is still no export executor,
queue route, download gateway, cleanup service, endpoint, or UI, so customer
export remains unavailable. The rollout applied exactly one migration, verified
all 34 forced-RLS tenant tables and exact privileges, and converged both
Terraform states to empty plans without generating an export object.

Migration `0033_customer_export_cleanup_authority.sql` defines the next dormant
boundary for abandoned attempts. A distinct cleanup role can lease at most 100
known object UUIDs after a 15-minute quarantine. The active writer attempt and
the object referenced by a ready export are excluded. Its separate storage
identity has delete only—no create, read, list, or KMS permission—and records
claim-bound, content-free evidence only after deletion or verified absence.
Failures leave the lease to expire for retry. The bounded cleanup entry point
reports possible backlog. Migration 0033 and its distinct IAM/database identity
are deployed in development; live verification passed with all 34 forced-RLS
tables and exact privileges. A manual-only Cloud Run cleanup job is deployed
with the delete-only identity, bounded inputs fixed by Terraform, failure and
batch-ceiling paging, and no scheduler or runtime argument overrides. Execution
`attune-development-export-cleanup-ntjd4` completed successfully with zero
candidates and no possible backlog. The authoritative bucket policy contains
only the writer create/delete role and cleanup delete-only role, and both
Terraform states are converged. Expired ready-object cleanup remains a separate
gate before writer activation.

Migration `0034_customer_export_expiry_cleanup.sql` implements that gate. The
same delete-only cleanup identity can lease only `ready` exports whose
server-selected expiry has passed. Each claim returns a canonical opaque object
ID and immutable generation; storage deletion must use that exact generation.
Only deletion or verified absence allows the claim-bound database completion to
move the job to `expired`, clear the wrapped DEK and every object/cryptographic
field, close the winning attempt, and emit content-free audit evidence. A stale
lease, substituted generation, or storage error leaves the export ready and
retryable. The code and migration remain undeployed until their development
migration and manual cleanup ceremony are reviewed.

## Required evidence before activation

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

Until these gates pass, the control plane must describe export as unavailable;
it must not present a decorative or nonfunctional download control.
