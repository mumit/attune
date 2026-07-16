# Hosted read-only policy ceremony

The first hosted policy profile is intentionally narrow. It gives a recently
authenticated tenant owner one explicit choice: enable private-alpha R0
read-only behavior, or leave the policy step incomplete. It does not expose a
generic policy editor and does not activate provider execution by itself.

## What the owner sees

After starting guided setup, the signed-in page shows the fixed profile before
confirmation:

- **Automatic:** verify the existing read-only Gmail and Calendar connection.
- **Not permitted:** send messages or email, change calendar events, delete or
  share provider data, or perform any R1–R4 capability.

The owner chooses **Enable read-only policy**. A session older than ten minutes
cannot make this authority change; the page asks the owner to sign out and sign
in again. The fixed review response contains only profile/version, R0, bounded
descriptions, and current status. It contains no tenant, principal, policy,
grant, connector, scope, or credential identifier.

## Deterministic effect

The browser sends an empty same-origin, CSRF-protected confirmation request. It
cannot submit a policy document, risk tier, capability, domain, actor, grant,
or resource reference. Trusted code binds the current opaque session and calls
one fixed PostgreSQL function under transaction-local tenant context.
The function independently rechecks that the opaque session ID belongs to the
same owner and tenant, is unrevoked/unexpired, and was created within ten
minutes; recent-authentication enforcement is therefore not only a browser or
route convention.

The function is owned by the memberless `attune_policy_executor` role. That
role has only the table privileges needed to read the active tenant/principal,
insert the fixed policy and its one exact grant, and advance the policy setup
step. The ordinary control-plane database role has read access but no direct
insert/update permission on policies or autonomy grants.

The exact version-1 document is:

```json
{
  "schema_version": 1,
  "profile": "private_alpha_read_only",
  "maximum_risk": 0,
  "capabilities": ["google.workspace.connection.verify"]
}
```

The matching grant is owner-bound, domain `private_workspace`, maximum risk
R0, and tied to the active policy version. Creation and onboarding advancement
are atomic and serialized per tenant. Repeating the same confirmation is
idempotent and does not increase the revision. An existing active policy or
grant set must match exactly; otherwise Attune marks the step
`externally_modified`, grants nothing new, and requires repair.

## Audit and failure behavior

Before database mutation, the control plane creates a content-free `allowed`
audit intent and requires the private audit writer to durably append it. Audit
outage prevents the effect. After the idempotent database function returns, it
records `observed` or `failed` through a separate deterministic intent. The
audit fields contain hashed actor/profile references, fixed action/outcome, and
profile version only.

If post-effect audit is temporarily unavailable, the API reports failure but
does not roll back or repeat the already committed grant blindly. Retrying the
same session reuses the audit intents and the idempotent policy function, then
finishes the missing audit write. Database, audit, and exception details never
reach the browser.

## Deployment and activation order

1. Build and deploy the immutable migrator; apply
   `0019_hosted_read_only_policy.sql`.
2. Require the database boundary verifier and an empty data Terraform plan.
3. Deploy the control-plane image with `enable_hosted_policy = false`.
4. Verify private audit-writer invocation and recent-session negative tests.
5. Set `enable_hosted_policy = true`; review the plan for only the environment
   gate and exact Cloud Armor priority `885` paths.
6. Confirm that unauthenticated review reaches the application and fails 401.
7. Have the recently authenticated owner review and explicitly enable the
   profile; verify allowed/observed audit, exact database rows, resumable UI,
   and zero Terraform drift.

This ceremony creates policy authority only. The typed capability gateway is
still not connected to a model planner or dispatch producer, so the grant
cannot cause a provider call until the remaining execution gates in
[`capability-gateway.md`](capability-gateway.md) are implemented and reviewed.
