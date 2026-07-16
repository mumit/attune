# Hosted channel installation and destination binding

This design turns an `authorized` Slack or Google Chat preference into a
verified owner-only route. Provider installation, owner identity, destination,
ingress, and delivery are separate facts. No one fact implies another, and the
channel onboarding step remains below `validated` until every selected route
passes all five checks.

## Product experience

The owner finishes each selected provider independently:

1. **Google Chat:** Attune creates a single-use, high-entropy link code after
   recent web authentication. The owner opens a direct message with the
   platform-owned Attune Chat app and sends `/link CODE`. Only a verified
   Google Chat `MESSAGE` event from a `DIRECT_MESSAGE` space may consume the
   code. The exact sender and DM become the owner and destination.
2. **Slack:** Attune starts Slack OAuth after recent web authentication. The
   callback must match a one-use state and browser binding. Attune exchanges
   the code through a private credential broker, verifies the returned app,
   team, scopes, bot, and installing Slack user, then resolves exactly one DM
   with that user. No user token is retained unless a future feature explicitly
   requires and explains it.
3. The page shows installation, ingress, destination, and fixed-content test
   separately for each provider. It never labels a route connected merely
   because OAuth returned or an app was added.
4. The owner explicitly requests one fixed, content-free delivery test. A test
   says only that the Attune route is connected; it contains no Gmail,
   Calendar, memory, prompt, or model output.

The initial hosted release supports owner DMs only. Named spaces, channels,
group DMs, shared channels, and administrator-installed broad destinations are
rejected. They require a future visibility review and replacement ceremony.

## Provider-specific boundary

Slack and Google Chat share durable state but not proof mechanisms.

| Fact | Google Chat proof | Slack proof |
|---|---|---|
| Provider ingress | Google bearer token with the exact configured HTTP audience | Slack signature over the raw body plus a five-minute timestamp window |
| Installation | Verified event for the platform-owned Chat app | One-use OAuth callback and verified `oauth.v2.access` response |
| Owner actor | Sender of the one-use link message | Installing user returned by the bound OAuth flow |
| Destination | Exact `DIRECT_MESSAGE` space from that event | Exact one-user IM resolved for the installer |
| Credential | Platform Chat app service identity, outside tenant rows | Bot token encrypted by the private credential broker |

Google documents bearer verification and audience modes in its
[request-verification guide](https://developers.google.com/workspace/chat/verify-requests-from-chat).
Chat interaction events carry the user and space, and `DIRECT_MESSAGE` is the
one-to-one space type. Slack requires OAuth state checking, returns bot/team
installation data from `oauth.v2.access`, and signs public HTTP requests over
the unmodified request body. Slack's `conversations.open` resolves a one-user
DM. Socket Mode remains useful for local development, but hosted distribution
uses HTTPS ingress because Socket Mode apps cannot be listed in the public
Slack Marketplace.

## Durable state

Provider identifiers are never stored raw. A private broker converts app,
workspace, actor, and destination identifiers to domain-separated HMAC-SHA-256
references using a key unavailable to public ingress and the ordinary control
plane. Raw provider tokens go only to the credential broker and encrypted
vault.

The shared state comprises:

- the existing tenant-bound `installations` row;
- a forced-RLS setup transaction containing only a random secret hash,
  provider/mechanism, owner, preference revision, session, expiry, and state;
- a forced-RLS destination binding containing only opaque installation,
  actor, and destination references plus fixed verification statuses; and
- content-free audit events for start, callback/link consumption, test request,
  observed delivery, replacement, and revocation.

Link codes and OAuth state are 256-bit random values, returned once, stored only
as hashes, expire after ten minutes, and are single-use. Starting a new attempt
cancels any older pending attempt for that owner/provider. Database functions
independently require the provider to remain selected at the same preference
revision. Callback consumption serializes against preference changes and
refuses expired, cancelled, consumed, ambiguous, or externally modified state.

## Trust separation

- Public provider ingress holds no Workspace, model, memory, tenant, or bot
  credential. It verifies the provider envelope, enforces size/time/replay
  limits, extracts a fixed normalized signal, and calls a private broker with
  workload identity.
- The private channel broker owns the HMAC key and is the only runtime allowed
  to consume setup transactions or resolve opaque routes. Public input never
  supplies a tenant identifier.
- A memberless database function owner performs cross-tenant link lookup and
  mutation. Its consume function is not granted to any runtime identity until
  the private broker and verified ingress are deployed and reviewed.
- The ordinary control plane may create a bounded setup attempt through one
  fixed function and read public status. It cannot mutate installations,
  destinations, consumed links, or validation state directly.
- Delivery workers receive only a canonical destination binding identifier.
  They cannot substitute an actor, provider installation, workspace, or raw
  channel ID.

## State machine

`pending` setup can become only `consumed`, `expired`, or `cancelled`.
Consumption creates a destination in `pending_test` and advances onboarding
from `authorized` to `applied`. A separately audited, explicit test may advance
that destination to `active`. The channel step becomes `validated` only when
every provider selected for either purpose has exactly one active owner-DM
binding and no selected route is missing.

Changing preferences after validation, retargeting a destination, reinstalling
into a different Slack team, or binding a different external actor requires a
replacement/disconnection ceremony. Provider removal, token revocation,
signature/audience failure, owner mismatch, or destination visibility change
fails closed and moves the affected route out of active service.

## Activation gates

1. Apply and verify the storage migration with no runtime consume grant.
2. Deploy control-plane read/start code with its feature gate false.
3. Deploy the private channel broker with no public caller and prove HMAC
   separation, cross-tenant refusal, one-use consumption, and audit failure.
4. Deploy each public ingress independently with callback routes blocked at the
   edge, then verify provider signatures/audiences, replay limits, body limits,
   and content-free logging.
5. Configure one platform-owned provider app and immutable callback/audience.
6. Enable one provider in development, complete a real owner-DM link, and run
   one explicit fixed-content test.
7. Require canonical database readback, mandatory audits, provider removal and
   replay tests, paging, and zero Terraform drift before enabling the second
   provider.

Successful Terraform, OAuth redirect, app addition, inbound event, or outbound
API response alone is never activation evidence.
