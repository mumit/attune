# Hosted capability gateway

This document defines the deterministic boundary between untrusted model output
and canonical hosted work. It supplements the normative requirements in
[`security-architecture.md`](security-architecture.md); it does not authorize a
provider effect by itself.

## Trust boundary

The only model-proposed object accepted by the first gateway contract is:

```json
{
  "version": 1,
  "capability": "google.workspace.connection.verify",
  "arguments": {}
}
```

The object has an exact schema and a 16 KiB serialized limit. Version must be
the integer `1` (not a truthy value), the capability must match the bounded
canonical name grammar, and arguments must satisfy the registered trusted
schema. Extra fields are refused. In particular, the proposal cannot contain a
tenant, principal, connector, provider, scopes, policy, risk tier, route, URL,
raw HTTP request, SQL, tool name, idempotency key, or approval assertion.

Registry membership is the infrastructure-owned enablement decision. A
registered definition fixes the contract version, product risk tier and
ceiling, policy domain, provider, exact required scopes, and argument
reconstructor. Duplicate definitions and definitions above their product risk
ceiling fail at construction time. Unknown names and versions fail closed.

## Authority resolution

The gateway receives `TenantContext` and principal UUID only from previously
verified trusted code. The PostgreSQL authority adapter resolves all remaining
authority in one tenant transaction under forced RLS. It requires:

- an active tenant and active principal;
- exactly one active tenant policy;
- exactly one unrevoked autonomy grant for the principal, capability, and
  fixed domain, bound to that active policy version;
- a grant maximum risk at least as high as the registered capability risk;
- exactly one active principal-owned connector for the registered provider;
  and
- a connector whose granted scopes contain every registered required scope.

Zero or ambiguous rows are the same denial. Database failure is also a denial.
The admitted object is immutable and binds the verified tenant/principal,
server-resolved connector, fixed capability/version/risk, active policy
version, and reconstructed arguments. It is suitable as input to a canonical
job producer; it is not a provider request and contains no credential.

## Current slice and non-goals

The implemented slice establishes proposal parsing, immutable trusted
reconstruction, exact registry lookup, atomic tenant-scoped authority
resolution, scope checks, stale-policy refusal, connector-ambiguity refusal,
and risk-ceiling enforcement. The initial exact-empty argument contract is
appropriate for operations whose operational input is entirely server-derived.

Registering a definition is necessary but never sufficient to activate a
write. R4 remains a dedicated non-model administrative workflow.

## Wired: the dispatch spine, and the first R2 write capability

This gateway is now wired to the real dispatch spine (docs/dispatch-broker.md)
for exactly one registered capability, `google.gmail.draft.create` v1,
registered at product risk tier **R2** -- the security architecture's own
risk-tier table (section 8.2) lists a Gmail draft as its R2 example
("explicit approval by default"); this registration conforms to that
normative table. The wiring is entirely dormant: no tenant holds an R2
autonomy grant (the fixed R0 profile in [`hosted-policy.md`](hosted-policy.md)
grants only `google.workspace.connection.verify` at R0), no Google OAuth flow
ever requests the `gmail.compose` scope this capability requires, and the
worker-side code path that can reach the registry at all sits behind
`ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY` (default off, worker-deployment
environment variable, not `.env.example`).

The flow, once the gate is on and a hypothetical tenant somehow held the
grant: a web conversation message matching a deterministic grammar (`"draft
reply <thread>: <body>"`, mirroring the memory command grammar's own
deterministic-first routing) is parsed entirely by trusted code -- the model
is not involved in constructing the proposal at all, and the gateway never
sees raw model JSON. Trusted code builds the exact
`{version, capability, arguments}` proposal and calls
`TypedCapabilityGateway.authorize()`. On denial, the executor returns an
honest "not authorized by your current policy" style message -- the
production case, since no tenant holds the grant. On admission, a new
producer (`attune.hosted.capability_admission
.PostgresCapabilityAdmissionRepository`) persists one immutable, append-only
`attune.capability_admissions` row and one pending `attune.approvals` row in
the same transaction -- admission is never execution authority, and no job or
dispatch intent exists yet. The stored assistant turn asks the owner to
reply `"approve draft"` or `"reject draft"`. That later message claims the
approval through a new one-use, actor-bound SECURITY DEFINER function,
`attune.claim_capability_approval` (migration 0043, owned by a memberless
`attune_capability_executor` role per the 0009 pattern), and only on a
`consumed` (approved) outcome creates the job and dispatch intent through the
existing, unmodified dispatch producer and sends the resulting intent to the
private broker. The worker then executes the fixed, one-use secret-broker
route for Gmail `users.drafts.create`, response-minimized to the draft id
only, and `WorkerDispatcher`'s existing pre/post-effect audit and
reconciliation-on-ambiguity apply to that execution unchanged.

`attune.approvals` (migration 0001, previously unused by any executor) is no
longer dormant scaffolding: its decide/consume transition is now a real
privilege boundary, not merely an atomicity convenience. Direct `UPDATE` is
revoked from every runtime role (`attune_worker`, `attune_control_plane`);
the claim function is the sole mutation path.

### What this satisfies against the section 8.1 execution checklist

1. **Data scope, destinations, time range** -- bound for this narrow
   capability: the thread reference is the approval's SEC-500 "destination"
   (hashed, bound into the approval row). There is no meaningful time-range
   dimension for a draft creation.
2. **Rate, concurrency, and cost budgets** -- **not implemented.** A
   remaining gate before activation.
3. **Source freshness and provider resource version** -- **partial.** The
   claim function reauthorizes immediately before honoring a decision
   (SEC-503): it re-checks that the resolved connector is still active and
   the policy version still matches the tenant's active policy. It does
   **not** refetch the live Gmail thread's own resource version (e.g. its
   `historyId`) before dispatch. A remaining gate.
4. **Canonical idempotency and replay state** -- implemented: the dispatch
   producer's idempotency key and the approval's one-use, SEC-501-idempotent
   claim (a replayed decision returns the recorded outcome rather than
   re-mutating or erroring).
5. **Content-free allow/deny audit through the private writer** -- **partial.**
   The job's claim/execute audit is `WorkerDispatcher`'s existing, unmodified
   mechanism. The admission-record and approval-decide steps themselves are
   **not yet separately audited** through the private writer. A remaining
   gap.
6. **Exact approval plus recent authentication for the applicable tier** --
   approval is implemented (SEC-500-502). Recent authentication (SEC-505) is
   a normatively **R3-specific** control and does not apply to this R2
   capability; this slice correctly does not implement it, and that is not a
   gap at this tier.

R3 capabilities remain unavailable until the approval/effect-integrity
ceremonies implement recent-authenticated approval of the exact action.
Before `google.gmail.draft.create` may be activated for any real tenant, the
budgets (2), live source-freshness refetch (3), and admission/approval audit
(5) gaps above must close, in addition to the fixed R0 policy still granting
no tenant R2 authority.

## Evidence

Unit tests reject malformed, oversized, unknown, wrong-version, extra-field,
raw-request, URL, identity, connector, and risk-smuggling proposals. They also
exercise missing authority, repository failure, policy ceilings, immutable
results, duplicate definitions, and unsafe registry configuration.
`test_gmail_draft_capability.py` exercises the registered R2 definition and
its bounded `{thread_ref, body}` argument schema.
`test_capability_admission.py` exercises the admission-persists-but-never-
dispatches pin, approve/reject/expired/not-found claim outcomes, and that a
refused broker dispatch surfaces as a failure rather than a false success.
`test_web_conversation_executor.py`/`test_google_chat_conversation_executor.py`
exercise the executor's draft grammar end to end with fakes, including the
gate-off byte-identical mutation-refusal pin (both for the web surface with
no gateway injected, and for the Google Chat surface, which never receives
one) and double-approve idempotency. `test_secret_broker_use.py`/
`test_secret_broker_service.py`/`test_secret_broker_client.py`/
`test_google_provider.py` exercise the fixed Gmail `drafts.create` route's
request shape, draft-id-only response minimization, and negative tests
(wrong connector/capability, oversized body, unknown request fields,
malformed provider response).

The disposable PostgreSQL 16/pgvector suite exercises the real control-plane
role and forced-RLS schema. It proves valid same-tenant admission, cross-tenant
refusal, stale-policy refusal, and refusal when two active connectors make
authority ambiguous. It also proves migration 0043 applies and the boundary
verifier passes; that `attune.capability_admissions` is truly append-only
(even a superuser-equivalent role cannot `UPDATE`/`DELETE` it); that direct
`UPDATE` on `attune.approvals` is refused for every runtime role while the
claim function's one-use, actor-bound semantics hold; and that
`PostgresCapabilityAdmissionRepository.record()` persists the admission and
its pending approval atomically without creating a job. Run both gates with:

```bash
pytest -q tests/test_capability_gateway.py
scripts/test-hosted-db.sh
```

The dispatch integration point this document used to describe as the "next
safe integration point" -- immediately before the exclusive dispatch producer
creates a canonical job and dispatch intent -- is now exercised end to end for
this one capability. The broker and worker continue to rebind
purpose/capability to canonical database state unchanged; gateway admission
still does not replace their independent checks, and no other capability or
surface is wired.
