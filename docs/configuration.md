# Configuration reference

*This is the self-hosted `.env` reference. Hosted multi-tenant services are
configured by the operator's infrastructure (Terraform and Secret Manager),
not a per-tenant `.env` file — see [`modes.md`](modes.md).*

Attune reads configuration from the process environment. The CLI loads `.env`
automatically, and `attune init` edits that file while retaining its current
values as prompt defaults. Never commit a populated `.env`, OAuth credential,
service-account key, or bearer token.

This page covers every variable in `.env.example`. Blank means unset or
disabled unless the description says that another value is inherited. Run
`attune doctor` after changing configuration; it checks the selected models,
Workspace backend, destinations, credentials, and route dependencies.

## Recommended model routing

Attune uses an OpenAI-compatible **Chat Completions** endpoint. OpenAI works
directly. Claude requires a gateway that exposes Anthropic models through that
same API; Anthropic's native API is not a drop-in endpoint for this setting.
Use the exact identifiers advertised by the configured gateway, which may add
a provider prefix such as `anthropic/` or expose an older model family.

A good mixed-provider starting point is:

```dotenv
ATTUNE_MODEL_DEFAULT=gpt-5.6-terra
ATTUNE_MODEL_CLASSIFY=claude-haiku-4-5
ATTUNE_MODEL_DRAFT=claude-sonnet-5
ATTUNE_MODEL_REASON=gpt-5.6-terra
ATTUNE_MODEL_CONSOLIDATE=gpt-5.6-terra
ATTUNE_MODEL_CONVERSE=claude-sonnet-5
ATTUNE_MODEL_MEMORY_EXTRACT=claude-haiku-4-5
```

The split uses small, fast models for classification and memory extraction;
Claude Sonnet for user-facing prose; and GPT for structured judgment and
memory consolidation. This is a recommendation, not a requirement. For the
simplest setup, set only `ATTUNE_MODEL_DEFAULT` to `gpt-5.6-terra` or
`claude-sonnet-5`. For quality-first reasoning or consolidation, try
`gpt-5.6-sol` or `claude-fable-5`; for an inexpensive all-OpenAI setup, use
`gpt-5.6-luna` for classification/extraction and `gpt-5.6-terra` elsewhere.

Model names and availability change. Check the current
[OpenAI model catalog](https://developers.openai.com/api/docs/models) and
[Anthropic model overview](https://platform.claude.com/docs/en/about-claude/models/overview),
then confirm gateway compatibility with `attune doctor`.

`attune init --recommended` fills this exact starting point (and the
embedding model/dimensions below) as editable defaults during the wizard —
it never overwrites a value you already configured, and every field it fills
stays a plain, editable line in `.env`.

For embeddings, start with OpenAI `text-embedding-3-small` and `1536`
dimensions. `text-embedding-3-large` at `3072` dimensions is the quality-first
alternative. See the [OpenAI embeddings guide](https://developers.openai.com/api/docs/guides/embeddings).
The vector dimensions are part of the Qdrant collection schema: do not change
the embedding model or dimensions on an existing data directory without a
planned re-embedding/migration.

## LLM and memory

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `ATTUNE_LLM_BASE_URL` | `https://api.openai.com/v1` | Base URL for an OpenAI-compatible Chat Completions API. Keep the default for OpenAI; otherwise use the gateway's versioned base URL, without `/chat/completions`. |
| `ATTUNE_LLM_API_KEY` | blank | Bearer credential for the chat API. Required at runtime. Store it only in a protected environment/secret store. |
| `ATTUNE_MODEL_DEFAULT` | blank | Fallback for every task without an override. Suggested: `gpt-5.6-terra` or, through a compatible gateway, `claude-sonnet-5`. At least this default or every individual task model must be set. |
| `ATTUNE_MODEL_CLASSIFY` | default model | Fast classification and triage. Suggested: `claude-haiku-4-5` or `gpt-5.6-luna`. |
| `ATTUNE_MODEL_DRAFT` | default model | Gmail draft generation. Suggested: `claude-sonnet-5`; use `gpt-5.6-terra` for an all-OpenAI setup. |
| `ATTUNE_MODEL_REASON` | default model | Calendar conflicts, decisions, and structured judgment. Suggested: `gpt-5.6-terra`; use `gpt-5.6-sol` for quality-first work. |
| `ATTUNE_MODEL_CONSOLIDATE` | default model | Nightly memory consolidation. Suggested: `gpt-5.6-terra`; this is a reasonable place to spend more on `gpt-5.6-sol` because memory quality compounds. |
| `ATTUNE_MODEL_CONVERSE` | default model | Interactive Slack/Google Chat replies. Suggested: `claude-sonnet-5` or `gpt-5.6-terra`. |
| `ATTUNE_MODEL_MEMORY_EXTRACT` | classify model, then default model | Extracts durable facts from interactions. Suggested: `claude-haiku-4-5` or `gpt-5.6-luna`. |
| `ATTUNE_EMBEDDING_BASE_URL` | LLM base URL | Base URL for an OpenAI-compatible embeddings API. Set it separately when chat and embeddings use different providers. |
| `ATTUNE_EMBEDDING_API_KEY` | LLM API key | Bearer credential for embeddings. Leave blank when the LLM credential also authorizes the embedding endpoint. |
| `ATTUNE_EMBEDDING_MODEL` | blank | Required for memory. Suggested: `text-embedding-3-small`; quality-first: `text-embedding-3-large`. The configured provider must expose it. |
| `ATTUNE_EMBEDDING_DIMENSIONS` | blank | Required vector length. Use `1536` with the default `text-embedding-3-small`, or `3072` with full-size `text-embedding-3-large`. It must match the model output and existing Qdrant collection. |
| `ATTUNE_QDRANT_HOST` | `127.0.0.1` | Durable Qdrant server host used by both runtime and Doctor. Keep the default when Attune runs on the host; the Compose assistant overrides it with the service name `qdrant`. Embedded Qdrant is intentionally not an implicit fallback. |
| `ATTUNE_QDRANT_PORT` | `6333` | Qdrant HTTP port used by both runtime and Doctor. Keep `6333` unless the private service uses another port. |

## Principal and runtime

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `ATTUNE_USER_ID` | `me` | Principal this instance represents. Use the complete mailbox address, such as `owner@example.com`, so ownership and safety checks are unambiguous. |
| `ATTUNE_INTERNAL_DOMAINS` | domain from `ATTUNE_USER_ID` | Comma-separated domains treated as internal, such as `example.com,subsidiary.example`. Review this boundary carefully. |
| `ATTUNE_DATA_DIR` | current directory | Durable state, audit log, local vectors, checkpoints, and watch cursors. Suggested: `~/.attune` locally or `/var/lib/attune` under systemd. Back it up and restrict access. |
| `ATTUNE_WORKSPACE_BACKEND` | `google_oauth` | `google_oauth` calls Google APIs directly; `mcp` uses contract-compatible Streamable HTTP services. MCP currently requires polling. |
| `ATTUNE_INGESTION_MODE` | `poll` | `poll` is the portable default. `google_pubsub` enables the advanced direct-OAuth GCP transport. Do not use Pub/Sub with MCP. |
| `ATTUNE_POLL_SECONDS` | `120` | Poll interval for Gmail, Calendar, and configured pollable sources. Suggested: `120`; values below `30` are clamped to `30`. Shorter intervals increase API traffic. |

## Direct Google OAuth and MCP

Only the variables for the selected Workspace backend are required.

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `GOOGLE_PROJECT_ID` | blank | Google Cloud project owning enabled Workspace APIs and, in push mode, Pub/Sub resources. Use the project ID, not its display name or numeric project number. |
| `ATTUNE_GOOGLE_CREDENTIALS_FILE` | Application Default Credentials | Authorized-user JSON for the principal in direct OAuth mode. Suggested: `~/.attune/google_authorized_user.json` locally or a protected absolute path on a server. Do not substitute a VM service account for human mailbox consent. |
| `ATTUNE_MAIL_LABELS_ENABLED` | `0` | Opt-in archive/label write path for triaged-noise mail (Phase 3 stage 1). google_oauth only — requires the optional `gmail.modify` scope alongside the scopes above. `attune doctor` fails if this is set while `ATTUNE_WORKSPACE_BACKEND=mcp` (contract v1 has no label-removal tool). |
| `ATTUNE_CALENDAR_WRITES_ENABLED` | `0` | Opt-in decline-invite/reschedule write path for calendar events (Phase 3 stage 2). google_oauth only — uses the `calendar.events` scope already requested for tentative holds, so no extra Google consent step is needed. `attune doctor` fails if this is set while `ATTUNE_WORKSPACE_BACKEND=mcp` (contract v1 has neither tool). |
| `ATTUNE_MAIL_SEND_ENABLED` | `0` | Opt-in SEND_REPLY write path (Phase 4 stage 2, G15). google_oauth only — requires the optional `gmail.send` scope alongside the scopes above, AND an explicit `attune autonomy grant send_reply ...` (which itself refuses with a non-zero exit while this is `0`). `attune doctor` fails if this is set while `ATTUNE_WORKSPACE_BACKEND=mcp` (send is not a contract v1 tool). |
| `ATTUNE_MCP_URL` | blank | Shared Streamable HTTP MCP endpoint implementing both Gmail and Calendar contract tools, such as `https://workspace-mcp.example.com/mcp`. Use this or both service-specific URLs. |
| `ATTUNE_MCP_GMAIL_URL` | shared MCP URL | Gmail-specific MCP endpoint. Set together with the Calendar URL when no shared URL is used. |
| `ATTUNE_MCP_CALENDAR_URL` | shared MCP URL | Calendar-specific MCP endpoint. Set together with the Gmail URL when no shared URL is used. |
| `ATTUNE_MCP_TOKEN` | blank | Optional bearer token sent to the MCP endpoint(s). Use a least-privilege secret issued for this Attune instance. |

## Slack, Google Chat, and routing

Slack and Google Chat are optional and independently selectable. Route values
are `slack`, `google_chat`, a comma-separated combination, or blank to disable
that behavior. Approvals intentionally accept only one channel. Both
interaction surfaces use the same bounded natural-language layer described in
the [user journey](user-journey.md).

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `SLACK_APP_TOKEN` | blank | Slack Socket Mode app-level token beginning `xapp-`. Required only when Slack is an interaction channel. |
| `SLACK_BOT_TOKEN` | blank | Slack bot token beginning `xoxb-`. Required for any Slack delivery or interaction route. |
| `ATTUNE_SLACK_CHANNEL` | blank | Stable proactive Slack destination: owner member ID (`U…`, recommended; Slack opens the app DM), existing DM (`D…`), private channel (`G…`), or channel (`C…`). Display names are mutable/non-unique and are not accepted. |
| `ATTUNE_SLACK_ALLOWED_USERS` | blank | Comma-separated Slack member IDs (`U…`) allowed to interact with Attune. Required when Slack interactions are enabled; normally list only the principal. |
| `ATTUNE_CHAT_CREDENTIALS_FILE` | blank | Credentials JSON for the Google Chat app: a service account using `chat.bot` (default), or an OAuth user credential (`type: authorized_user`, scoped to `chat.messages`/`chat.spaces.readonly`) for organizations that disallow creating IAM service-account keys — see [deployment.md](deployment.md)'s Google Chat section. Required for any Google Chat route and must be a different file than `ATTUNE_GOOGLE_CREDENTIALS_FILE`; `attune doctor` fails if the two paths are identical. |
| `ATTUNE_CHAT_SPACE` | blank | Proactive Google Chat destination resource such as `spaces/AAAA`. Prefer an owner-only direct-message space. |
| `ATTUNE_CHAT_ALLOWED_USERS` | blank | Comma-separated Google Chat user resources such as `users/123456789`. Required for Google Chat interactions; normally list only the principal. |
| `ATTUNE_BRIEF_CHANNELS` | inferred from configured destinations when absent | Destinations for scheduled/manual briefs. Suggested: `slack`, `google_chat`, or `slack,google_chat`; explicit blank disables delivery. |
| `ATTUNE_APPROVAL_CHANNEL` | first inferred channel when absent | Single surface for approval requests, `slack` or `google_chat`. Suggested: the principal's most reliable private surface; explicit blank disables approval delivery. |
| `ATTUNE_NOTIFICATION_CHANNELS` | inferred from configured destinations when absent | Destinations for nudges and notifications. Use either or both channel names; explicit blank disables delivery. |
| `ATTUNE_INTERACTION_CHANNELS` | inferred from configured destinations when absent | Surfaces that accept commands, messages, and approval actions. Enabling one also requires its allowlist and interaction transport. |
| `ATTUNE_ACK_DESTINATION_VISIBILITY` | `0` | Safety acknowledgement for proactive messages to any Google Chat space or a non-DM Slack destination. Keep `0` for an owner Slack DM; set `1` only after checking destination membership and content exposure. |

## Attended sources (Phase 2 stage 1)

Opt-in per channel/space. A configured source is attended EXACTLY like a
Gmail thread — polled, triaged, and (if not NOISE) recorded into the
attention store — never a command surface and never a reply path, regardless
of who sent the message. This is unrelated to `ATTUNE_SLACK_ALLOWED_USERS` /
`ATTUNE_CHAT_ALLOWED_USERS`, which govern who may command Attune over a DM.
`attune doctor` fails fast if either variable below is set without the
matching read credential.

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `ATTUNE_SLACK_SOURCE_CHANNELS` | blank | Comma-separated Slack channel IDs (`C…`/`G…`) to attend as signal sources. Requires `SLACK_BOT_TOKEN`. Blank disables the feature. |
| `ATTUNE_CHAT_SOURCE_SPACES` | blank | Comma-separated Google Chat space resource names (`spaces/AAAA`) to attend as signal sources. Requires `ATTUNE_CHAT_CREDENTIALS_FILE`. Blank disables the feature. |
| `ATTUNE_ATTENTION_PATH` | `ATTUNE_DATA_DIR/attention.json` | Path to the bounded attention store (recent ROUTINE/URGENT source items; 200-item cap, 7-day retention) — the seam a future unified brief reads from. |
| `ATTUNE_BRIEF_SNAPSHOT_PATH` | `ATTUNE_DATA_DIR/brief_snapshot.json` | Path to the "since yesterday" brief snapshot (Phase 3: unread thread ids/subjects, today's event ids/titles, waiting-on ids, a timestamp; ignored once older than 48h). Written only by the daily posted brief, never by an on-demand brief request or the CLI preview. |

## Schedule, conversation, and logging

Times use 24-hour `HH:MM` notation in `ATTUNE_TIMEZONE`.

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `ATTUNE_TIMEZONE` | `UTC` | IANA timezone for schedules and date interpretation, such as `America/Vancouver`. Set this to the principal's normal timezone. |
| `ATTUNE_BRIEF_TIME` | `07:30` | Daily brief time. Choose a time shortly before the workday begins. |
| `ATTUNE_CONSOLIDATE_TIME` | `02:00` | Nightly memory-consolidation time. Keep it in a low-activity window. |
| `ATTUNE_NUDGE_TIME` | `14:00` | Daily time to surface follow-ups that have gone quiet. |
| `ATTUNE_NUDGE_MIN_AGE_DAYS` | `4` | Minimum age of an unresolved follow-up before it can be nudged. |
| `ATTUNE_NUDGE_COOLDOWN_DAYS` | `7` | Minimum wait before repeating a nudge for the same item. Increase it if reminders feel noisy. |
| `ATTUNE_APPROVAL_IGNORE_HOURS` | `48` | Hours after which an unanswered approval request is marked ignored. |
| `ATTUNE_IMPORTANCE_PATH` | `ATTUNE_DATA_DIR/importance_profile.json` | Path to the deterministic per-sender importance profile (Phase 1: approve/edit/ignore/reject counts, decayed over time). Inspect and correct it with `attune importance`. |
| `ATTUNE_GRADUATION_STATE_PATH` | `ATTUNE_DATA_DIR/graduation_state.json` | Path to the graduation/demotion approval-card bookkeeping (Phase 4 stage 2, G13): a posted card's exact snapshot (action/domain/rung/scope) and 30-day rejection cooldowns. Delete it to reset cooldowns; it never holds a grant itself — the permission matrix (`ATTUNE_AUTONOMY_STATE_PATH`) does. |
| `ATTUNE_CONVERSE_WINDOW_TURNS` | `10` | Maximum recent interaction turns retained in conversational context. |
| `ATTUNE_CONVERSE_TTL_MINUTES` | `120` | Idle minutes after which conversational context expires. |
| `ATTUNE_LOG_LEVEL` | `INFO` | Python log level. Use `DEBUG` temporarily for diagnosis; `INFO` is recommended in normal operation. |
| `ATTUNE_LOG_JSON` | blank | Set to `1` for one JSON object per log line, recommended for journald/Cloud Logging ingestion; leave blank for human-readable local logs. |

## Local rate ceilings

Security finding F9 (docs/current-state.md's 2026-07-18 review): two
bounded, deterministic ceilings, no model calls, process-local state
(resets on restart, no cross-process coordination). Both are the one
exception to "avoid new variables" — a ceiling that can't be tuned for a
deployment's real traffic isn't usable.

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `ATTUNE_INBOUND_RATE_LIMIT` | `20` | Max inbound Slack/Chat DMs per (channel, user) per 5-minute sliding window before a fixed "please wait" refusal (see `dispatcher.py`'s `InboundRateLimiter`). Raise it if a legitimate power user hits it in normal use. |
| `ATTUNE_TRIAGE_BATCH_LIMIT` | `25` | Max Gmail threads drafted per notification batch in `handle_gmail_notification`; anything beyond this is enqueued onto the existing durable retry queue (`ingestion/retry_queue.py`), never dropped. Raise it only if backlog notifications after downtime are routinely larger than this. |

## Google Pub/Sub transport

These advanced settings apply only to direct Google OAuth with
`ATTUNE_INGESTION_MODE=google_pubsub`. Use complete resource names so the
project relationship is explicit. The [deployment guide](deployment.md)
creates and grants the corresponding resources.

| Variable | Default | Purpose and suggested value |
|---|---|---|
| `ATTUNE_GMAIL_PUBSUB_TOPIC` | blank | Gmail watch topic, for example `projects/PROJECT/topics/attune-gmail`. Gmail must be allowed to publish to it. |
| `ATTUNE_GMAIL_PUBSUB_SUBSCRIPTION` | blank | Pull subscription consumed by Attune, for example `projects/PROJECT/subscriptions/attune-gmail-sub`. |
| `ATTUNE_CHAT_PUBSUB_TOPIC` | blank | Optional Google Workspace Events topic for Chat-space change notifications. Leave blank unless intentionally using that separate feed. |
| `ATTUNE_CHAT_PUBSUB_SUBSCRIPTION` | blank | Pull subscription paired with `ATTUNE_CHAT_PUBSUB_TOPIC`. Set both or neither. |
| `ATTUNE_CHAT_INTERACTION_PUBSUB_TOPIC` | blank | Topic receiving verified Google Chat app `MESSAGE` and `CARD_CLICKED` events from the republisher. Required for Google Chat interactions in push mode. |
| `ATTUNE_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION` | blank | Pull subscription consumed by Attune for Chat app interaction events. Required when `google_chat` is an interaction channel in push mode. |
| `ATTUNE_CALENDAR_PUBSUB_TOPIC` | blank | Topic to which the Cloud Run republisher publishes count-free Calendar change signals. |
| `ATTUNE_CALENDAR_PUBSUB_SUBSCRIPTION` | blank | Pull subscription consumed by Attune for Calendar change signals. |
| `ATTUNE_CALENDAR_WEBHOOK_ADDRESS` | blank | Public HTTPS callback registered by `events.watch`, normally `https://SERVICE.run.app/calendar-webhook`. It must route to the deployed republisher. |
| `ATTUNE_CALENDAR_ID` | `primary` | Calendar watched and synchronized. Keep `primary` for the principal's main calendar or supply a specific calendar ID. |

## Common channel configurations

Google services do not require Google Chat. For example, Gmail/Calendar with
Slack-only delivery and interaction uses:

```dotenv
SLACK_APP_TOKEN=xapp-...
SLACK_BOT_TOKEN=xoxb-...
ATTUNE_SLACK_CHANNEL=U...
ATTUNE_SLACK_ALLOWED_USERS=U...
ATTUNE_BRIEF_CHANNELS=slack
ATTUNE_APPROVAL_CHANNEL=slack
ATTUNE_NOTIFICATION_CHANNELS=slack
ATTUNE_INTERACTION_CHANNELS=slack
```

Google Chat without Slack uses `google_chat` in all four routing variables and
leaves both Slack tokens blank. To send briefs to both while accepting
interactions only in Slack:

```dotenv
ATTUNE_BRIEF_CHANNELS=slack,google_chat
ATTUNE_APPROVAL_CHANNEL=slack
ATTUNE_NOTIFICATION_CHANNELS=slack,google_chat
ATTUNE_INTERACTION_CHANNELS=slack
ATTUNE_ACK_DESTINATION_VISIBILITY=1
```
