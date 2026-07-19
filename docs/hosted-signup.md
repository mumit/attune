# Hosted production signup

This closes `docs/future-state.md` Phase 6's "hosted onboarding: production
signup (replacing operator provisioning)" bullet (`docs/gap-analysis.md` G19)
and the UX review's hosted item #2: today a new Identity Platform subject's
first sign-in dead-ends at `identity_membership_unavailable` (409) because
membership is created only by the private operator ceremony in
[`identity-platform.md`](identity-platform.md) and
[`decisions.md`](decisions.md)'s "Initial hosted membership uses a
one-purpose operator boundary" entry. `docs/user-journey.md` §0 already
promises "a production signup flow will replace this development ceremony
with an explicit tenant creation or invitation step." This document designs
that flow, dormant behind a default-off gate, and is read before touching any
identity-boundary code.

## 1. Explicit-consent principle

Signup is a **deliberate `POST` the human makes after identity
verification** -- never a side effect of login. `POST /v1/session` (login)
never creates a tenant; a zero-mapping subject still gets exactly the same
`409 identity_membership_unavailable` response it gets today, byte-identical
(pinned by test). Creating an account is a second, explicit action the human
takes only after seeing that dead-end, mirroring how connecting Google
Workspace is a second explicit action after signing in (`user-journey.md`
§0), never bundled into the sign-in click.

Membership is still **never inferred from email or domain**. The signup
route does not read, request, or store the Identity Platform email claim; it
crosses the boundary with the same locally-derived SHA-256 subject hash the
login and operator paths already use. A verified Google account creates
exactly one new tenant with itself as the sole principal, or -- if that exact
subject is already mapped -- resolves to that existing mapping. It can never
join, list, or affect any other tenant's membership.

## 2. Trust chain: reuse, not reimplementation

`POST /v1/signup` accepts exactly the payload shape `POST /v1/session`
already accepts and requires the same anti-CSRF login binding:

1. `GET /v1/session/bootstrap` (already gated, already exists) issues the
   five-minute `__Host-attune_login` challenge cookie.
2. The browser completes the same Google Identity Platform popup flow and
   `POST`s `{"id_token", "login_challenge"}` to `/v1/signup`, same-origin,
   with the login-challenge cookie compared using the same
   `hmac.compare_digest` check `open_session` uses.
3. The token is verified by calling `verify_identity_platform_token` --
   the exact function `open_session` calls, not a parallel copy. Same
   issuer/audience check, same Google-provider-and-verified-email
   requirement, same five-minute `auth_time` freshness window, same
   compiled-in certificate URL. A tampered, expired, wrong-audience, wrong-
   provider, or unverified-email token is refused identically to login
   (parameterized tests share `test_identity.py`'s `claims()` fixture).
4. Only the resulting `VerifiedIdentity.subject_hash` (already a local
   SHA-256 digest -- the raw subject string never leaves
   `verify_identity_platform_token`) and `issuer` cross into SQL, exactly as
   they do for `open_session`.

There is exactly one token-verification code path in the codebase. Signup
does not get its own JWT validation, its own certificate fetch, or its own
freshness window -- it calls the same function with the same
`project_id`, through the same `token_verifier` constructor hook
`control_plane_service.create_app` already exposes for tests.

## 3. Why signup runs without an Attune session

Zero mappings fail closed **before** a session is issued -- that is the
entire point of the operator-boundary decision. Signup is therefore
necessarily the one authenticated-but-sessionless ceremony in the control
plane: there is no session to require, because the very thing the ceremony
does is decide whether one may ever exist for this subject. Its authority
is bound to the verified token itself (the fresh, freshness-checked,
signature-verified `VerifiedIdentity`), not to a cookie that does not exist
yet.

This is different from every other mutating route in `control_plane_service`
(policy confirm, channel setup, export requests), which all require an
established `IdentitySession` plus same-origin/CSRF proofs, several with the
additional ten-minute recency gate. Signup cannot use `_authorize_mutation`
at all -- there is no session repository call that could ever return one.
Instead it reuses exactly the mechanism `open_session` uses to resist
sign-in CSRF: the same-origin check plus the login-challenge cookie
comparison. No new anti-forgery primitive is introduced.

## 4. Tenant creation: a new function, not a reused grant

**Decision: new function, same owner role.** `attune.provision_initial_identity`
(migration 0016) is not EXECUTE-granted to the control plane. Two properties
of that function make reuse unsafe for a self-service path:

- **It accepts a caller-supplied `tenant_slug`.** The operator ceremony
  deliberately passes a human-chosen slug from `ATTUNE_INITIAL_TENANT_SLUG`.
  If `attune_control_plane` -- reachable by an ordinary web request, unlike
  the private `attune-development-identity-provision` job's distinct
  workload identity -- could pass an arbitrary slug, it could also *learn*
  slug collisions: a `23505` conflict on a slug that maps to someone else's
  tenant is an oracle for "this slug is taken," and, worse, a slug the
  control plane does not own could be used to attempt to attach a second,
  different subject as if it were that tenant's "first" principal by racing
  the empty-tenant branch. The operator ceremony is safe *because* the slug
  is a fixed, reviewed, operator-supplied constant used exactly once, not
  because the function itself refuses bad slugs.
- **Granting it to `attune_control_plane` would blur two distinct
  boundaries.** `decisions.md`'s "one-purpose operator boundary" entry is
  explicit that the function is reachable only by a distinct workload/IAM
  identity with no other capability. Adding a second, very differently
  trusted caller (a public-facing web process handling arbitrary requests)
  to that same function's grant list is exactly the "weakens the operator
  boundary" case the task brief warns against, independent of whether the
  slug problem above existed.

So migration 0045 adds **`attune.provision_hosted_signup_tenant(subject_hash,
issuer, region)`** -- no slug parameter at all. The function generates its
own slug from the tenant id it creates
(`'tn-' || replace(tenant_id::text, '-', '')`), so there is no caller-supplied
value that could ever become part of an identifier. It preserves every
property the operator function has:

- **Atomic tenant + first principal.** One `INSERT` creates the tenant, the
  next creates its sole principal, in one transaction.
- **Serialized.** It takes the *same* fixed advisory lock
  (`pg_advisory_xact_lock(214748301)`) `provision_initial_identity` already
  takes. Both ceremonies write into the same `(issuer, subject_hash)`
  uniqueness space on `attune.principals`, so they must serialize against
  *each other*, not just against themselves -- a signup call racing the
  private operator job for the same subject must not create two tenants.
  Sharing the lock constant is deliberate, not an oversight.
- **Idempotent on exact replay.** A second call for a subject that already
  has exactly one active principal in an active tenant returns that same
  `(tenant_id, principal_id, created=false)` row rather than erroring or
  creating a second tenant.
- **Cannot add members to an established tenant.** There is no tenant
  identifier input at all -- the function can only ever create a brand-new
  tenant or recognize the caller's own prior signup. It has no code path
  that reads or writes any tenant other than the one the exact subject
  already belongs to.
- **Fails closed on ambiguity.** If the subject somehow matches more than
  one principal, or matches a principal whose tenant or own status is not
  `active`, the function raises the same generic `23505` conflict the
  operator function raises. The control plane maps every such conflict to
  a generic `503`-class "unavailable" response -- it never distinguishes
  "ambiguous" from "suspended" from "database hiccup" in what the caller
  sees, so no exception message can leak information about another tenant.

**Ownership: the existing memberless owner role, not a new one.** The
function is owned by `attune_identity_provisioning_executor` -- the same
`NOLOGIN BYPASSRLS` memberless owner migration 0016 already created. That
role already holds exactly `SELECT, INSERT` on `attune.tenants` and
`attune.principals` and `USAGE` on `attune`/`attune_ext` -- precisely the
access the new function needs, no more. Introducing a second owner role with
an identical grant footprint would be pure duplication; the memberless-owner
pattern is about the *function* being the sole gateway to those grants, not
about one owner role per caller. Migration 0045 therefore needs no new role,
no new `FUNCTION_OWNER_ROLES` entry, no new `FUNCTION_OWNER_TABLE_PRIVILEGES`
tuple, and no new schema-privilege row in `migrate.py`'s boundary verifier --
it only appends one new `(signature, caller_role, owner_role)` tuple to the
existing `privileged_functions` catalog:
`("attune.provision_hosted_signup_tenant(bytea,text,text)",
"attune_control_plane", "attune_identity_provisioning_executor")`.

**Why `attune_control_plane` gets `EXECUTE` only through this function.**
The grant is `GRANT EXECUTE ON FUNCTION
attune.provision_hosted_signup_tenant(...) TO attune_control_plane` and
nothing else -- no `SELECT`/`INSERT` on `attune.tenants` or
`attune.principals` is ever granted to `attune_control_plane` directly, for
this feature or any other. `SECURITY DEFINER` plus a memberless owner is what
makes that possible: the function runs with the owner role's table
privileges regardless of who calls it, so the caller only ever needs
`EXECUTE`. This is the same shape every other control-plane mutation already
uses (`open_identity_session`, `activate_hosted_read_only_policy`,
`configure_hosted_channels`, ...) -- signup does not introduce a new access
pattern, it is one more instance of the established one.

## 5. No session is minted by signup

`POST /v1/signup` returns `{"status": "created"}` (`201`) or `{"status":
"already_provisioned"}` (`200`) and nothing else -- no session cookie, no
CSRF cookie. The client then performs the **ordinary** sign-in flow
(`POST /v1/session`, same as any other visit).

This was chosen over minting a session inline for three reasons:

- **One auditable path for "how did this session get opened."** Every
  session in the system is opened by `open_identity_session`, called from
  exactly one route (`POST /v1/session`), following exactly one successful
  membership lookup. Minting a session from the signup route would create a
  second code path capable of setting `__Host-attune_session`, which is
  exactly the kind of duplication the identity boundary's threat model
  exists to prevent.
- **The membership the freshly-provisioned subject needs to open a session
  is the membership signup just created.** `open_identity_session` already
  does the "resolve exactly one active mapping" work; calling it a second
  time from a different route is redundant, not an optimization.
- **The token is still fresh enough.** `verify_identity_platform_token`'s
  five-minute `auth_time` window comfortably covers "click create account,
  then click continue with Google" as one user-perceived action (the
  browser does the second popup automatically in the same interaction --
  see §7); there is no meaningful latency cost to "sign up, then sign in."

If a future phase finds the two-step flow adds real friction, reusing
`open_session` verbatim (not reimplementing it) from inside the signup
handler after a `created` result would be the only acceptable change --
this document intentionally leaves that as a documented option rather than
building it now, since it is not required for a dormant, gated first phase.

## 6. Tenant slug and name derivation

The slug is **entirely server-generated** from the tenant id the function
itself creates via `attune_ext.gen_random_uuid()`
(`'tn-' || replace(tenant_id::text, '-', '')`). No user-controlled string --
not even a sanitized one -- ever reaches `attune.tenants.slug` through this
path. There is no request field for it; `POST /v1/signup`'s only body
fields are `id_token` and `login_challenge`, exactly like `POST /v1/session`.

This phase deliberately accepts **no display name at all**, bounded or
otherwise. `attune.tenants` has no display-name column today, and adding one
is unnecessary scope for a first, dormant phase whose job is to replace the
operator dead-end -- not to build tenant naming. The principle the task
brief asks this document to record still holds for whenever that column is
added: a display name is *data* (bounded, stored in a plain column or the
onboarding state, freely later editable) and must never be concatenated into
`slug` or any other identifier. Region is likewise not caller-supplied: the
control plane passes one operator-configured `ATTUNE_HOSTED_SIGNUP_REGION`
value, mirroring the operator ceremony's `ATTUNE_INITIAL_TENANT_REGION`.

## 7. Abuse posture

**Cloud Armor remains the authoritative, global control**, exactly as it is
for every other onboarding ceremony
(`hosted-policy.md` priority `885`, `hosted-channels.md` priority `886`, the
channel-installation route at `887`): a fixed, reviewed edge rule admitting
only the exact `/v1/signup` path at **10 requests per 60 seconds per IP** --
the same constant every other infrequent, deliberate onboarding ceremony
uses (`docs/decisions.md`'s "Web conversation acceptance uses ordinary
proofs, not recency" entry contrasts this 10-per-60-second class against the
wider 60-per-60-second polling class; signup is unambiguously in the
infrequent-ceremony class, not the polling class). **This edge rule is
required operator work before activation** and is intentionally not added to
Terraform by this change -- consistent with how `ATTUNE_ENABLE_HOSTED_BRIEF`
shipped implemented-and-tested-but-not-deployed with no edge rule authored
yet. The next unused priority in the reviewed range is `894`; this document
records that fact for the operator who eventually authors the rule, without
claiming it has been applied.

**An in-process, per-key request throttle is added defensively in the
application** (`hosted_signup.py`'s `SignupThrottle`, constants
`HOSTED_SIGNUP_THROTTLE_LIMIT = 10` requests per
`HOSTED_SIGNUP_THROTTLE_WINDOW = timedelta(seconds=60)`, deliberately mirroring
the edge constant so the two layers agree). It is checked twice per request:

1. **Per client IP** (the leftmost `X-Forwarded-For` entry), checked
   *before* token verification. Google's external HTTPS load balancer --
   the sole ingress to this Cloud Run service per `hosted-gcp.md`'s
   architecture table -- overwrites this header with the verified client
   IP; it is not spoofable by a direct client the way it would be behind an
   arbitrary reverse proxy. This check exists to bound the CPU cost of
   repeated signature verification from a single volumetric source before
   spending that cost.
2. **Per verified subject hash**, checked immediately after verification,
   to bound how many times one specific stolen-but-still-fresh token can be
   replayed regardless of source IP.

This throttle is **explicitly not a substitute for Cloud Armor**: it is
scoped to one running control-plane instance's memory, is not shared across
Cloud Run's multiple instances, and is reset on every deploy or restart.
Nowhere else in this codebase does the application layer implement its own
per-IP limiter -- every other ceremony (`open_session` included) relies
solely on the edge. Signup is the first to add an in-process backstop,
justified by the fact that it is also the first ceremony whose failure mode
is *creating billable tenant rows*, not merely reading or flipping an
existing tenant's state -- a slightly higher bar than the rest of the
onboarding surface.

## 8. Content-free audit

Audit for `created` and `already_provisioned` is written through the
**existing** tenant-scoped `attune.audit_intents` -> `attune.audit_events`
pipeline (`PostgresAuditProducerRepository` / `AuditWriterClient`), the same
pipeline `HostedPolicyService` and `HostedChannelService` already use. The
event's `actor_ref_hash` is the verified subject hash (never the raw subject,
never the email); `metadata` carries only `{"created": true|false}`
(schema-versioned via the fixed `action` string, not a free-form value). The
idempotency key is derived from `(tenant_id, outcome)`, so a subject that
calls signup a hundred times after being provisioned produces one durable
`already_provisioned` audit row, not a hundred -- consistent with how this
ledger already records durable facts, not a per-request access log.

**`attempted` and `throttled` cannot use that pipeline**, and this is a
structural fact worth stating plainly rather than working around: every row
in `attune.audit_intents` has a `NOT NULL` foreign key to `attune.tenants`
(migration 0004), and both of these moments happen *before* any tenant
necessarily exists for the caller. This is not a new gap this feature
introduces -- `open_session`'s own `409` (zero-mapping) response has never
been written to `audit_intents` either, for the identical reason. Both
events are instead recorded as fixed, content-free process log lines (no
subject hash, no IP, no email -- matching the existing
`LOG.warning("google_oauth_exchange_refused stage=...")` style already used
in `google_oauth.py`), which is what `identity-platform.md`'s activation
gates already call "content-free logging" for this boundary.

## 9. The default-off gate

`ATTUNE_HOSTED_SIGNUP_ENABLED` (`true`/`false`, default `false`) follows the
exact pattern every other hosted control-plane feature flag uses in
`control_plane_app.py`. When it is `false` (or identity itself is off),
`POST /v1/signup` does not exist -- Flask never registers the route, so the
response is the framework's ordinary `404`, identical in shape to how
`/v1/session/bootstrap` 404s while identity itself is dormant. When it is
`true`, `create_app` requires identity to be enabled and a
`HostedSignup`-shaped service to be supplied, exactly like every other
`..._enabled` flag's validation in `create_app`.

## 10. Explicitly out of scope

- **Invitations and multi-member tenants.** The provisioning function
  preserves the one-principal-per-tenant invariant of
  `provision_initial_identity` exactly: it can create a tenant with its
  first principal, or recognize its own caller as that principal, and
  nothing else. Inviting a second principal into an existing tenant is a
  different function, a different audit shape, and a different consent
  ceremony (the invitee's own explicit acceptance) -- none of it is touched
  here.
- **A caller-chosen tenant name or slug**, per §6.
- **Minting a session from the signup response**, per §5.
- **A new anti-abuse primitive beyond the in-process throttle**, per §7 --
  CAPTCHA, phone verification, and similar are not part of this phase.

## 11. What remains operator work before activation

Setting `ATTUNE_HOSTED_SIGNUP_ENABLED=true` in an environment additionally
requires, mirroring every prior activation in `hosted-gcp.md`:

1. Migration 0045 applied and the database boundary verifier passing (new
   `privileged_functions` tuple, unchanged role/table/schema-privilege
   catalogs).
2. The Cloud Armor edge rule from §7 authored, reviewed, and confirmed live
   (recommended priority `894`, exact path, 10-per-60-second-per-IP).
3. A live probe: an unprovisioned test identity calling `/v1/signup`
   receives `created`; a second call receives `already_provisioned`; the
   resulting subject can then complete the ordinary sign-in flow and reaches
   the same **Signed in to Attune** state the operator-provisioned path
   reaches today.
4. Abuse monitoring -- confirmation that the edge rule's rejected-request
   metric and the in-process throttle's rejection path are both visible to
   whatever the operator already watches for the other onboarding
   ceremonies (no new dashboard is designed here; this reuses existing
   observability, it does not invent new observability).

None of this is claimed to be done by this change. This document and the
code it describes bring the feature to *implemented, tested, and dormant* --
the same bar `ATTUNE_ENABLE_HOSTED_BRIEF` and
`ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY` were held to before their own
eventual activation gates.
