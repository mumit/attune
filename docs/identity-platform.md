# Hosted sign-in with Google Identity Platform

Identity Platform authenticates hosted users; it does not authorize access to
Gmail, Calendar, Chat, or any other Workspace data. Attune uses two separate
Google OAuth web clients so those trust decisions cannot be confused.

| Client | Purpose | Secret location | Redirect |
|---|---|---|---|
| Hosted sign-in | Identity Platform `google.com` provider with only identity scopes | Google Identity Platform provider configuration | The handler URI shown by Identity Platform setup details |
| Workspace connector | Explicit Google Workspace consent and offline refresh credential | Broker-only `attune-development-google-oauth-client` Secret Manager secret | `https://dev.attune.mumit.org/oauth/google/callback` |

Never reuse either client ID or secret for the other purpose. A successful
sign-in is not connector consent, and a connector ID token is not an Attune
login credential.

## Security contract

The browser obtains an Identity Platform ID token through Google's supported
web flow. The control plane accepts it only with an exact same-origin request
and a five-minute, `Secure`, `HttpOnly`, `SameSite=Lax` login-binding cookie. It
then requires:

- the exact `https://securetoken.google.com/PROJECT_ID` issuer and project
  audience;
- a Google-provider Identity Platform claim, verified email, and authentication
  time no more than five minutes old;
- a valid Google signature fetched from the one compiled-in certificate URL;
  and
- exactly one active Attune tenant/principal mapping for the hashed Identity
  Platform subject.

Email address and domain never establish tenant membership. Zero matches and
multiple matches both fail closed. On success, Attune issues an independent
256-bit opaque session cookie and CSRF value. PostgreSQL stores only their
SHA-256 hashes. Sessions are tenant/principal-bound, revocable, capped at eight
hours, and resolved only through memberless-owner functions; the control-plane
database role has no direct session-table access.

The sign-in page uses a version- and integrity-locked Firebase Auth bundle built
inside the image. Provider state is memory-only. After Google returns, the page
exchanges the fresh Identity Platform ID token for the independent Attune cookie
and signs out the transient Firebase client. Content Security Policy permits the
local bundle, Google's fixed popup helper, the two Identity Platform APIs, and
the exact generated authentication frame; no provider token enters a URL,
Terraform value, application log, or durable browser store.

## Development operator ceremony

The foundation enables `identitytoolkit.googleapis.com`, but Terraform does not
initialize Identity Platform, manage its generated browser key, or manage its
Google provider. Initialization is a one-time control-plane mutation, while the
provider resource requires a client secret that would be persisted in Terraform
state. Keep these actions in a separately reviewed operator ceremony.

1. In project `attune-development-502421`, open **Identity Platform** and select
   **Get started**. Keep anonymous, email/password, and phone sign-in disabled,
   and do not enable any provider other than Google. Google sign-in may create
   an Identity Platform user, but that user gains no Attune tenant membership
   unless an operator separately creates the exact principal mapping.
2. In **APIs & Services > Credentials**, restrict the auto-created Firebase
   browser key to the exact HTTPS Attune origin and Google's generated auth
   handler/hosting origins. Restrict its API allowlist to Identity Toolkit
   (`identitytoolkit.googleapis.com`) and Token Service
   (`securetoken.googleapis.com`). The key is public project identification,
   not user authorization, but these controls limit cross-site quota abuse.
   Test the full popup and redirect flows after every restriction change.
3. Create a dedicated **Web application** OAuth client for Attune sign-in. Add
   `https://dev.attune.mumit.org` as its JavaScript origin and use the exact
   Identity Platform handler URI shown under **Setup details** as its redirect.
   Request identity scopes only; do not add Workspace scopes.
4. Add the Google provider in Identity Platform using that sign-in client ID and
   secret. Add `dev.attune.mumit.org` as an authorized domain. Do not add
   `localhost` to the development or production provider.
5. Create a second Web application OAuth client for Workspace connector consent
   with the exact redirect
   `https://dev.attune.mumit.org/oauth/google/callback`. Download its standard
   client JSON to an owner-readable temporary file. Check **Google Auth
   Platform > Audience** before activation:

   - **External + Testing** displays a **Test users** section. Add each
     development account there; testing grants expire after seven days.
   - **Internal** does not display a test-user list. Accounts in the Google
     Workspace organization are the allowed audience.
   - **External + In production** does not display a test-user list. Sensitive
     scopes may instead trigger Google's unverified-app limits until the app is
     verified.

   Absence of the Test users control is therefore expected for Internal or
   published apps; it is not a client-creation failure.
6. Add that complete JSON as a new version of the existing connector secret
   without placing it in a command argument or Terraform state:

   ```bash
   chmod 600 /path/to/connector-client.json
   gcloud secrets versions add attune-development-google-oauth-client \
     --project=attune-development-502421 \
     --data-file=/path/to/connector-client.json
   rm -f /path/to/connector-client.json
   ```

The local file deletion is not guaranteed secure erasure on copy-on-write or
encrypted filesystems; prefer a protected ephemeral volume and retain no cloud
sync, shell history, support bundle, or CI artifact containing either secret.
Do not print the complete Identity Platform project configuration during
verification: callers with hash-config permission can receive password-hash
material even when password sign-in is unused. Query or redact only the exact
non-secret fields under review.

## Staged development activation

The first subject cannot be mapped safely before it exists. Development may set
the two identity flags true only after the provider, authorized domains, browser
key restrictions, fixed browser assets, Cloud Armor routes, and content-free
logging have been reviewed. At that point the database must contain no mapping
for the test identity. The first sign-in must verify Google and return the
generic unprovisioned-membership response without issuing an Attune session.
An operator then reads only the expected test user's non-secret Identity
Platform subject, hashes it, and creates exactly one tenant/principal mapping.

That mapping uses the private `attune-development-identity-provision` job, not
the database migrator or ad-hoc SQL. Its IAM database login receives only the
`attune_identity_provisioner` role, which can execute one fixed function and
cannot select or mutate tenant tables directly. The function serializes the
ceremony, creates a tenant only together with its first principal, treats an
exact retry as success, and rejects every conflicting existing state. The
operator pipes the selected Identity Platform subject directly through local
SHA-256 into a one-version CMEK-backed secret; the email, raw subject, and hash
are absent from Terraform, image layers, job arguments, and logs. Destroy the
version immediately after a successful job. The empty secret container remains
for an explicitly reviewed recovery ceremony.

The exact build, migration, execution, verification, and secret-destruction
commands are in [`../deploy/gcp/data/README.md`](../deploy/gcp/data/README.md).

Development identity sign-in and the first exact tenant/principal mapping were
activated and verified on 2026-07-15. A successful visit now reports **Signed
in to Attune**. This is not customer activation: connector OAuth stays off, no
Workspace data is accessible, and production remains disabled.

## Activation gates

Keep production identity disabled and keep development/production connector
OAuth disabled until all applicable items are evidenced:

- sign-in provider settings and authorized domains independently reviewed;
- separate client IDs and redirects verified;
- a dedicated non-production identity mapped to exactly one test tenant;
- valid, stale, wrong-project, wrong-provider, unverified-email, login-CSRF,
  session-fixation, CSRF, ambiguous-membership, disabled-principal, suspended-
  tenant, expiry, revocation, and replay tests;
- Cloud Armor route/rate rules, content-free logs, alerts, and a verified page;
  and
- connector callback non-retention plus the complete brokered consent test.

The development identity flags are now true. Connector OAuth is a later,
independent activation using the gates in
[`oauth-transaction.md`](oauth-transaction.md); it must not be inferred from
the identity activation.
