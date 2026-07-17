# Hosted data lifecycle

This document is the design authority for retention, customer export, erasure,
and backup restore suppression in the operated service. It complements the
normative [security architecture](security-architecture.md). A feature is not
customer-ready merely because `export_jobs` or `deletion_markers` contains a
row: the corresponding bounded executor, audit path, expiry, and recovery
procedure must also be active and verified.

## Principles

- Attune stores only what a declared feature needs and does not become a second
  permanent mailbox or calendar.
- The authenticated owner can inspect, export, and delete Attune-held data.
  Export does not mean exporting Google source data that Attune never retained.
- Secrets, bearer tokens, ciphertext, internal hashes, model prompts, and
  security-sensitive implementation metadata are never placed in a customer
  export.
- Retention expiry and owner deletion cover relational rows, embeddings, object
  storage, caches, task payloads, derived summaries, and temporary exports.
- Security audit evidence may outlive an account only in deidentified form and
  for the published security/legal period.
- A restore must replay durable suppression records before restored customer
  data or credentials can become reachable.

## Initial operated-service policy

These are launch defaults, not claims about the current development deployment.
They must be enforced and surfaced in-product before an external-customer
launch. Shorter legal or contractual limits override these maxima.

| Data class | Default lifecycle | Account deletion |
| --- | --- | --- |
| Live Workspace results | Transient for the bounded request; do not retain raw Gmail bodies, attachments, or Calendar payloads by default | Nothing to erase when the transient boundary is honored |
| Conversation turns and derived summaries | 30 days after last activity; owner-selectable from 1–365 days | Erase, including cached and derived copies |
| Explicitly taught memory | Until the owner deletes it or the account | Erase relational content and embeddings |
| Connector credentials | Until revocation, replacement, or account deletion | Revoke upstream where supported, then cryptographically erase the wrapped data key and ciphertext |
| Provider events and ingress deduplication | At most 7 days after terminal processing | Erase after in-flight work is stopped |
| Jobs, retries, checkpoints, delivery records, and reconciliation state | At most 30 days after terminal state; unresolved reconciliation is retained until resolved under incident policy | Cancel safely, reconcile ambiguous effects, then erase |
| OAuth, setup, link, and identity transactions | Through their short protocol expiry, then at most 24 hours for abuse/replay diagnosis | Invalidate immediately, then erase |
| Usage and customer-visible activity metadata | 365 days unless a shorter contractual period applies | Deidentify only fields required for billing, fraud, or legal records; erase the remainder |
| Hash-chained security audit | At least 365 days, with the production object-store policy locked only after legal and recovery review | Retain deidentified evidence; remove direct/customer-content fields |
| Generated export objects | 24 hours after becoming ready or first successful download, whichever is earlier | Erase immediately |
| Restore-suppression tombstones | Longer than the longest backup/PITR horizon plus a 30-day safety margin | Retain only non-reversible identifiers and lifecycle evidence, then expire |

Changing a default is a policy migration: it requires product disclosure,
backfill/expiry analysis, subprocessor review, tests, and an operator rollback
plan. It is not an environment-variable tweak.

## Reviewed storage inventory

`attune.hosted.data_lifecycle.RELATIONAL_ASSETS` classifies every hosted
tenant-bearing relation as account state, customer content, credential,
operational state, security audit, or deletion ledger. It also declares the
account-deletion action and whether the relation contributes to a customer
export. Database verification requires the live Attune relation set to match
that inventory exactly and requires forced row-level security on every entry.
Adding a table without reviewing this document and the executable inventory
therefore fails migration verification.

Relational coverage is necessary but not sufficient. Every release must also
review these non-relational locations:

| Location | Permitted data | Lifecycle control |
| --- | --- | --- |
| Vector index | Embeddings plus tenant/principal and source identifiers; no credentials | Delete with its source memory and verify no cross-tenant remnants |
| Secret Manager / KMS | Platform secrets and wrapping keys; connector ciphertext remains in the tenant database | Version destruction and key-destruction runbooks; never export values |
| Cloud Tasks / Pub/Sub | Purpose-bound IDs and minimal signals, not Workspace bodies or credentials | Short infrastructure TTL plus idempotent terminal processing |
| Cloud Logging / Error Reporting | Content-free operational and security metadata | Sink exclusions prevent query strings, tokens, prompts, results, and message content; environment retention is reviewed explicitly |
| Audit object bucket | Deidentified, integrity-protected security evidence | CMEK, public-access prevention, versioning, and reviewed retention lock |
| Temporary export bucket | Per-export encrypted object behind server-side authorization | Separate bucket/key boundary, 24-hour lifecycle deletion, no public ACL or bearer URL in logs |
| Managed model provider | Minimum request context needed for the live answer | Approved no-training/retention terms and a documented subprocessor policy |
| Google/Slack source systems | Authoritative user data | Attune deletion does not delete source mail, events, or channel messages unless a separate explicit provider action is approved |
| Backups and PITR | Encrypted snapshots of the database | Natural expiry plus mandatory restore suppression before service activation |

## Export journey

1. A recently authenticated owner requests one fixed scope: account data,
   conversation data, memories, preferences, or customer-visible audit.
2. Attune records a content-free audit intent and queues an idempotent export.
   The browser never chooses a tenant or object-store path.
3. A dedicated export identity reads only the allowed tenant scope, constructs
   a versioned manifest, scans it for forbidden secret fields, encrypts it with
   a fresh data key, and publishes it to the temporary export store.
4. The owner reauthenticates to stream the object through Attune. Download
   authorization is not a long-lived public signed URL.
5. The object and wrapped key expire within 24 hours. Repeated requests and
   expiry are auditable without retaining the exported content.

Exports are asynchronous and size-limited. Partial generation fails closed and
removes the partial object. Customer exports include provenance and timestamps
needed to understand the data, but not credentials, internal authorization
material, unrelated principals, raw embeddings, or security secrets.

## Account deletion and restore suppression

Deletion is an R4 administrative workflow requiring recent web authentication,
an explicit destructive confirmation, and a fixed server-side account scope.
It immediately suspends new work, invalidates sessions, disables channel
delivery, and revokes connector use. The executor then reconciles ambiguous
provider effects before erasing content and cryptographically erasing
credentials. Completion means every registered store has produced verified
evidence; a database status change alone is not completion.

The existing `attune.deletion_markers` relation is useful for tenant-scoped
object deletion work, but it is not the account restore-suppression authority:
it has foreign keys to the tenant and requesting principal. The operated design
requires a separate, minimal suppression ledger outside the deletable tenant
graph. That ledger contains only keyed, non-reversible account/subject
identifiers, request/completion times, policy version, backup horizon, and
expiry. It has a dedicated memberless function owner and append-only mutation
rules; ordinary control-plane and worker identities cannot enumerate it.

Every restore procedure must, before enabling ingress or workers:

1. restore into an isolated project/network with all egress and customer ingress
   disabled;
2. load the current suppression ledger from the independent authority;
3. remove suppressed tenant data, credentials, vectors, objects, tasks, and
   derived records;
4. run the complete lifecycle inventory verifier and credential non-use tests;
5. record approval from security and the incident/recovery owner; and
6. only then admit traffic.

A missing, stale, unavailable, or unverifiable suppression ledger blocks the
restore. It never falls back to activating the snapshot.

## Delivery sequence

1. **Inventory gate (implemented):** exact executable relational inventory and
   fail-closed live-schema verification.
2. **Retention executor (first slice implemented, paused-first):** a dedicated
   identity and memberless function owner can prune at most 1,000 rows per
   table from expired OAuth transactions, channel-link transactions, identity
   sessions, and processed provider events. The database function holds a
   singleton transaction lock and atomically emits per-tenant, content-free
   audit intents. Its Cloud Run job is deployed and a content-free empty run is
   verified in development. An independent, non-database scheduler identity
   can invoke only this job. Its daily development schedule was activated only
   after the paused-first authenticated scheduler path, paging controls, IAM
   isolation, database verifier, and empty plans were evidenced. New
   environments still deploy it paused by default. The implementation
   emits aggregate structured output, bounds both rows and batches, and defines
   paged failure and possible-backlog policies; both incident paths are live
   verified. Nonzero deletion, recent-record survival, direct-table denial,
   and audit creation are verified against real PostgreSQL without adding a
   broad synthetic-data identity to the operated project. Operational records and
   conversation/memory policies across database, vectors, caches, and task
   payloads remain later slices.
3. **Export path (projection/archive slice implemented, dormant):** migration 0029
   replaces arbitrary scope JSON and generic table mutation with four fixed
   scopes, a recent-session-bound idempotent request function, and a distinct
   one-use executor claim. Both transitions emit content-free audit intents;
   the executor has no direct table access. Migration 0030 adds a current-owner,
   claim- and lease-bound positive projection with a 100,000-record ceiling.
   Unreviewed nested JSON is excluded, and the returned records pass through a
   deterministic, bounded, secret-negative archive builder. A separately
   keyed temporary object store exists as dormant substrate.
   Migration 0031 implements an exact-claim completion transition that binds
   immutable object generation and encrypted-envelope metadata, chooses the
   24-hour expiry, and audits atomically. Without a writer it cannot reach
   `ready`; there is no download surface, cleanup executor, or UI yet.
4. **Deletion authority:** independent restore-suppression ledger and complete
   account erasure orchestrator, initially dormant.
5. **Recovery proof:** isolated backup restore exercise demonstrating that a
   deleted synthetic tenant and credential cannot return.
6. **Customer activation:** explicit development-owner ceremonies and evidence,
   followed by staging assurance and launch review.

Until its step is implemented and verified, the setup UI must describe it as
unavailable; it must not present a decorative control or imply completion.
