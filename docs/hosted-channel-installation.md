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
as hashes, expire after ten minutes, and are single-use. Google Chat
consumption first places a 60-second-or-shorter claim and creates a durable
pre-effect audit intent. The broker writes that audit through the private audit
writer before it may finalize the installation and destination; audit failure
releases the claim without consuming the link. Starting a new attempt
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
  mutation. Only the dedicated, unprivileged channel-broker database role may
  execute its claim, release, and consume functions; that role has no direct
  table access.
- The ordinary control plane may create a bounded setup attempt through one
  fixed function and read public status. It cannot mutate installations,
  destinations, consumed links, or validation state directly.
- Delivery workers receive only a canonical destination binding identifier.
  They cannot substitute an actor, provider installation, workspace, or raw
  channel ID.

The raw provider route needed for later delivery is retained only as a
per-binding AES-256-GCM envelope. Its associated data binds tenant,
destination UUID, provider-purpose, and route version; the DEK is wrapped by
the connector KMS key. The ordinary control plane can neither read the route
nor use the KMS key. The private channel broker is the only service that can
decrypt it, and its delivery endpoint accepts only a canonical destination
UUID from the exact control-plane workload identity. The provider adapter owns
the immutable connection-test sentence and Chat API URL; neither is accepted
from the browser, database, or request body.

## State machine

`pending` setup can become `claimed`, `expired`, or `cancelled`; a short
`claimed` lease can return to `pending` on pre-audit failure or expiry, or move
once to `consumed` after the canonical checks pass.
Consumption creates a destination in `pending_test` and advances onboarding
from `authorized` to `applied`. A separately audited, explicit test may advance
that destination to `active`. The channel step becomes `validated` only when
every provider selected for either purpose has exactly one active owner-DM
binding and no selected route is missing.

Migration 0023 detects bindings created before encrypted route retention. It
reports them as `needs_relink`, never fabricates a route, and permits a new
one-time link only as an explicit adoption ceremony. Adoption succeeds only
when the newly verified installation, human actor, and direct-message HMAC
references exactly match the existing pending binding. It then stores the
encrypted route atomically. A mismatch, active binding, or already provisioned
route is refused and requires the replacement ceremony.

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

The Google Chat implementation preserves these stages as independent flags.
The ingress can be deployed behind a serverless NEG and a default-deny Cloud
Armor policy while absent from the public URL map. Its public handler accepts
only Google's verified service identity at the exact endpoint audience,
requires matching human sender and `DIRECT_MESSAGE` space facts at both event
levels, and recognizes only the exact `/link CODE` command. It sends the
private broker the transient app, actor, DM, and code values—but never a tenant
or database identifier. The broker alone derives keyed references and resolves
the tenant through the one-use claim ceremony.

For an interactive `MESSAGE`, the command parser prefers Google Chat's
`message.argumentText`, whose contract removes Chat-app mentions. The selected
field must match the exact `/link CODE` grammar, optionally preceded by the one
ASCII separator that mention removal can retain; no general trimming occurs.
If the output-only field is absent or empty, the parser accepts only an exact
`message.text` fallback. Sender and space
equality, human actor type, direct-message type, token identity, and exact
audience are validated independently of the command body.

The signed top-level Event `space.spaceType` is authoritative for the
`DIRECT_MESSAGE` check. The nested `message.space.name` must match the
top-level resource name; if nested `spaceType` is present it must also be
`DIRECT_MESSAGE`, but its absence is valid. The deprecated `type` spelling is
accepted only as a fallback when canonical `spaceType` is absent.

## Development rollout

On 2026-07-16 UTC, commit `27cda78` was deployed dormant-first. Migration
0021 ran once in execution `attune-development-database-migrate-rlc6q`; the
live verifier reported 31 tenant tables forced through RLS. Control-plane
digest
`sha256:7a084cd8776ce1b2130bf5d55287ee19f50ac8491e5ba2c23144699ae0176089`
was then deployed with `ATTUNE_HOSTED_CHANNEL_SETUP_ENABLED=false`. Health
returned 200, the installation-status route remained blocked at the edge with
403, and both data and edge Terraform plans converged empty. No setup attempt,
link, destination, provider credential, ingress, or message was created. This
evidence satisfies activation gates 1 and 2 only.

The next dormant stage completed on 2026-07-16 UTC. Migration 0022 ran once in
execution `attune-development-database-migrate-tbd9h` from immutable digest
`sha256:386ceb843a33de4594c1b438a941bfa8823d500ecf50ef6ceb5079fd9ca2f7aa`;
the verifier again reported 31 forced-RLS tenant tables. The private broker is
Ready on revision `attune-development-channel-broker-00003-ksw`, pinned to
digest
`sha256:b5df7b42ea722ae621671fbc6cd05a66a2af29034aa09ec7e2c89daaec2b63ba`.
It has internal-only ingress, a dedicated runtime identity, and exactly one
Cloud Run invoker: the dedicated ingress service account.

Google Chat ingress is Ready on revision
`attune-development-google-chat-ingress-00001-sql`, pinned to digest
`sha256:abd3ff681cf4f576f00bcdc7ed509de7f3e3ddd3e0c85d22ab7acfac2411ad94`.
Its default URL is disabled; it is attached to an unrouted serverless backend
with request logging disabled and a default-deny Cloud Armor policy. The public
event path still returns 403 and is absent from the URL map.
`enable_google_chat_ingress`, `google_chat_provider_ready`, and
`enable_hosted_channel_setup` remain false. Runtime and edge Terraform plans
converged empty. No setup, link, claim, destination, provider app event, or
message was created. This satisfies the private-boundary and dormant-ingress
parts of gate 3; provider configuration, adversarial evidence, route
activation, and a real owner-DM ceremony remain separate gates.

The Google Chat provider and edge gate were activated later on 2026-07-16.
The platform-owned app is named `Attune`, uses the first-party hosted avatar,
accepts direct messages only, and is visible only to `khan@mumit.org`. Its
exact endpoint and HTTP-endpoint authentication audience are both
`https://dev.attune.mumit.org/v1/provider/google-chat/events`. Group spaces,
App Home, commands, and link previews remain disabled.

The reviewed Terraform activation plan added only the exact-path Cloud Armor
throttle rule and the URL-map rule to the dedicated ingress backend: zero
resources added, two changed in place, and zero destroyed. After apply,
unauthenticated and invalid-bearer POSTs, GET on the exact path, and POST on a
near-miss path all returned 403; health remained 200; and Terraform converged
empty. `enable_google_chat_ingress` and
`google_chat_provider_ready` are now true in development. A real Google Chat
delivery and owner-DM link ceremony are still required before gate 3 is
complete.

The first real owner DM reached the verified ingress and received the expected
unlinked response without invoking the private broker. The first link attempt
then revealed the provider-field distinction above: the old revision parsed
`message.text` instead of the mention-stripped `message.argumentText`. No code
was consumed. The corrected image is pinned to
`sha256:845e891947164c5f171535a5aef771c449abb9aada357461f1ed665c5985abbc`
on revision `attune-development-google-chat-ingress-00004-2rf`. All 899 tests
passed; live unauthenticated and invalid-bearer probes remained 403; health was
200; and the post-deployment Terraform plan was empty. A fresh one-time code is
required for the resumed ceremony.

The second live attempt established that direct-message `argumentText` can be
present as an empty protobuf string. The current parser treats only absence or
empty string as permission to try the exact `text` fallback. It does not
generally trim whitespace or fall back after any other non-empty value.
Content-free reason
codes (`event_envelope`, `identity_envelope`, `actor_space_binding`, or
`command_body`) provide bounded live diagnostics without retaining message,
code, actor, or destination data.

The third live attempt identified the separate space-schema fixture error:
production sends canonical `spaceType`, while the original fixture used
`type`, and the nested message space need not repeat the type. The current
revision implements the top-level authority and nested-name binding described
above. Its image-only plan changed one resource in place with no additions or
destructions; negative authentication remained 403, health remained 200, and
the final Terraform plan was empty.

The subsequent fresh code linked once through verified Google ingress and the
private broker. Replaying the consumed code returned the bounded unavailable
response, and canonical setup readback reported `pending_test`. No link code,
actor ID, space ID, or tenant ID was retained in application logs or rollout
notes. This completes the real owner-DM link and replay-rejection evidence;
the separately requested asynchronous delivery test remains the next gate.

During route adoption, the first link message was provider-authenticated but
rejected as `command_body`; an identical retry was accepted and consumed. The
bounded evidence is consistent with Google retaining the single separator
after stripping the Chat-app mention. The parser now accepts only that one
provider-shaped prefix in `argumentText`, while still rejecting tabs, multiple
or trailing spaces, suffixes, and a prefixed fallback `message.text`.

Migration 0023 implements that next gate without a general message-send
surface. A recent-authenticated, CSRF-bound browser request contains no body.
The control plane resolves the owner's sole canonical pending destination and
passes only its UUID to the private broker. The broker writes the pre-effect
audit, decrypts the KMS-wrapped route, calls the fixed Google Chat
`spaces.messages.create` path with the `chat.bot` scope and an idempotent
request ID, validates the returned message resource, records the outcome, and
then exposes `active`. The fixed text states only that the connection test
succeeded and that no workspace data was accessed.
