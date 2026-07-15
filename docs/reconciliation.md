# Hosted effect reconciliation

Hosted workers never translate an ambiguous provider or persistence result into
a blind retry. They atomically move the canonical leased job to `reconcile` and
open one tenant-bound, content-free `job_reconciliations` record.

## Intake contract

The worker may open only one of four fixed reasons:

- `pre_effect_audit`: required audit failed before the executor ran;
- `executor_ambiguous`: the executor raised after it may have attempted an
  effect;
- `post_effect_audit`: the executor returned but its required audit failed; or
- `job_finalize`: execution and audit returned but canonical job completion
  could not be confirmed.

The record contains the canonical job reference, fixed reason, state, times,
and optionally a one-way provider-request reference. It contains no provider
payload, result body, credential, exception text, or model output. Moving the
job and inserting the record share one tenant transaction. Replays return the
existing record and cannot replace the first ambiguity reason.

## Authority and isolation

The table is forced through the same tenant RLS boundary as jobs. Workers may
select and insert records but cannot resolve, update, or delete them. A database
trigger accepts only an open record for a canonical job already in
`reconcile`. The control-plane role is read-only; no HTTP/operator resolution
surface is enabled yet. Resolution will require a separate narrow database
function and authenticated operator workflow that refetches provider state,
records bounded evidence, audits the decision, and atomically selects an exact
terminal outcome.

Until that workflow exists, an open record is a stop signal. Operators may
inspect content-free counts and alerts but must not manually edit job state or
requeue the original effect. Expired leases without a reconciliation record are
a separate alert condition because they indicate that intake itself failed.

## Remaining gate

Before customer/provider writes, implement and adversarially test provider-
specific evidence collection, an audited resolution transition, alerting and
SLOs, tenant/operator authorization, replay behavior, and kill switches. The
current intake boundary makes ambiguity durable; it does not claim to decide
whether an external effect occurred.
