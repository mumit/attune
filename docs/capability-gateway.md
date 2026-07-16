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

No public endpoint or model planner is wired to this gateway yet, and the
gateway does not execute or enqueue anything. It does not yet satisfy the full
execution checklist in section 8.1 of the security architecture. Before a
capability may be activated, its reviewed path must additionally bind:

1. data scope, destinations, and time range;
2. rate, concurrency, and cost budgets;
3. source freshness and provider resource version;
4. canonical idempotency and replay state;
5. content-free allow/deny audit through the private writer; and
6. exact approval plus recent authentication for the applicable tier.

R2 and R3 capabilities remain unavailable until the approval/effect-integrity
ceremonies implement those controls. R4 remains a dedicated non-model
administrative workflow. Registering a definition is necessary but never
sufficient to activate a write.

## Evidence

Unit tests reject malformed, oversized, unknown, wrong-version, extra-field,
raw-request, URL, identity, connector, and risk-smuggling proposals. They also
exercise missing authority, repository failure, policy ceilings, immutable
results, duplicate definitions, and unsafe registry configuration.

The disposable PostgreSQL 16/pgvector suite exercises the real control-plane
role and forced-RLS schema. It proves valid same-tenant admission, cross-tenant
refusal, stale-policy refusal, and refusal when two active connectors make
authority ambiguous. Run both gates with:

```bash
pytest -q tests/test_capability_gateway.py
scripts/test-hosted-db.sh
```

The next safe integration point is immediately before the exclusive dispatch
producer creates a canonical job and dispatch intent. The broker and worker
must continue to rebind purpose/capability to canonical database state; gateway
admission does not replace their independent checks.
