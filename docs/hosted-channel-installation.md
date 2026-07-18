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

Google Chat disconnection is a distinct browser ceremony. It requires recent
authentication, same-origin and CSRF proofs, and the exact confirmation value;
the request contains no tenant, principal, installation, destination, actor,
route, or provider resource. Migration 0026 gives a dedicated memberless
`attune_channel_lifecycle_executor` just enough authority to resolve the
canonical owner destination, cancel outstanding setup claims, delete its
encrypted route, revoke the destination and installation, clear delivery
claims, and return onboarding to `authorized`. The ordinary control plane
cannot update those tables directly.

Relinking after revocation starts with a fresh one-use link transaction. The
broker may reuse the durable destination row only after the signed Google app,
human actor, and DM facts pass the new proof. It replaces the encrypted route,
sets the destination to `pending_test`, and requires the fixed delivery test
before either ingress or outbound delivery becomes active. Existing active or
pending destinations remain non-retargetable.

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

## Slack implementation

Migration `0038_slack_channel_installation.sql` and the accompanying services
implement the Slack half of this design. It is code-complete and tested
offline and against real PostgreSQL; no Slack app, ingress, or broker route is
deployed or activated by merging it. Every stage remains behind its own
default-off gate.

State machine. Slack reuses the shared `hosted_channel_setup_transactions`
and `hosted_channel_destinations` tables with `provider = 'slack'` and
`mechanism = 'oauth'`. The one-use OAuth `state` is the 256-bit setup secret:
the control plane returns it once inside the fixed
`https://slack.com/oauth/v2/authorize` URL and stores only its hash. The
broker's `claim_slack_install`, `release_slack_install_claim`, and
`consume_slack_install` functions mirror the Google Chat claim ceremony: a
60-second-or-shorter claim, a durable pre-effect audit through the private
audit writer before mutation, single use, preference-revision equality, and
replacement-ceremony refusal when a live destination exists. Reinstall after
an explicit disconnect reuses the canonical revoked destination row under new
proof, exactly like Google Chat relink.

Browser binding. Slack's callback is a top-level cross-site navigation, so
origin and CSRF headers cannot exist there. The control plane requires the
Attune session cookie, accepts only the exact `code`/`state`/`error` query
fields, and forwards the state, code, and the session's tenant and principal
to the private broker over workload identity. `consume_slack_install`
independently rechecks that the one-use setup transaction belongs to exactly
that tenant and principal, so a stolen callback URL cannot bind another
account's installation, and a signed-in browser cannot consume another
tenant's state.

Credential boundary. Only the private channel broker holds the Slack client
secret and calls `oauth.v2.access`. It verifies the fixed app ID, `bot`
token type, exact scope set (`chat:write`, `im:write`, `im:history`), and
canonical team/installer/bot identifiers, refuses any response carrying a
user token, and resolves exactly one installer DM with `conversations.open`.
The bot token and the team/DM route are stored only as per-destination
AES-256-GCM envelopes whose DEKs are wrapped by the connector KMS key: the
route in `hosted_channel_routes` and the token in the new forced-RLS
`hosted_channel_credentials` table (`purpose = 'slack_bot_token'`, lifecycle
class credential/crypto-erase). The ordinary control plane can read neither
table; the memberless link executor owns the fixed functions.

Ingress. `attune.hosted.slack_ingress_app` is a separate public service with
its own workload identity. It verifies the v0 signature over the unmodified
raw body with a five-minute timestamp window, enforces a 64 KiB body limit,
answers the signed `url_verification` handshake, and accepts only a plain
human `message` event with `channel_type = "im"` and no subtype, bot, or edit
markers. Everything else is acknowledged with a content-free `{"ok": true}`
so Slack does not retry. Normalized signals use domain-separated references
(`teams/{team}`, `teams/{team}/users/{user}`,
`teams/{team}/channels/{channel}`, `.../messages/{ts}`) that the broker
HMACs under the `slack` domain, so Slack and Google Chat references can never
collide. The ingress holds no tenant, database, model, bot, or Workspace
credential.

Conversation and delivery. `accept_slack_owner_message` mirrors the Google
Chat acceptance: it resolves authority only from stored, hashed provider
facts on an active owner-DM destination with live routes and credentials,
requires an active Google connector and policy, and idempotently creates the
`channel.slack.converse` job, dispatch intent, and content-free audit. The
worker executes it with the same bounded read-only conversation executor
(fixed model tasks, brokered Gmail/Calendar reads, mutation refusal,
authoritative server time) and delivers through the broker's
`/v1/slack/deliver-reply`, which decrypts the route and token, posts through
the fixed `chat.postMessage` URL, and records the provider timestamp
reference hash. The delivery test sends the same immutable connection-test
sentence used for Google Chat.

Acknowledgment. Slack's Events API ignores the synchronous response body, so
unlike Google Chat -- which renders that response inline -- the owner would
otherwise see no feedback until the conversation pipeline replies seconds
later. Once `accept_slack_owner_message` durably accepts a message and the
ingress dispatches it, the ingress calls the broker's
`/v1/slack/acknowledge` with the same team/actor/destination/message
references. `claim_slack_acknowledgment` resolves the same active,
delivery-verified owner-DM destination, records a content-free pre-effect
audit keyed to the provider message, and is idempotent per message: a
retried Slack event wins the claim at most once and is never sent twice. The
broker then decrypts the route and token and sends the fixed, provider-owned
sentence ("Working on it.") through `chat.postMessage`, recording the
outcome audit through `complete_slack_acknowledgment`. A failed or skipped
acknowledgment is logged content-free and never affects the `200` already
returned to Slack, and is never retried -- the conversation reply is still
coming.

Lifecycle. `disconnect_hosted_channel_destination_v2` extends the owner
disconnect ceremony to Slack: it cancels outstanding setup attempts, deletes
the encrypted route and bot-token envelopes, revokes the destination and
installation, and returns onboarding to `authorized`. Google Chat requests
continue to delegate to the original audited function.

Caller separation. The private channel broker now recognizes four distinct
workload identities—Google Chat ingress, Slack ingress, control plane, and
worker—and refuses configuration where any two coincide. Slack routes are
absent entirely unless the Slack broker is configured
(`ATTUNE_SLACK_CHANNEL_ENABLED`, client ID/secret resource, app ID, redirect
URI, and the Slack ingress service account).

Slack deployment order (none of this is performed by merging the code):

1. Apply and verify migration 0038; the live verifier must report
   `hosted_channel_credentials` forced through RLS and the executor grants.
2. Create the platform-owned Slack app with the exact three bot scopes, the
   exact redirect URL
   `/v1/onboarding/channel-installations/slack/callback`, and the exact
   events URL `/v1/provider/slack/events`; store the client secret and
   signing secret in Secret Manager readable only by the channel broker and
   Slack ingress respectively.
3. Add the Slack ingress service account, Cloud Run service
   (`deploy/slack-ingress`), dormant edge configuration, and channel-broker
   invoker binding to the Terraform foundation/runtime/edge modules, mirroring
   the Google Chat ingress dormant-first sequence.
4. Deploy the channel broker with `ATTUNE_SLACK_CHANNEL_ENABLED=true` and the
   control plane with `ATTUNE_HOSTED_SLACK_INSTALL_ENABLED=true` only after
   negative identity, replay, and state-binding probes pass.
5. Complete one real owner installation, the explicit fixed delivery test,
   replay rejection, disconnect, and reinstall with live evidence and empty
   Terraform plans before enabling `ATTUNE_ENABLE_SLACK_CONVERSATION` on the
   worker and ingress.

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
Revision `attune-development-google-chat-ingress-00005-64b`, pinned to
`sha256:2614674ffb1d8827441365e0a977b90cb69538ef2e78a5cd3af940259544776d`,
deployed this rule as the sole in-place resource change. All 909 tests passed;
health remained 200, four unauthenticated or malformed edge probes remained
403, and Terraform converged empty. The fixed outbound test had delivered
`Attune connection test succeeded. No workspace data was accessed.` and
activated the destination, completing the development delivery gate.

Migration 0023 implements that next gate without a general message-send
surface. A recent-authenticated, CSRF-bound browser request contains no body.
The control plane resolves the owner's sole canonical pending destination and
passes only its UUID to the private broker. The broker writes the pre-effect
audit, decrypts the KMS-wrapped route, calls the fixed Google Chat
`spaces.messages.create` path with the `chat.bot` scope and an idempotent
request ID, validates the returned message resource, records the outcome, and
then exposes `active`. The fixed text states only that the connection test
succeeded and that no workspace data was accessed.

The Google Chat destination lifecycle was deployed dormant-first on
2026-07-16 UTC. The first migration execution exposed an ownership defect
before any migration committed: the Cloud SQL non-superuser migrator could not
replace a security-definer function owned by the memberless link executor.
Commit `ea4e3cb` corrected the migration by assuming that narrow owner role
only for the function replacement. Execution
`attune-development-database-migrate-mfjp9` then applied migration 0026 once
from immutable migrator digest
`sha256:1234cbd61bb9db1aa29c2282bbeb29c234a51a7996d910a84021cf3952cc38f6`;
the verifier reported 33 tenant tables forced through RLS.

Control-plane digest
`sha256:0e2bc4e2b99a052596cfe217d8516719e063fbde307c257ebc31869e35f0f68b`
was first deployed with the lifecycle gate false. Health returned 200, the
exact lifecycle path remained edge-blocked with 403, and Terraform converged
empty. Activation then changed only the control-plane flag and Cloud Armor
policy in place, adding priority 888 for the exact disconnect path at five
requests per minute per IP. After global propagation, an unauthenticated
request reached the application and returned the bounded `invalid_session`
401; health remained 200 and both data and edge Terraform plans were empty.
No destination was disconnected or relinked during infrastructure activation.
The recent-authenticated owner ceremony remained separate live evidence.

That owner ceremony subsequently completed on 2026-07-16 UTC. Disconnection
returned the setup state to unlinked, removed conversation authority, and an
ordinary owner-DM message failed closed before dispatch. A fresh one-use link
returned the durable destination to `pending_test`. The first delivery attempt
then exposed an authenticated-encryption context defect: relink had encrypted
the route against a candidate UUID before the database reused the canonical
destination UUID, so decryption correctly failed with `InvalidTag` and no
message was sent.

Migration 0027, from commit `036b560`, makes the memberless link executor
resolve a revoked canonical destination before encryption. Execution
`attune-development-database-migrate-kmtjg` applied it once from immutable
digest
`sha256:89686c6318f633eece391a601943f1b11c85b5497262159d7e11e4e31c53b6b5`;
the verifier again reported 33 tenant tables forced through RLS and Terraform
converged empty. A second explicit disconnect and fresh link then delivered the
fixed connection test, activated canonical readback, and accepted and answered
an ordinary Calendar message. The lifecycle regression now covers acceptance
after verified relink, not merely route recreation.

The live Calendar response also revealed that the answer model had inferred
“today” from earlier email context. Worker digest
`sha256:3cc73486d435505462749d51bb5c0c7b4f34cecaa4a0da7ae228e6fbcc1d8a5a`
now receives an authoritative server datetime in `America/Vancouver` outside
untrusted conversation and Workspace results. A one-resource in-place rollout
became Ready with no warnings and an empty Terraform plan. Repeating the same
“tomorrow” question returned the correct date and Calendar answer without
using the earlier email as temporal authority.

The Slack ingress Terraform substrate now exists dormant-first: a dedicated
`slack_ingress` service account distinct from every other workload identity, a
dormant Cloud Run service with its default URL disabled behind an unrouted
serverless backend and a default-deny Cloud Armor policy, direct
least-privilege secret accessors, and an edge backend whose exact
`/v1/provider/slack/events` path stays absent from the URL map until
`enable_slack_ingress` is set. The distinct identity is deliberate: the broker
composition enforces four distinct caller identities (Google Chat ingress,
Slack ingress, control plane, worker) and refuses to start otherwise, so a
compromised provider ingress can exercise only its own provider's broker
routes. The Slack signing secret and client secret bypass the secret broker,
mirroring the channel-reference HMAC: each is a platform credential read
directly from Secret Manager at startup by exactly one fixed service — the
signing secret by the Slack ingress identity and the client secret by the
channel-broker identity — and both are excluded from the secret-broker
accessor grant. Channel-broker Slack configuration stays behind
`slack_channel_enabled`, which remains false. Provider app creation, secret
version population, and staged gate activation remain operator ceremonies.

Slack was activated in development on 2026-07-17 UTC. Migration 0038 first
failed closed in execution `attune-development-database-migrate-5wcv5` on a
pre-existing defect: the export-download Cloud SQL IAM user was missing
because the `instanceUser` role existed but the `google_sql_user` set omitted
`export_download`. Commit `53c31d3` corrected the omission. Execution
`attune-development-database-migrate-z8fw8` then applied migration 0038 once
from immutable migrator digest
`sha256:5bb669763fdf74cbd125a3a9500ff1233db05c29372871a1c93b141cdbf8e472`; the
live verifier reported 36 tenant tables forced through RLS. Migrator success
is enforced by exit code and the full boundary verifier rather than the
migrator's stdout summary, which did not surface in Cloud Logging
`textPayload`.

Five images were built and pinned by digest for the rollout: channel-broker
`sha256:8978f4fe08eac5aacc181bca62dba10cbd42a06f015b161243d205c0e85b6089`,
control-plane
`sha256:9cbf265d589531ba6111bfc1b4ff7c705d3e065b3167ceac2152a412fd4f789d`,
worker
`sha256:f5276911867674f53735868a8a7c10ce2015e0730433718ca5e58012077f9fb0`,
slack-ingress
`sha256:9f9f6a49670a36726b9a95a6eb82bf7e40b380bb739a04ed46cdafc884d4fc67`, and
dispatch-broker
`sha256:63af87280494a2c852759260cb4fceec417d1659f976c14caf22bdcd26211d0e`.
At completion, Ready revisions were slack-ingress `00002-8rt`, channel-broker
`00011-26t`, control-plane `00028-f5l`, worker `00014-2vw`, and
dispatch-broker `00011-hb7`. All four Terraform modules (foundation, data,
runtime, edge) converged to empty plans.

The rollout followed the documented dormant-first gates. Foundation identity
and secret access were provisioned first. A dormant deploy followed with all
gates false, and the public event-path probe returned 403. `enable_slack_ingress`
was then activated with a `slack_provider_ready` attestation: after Cloud
Armor propagation, an unsigned POST on the exact `/v1/provider/slack/events`
path reached the application and received its content-free JSON 403
signature refusal, while GET and near-miss-path probes remained edge-denied.
`slack_channel_enabled` came next; the broker revision reaching Ready proved
the client-secret fetch, HMAC key, and the four-distinct-identities startup
check. `enable_hosted_slack_install` was activated last, together with Cloud
Armor rules 891 and 892; all four onboarding paths (install, callback, test,
disconnect) reached application authorization and failed closed with 401
`invalid_session` while unauthenticated.

The live owner ceremony followed. The setup page's new Slack section (commit
`cc8f1f1`) started a one-use OAuth state, and the first consent attempt failed
closed with a content-free `SlackProviderFailure` because the deliberately
NAT-less VPC blocked `slack.com` egress — Google Chat had ridden Private
Google Access, which Slack's ordinary internet API cannot use. The dedicated
broker-egress subnetwork with subnet-scoped Cloud NAT (commit `9eadd66`,
subnet later widened to `/24`) restored egress while every other workload
kept the no-NAT posture. A fresh ceremony then installed, resolved the
installer's DM, stored the encrypted route and bot token, and the explicit
fixed delivery test delivered `Attune connection test succeeded. No workspace
data was accessed.` and activated the destination.

The first live conversation attempt was accepted durably but not dispatched:
the Slack ingress's distinct identity had no `run.invoker` on the dispatch
broker and was then refused by the dispatch broker's one-email-per-kind
caller map. Two fixes resolved this: the gated `dispatch_broker_invoker`
grant for the slack-ingress identity, and commit `577d803`, which allows
multiple authorized emails per producer kind while still refusing unknown
callers and rejecting duplicates at startup. A fresh owner DM then completed
the full path — verified ingress, durable acceptance, dispatch, a bounded
read-only Calendar answer, and reply delivery through the private broker.

Slack Event Subscriptions verified the Request URL
`https://dev.attune.mumit.org/v1/provider/slack/events` through the signed
`url_verification` handshake and subscribes to `message.im` only.

Remaining before this channel's gate is called complete: the live disconnect
/ fail-closed refusal / reinstall / delivery-test / conversation-recovery
regression (implemented and exercised for Google Chat, not yet exercised live
for Slack), and the mutation-refusal probe.

The live Slack lifecycle regression completed on 2026-07-17/18 UTC. Owner
disconnect first correctly refused twice with `409 recent_authentication_required`
until fresh web authentication, a state the setup page's shared status line
now surfaces; disconnect then succeeded. A subsequent owner DM was refused
fail-closed before dispatch with the content-free "Slack owner destination is
unavailable" response. Reinstall then exposed a real defect: `consume_slack_install`
unconditionally inserted a new `installations` row and collided with the
tenant/provider/reference unique constraint, where the Google Chat relink path
instead reuses and reactivates the revoked installation row. Migration 0039
(commit `c0d99bb`) corrected the reuse branch to match, and the real-PostgreSQL
journey now reinstalls with identical provider references, reproducing the
collision test-first before the fix. Execution
`attune-development-database-migrate-8t5jp` applied it from immutable migrator
digest
`sha256:389c6afcc38e4340006a7f87b8138f27ad89c124a0b7b379e6b0e747cfea716a`. A
fresh install, the fixed delivery test, and a live conversation then completed,
closing the regression.

The same window shipped two responsiveness changes. Measured end-to-end reply
latency had been roughly 15 seconds -- ingress acceptance and reply delivery
were each sub-second, with the remainder spent in the scale-to-zero worker
path and two sequential model calls. Commit `af9037c` and migration 0040 added
the private broker's audited, idempotent "Working on it." acknowledgment
described above; execution `attune-development-database-migrate-cj9qg` applied
0040 from immutable migrator digest
`sha256:1effc96e9e021a7fa6c6f196da9ba08fd4c6631f73a07bad281ab247632850c7`.
Separately, commit `e850857` runs the deterministic keyword router before the
classify model task, calling it only for ambiguous requests and removing one
model round-trip from typical Gmail, Calendar, brief, and mutation-refusal
turns on both channels.

Deployed digests and revisions after this work: channel-broker
`sha256:59f1572962eca471aeb83479f306ee3600e87298dc6a2728b13a10c993afbb6b`
(revision `00012-b2k`), slack-ingress
`sha256:1eeb53c0376bdbabb5b9c71e63038b30588324442b45cedbfaecf2910dbf795c`
(`00003-bk9`), and worker
`sha256:0fb3ac70a2e7f138ca6678fe8083c29ac516ccc4cd2b5c25de9d00d004eb7223`
(`00015-h8n`). All Terraform plans converged empty. Warm minimum instances for
the worker and model gateway remain a deliberate development cost decision, so
scale-to-zero cold starts remain the dominant residual latency.

Still outstanding for this channel: the explicit mutation-refusal probe over
Slack. The refusal path is covered by tests and was exercised live over Google
Chat, but not yet live over Slack.
