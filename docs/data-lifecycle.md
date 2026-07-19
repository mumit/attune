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

Two tenant-bearing relations added for hosted intelligence persistence
(`docs/future-state.md` Phase 5 item 1; see `docs/decisions.md` 2026-07-19):
`attune.importance_signals` (the hosted per-sender importance profile) and
`attune.attention_items` (recorded attended Slack/Google Chat signal) are
both classified `customer_content` / `erase` / exportable, the same triple as
`memories`/`conversation_turns` — the principal's own owner-inspectable,
owner-correctable learned state and recorded chat content, respectively.
Their sender/channel/thread references are keyed HMAC digests, never
plaintext, at rest. Neither table is pruned by the retention executor
described below; each bounds itself with a self-contained write-time prune
(decay window plus a per-sender signal cap for importance; a retention
window plus a total item cap for attention), mirroring the local JSON stores
they persist alongside. This stage is dormant: no executor reads or writes
either table yet.

Two further tenant-bearing relations added for per-tenant model
configuration and usage metering (`docs/future-state.md` Phase 6 "hosted
operations"; see `docs/decisions.md` 2026-07-19 and
`docs/hosted-model-profiles.md`): `attune.tenant_model_preferences` (the
owner's chosen operator-defined model profile, one row per tenant) is
classified `account` / `erase` / exportable, the same triple as
`hosted_channel_preferences` — a bounded owner preference, not customer
content. `attune.model_usage_daily` (content-free per tenant/task/profile/
UTC-day aggregate counters feeding future billing) is classified
`operational` / `erase` / not exportable, the same triple as the existing
generic `usage_records` table it sits alongside. Neither relation is
pruned by the content-retention executor described below; both are swept
by the account-deletion walk like every other `erase`-classified relation.

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
   a fresh data key, and publishes it to a distinct opaque object reserved for
   that execution attempt. Expired retries retain prior candidates for cleanup
   and never reuse their object identity.
4. The owner reauthenticates to stream the object through Attune. Download
   authorization is not a long-lived public signed URL.
5. The object and wrapped key expire within 24 hours. Repeated requests and
   expiry are auditable without retaining the exported content.

Exports are asynchronous and size-limited. Partial generation fails closed; a
terminal failure is recorded only after the current attempt's object is proven
absent. Known abandoned attempts remain cleanup work and the one-day bucket
lifecycle bounds an object left by abrupt process death. Customer exports
include provenance and timestamps needed to understand the data, but not
credentials, internal authorization material, unrelated principals, raw
embeddings, or security secrets.

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

## Content retention and tenant deletion design (2026-07-19)

This section is the implementation design for the two slices the "Delivery
sequence" below marked as later work: bounded age-based retention for
customer content, and an owner-initiated, right-to-be-forgotten tenant
deletion ceremony (`docs/future-state.md` Phase 6 "hosted operations";
`docs/gap-analysis.md` G19; the hosted review's gap #4). Both remain dormant
behind default-off gates until their own development activation evidence is
recorded, exactly like the protocol-retention and export slices above.

### Content retention

The contract already fixes the window: "Conversation turns and derived
summaries: 30 days after last activity". This slice extends the *pattern* of
`protocol_retention.py`/`prune_expired_protocol_records` (0028) — a dedicated
identity (`attune_content_retention`), a memberless function owner
(`attune_content_retention_executor`), a bounded batch/singleton-lock function
(`attune.prune_expired_customer_content`, migration 0046), and per-tenant
content-free audit intents (`content_retention.*` actions) — to two relations:

- `conversation_turns`/`conversations`: a conversation is stale only when
  every one of its turns is older than the 30-day window (an active
  conversation never loses its older turns just because they are
  individually old); once a conversation has zero remaining turns and is
  itself outside the window, the now-empty shell row is pruned too. Both
  passes are bounded and audited per tenant, mirroring the four existing
  per-artifact loops in `prune_expired_protocol_records`.
- `hosted_brief_deliveries`: classified the same triple as
  `conversation_turns` (see `data_lifecycle.py`'s comment on that table) — the
  bounded rendered brief text is "derived summaries" under the same contract
  row, so it ages off the same 30-day window by its own delivery timestamp.

**Deliberately out of scope for this executor:** `memories`/`memory_embeddings`
(the contract keeps taught memory "until the owner deletes it or the
account" — no age-based sweep), and `importance_signals`/`attention_items`
(already self-bound at write time with their own decay window/cap, as the
"Reviewed storage inventory" section above already states; a second,
independent sweep would just be a second, conflicting bound on the same
rows). Nothing changes about that existing paragraph.

**Chosen, not contract-fixed:** the contract states the 30-day number but not
its enforcement granularity. This design pins per-turn/per-brief age plus a
conversation-level "no turn in window" test as the concrete rule, and treats
the owner-selectable 1–365 day range as future work — the executor always
uses the fixed 30-day default until a per-tenant override column and its own
product surface exist. The window itself is a database constant inside
migration 0046, not an environment variable: changing it is the same "policy
migration" the "Initial operated-service policy" section already requires
(disclosure, backfill analysis, tests, rollback plan), consistent with
"operator-configurable via infrastructure, not env sprawl" — a future
override is a reviewed migration or a per-tenant column, never an
`ATTUNE_*` knob.

Batching/bounds mirror the protocol executor exactly: `batch_size` (1–1000)
and `max_batches` (1–10) bound every invocation; the Cloud Run job and its
scheduler are new environments-paused-by-default, following the same
paused-first authenticated-path, paging, IAM-isolation, and verifier evidence
gate the protocol executor already passed before its own daily schedule was
enabled. Gate: `ATTUNE_ENABLE_CONTENT_RETENTION` (default off) — the job
entry point (`content_retention.main`) refuses to open a database connection
at all unless the gate reads `"true"`, so an accidentally-invoked job is a
content-free no-op even before the scheduler's own paused state is
considered.

### Tenant deletion

The ceremony bar mirrors the Workspace-disconnect ceremony
(`GoogleConnectorRevocation`/`disconnect_google_connector`) and the customer
export request (`docs/customer-export.md`): recent web authentication
(re-checked independently inside the database function, not only by the
control plane), an explicit destructive confirmation body the browser cannot
vary (`{"confirmation": "delete my account"}`), and same-origin CSRF. Unlike
disconnect, deletion is not immediate: it creates a durable
`attune.deletion_requests` row with a **14-day grace period**.

**Why 14 days, chosen (the contract does not fix a deletion-ceremony grace
length):** long enough for an owner who changed their mind under stress to
notice and cancel without contacting support (roughly double the 7-day
window several comparable consumer deletion ceremonies use, chosen
conservatively since Attune has no support-repair path yet per the roadmap),
short enough that "right to be forgotten" is not indefinitely deferred. It is
a database constant inside migration 0046 (`interval '14 days'`), not an
environment variable — the same "operator-configurable via infrastructure,
not env sprawl" posture as the retention window above; changing it is a
reviewed migration, not a config edit.

The request is cancellable during the grace period by the same recent-auth
bar that created it (`attune.cancel_tenant_deletion_request`); once claimed
by the executor (grace elapsed), it is no longer cancellable — the walk may
already be under way. At most one active (`pending` or `claimed`) request
exists per tenant (a partial unique index), matching the "one active export
per owner/scope" idiom from the export ceremony.

**Executor.** `tenant_deletion_executor.run_tenant_deletion_once` claims at
most one due request (`attune.claim_tenant_deletion`, a dedicated
`attune_deletion` identity and memberless `attune_deletion_executor` function
owner, mirroring the retention executor's identity posture exactly) and walks
**every** relation `attune.hosted.data_lifecycle.RELATIONAL_ASSETS` classifies
with `DeletionRule.ERASE` or `DeletionRule.CRYPTO_ERASE` — read from the
registry on every call, never a hand-copied table list, so adding a
classified relation to the registry is the only change needed to include it
in deletion. A relation the walk's own DataClass/DeletionRule matching cannot
place into "erase now", "retained tombstone", or "retained deidentified"
raises immediately rather than being silently skipped; this is pinned by an
offline test that appends a fake, unrecognized classification to the
registry and asserts the walk fails closed.

Per relation, `attune.erase_tenant_deletion_relation` (migration 0046) applies
the rule:

- **`DeletionRule.ERASE`** (`ACCOUNT`, `CUSTOMER_CONTENT`, and `OPERATIONAL`
  classes): a bounded, batched `DELETE ... WHERE tenant_id = $1 LIMIT $2`
  against a fixed, reviewed relation-name allowlist (a defense-in-depth
  identifier check for the dynamic SQL, not the policy — the policy is the
  Python registry) — except the two identity anchor tables, below.
  - **`tenants`/`principals` are a deliberate special case within the same
    "erase" rule**, not a hand-listed exclusion: instead of a physical
    `DELETE`, they transition to the `deleted` status the schema already
    reserved for them since migration 0001 (`tenants.status`/
    `principals.status` CHECK constraints). A physical delete would break
    every other surviving tenant-scoped foreign key, including the deletion
    ledger itself; a status flip does not, and neither column retains
    reversible content once terminal (`subject_hash`/`issuer` are already
    opaque hashes, `slug` is a generated identifier). They are processed
    last in the walk, once every other relation is confirmed drained, so
    that a crash never leaves a tenant marked terminal with content still
    present.
- **`DeletionRule.CRYPTO_ERASE`** (`CREDENTIAL` class:
  `connector_credentials`, `hosted_channel_credentials`): the executor first
  makes a **best-effort** call through the *existing* Google connector
  revocation broker path (`GoogleConnectorRevocation.disconnect`, the same
  service `DELETE /v1/connectors/google` already uses) for the tenant's
  owner principal, then applies the same generic bounded `DELETE` as above
  regardless of whether the upstream call succeeded — deleting the row
  destroys the wrapped data-encryption key and ciphertext together, which is
  itself a valid, sufficient cryptographic erasure even if the upstream
  provider call is unavailable. A live Slack/Google Chat channel-credential
  upstream revocation call is **out of scope for this slice** (see below).
- **`DataClass.SECURITY_AUDIT` / `DeletionRule.DEIDENTIFY`** (`audit_heads`,
  `audit_events`, `audit_intents`): never a target of the walk. These
  relations already hold only hashed actor/action/outcome metadata by
  construction — every audit intent in this codebase is written content-free
  — so there is no additional field left to deidentify at deletion time; the
  append-only triggers on `audit_events` would refuse an `UPDATE`/`DELETE`
  regardless. **What survives:** the complete hash-chained audit trail and
  every `deletion.*`/`content_retention.*` audit intent recorded during the
  walk itself. Deletion of content is not deletion of the audit trail.
- **`DataClass.DELETION_LEDGER` / `DeletionRule.RETAIN_TOMBSTONE`**
  (`deletion_markers`, and the new `deletion_requests` — see below): never a
  target of the walk.

**Foreign-key ordering is not hand-derived.** The registry does not encode a
dependency graph between its ~36 erasable relations. The executor instead
attempts every pending relation each pass; a relation whose `DELETE` fails
with a foreign-key violation (SQLSTATE `23503`, detected in a
driver-agnostic way so it works under both psycopg and pg8000) is deferred to
the next pass, and passes repeat until every relation drains or a pass makes
no progress at all. A pass with no progress is a genuine, unresolvable
ambiguity, not a hand-listed ordering bug, and fails the run closed the same
way an unrecoverable foreign-key cycle would in any schema.

**Idempotent resume.** A crash between calls leaves the request row
`claimed` with its original `claim_run_id` intact. The next invocation's
`claim_tenant_deletion` call finds that same row (a dedicated resume path,
distinct from claiming a fresh due request) and returns the **same**
`claim_run_id` rather than minting a new one, so every subsequent
`erase_tenant_deletion_relation` call — itself idempotent, since a relation
already at zero rows for the tenant simply returns zero — can safely repeat
the whole walk from the top of the registry without risk of double-processing
or of the ownership check (`erase_tenant_deletion_relation` verifies the
caller's `claim_run_id` against the row's) rejecting a legitimate resume.

**Reconciliation-style ambiguity.** Mirroring `docs/reconciliation.md`'s
posture exactly: an executor that catches a genuine, non-foreign-key error
mid-walk (or that cannot make progress despite full passes) never blindly
retries. It calls `attune.fail_tenant_deletion` with one of four fixed,
content-free reason codes (`pre_effect_audit`, `executor_ambiguous`,
`post_effect_audit`, `completion_unconfirmed` — the same shape as
`job_reconciliations`' four intake reasons) and leaves the tenant in
`deleting` — neither `active` nor `deleted` — as an honest stop signal. A
`failed` request is not auto-retried by the claim path; only a bare process
crash (no exception ever raised, nothing to catch) resumes automatically,
because the row stays `claimed` and the next run's resume path picks it back
up. Resolving a genuinely `failed` request is explicitly future operator
workflow, matching reconciliation's own "remaining gate" language.

**Sessions and connectors.** `identity_sessions` is itself `OPERATIONAL`/
`ERASE`-classified, so the generic walk deletes every one of the tenant's
sessions as an ordinary consequence of walking the registry — no separate
"invalidate sessions" step is needed. Connector credentials are revoked and
crypto-erased as described above.

**Terminal state and completion.** `attune.complete_tenant_deletion` marks
the request `completed` only after every relation in the walk has returned
successfully; completion is not a status flip alone; per this document's
opening principle, a database status change is not proof of completion by
itself; here it follows a walk that has already proven every classified
relation drained (or was correctly retained). The tenant row itself reached
its terminal `deleted` status as the walk's own last step.

**`deletion_requests`'s own classification.** It is `DataClass
.DELETION_LEDGER` / `DeletionRule.RETAIN_TOMBSTONE` — the same triple as
`deletion_markers` — because it must survive the tenant's own deletion long
enough to prove the ceremony happened: it is never a target of its own erase
walk, and its foreign keys into `tenants`/`principals` remain valid forever
precisely because those two rows are never physically removed (see above).
Unlike the account restore-suppression ledger this document already
describes (deliberately *outside* the deletable tenant graph, with its own
memberless function owner and append-only mutation rules),
`deletion_requests` is intentionally tenant-scoped and forced through
ordinary per-tenant RLS: it is ceremony evidence for *this* tenant's
principal-facing status page, not the independent, cross-tenant
restore-suppression authority. Building that separate ledger remains
`docs/data-lifecycle.md`'s existing "Deletion authority" delivery-sequence
item, unaffected by this slice.

**Gate:** `ATTUNE_HOSTED_DELETION_ENABLED` (default off). When off, the
control-plane routes (`POST`/`GET`/`DELETE /v1/account/deletion-request(s)`)
are not registered at all — an unauthenticated *or* authenticated request
receives a plain 404, the same "absent from the routing table, not merely
401" pin the other default-off ceremonies use — and the sign-in page's
"Delete account" affordance is shown optimistically (mirroring hosted
signup's own pre-session availability signal) and hides itself the moment
its own status route responds 404. The background executor's job entry
point (`tenant_deletion_executor.main`) independently checks the same gate
before ever calling `claim_tenant_deletion`.

**Explicitly out of scope for this slice (stated honestly, not silently
deferred):**

- Backup-restore-suppression mechanics beyond what this document already
  specifies (the independent, cross-tenant ledger and its restore procedure)
  — this slice's tenant deletion is entirely within the live tenant graph and
  does not touch backup/PITR state.
- Provider-side revocation for Slack/Google Chat channel credentials (the
  Google Workspace OAuth connector is the only upstream call this slice
  makes); per the "Google/Slack source systems" row above, Attune deletion
  never touches source-system data regardless.
- The owner-selectable 1–365 day content-retention window (still fixed at
  the contract's 30-day default) and any per-tenant deletion-grace override.
- Resolving a `failed` deletion request: an explicit, separately reviewed
  operator workflow, matching `docs/reconciliation.md`'s own remaining-gate
  language for ambiguous effects.

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
   broad synthetic-data identity to the operated project. A second, dormant
   executor now covers customer content (conversation turns/conversations and
   hosted brief deliveries) behind its own default-off gate
   (`ATTUNE_ENABLE_CONTENT_RETENTION`) and identity posture, per the
   "Content retention and tenant deletion design" section above; it has not
   yet passed the paused-first activation ceremony the protocol executor did,
   and its Cloud Run job/scheduler are not yet deployed.
3. **Export path (implemented behind a default-off edge gate):** migrations
   0029–0037 provide fixed projections, bounded secret-negative archives,
   envelope encryption, canonical task delivery, owner-bound request/status,
   a 90-second one-time download grant, and exact-generation cleanup. Writer,
   download, cleanup, and scheduler identities are disjoint. The private alpha
   exposes only account-and-preferences; other reviewed scopes remain disabled.
4. **Deletion authority:** migration 0046 implements the owner-initiated
   tenant deletion ceremony described above (durable request, grace period,
   registry-driven executor) behind `ATTUNE_HOSTED_DELETION_ENABLED`, still
   dormant pending the same paused-first activation ceremony as the other
   slices. The independent, cross-tenant restore-suppression ledger this
   document describes above remains unbuilt.
5. **Recovery proof:** isolated backup restore exercise demonstrating that a
   deleted synthetic tenant and credential cannot return.
6. **Customer activation:** explicit development-owner ceremonies and evidence,
   followed by staging assurance and launch review.

Any default-off lifecycle feature must remain absent from the UI until its
backend, authorization, cleanup, monitoring, and recovery evidence are active.
The UI must not present a decorative control or imply unavailable authority.
