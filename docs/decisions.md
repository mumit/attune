# Architectural decisions

Newest first. This log records decisions that constrain current implementation.

## 2026-07-18 — Phase 1 learned importance, stage 2: deterministic triage adjustment

- `triage_thread` applies the per-sender importance profile as a
  deterministic, audited nudge on top of the model's own classification.
  The adjustment is asymmetric on purpose: LOW demotes one step
  (URGENT→ROUTINE, ROUTINE→NOISE), but HIGH only ever promotes
  NOISE→ROUTINE — never to URGENT. Urgency is a judgment about the content
  of the current message; the profile is a judgment about the sender's
  track record, and letting a good track record fabricate same-day urgency
  the model itself didn't see would be the profile inventing facts about
  the current message rather than protecting an important sender's mail
  from being dropped.
- The adjustment DOES apply on top of the model's ROUTINE parse-failure
  default, unlike the pre-existing soft memory-reaction garnish (which must
  never move that default). The distinguishing factor is provenance: the
  memory garnish is retrieved, unverified context feeding a model call
  whose failure must not be compounded; the importance profile is the
  principal's own already-recorded, deterministic state (a pin, or a
  counted signal run) — the same class of trusted input the autonomy gate
  already treats as authoritative.
- `TriageResult` keeps `base_priority` (the model's own classification)
  alongside the effective `priority` and an `adjusted` flag; the dispatcher
  prepends a content-free `"triaged"`/`"triaged_noise"` audit event
  (priority/base_priority/adjusted only) to both the NOISE-skip and the
  proceed-path audit records.
- URGENT mail gets presentation-only differentiation: the approval card's
  `title` (not the draft body) carries a "🔴 URGENT" marker plus the
  model's own reason, and a separate short heads-up goes to the configured
  notification route. The marker deliberately never touches the draft text
  itself — that text can become the actual sent reply if approved/edited,
  so nothing presentation-only may leak into it. `DraftApproveState` gained
  `priority`/`priority_adjusted` as a seam for future (Phase 4) autonomy
  gating; the graph does not branch on them yet.
- Calendar hold offers (`MAX_HOLD_OFFERS_PER_RUN`) are now ranked by the
  conflicting event's attendees' importance tier before the per-run cap is
  applied, since `CalendarEvent` has attendees but no organizer field —
  "the counterpart's importance" is read as the best tier among its
  attendees, the closest available proxy. Every conflict is still notified
  regardless of rank; ranking only orders who gets a card first once the
  cap binds. Absent a profile, every conflict ranks equally and Python's
  stable sort preserves arrival order (back-compat).
- The brief's unread-mail section is ordered HIGH/NORMAL/LOW by sender
  tier, stable within each tier — presentation only, never a filter; LOW
  senders stay visible (dropping mail is triage's job, not the brief's).
  `runtime.py`'s daily posted brief threads the real
  `app.importance_profile` through; the CLI's plain, `--post`-less preview
  path deliberately does not construct one by default (it would create a
  local JSON state file — and its lock file — as a side effect of a
  read-only preview command, contradicting that path's existing "no extra
  state" contract), but accepts one via `assemble_brief`'s new optional
  argument for callers that want it.

## 2026-07-18 — The local audit log is hash-chained; local state takes file locks

- Every line the local `JsonlAuditLog` appends now carries `prev_hash` and
  `entry_hash` (SHA-256 over the previous hash plus the entry's canonical
  JSON, genesis all-zeros), mirroring the hosted hash-chained audit in a
  lightweight file form. `verify()` walks the chain and Doctor runs it as a
  non-fatal `audit-chain` check, because `grants.py` folds this file into
  autonomy-graduation suggestions and a silently edited or deleted line
  would skew them.
- Lines written before hashing are tolerated only as a prefix; an unhashed
  line after the chain begins is treated as tampering. Pure tail truncation
  is honestly documented as undetectable from the file alone — an external
  anchored head (the hosted outbox's role) is the future answer, not a
  heavier local database.
- `JsonPendingApprovals` and `JsonlAuditLog` read-modify-write sections now
  also hold an OS-level advisory `flock` on a dedicated `.lock` file
  (`fslock.locked`), closing the cross-process double-claim race that an
  in-process `threading.RLock` alone cannot. The lock is advisory by scope
  (one principal, cooperating processes); platforms without `fcntl` degrade
  to the in-process lock with one logged warning.
- This was selected over adopting SQLite for these stores (heavier swap,
  same trust boundary), OS append-only file attributes (root-owned, not
  portable), and signing entries with a key (a local attacker who can edit
  the file can read a local key; the chain targets accidental and
  unprivileged tampering, not a root adversary).

## 2026-07-18 — The authenticated session is the web conversation route

- The browser conversation surface has no installation, preference, or
  destination ceremony, and no channel-broker involvement. An ordinary
  signed-in owner session with an active policy and an active Google
  connector is the whole authority; migration 0041's
  `attune.accept_web_owner_message` re-checks exactly that at acceptance
  time, and the shared bounded read-only conversation executor re-checks it
  again at execution time.
- The stored assistant turn is the delivery. There is no destination row, no
  reply broker, and no push: the browser polls `GET /v1/conversation/turns`
  for canonical turns. This was selected over inventing a destination/route
  concept for a channel that already has a trusted, authenticated transport.
- This was selected over folding the browser into the Slack/Google Chat
  channel-preference ceremony, which would have implied an installation and
  destination step the browser does not need and cannot outgrow.

## 2026-07-18 — Web conversation acceptance uses ordinary proofs, not recency

- `POST /v1/conversation/messages` requires ordinary session, same-origin,
  and CSRF proofs, the same bar as any authenticated read. It deliberately
  does not require the ten-minute recent-authentication window reserved for
  destructive or authority-changing ceremonies (policy confirmation, channel
  disconnection, export authorization): sending a bounded, read-only-executed
  conversation message is not one of those.
- Edge throttling is sized accordingly: Cloud Armor priority `893` allows 60
  requests per 60 seconds per IP over the exact message and turn-poll paths,
  versus the 10-per-60-second rules on the onboarding ceremonies, because a
  browser tab polling turns every two seconds must not trip the same limit
  built for an infrequent, deliberate action.
- This was selected over reusing the recent-authentication gate outright,
  which would have forced a re-authentication prompt into an ordinary
  conversation loop for no additional protection, since the executor itself
  is bounded and read-only regardless of session age.

## 2026-07 — Hosted channel choice is not channel authority

- Owners choose Google Chat, Slack, or both independently for interaction and
  briefs. At least one purpose is required; unsupported and duplicate values
  fail closed.
- The bounded preference is audited and tenant-bound but advances onboarding
  only to `authorized`. It contains no app, token, installation, destination,
  allowlist, ingress, or provider authority and sends no test message.
- Recent authentication, same-origin CSRF, a fixed function owner, forced RLS,
  and mandatory pre/post audit protect configuration. A validated route cannot
  be silently retargeted; it requires a future replacement ceremony.
- Browser-only was not offered because no hosted conversational web surface
  exists. This was selected over pretending a preference is a working route or
  coupling brief and interaction delivery to one provider.

## 2026-07 — Hosted policy starts with one recent-authenticated R0 profile

- Private alpha exposes a fixed read-only profile rather than a generic policy
  editor. The browser reviews bounded automatic/excluded behavior and submits
  no policy, capability, grant, risk, identity, or resource fields.
- Confirmation requires same-origin CSRF proof and a session created within ten
  minutes. An eight-hour session remains sufficient for ordinary reads but is
  not recent authentication for an autonomy change.
- A content-free allowed audit must be durably written before effect. One
  memberless function owner atomically creates the exact policy/grant and
  advances onboarding; the ordinary control-plane role cannot directly mutate
  policy or grant rows. A separate observed/failed audit completes the attempt.
- Existing state must match the exact profile and sole grant. Mismatch becomes
  `externally_modified` and requires repair; Attune neither overwrites nor
  silently adopts it. This was selected over free-form policy JSON, email-based
  trust, long-lived session authority, and application-only database controls.

## 2026-07 — Model proposals terminate at a typed capability gateway

- Hosted model output may propose only an exact versioned capability name and
  schema-bounded arguments. It cannot propose identity, tenant, connector,
  scopes, provider routing, risk, policy, URLs, raw requests, or approval.
- Infrastructure-owned registry definitions fix provider scopes, domain, risk
  tier and ceiling, and trusted argument reconstruction. Unknown, duplicate,
  malformed, oversized, and extra-field proposals fail closed.
- Verified tenant/principal, active policy and matching autonomy grant,
  connector ownership/scopes, and the grant risk ceiling are resolved in one
  forced-RLS transaction. Missing, stale, cross-tenant, database-failed, or
  ambiguous authority produces no admission.
- Admission is immutable canonical input, not execution authority. Dispatch
  rebinding, budgets, freshness, idempotency, audit, approvals, recent
  authentication, and provider-specific effect controls remain independent
  activation gates. This was selected over a generic tool loop, caller-supplied
  policy context, or treating model JSON as a provider request.

## 2026-07 — Hosted login is separate from Workspace consent

- Google Identity Platform verifies hosted login through a dedicated identity-
  only OAuth client. Workspace connector consent uses a different client,
  redirect, secret, and broker-owned exchange path.
- The control plane accepts only a fresh, verified Google-provider Identity
  Platform token with exact issuer and project audience, then replaces it with
  independent opaque and CSRF session values whose hashes are tenant-bound in
  PostgreSQL for at most eight hours.
- Email and domain are not membership authority. A memberless function owner
  resolves the hashed subject across tenants and creates a session only for
  exactly one active mapping; zero and multiple mappings return no session.
- The Identity Platform provider secret is configured outside Terraform because
  the provider resource persists it in state. API enablement, dormant runtime
  flags, database coordinates, and deny-by-default edge routes remain
  declarative.

## 2026-07 — Google code exchange is private and broker-owned

- The public callback identity may invoke exactly one internal-only OAuth
  exchange service. That service accepts only authorization code, state, and
  callback binding; all tenant and connector authority is recovered through a
  one-time database lease.
- The exchange has function-only database access and no log writer, Secret
  Manager, KMS, queue, or provider credential role. The secret broker alone
  reads the platform Google web-client secret, calls fixed Google endpoints,
  validates issuer, audience, time, nonce, PKCE result, and exact scopes, and
  stores only an envelope-encrypted refresh credential.
- Every transaction is also bound to a canonical requested
  `google.oauth.install` credential intent. The migration fails if dormant
  transaction rows unexpectedly exist; it does not guess or backfill authority.
- The services are deployed dormant before activation evidence. This was
  selected over exchanging in the public callback, giving the exchange direct
  vault/secret authority, accepting tenant data over HTTP, or activating OAuth
  merely because infrastructure deployment succeeds.

## 2026-07 — OAuth transactions cross tenants only through a leased function

- The authenticated control plane inserts tenant-visible, ten-minute Google
  OAuth transactions bound to a canonical pending connector. It cannot update,
  delete, truncate, or bypass RLS on those rows.
- A dedicated OAuth-exchange IAM database user receives an unprivileged
  `NOLOGIN NOBYPASSRLS` runtime role. It has no table privilege and may call
  only fixed lease/finalize functions.
- The functions use a separate memberless `NOLOGIN BYPASSRLS` owner with only
  select/update access to OAuth transactions and select access to connectors.
  Lease requires both independent state and callback-binding hashes; finalize
  requires the binding again, accepts only a leased row, and clears the current
  PKCE verifier value.
- This was selected over a caller-supplied tenant, a shared callback/database
  identity, direct cross-tenant table reads, or UUID-only finalization. It
  contains confused-deputy and object-reference substitution paths while
  keeping the public callback scrubber credential-free.

## 2026-07 — OAuth callbacks use a credential-free scrubber

- The exact Google callback path routes to a dedicated Cloud Run service and
  workload identity rather than the general control plane. The dormant service
  parses no OAuth fields, has no tenant, database, secret, KMS, queue, or
  provider authority, and immediately redirects the browser to `/`.
- Load-balancer logging is disabled only for the callback backend. Cloud Armor
  still emits `requests` entries when backend logging is off, so a protected
  `_Default` exclusion drops both Cloud Run and load-balancer request logs by
  the dedicated service/backend resource identities. It avoids any filter that
  parses a URL already carrying an authorization code. The immutable sink
  remains Cloud-Audit-only.
- Exact host, path, method, source rate, no-NAT egress, disabled default URI,
  and load-balancer-only ingress remain independent controls. Synthetic secret
  values must be absent from both request-log planes before activation.
- Global URL-map convergence is asynchronous. The OAuth client and redirect URI
  must not be configured until a documented soak and multi-location synthetic
  probes prove that no old logged backend still serves the callback path.
- Cloud Logging Data Access audit records server-side query filters. Callback
  non-retention tests fetch a timestamp-bounded window and search it locally;
  operators must never put codes, tokens, state, or test markers in a remote
  logging filter.
- This establishes callback URL non-retention but does not activate OAuth.
  Session-bound one-time state, PKCE, identity linking, broker handoff, and
  content-free audit are separate gates.

## 2026-07 — Immutable audit export excludes request logs

- The retained GCP sink exports only Cloud Audit activity, data-access, policy,
  and system-event logs. It does not export all project logs.
- OAuth callbacks necessarily carry short-lived authorization codes in their
  query string. Copying Cloud Run or load-balancer request URLs into a
  CMEK-protected, retention-controlled bucket would turn ephemeral credentials
  into durable secret records.
- Canonical Attune security decisions remain content-free and hash-chained in
  the application audit. Callback request-log non-retention is a separate edge
  launch gate; filtering the retained export alone is insufficient.

## 2026-07 — Provider routes activate atomically and fail closed

- `google.workspace.connection.verify` is present in neither the worker nor
  dispatch registry by default. One Terraform variable adds it to both,
  avoiding a producer/consumer mismatch during release. Its executor creates
  separately authorized Gmail-profile and Calendar-primary credential uses.
- Terraform rejects activation unless the fixed dispatch broker is enabled and
  at least one Monitoring notification channel is configured. Operators must
  separately prove channel verification, a test page, dedicated test identity,
  credential-free egress, and authenticated end-to-end evidence.
- The worker accepts only a canonical connector UUID, creates its own
  tenant-bound two-minute use intent with a stable job-bound idempotency key,
  and calls a typed broker client with a fixed route and bounded response.
  Provider URLs, user IDs, credentials, and access tokens are not job fields.
- This was selected over shipping an always-registered but undocumented route,
  separate worker/dispatch toggles, or treating a successful Terraform plan as
  authorization for customer traffic.

## 2026-07 — Connector verification is a principal-bound composite fixed job

- A signed-in browser may request only the fixed
  `google.workspace.connection.verify` job. Tenant, principal, active Google
  connector, exact scope set, capability, and worker destination are resolved
  from the Attune session and canonical server-side state. The worker creates
  distinct one-use intents for `google.gmail.profile.read` and
  `google.calendar.primary.read`; one composite job succeeds only after both.
- The browser receives an opaque job UUID and only queued, running, succeeded,
  or failed. Status resolution rebinds the job to the session principal and
  active connector; the UUID alone conveys no authority. Mailbox counters,
  calendar ID/timezone, and provider details never cross the browser boundary;
  Calendar metadata never leaves the secret broker.
- This was selected over a privileged operator smoke command, returning Gmail
  profile data to the UI, or treating successful OAuth token storage as proof
  that the granted credential can perform the reviewed provider read.

## 2026-07 — Fixed Google egress uses exact private DNS without NAT

- The GCP application subnet uses Private Google Access and no Cloud NAT.
  Private zones for exactly `oauth2.googleapis.com`, `www.googleapis.com`,
  `gmail.googleapis.com`, and `secretmanager.googleapis.com` resolve their
  apex records to the `private.googleapis.com` VIP. There is no wildcard
  `*.googleapis.com` override. Code restricts the latter additions to Google
  signing-certificate retrieval and the platform OAuth-client-secret read.
- This was selected over Cloud NAT, which would make arbitrary internet egress
  reachable, and over the usual wildcard private Google API zone, which would
  expose more provider hostnames to workloads.
- The VIP itself supports more Google APIs, so exact DNS is defense in depth,
  not authorization. Broker-fixed URLs and paths, TLS hostname verification,
  disabled redirects and ambient proxies, canonical capabilities,
  route-specific IAM, and minimized responses remain required.
- An ephemeral credential-free worker job proves the two endpoints return
  expected unauthenticated refusals. Adding a provider hostname is a reviewed
  infrastructure and application change, never an operational workaround.
- Project API activation is a separate required control. The foundation
  declaratively enables `gmail.googleapis.com` and
  `calendar-json.googleapis.com`; successful OAuth consent and token refresh
  do not prove that either API is enabled. The broker still fixes each exact
  operation and the runtime keeps the composite route disabled by default.

## 2026-07 — Provider credentials stay behind fixed broker operations

- Hosted workers receive neither stored credentials nor OAuth access tokens. A
  provider route accepts only an opaque one-time intent, maps its canonical
  capability to one reviewed request, and returns a minimized, typed result.
  The first routes are Gmail's read-only `users/me/profile` operation, which
  omits `emailAddress`, and Calendar's read-only `calendars/primary` operation,
  which returns no provider data to the worker.
- This makes destination allowlisting and data minimization structural,
  prevents model- or caller-controlled URLs and user IDs, limits SSRF and token
  exfiltration paths, and gives every decrypt/use a durable audit boundary.
- Each additional provider operation needs its own schema, route authorization,
  response minimization, negative tests, egress review, rate policy, and, for
  writes, reconciliation design. Generic proxying and access-token-return
  endpoints are prohibited.
- Credential-use leasing is durably limited per tenant and exact capability,
  rather than by an in-process counter, so horizontally scaled broker instances
  share one boundary. Content-free anomaly markers drive an operational alert;
  tenant or provider content is not copied into logs or metric labels.

## 2026-07 — Ambiguous effects open durable reconciliation

- A worker that cannot prove pre-effect audit, executor outcome, post-effect
  audit, or canonical completion atomically moves the leased job to
  `reconcile` and opens one tenant-bound record with a fixed reason.
- Reconciliation records contain no provider body, credential, exception text,
  or model output. An optional provider request reference is stored only as a
  fixed-length one-way hash.
- Workers can open but cannot resolve or delete records. Provider-specific
  evidence collection and an authenticated, audited resolution workflow remain
  a launch gate; an open record is not permission to retry.
- This was selected over treating a 5xx as retry authority or leaving a leased
  job without durable ambiguity state. The contract is in `reconciliation.md`.

## 2026-07 — Cross-tenant functions have memberless owners

- Forced RLS remains enabled on every tenant table. Narrow cross-tenant
  `SECURITY DEFINER` functions are owned by distinct dispatch, audit, and vault
  `NOLOGIN BYPASSRLS` roles so the functions can resolve opaque intents without
  accepting a caller-selected tenant.
- No IAM/runtime login is a member of an owner role. The roles are non-superuser,
  cannot create roles or databases, cannot log in, and receive only the table
  privileges required by their fixed functions.
- The migrator receives owner-role membership and schema-create authority only
  inside the migration transaction, revokes both before commit, and verifies
  function ownership, role flags, and zero members after every run.
- This was selected over disabling forced RLS, granting runtime roles
  `BYPASSRLS`, or giving brokers direct cross-tenant table access.

## 2026-07 — Credential mutation uses an opaque-intent secret broker

- The control plane creates a short-lived tenant-bound install or revoke intent;
  the private broker accepts only that canonical intent UUID plus the credential
  object required for installation. It does not accept tenant, connector,
  provider, capability, KMS, or destination authority from the request.
- Cloud Run IAM and application verification both restrict the caller to the
  exact control-plane service account and a stable custom audience. Static
  shared API keys and generated-URL guessing are rejected.
- The broker is the only connector-KMS user. It creates a fresh AES-256-GCM DEK
  per version, binds ciphertext to canonical tenant/connector/provider/version
  state, wraps the DEK with KMS, and persists no plaintext.
- A content-free tenant-bound audit event is required before each mutation and
  again after it. Audit/KMS/database ambiguity fails closed; serialized leases
  prevent overlapping install/revoke effects for one connector.
- Provider use remains broker-mediated rather than releasing refresh tokens to
  workers. Live KMS evidence and fixed Google operations are separate launch
  gates. The complete contract is in `secret-broker.md`.

## 2026-07 — Hosted audit accepts tenant-bound intents, not event bodies

- Tenant-scoped workloads persist idempotent audit intents under forced RLS.
- The dispatch broker can create only fixed-purpose audit intents derived from
  canonical dispatch state and has no direct audit-table authority.
- The private writer accepts only an opaque intent UUID. Its database identity
  can execute only the atomic intent-to-hash-chain function; direct table access
  and the legacy free-form append function are denied.
- This was selected over a privileged `{tenant_id, event}` HTTP API because
  workload IAM authenticates a caller but does not prove a request's tenant.
- Security-sensitive effects fail closed when the intent cannot be written.
  The complete contract is in `audit-writer.md`.
- Dispatch specifically requires a written `allowed` event before task
  creation and records the observed result afterward; deterministic task names
  make post-effect audit recovery safe.

## 2026-07 — A private broker exclusively owns hosted task dispatch

- Producers persist a tenant-bound job and dispatch intent in one transaction;
  they invoke the broker with an opaque intent ID rather than a tenant ID,
  target URL, task body, or executable argument.
- The broker verifies the exact producer workload identity and uses a narrow
  database function to lease canonical tenant, job, purpose, and capability.
  Deterministic Cloud Task names make crash recovery and `AlreadyExists`
  idempotent.
- Only the broker can enqueue or use the task-delivery identity. Queues use
  infrastructure-controlled exact routing. Producers cannot choose worker
  targets or mint delivery authority.
- Cloud Tasks OIDC still authenticates delivery at the worker. The worker
  atomically rebinds tenant, job kind, and capability to database state and
  refuses execution when required audit is unavailable.
- Direct producer enqueue is rejected. KMS signatures do not constrain an
  authorized malicious signer; per-tenant queues remain a higher-assurance cell
  option. The complete contract is in `dispatch-broker.md`.

## 2026-07 — Hosted storage fails closed on missing tenant context

- Hosted PostgreSQL migrations are immutable and checksum recorded. A separate
  private Cloud Run job owns schema changes through a dedicated IAM database
  identity; runtime identities never own tables or receive `BYPASSRLS`.
  Memberless `NOLOGIN` function-owner roles may hold `BYPASSRLS` solely for
  reviewed cross-tenant `SECURITY DEFINER` functions.
- Every durable customer object carries an immutable tenant ID and is forced
  through RLS. Composite foreign keys prevent cross-tenant relationships, and
  variable-dimension pgvector rows remain inside the same policy boundary.
- Tenant context is transaction-local so pooled connections cannot retain it.
  Missing context is an error. The context must originate from verified trusted
  code; RLS is defense in depth and is not authentication of a caller-selected
  tenant.
- Audit events are appended through a tenant-checking hash-chain function;
  application roles cannot insert, update, delete, or truncate the audit table.
- Hosted repositories require a typed trusted tenant at the API boundary.
  Cloud Tasks bodies carry identifiers and purpose only; exact Google OIDC
  caller and audience verification precedes canonical database retrieval and
  atomic claim, so queue payloads never become executable instructions.
- Cloud Tasks OIDC authenticates delivery but does not turn body fields into an
  Attune signature. The dispatch core atomically rebinds job kind and capability
  to canonical state, requires audit before execution, and reconciles ambiguous
  results. A live endpoint is blocked on fixed queue routing, least-privilege
  producers, the private audit writer, and registered capability executors.
- Customer data remains prohibited. This data boundary does not substitute for
  broker-mediated provider authorization, hardened job delivery, identity
  links, ingress verification, capability gateway, deletion workflow, or
  assurance gates.

## 2026-07 — GCP is the first operated SaaS platform

- The first hosted implementation uses Cloud Run, Cloud Tasks, private Cloud
  SQL PostgreSQL, Secret Manager/KMS, Artifact Registry, and retained Cloud
  Storage audit objects. Each trust boundary has a separate service identity.
- Hosted vector storage starts with PostgreSQL `vector` and tenant RLS rather
  than a shared Qdrant service, reducing privileged stores and unifying tenant
  deletion, backup, and audit boundaries. The memory interface remains portable.
- GCP is an implementation choice, not a product branch. The self-hosted
  single-principal runtime and polling mode stay portable; cloud-specific code
  remains behind hosted adapters and declarative infrastructure.
- The Terraform foundation creates no secret versions and deploys no current
  single-principal runtime. Customer data is prohibited until hosted schema,
  secret-broker, identity-link, ingress, audit, and isolation gates pass.

## 2026-07 — Local setup is planned, resumable, and resource-owned

- `attune init --target local` writes configuration first, displays an exact
  deterministic Docker Compose plan, and applies it only after confirmation.
  The subprocess uses a fixed argument array rather than a shell and receives
  no Attune environment or credential.
- The packaged local plan pins Qdrant `v1.18.2`, binds it only to loopback,
  persists a named volume, and enables Docker's no-new-privileges control.
- Setup state is schema-versioned, atomic, owner-readable only, and contains
  statuses, resource identifiers, and a one-way configuration digest rather
  than settings or secrets. Changed configuration or packaged-plan digest
  invalidates downstream apply/validation success; interrupted and failed
  applies are retryable.
- `attune status` reports the secret-free record; `--check` adds live Doctor
  validation. `attune repair` previews and reapplies the fixed plan only when a
  matching state record establishes ownership.
- Setup validation loads the selected environment exactly. Cleared Attune
  settings remove stale in-process values so Doctor cannot pass using a token
  that is no longer present in the file.

## 2026-07 — Security architecture is normative and the model is non-authoritative

- `security-architecture.md` defines stable `SEC-*` requirements, data classes,
  trust boundaries, feature-review evidence, adversarial tests, and hosted
  launch gates. Target hosted controls are explicitly distinguished from the
  current single-principal runtime.
- The model may propose a versioned typed intent, but deterministic code owns
  actor and tenant identity, capability selection, argument validation, policy,
  approval, credential access, and provider effects. Prompt instructions and
  prompt-injection detectors are not authorization controls.
- Hosted Attune uses tenant-aware durable services and stateless workers; local
  SQLite, JSON, JSONL, and Qdrant state are not stretched into a shared tenant
  boundary.
- Autonomy can progress only within a product-defined risk ceiling. History or
  memory cannot unlock autonomous external sends, destructive/bulk operations,
  sharing changes, or access grants.
- Security exceptions are explicit, owned, compensating-control-backed, and
  time-bounded.

## 2026-07 — Initial hosted membership uses a one-purpose operator boundary

- A successful Identity Platform login never creates Attune membership from an
  email or domain. Zero mappings fail closed before an application session is
  issued.
- The first development mapping is created by a private Cloud Run job with a
  distinct workload/IAM database identity. It can execute one fixed
  `SECURITY DEFINER` function and has no direct tenant-table access.
- The function creates a tenant atomically with its first principal, serializes
  concurrent calls, makes exact replay idempotent, and rejects conflicting
  subject or slug state. It cannot add members to an established tenant.
- Only a locally derived SHA-256 subject hash crosses the boundary, through a
  one-time CMEK-backed secret version destroyed after execution. Terraform,
  job overrides, image layers, and content-free logs contain no identity
  material.
- The bulk-access migrator remains migration-only and accepts no runtime
  overrides. It is not an identity administration interface.

## 2026-07 — Customer exports use disjoint write, download, and cleanup identities

- The control plane exposes only the account-and-preferences scope during the
  private alpha. Request and download authorization require recent owner auth;
  status remains owner-bound but does not expose storage or key metadata.
- The writer has object create/delete plus KMS encrypt, the download gateway has
  exact object get plus KMS decrypt, and cleanup has exact object delete. None
  combines read/decrypt with delete, and no identity can list export objects.
- Download uses a 90-second random one-time secret in POST bodies, never a URL
  or signed storage link. It authenticates/decrypts before atomically consuming
  the grant; consumed objects are scheduled for exact-generation deletion.
- Automated cleanup uses a fourth scheduler identity that can invoke only the
  bounded cleanup job. The bucket lifecycle remains disaster backstop, not
  application deletion evidence.

## 2026-07 — Qdrant server mode is the memory default

- Attune defaults to the durable Qdrant server at `127.0.0.1:6333`; embedded
  Qdrant/SQLite is not an implicit fallback because Mem0 writes on worker
  threads and the local SQLite client is not safe across those threads.
- Runtime memory configuration and Doctor consume the same typed host and port,
  so a passing readiness check validates the service the runtime actually uses.
- The Compose assistant overrides the host with the internal service name
  `qdrant`; host-based deployments retain the loopback default.

## 2026-07 — Channel conversation uses bounded live Workspace reads

- Slack and Google Chat share one natural-language planner rather than separate
  channel keyword routers.
- The planner selects a fresh brief, capped Gmail search, bounded Calendar
  window, or general memory-informed conversation. Direct OAuth and MCP behave
  identically above the connector boundary.
- Live results are provenance-framed as untrusted, source fields are bounded,
  and answers must be grounded in the returned data. Read failures are
  reported rather than silently replaced with memory-only answers.
- Free-form mutations are recognized but refused. Writes remain in explicit,
  audited workflows with autonomy gates and human approval.

## 2026-07 — Hosted channel conversation is asynchronous and brokered

- Linking and fixed-content delivery verify a destination but do not activate
  natural-language processing.
- Verified ingress passes bounded provider facts to the private channel broker,
  which alone resolves an active tenant binding and atomically deduplicates the
  event, appends the user turn, and creates a fixed dispatch intent.
- Hosted workers obtain bounded Workspace results through the secret broker and
  bounded model results through a separate model gateway. They receive neither
  OAuth refresh tokens nor model API credentials.
- Responses return through the channel broker to the canonical encrypted
  owner-DM route. The full contract and gates are in
  [`hosted-conversation.md`](hosted-conversation.md).

## 2026-07 — Routes and MCP capability contracts fail fast

- Selecting a channel route is an operational commitment. Doctor now treats
  missing channel credentials, destinations, interaction allowlists, and Chat
  approval subscriptions as fatal configuration errors instead of letting the
  runtime silently omit delivery.
- An empty route explicitly disables that behavior and remains valid.
- The generic Workspace MCP adapter has a versioned contract. Version 1 requires
  four Gmail tools and two Calendar tools; Doctor checks `tools/list` before
  startup. The contract intentionally supports draft creation but not sending.
- Live Chat and MCP conformance remain deployment smoke tests because they
  require chosen external services and credentials; offline reference fixtures
  pin the protocol-independent behavior.

## 2026-07 — Rename and provider-neutral configuration

- The project, distribution, import package, CLI, state defaults, and current
  documentation are named Attune / `attune`.
- Model access uses `openai.OpenAI(api_key=..., base_url=...)`. Compatible
  gateways already use bearer authentication through the SDK, so the separate
  transport package was deleted.
- Base URLs, chat models, extraction model, embedding model, and dimensions are
  configuration. No gateway or model catalog is hardcoded.

## 2026-07 — One principal; portable deployment

- An instance represents one principal with isolated credentials, memory,
  workflow state, and audit data. There are no organization-named or
  personal/corporate configuration branches.
- Hosting target is operational configuration. Polling is portable and default;
  Google Pub/Sub is named explicitly wherever Google-specific infrastructure is
  required.

## 2026-07 — Google OAuth and MCP are both supported

- Direct Google OAuth is the default and supports polling and Pub/Sub.
- MCP Streamable HTTP is a real polling backend, not a placeholder. Its benefit
  is moving credentials, consent, policy, and auditing to a managed boundary;
  it is not assumed to provide richer product functionality.
- Shared and service-specific MCP endpoints are supported. Runtime startup and
  Doctor validate tool availability without loading Google user credentials.

## 2026-07 — Explicit optional-channel routing

- Slack and Google Chat are optional peers.
- Briefs and notifications can target multiple channels. Approvals target one
  channel to avoid decision races. Interaction surfaces are independently
  selectable.
- Google Chat app messages and card actions use a verified synchronous endpoint
  and stateless Pub/Sub handoff. Proactive Chat messages use a separate app
  service account, not the principal's Workspace OAuth credential.

## 2026-07 — Initializer edits instead of overwriting

- `attune init` loads an existing `.env`, masks secrets, uses current values as
  defaults, preserves comments and unknown variables, migrates legacy keys,
  creates a backup, writes atomically, and uses owner-only permissions.
- Blank keeps a value and `-` clears it. `--fresh` is the explicit destructive
  reset path.

## Durable workflow and security decisions

- LangGraph checkpoints all approval workflows; pending approvals survive
  restarts and resume idempotently.
- Autonomy is granted per action/domain and progresses from observe to draft to
  notify-after-action to autonomous action. The assistant never self-grants.
- Untrusted workspace content is provenance-tagged. Notification payloads are
  reconciliation signals rather than direct commands.
- The credential-holding runtime opens no public listener. The republisher is
  stateless and has only publish permissions.
- Source cursors advance after successful processing or durable retry enqueue.
- Human actors and proactive destinations are allowlisted/reviewed; all effects
  and authorization failures are appended to the audit trail.
- Mem0/Qdrant provide current memory storage behind an internal interface so a
  future temporal/entity store can replace them without changing workflows.

## 2026-07 — Hosted Slack installation and conversation

- The one-use Slack OAuth `state` is the channel setup secret: the browser
  receives it exactly once inside the fixed authorize URL, the database stores
  only its hash, and the private broker consumes it through the same
  claim/pre-audit/consume ceremony as a Google Chat link code.
- Because Slack's callback is a cross-site top-level navigation, origin and
  CSRF headers cannot authenticate it. The binding is the Attune session
  cookie plus the one-use state, and `consume_slack_install` independently
  rechecks the session's tenant and principal against the setup transaction.
  Tenant identity is accepted only from the exact control-plane workload
  identity, mirroring the delivery-test trust decision.
- Only the private channel broker holds the Slack client secret and bot
  token. The bot token is retained solely as a per-destination AES-256-GCM
  envelope in the forced-RLS `hosted_channel_credentials` table
  (credential/crypto-erase lifecycle class), separate from the destination
  route envelope, and a returned Slack user token is refused outright.
- The broker verifies the fixed app ID, `bot` token type, and the exact scope
  set `chat:write`, `im:write`, `im:history`; any extra or missing scope
  fails installation. The initial hosted release supports installer owner-DMs
  only.
- Slack ingress is a separate public service with its own workload identity.
  It authenticates requests by v0 HMAC over the raw body within a five-minute
  window, accepts only plain human `im` messages (no subtype, bot, or edit
  markers), and acknowledges everything else content-free so Slack does not
  retry. The channel broker requires all four caller identities (both
  ingresses, control plane, worker) to be distinct.
- Slack provider references are HMAC-hashed under a `slack` domain separator
  (`teams/…`, `teams/…/users/…`, `teams/…/channels/…`, `…/messages/{ts}`),
  so Google Chat and Slack references can never collide in shared tables.
- The bounded read-only conversation executor is shared: Slack parameterizes
  the job kind (`channel.slack.converse`), surface, event kind, and reply
  route as SQL parameters and constructor arguments rather than duplicating
  the executor. Workspace reads still use the tenant's Google connector.
- Google Chat SQL functions are never modified for Slack; migration 0038 adds
  parallel Slack functions plus `disconnect_hosted_channel_destination_v2`,
  which delegates Google Chat to the original audited function and extends
  the ceremony to delete Slack credentials.

## 2026-07-17 — Per-provider ingress identities

- Each provider ingress runs its own workload identity; Google Chat ingress
  and Slack ingress are never the same service account.
- The channel broker enforces distinct caller identities per route and
  refuses to start if any two of its caller identities coincide, so a
  compromised provider ingress can exercise only its own provider's broker
  routes.
- Dispatch attribution is a separate mechanism from the channel broker's
  distinct-identity check: the dispatch broker's caller map now accepts
  multiple authorized emails per producer kind (needed once the Slack
  ingress identity required its own `run.invoker` grant), while unknown
  callers are still refused and duplicate entries are still rejected at
  startup.

## 2026-07-17 — Subnet-scoped NAT exception

- Internet egress exists only on the dedicated broker-egress subnetwork,
  reached through a subnet-scoped Cloud NAT, because Slack's API is ordinary
  internet rather than a Google API reachable over Private Google Access.
- Every other workload keeps the no-NAT fail-closed posture established for
  the GCP provider boundary; the NAT exception is scoped to that one subnet
  and does not extend arbitrary egress to any other service.
- The broker-egress subnet was widened from `/28` to `/24` after Cloud Run
  direct-VPC health checks refused the `/28` for insufficient free
  addresses; the NAT scope itself (that one subnet) is unchanged.
