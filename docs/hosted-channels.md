# Hosted channel preference ceremony

Hosted Attune lets the owner choose Slack, Google Chat, or both independently
for natural-language interaction and brief delivery. This ceremony records an
effect-free preference only. It does not install either app, accept a token,
select a destination, enable ingress, send a test message, or mark the channel
step complete.

## Owner experience

After guided setup starts, the owner sees two bounded checkbox groups:

- **Conversation:** Google Chat and/or Slack.
- **Morning briefs:** Google Chat and/or Slack.

At least one purpose must have a channel. The choices may differ—for example,
conversation in Google Chat and briefs in Slack. A browser-only option is not
offered because the hosted product does not yet have a conversational web
surface; presenting one would promise a route that does not exist.

Saving changes marks the channel step `authorized`, not `validated`. The page
states that app installation, owner/destination binding, verified ingress, and
a bounded test remain required. Once a future installation ceremony validates
the configuration, changing it requires an explicit replacement/disconnection
ceremony rather than silently retargeting messages.

## Security boundary

The request has exactly three fields: schema version 1 and two arrays whose
only values may be `google_chat` and `slack`. Duplicate, unknown, empty-total,
extra-field, oversized, stale-session, cross-origin, or CSRF-unbound requests
fail before mutation. Tenant, principal, installation, app, destination,
credential, and provider authority never come from the browser.

Configuration requires a session created within ten minutes. A content-free
`allowed` audit must reach the private audit writer before mutation; a separate
`observed` or `failed` intent closes the attempt. Audit metadata contains only
the fixed action/schema and hashed actor/preference references.

PostgreSQL stores canonical sorted arrays in a forced-RLS tenant table. The
ordinary control-plane role can read the record but cannot insert or update it.
One memberless `attune_channel_config_executor` owns the fixed function and has
only session-read, preference mutation, and onboarding-step privileges. The
function independently rechecks recent owner session and tenant context,
serializes changes, and is idempotent.

## Deployment order

1. Apply and verify `0020_hosted_channel_preferences.sql`.
2. Deploy the control plane with `enable_hosted_channels = false`.
3. Require an empty data plan, private audit-writer availability, and negative
   recent-session/CSRF/database tests.
4. Enable the gate and review Cloud Armor priority `886`, which admits only the
   exact GET/PUT path `/v1/onboarding/channels`.
5. Confirm unauthenticated access reaches application authorization and fails
   401, then have the freshly authenticated owner save preferences.
6. Verify mandatory audit, canonical preferences, `authorized` onboarding
   status, resumability, and zero Terraform drift.

Slack OAuth/app installation, Google Chat app installation, verified callbacks,
owner-only destination binding, test delivery, replacement, and disconnection
remain separate ceremonies. None may infer completion from this preference.
Their shared state and distinct provider proofs are specified in
[`hosted-channel-installation.md`](hosted-channel-installation.md).

## Development rollout evidence

Development rollout on 2026-07-16 UTC used immutable migrator digest
`sha256:9720b34f541a5bcc7e0a2e9a30a91058e8248e3dd5db12e3db4b09253365634a`
and control-plane digest
`sha256:a955271a12d185a734b0d130f54cff659f7e6d34862007fb3535fa7e7685d2af`.
Migration execution `attune-development-database-migrate-pcpm9` applied exactly
one migration and verified 29 tenant tables forced through RLS. The data plan
was then empty.

The control plane was first deployed with the channel gate explicitly false.
A second reviewed plan enabled only the application gate and exact Cloud Armor
priority `886`; it changed two resources in place and created or destroyed
none. After global policy convergence, an unauthenticated request reached
application authorization and returned `401 {"error":"invalid_session"}`.
The final edge plan was empty. Deployment did not create or save a channel
preference; that remains a distinct, recently authenticated owner ceremony.

The owner completed that ceremony later on 2026-07-16 UTC. Stale-session
submissions were refused with 409. After fresh authentication, the PUT returned
200 and both mandatory private audit writes returned 200. Readback showed the
canonical `authorized` state with Google Chat and Slack selected independently
for both conversation and morning briefs. No installation, destination,
ingress, credential, or message was inferred from that success.
