# Decisions

A running log of settled architectural decisions, so the reasoning survives even
when the design doc gets long. Newest first.

## 2026-07 — Calendar ingestion (design 4.3, 4.6's one webhook exception)

- **Two ingestion modules, mirroring the Gmail split exactly**:
  `ingestion/calendar_watch.py` (channel registration/renewal, parallel to
  `gmail_watch.py`) and `ingestion/calendar_sync.py` (notification
  reconciliation, parallel to `gmail_history.py`). Same reason for the split:
  channel/watch lifecycle and change-tracking are genuinely separate concerns.
- **Calendar's change-tracking is structurally different from Gmail's, and
  that difference is load-bearing.** Gmail's `users.watch()` call itself
  returns a fresh `historyId`, so re-registering the watch on
  `HistoryExpired` happens to also re-baseline it — one recovery action fixes
  both problems. Calendar's `events.watch()` returns no sync token at all; a
  sync token can *only* come from a full `events.list()` pass. So renewing
  the notification channel (`ensure_calendar_watch`) and recovering from an
  expired sync token (`full_calendar_sync`) are two **unrelated** operations
  here — `runtime.Runtime.process_calendar_notification()` catches
  `SyncExpired` and calls `full_calendar_sync`, never
  `renew_calendar_watch()`. Conflating the two would look plausible (it's
  exactly what works for Gmail) and would silently fail to recover.
- **Channel renewal stops the superseded channel** (`stop_calendar_channel`,
  called from inside `ensure_calendar_watch` on a real renewal) so Google
  doesn't accumulate stale channels against the same calendar resource —
  Gmail/Chat have no equivalent "stop" step since Pub/Sub watches don't leak
  the same way.
- **Calendar is confirmed as the one source needing a real inbound webhook**
  (design 4.6): `events.watch()` only delivers via HTTPS POST, no Pub/Sub
  option. The architectural answer stays the same as the rest of the system
  (rule 5): a thin, stateless external republisher receives and validates the
  webhook, then republishes onto a Pub/Sub topic
  (`calendar_pubsub_subscription`) this process pulls from — this codebase
  only implements the pull side and `decode_calendar_headers` (for that
  republisher's convenience, mirroring `decode_pubsub_message`), never an
  inbound listener.
- **No action layer wired yet, deliberately.** `Runtime.process_calendar_notification()`
  stops at "here are the changed/cancelled event ids" — the same boundary
  `gmail_history.process_notification` has before `dispatcher.py` takes over
  and invokes the draft-approve graph. There is no scheduling graph yet
  (design confirms this is unbuilt), so inventing a dispatcher-level
  "handle_calendar_notification" would mean fabricating an action the design
  doesn't define. This is an honest stopping point, not a gap to silently
  paper over.
- **New concrete state**: `JsonCalendarChannelState` (epoch-ms expiration,
  same convention as `JsonGmailWatchState`) and `JsonCalendarSyncState`
  (trivial — a sync token is just an opaque string, no datetime involved).
- **New config**: `calendar_pubsub_topic`/`calendar_pubsub_subscription`,
  `calendar_webhook_address` (the republisher's HTTPS endpoint — Google POSTs
  here, not to this process), `calendar_id` (default `"primary"`),
  `calendar_watch_state_path`/`calendar_sync_state_path`.

## 2026-07 — Slack conversational Q&A (design 4.4)

- **`dispatcher.py` gains `handle_slack_message`**, sharing a new
  `_respond_to_message` helper with `handle_chat_message` so the brief-keyword-
  vs-`_converse` routing logic is defined once, not duplicated per channel.
  Unlike `handle_chat_message`, it takes already-extracted `text`/`user_id`
  rather than a raw event — Slack has no separate ingestion/decode step the
  way Gmail/Chat need Pub/Sub; Socket Mode delivers events synchronously
  in-process, so parsing happens right where the event arrives.
- **`SlackChannel` gains a `message_fn` constructor param** (mirrors
  `resume_fn`'s shape: injected, testable without a live graph) and registers
  `@app.event("message")`. The handler filters to DMs only
  (`channel_type == "im"`, matching design 4.4's specific mention of
  `message.im`, not all channel traffic) and drops the bot's own messages
  (`bot_id` present or `subtype == "bot_message"`) — same self-reply-loop
  rationale as `chat_events.process_chat_event`'s BOT-sender filter, just
  enforced at the channel layer since Slack has no separate ingestion layer.
  An unconfigured `message_fn` raises on the first DM rather than silently
  ignoring users — deliberately loud, matching `GoogleChatChannel`'s
  unconfigured-`send_fn` precedent.
- **`runtime.build_runtime()` wires it**: the auto-built `SlackChannel` gets a
  `message_fn` closure over the real `AppContext`/connector, calling
  `handle_slack_message` with `brief_fn` bound to `assemble_brief`. This
  closes the last channel-parity gap between Slack and Chat: both now support
  brief-on-request and conversational Q&A, not just approval-button clicks.
- **Still open**: nothing wires Slack DM *replies* to memory capture the way
  the draft-approve graph's `capture` node does — a Q&A exchange isn't itself
  a draft/approve/reject signal, so this is likely fine as-is, not a gap to
  close, but worth flagging if future design work assumes Q&A also feeds
  learning.

## 2026-07 — Runtime entrypoint (design 4.6, always-on process)

- **`runtime.py`** is the wiring layer that turns the tested library into an
  actually-running process: `build_runtime()` (override-or-build-real, same
  pattern as `build_app()`) assembles `AppContext` + credentials + connector +
  a raw Gmail service (ingestion needs one directly, independent of whichever
  `WorkspaceConnector` is configured) + Slack/Chat channels, and `Runtime`
  exposes the testable wiring: `process_gmail_notification`,
  `process_chat_event`, `renew_gmail_watch`, `renew_chat_subscription`.
- **Two kinds of code, deliberately kept apart.** The wiring methods above are
  fully unit-tested with injected fakes, same as every other module. The live
  loops (`run`, `run_gmail_pubsub_loop`, `run_chat_pubsub_loop`) are thin and
  `pragma: no cover`, matching the existing `SlackChannel.start()` precedent —
  they need a real GCP project and Slack workspace, so their correctness rests
  on the wiring logic they call being independently tested, not on testing the
  loop itself. `python -m aidedecamp` (`__main__.py`) just calls
  `build_runtime().run()`.
- **Pull, not push, per rule 5.** Gmail and Chat notifications arrive via a
  synchronous **pull** subscription (`google-cloud-pubsub`'s
  `SubscriberClient.pull()`, lazily imported, added to the `[google]` extra) —
  outbound-only, this process calls out to Pub/Sub, nothing calls in. Slack
  uses its existing Socket Mode. `run()` starts the two pull loops on daemon
  threads and blocks the main thread on Slack's Socket Mode connection (or, if
  Slack isn't configured, just waits, so the daemon threads keep running).
- **New concrete state implementations**: `ingestion/state.py`'s
  `JsonGmailWatchState`/`JsonChatSubscriptionState` are the first real
  implementations of the `WatchState`/`SubscriptionState` protocols — until
  now every test used a dict-backed fake and nothing persisted for real.
  Deliberately NOT interchangeable despite the similar shape: each serializes
  `expiration` the way its *consuming* module's read path expects — epoch
  milliseconds for `gmail_watch.ensure_watch` (`_from_epoch_ms`), an ISO-8601
  string for `chat_events.ensure_subscription` (`_parse_expire_time`). Getting
  this backwards wouldn't fail loudly, it would silently mistime renewal —
  covered by tests that exercise each state class through its consuming
  module's actual renewal-decision path, not just round-tripping the field.
- **`make_slack_say(bot_token, channel)`** (new, in `channels/slack.py`) is the
  Slack counterpart to the existing `make_chat_send_fn`: a `say`-shaped
  callable for proactive posts (a Gmail-triggered approval card, a brief) that
  don't arrive inside a live Slack event with their own `say` in scope.
- **`GoogleChatChannel.post_text(space, text)`** (new) — the plain-text
  counterpart to `post_brief`/`post_approval`'s Cards v2 payloads, needed to
  wire `dispatcher.handle_chat_message`'s conversational replies to Chat; there
  was no way to send a bare-text message before this.
- **New config fields**, following the existing `*_path`/`*_topic` naming
  convention: `user_id` (default `"me"` — one deployment acts as one identity,
  used both as the Gmail API user alias and the memory/audit `user_id`),
  `slack_default_channel`/`chat_default_space` (where to post proactively,
  absent a live event context), `gmail_pubsub_subscription`/
  `chat_pubsub_subscription` (the pull subscription, distinct from
  `gmail_pubsub_topic`/`chat_pubsub_topic` — watch/subscribe calls *publish* to
  a topic, the runtime *pulls* from a subscription attached to it),
  `gmail_watch_state_path`/`chat_subscription_state_path` (where the two new
  JSON state files persist).
- **Still not wired**: Slack conversational Q&A (`process_chat_event` only
  targets Chat — Slack has no `_converse` equivalent yet) and Calendar
  ingestion (no push-notification path exists). Both remain in `CLAUDE.md`'s
  next-steps list.

## 2026-07 — Audit log (structured, retrievable reason-for-action)

- **`audit/log.py` implements the design-4.7 requirement.** The draft-approve
  graph already produced structured `audit_events` (`retrieved`, `drafted`,
  `autonomy_gate`, `human_decision`, `auto_applied`, `signal_captured`) via
  `_audit()`, but they only ever lived inside the LangGraph checkpoint, keyed
  by `thread_id` — there was no way to ask "why did it do that" across
  workflows or after a checkpoint is pruned. This closes that gap.
- **`JsonlAuditLog`**: one JSON object per line, append-on-write, linear scan
  on read, at `settings.audit_log_path` (already-existing config, previously
  unused). Deliberately the simplest thing that satisfies "retrievable later"
  — a SQL/index-backed store is a drop-in swap behind the same two-method
  `AuditLog` protocol (`record`/`query`) later, exactly like the
  `MemoryStore` substrate-agnostic pattern.
- **`record()` stamps join keys onto every raw event**: `thread_id`,
  `workflow`, `domain`, `user_id` are attached at write time so a later
  `query()` needs only the audit file, never the original checkpoint.
  `query()` filters by any combination of `thread_id` / `domain` / `user_id` /
  `since`, plus a `limit` that keeps the most recent N.
- **Wired into `AppContext`** as a required field (`build_app()` constructs a
  real `JsonlAuditLog(settings.audit_log_path)` when no override is
  injected, following the same override-or-build-real pattern as `client`,
  `store`, and `checkpointer`).
- **Wired into `dispatcher.handle_gmail_notification`** via an optional
  `audit_log` keyword: when supplied, each workflow's `audit_events` are
  recorded against its `lg_tid` right after `graph.invoke()` returns. Left
  optional (not required) so existing callers/tests that don't care about
  audit history aren't forced to inject one.

## 2026-07 — Credentials, Chat ingestion, and dispatcher

- **`credentials.py`** resolves Google auth in priority order: (1) explicit
  credentials JSON file (`settings.google_credentials_file` → detects service
  account vs. OAuth user credentials by the `"type"` field), (2) ADC
  (`google.auth.default`). `SCOPES_DEFAULT` is the minimal set (read + compose
  for Gmail, read for Calendar, send + space-read for Chat). Scopes are
  injectable for tests/overrides. `google-auth` is a lazy import under the
  `[google]` extra, consistent with the optional-dependency convention.

- **`ingestion/chat_events.py`** follows the Gmail watch pattern exactly:
  `ensure_subscription` renews proactively at < 48h remaining, persists via
  an injected `SubscriptionState`, and falls back to a 7-day expiry when the
  Workspace Events API omits `expireTime`. `process_chat_event` extracts a
  bot-safe `ChatMessage` — returns `None` for BOT senders (preventing
  self-reply loops) and prefers `argumentText` over `text` to strip @mention
  prefixes before dispatching.

- **`dispatcher.py`** is the single routing seam: `handle_gmail_notification`
  processes a decoded Pub/Sub notification → reconciles history → fetches each
  changed thread via the connector → starts one draft-approve workflow per
  thread (LangGraph `thread_id = "gmail:<gmailTid>:<historyId>"`), waits for
  the interrupt, then calls an injected `post_approval(lg_tid, draft, rationale)`
  so the channel can post an approval card. `handle_chat_message` decodes a
  Chat space event, dispatches to an injectable `brief_fn` on brief/summary
  keywords, and falls back to `_converse()` (memory search + CONVERSE model)
  for everything else. Both functions accept all collaborators as keyword
  arguments and are fully testable offline.

- **Channel-agnostic callables** (`post_approval`, `post_text`, `brief_fn`)
  decouple the dispatcher from both Slack and Google Chat — the dispatcher
  never imports channel code. Space IDs and `say` callables are bound into
  the callables at assembly time (in `app.py` or the channel handler), not
  passed through the dispatcher. This makes the dispatcher a stable seam: the
  same function handles mail/chat regardless of which channel posts the card.

## 2026-07 — Name and license
- **Name: Aide-de-camp** (PyPI `aidedecamp`). Renamed from the working title
  "Steward" to avoid collision with several existing near-identical GitHub
  projects (study8677/Steward, rcarmo/python-steward, googlicius/obsidian-steward).
  The metaphor — a trusted officer who acts within delegated authority — matches
  the earned-autonomy ladder.
- **License: MIT**, matching the permissive norm of the surrounding ecosystem
  and keeping enterprise dependency review frictionless.

## 2026-07 — Monorepo, two packages
- `bearer-openai` is generic and vendor-neutral; `aidedecamp` depends on it and
  holds all Fuel iX specifics. Kept in one repo for now; `bearer-openai` is
  written to know nothing of `aidedecamp` so it can be split out later.

## 2026-07 — Fuel iX values verified
- `base_url = https://api.fuelix.ai`. Models: `claude-haiku-4-5`,
  `claude-sonnet-4-7`, `claude-sonnet-5`, `gpt-5.4`, `gpt-5.6-luna`,
  `gpt-5.6-terra`.
- Task routing: classify→Haiku 4.5, draft/converse→Sonnet 4.7,
  reason/consolidate→Sonnet 5. GPT models defined but unrouted pending a
  cost/quality comparison. Retune in `aidedecamp/fuelix.py:DEFAULT_ROUTING`.

## 2026-07 — Token handling
- Bearer token is swappable config (env / secrets store), never hard-coded. A
  401 raises `TokenRejectedError` ("needs manual rotation") rather than being
  swallowed by a retry loop.

## 2026-07 — Memory layer (Mem0, Fuel iX-wired)
- **Substrate: Mem0 self-hosted**, behind a substrate-agnostic `MemoryStore`
  interface (`add`/`search`/`get_all`/`delete`/`consolidate`) so the planned
  migration to Graphiti is an implementation swap, not an API change.
- **Mem0's extraction LLM points at Fuel iX** (`openai_base_url` = Fuel iX,
  Haiku 4.5 as the cheap per-write extractor), not OpenAI — no data leaves via a
  second unmanaged path. **Embeddings also served by Fuel iX** (verified:
  `text-embedding-3-small` @1536 default, `text-embedding-3-large` @3072,
  `ada-002` @1536); `build_mem0_config(embedding_model=...)` derives the vector
  store dims from the chosen model so the dims/model coupling can't be
  mis-set. 3-small chosen as default (quality/cost balance); 3-large is an
  upgrade requiring collection recreate; ada-002 not used (older, no advantage
  over 3-small at equal dims).
- **Capture signals**: correction diffs stored with light inference (extract the
  *preference*); raw action signals (approved/edited/ignored/rejected) stored
  verbatim (`infer=False`) so consolidation reasons over ground truth.
- **Personal vs TELUS isolation** is by separate deployments with separate
  stores, not by scoping within one store — cross-deployment leakage impossible
  by construction.
- Deep scheduled consolidation (cross-memory dedupe / stale-fact supersession on
  the strong model) deferred to Phase 4; Mem0's write-time update covers v1.

## 2026-07 — Orchestrator (LangGraph, HITL draft-and-approve)
- **LangGraph**, one small checkpointed graph per workflow rather than a single
  giant graph. Checkpointer required for interrupts; InMemorySaver in dev,
  Postgres/SQLite in production.
- **Draft-and-approve is the canonical rung-2 loop**: retrieve → draft → gate →
  approve(interrupt) → apply → capture. The assistant does the mechanical work;
  the human makes the judgment call via `interrupt()`/`Command(resume=...)`.
- **The autonomy gate fails safe**: without an explicit per-(action,domain)
  grant at ACT_NOTIFY+, the graph always routes through the human approval
  interrupt — it can never silently send. Only a deliberate graduated grant
  skips the interrupt.
- **State discipline**: audit trail is the one accumulator field
  (`Annotated[list, add]`); everything else overwrite. Raw bodies/transcripts
  kept out of state to avoid checkpoint bloat. `iteration_count` guards loops.
- **Provenance at the prompt boundary**: incoming content is tagged UNTRUSTED in
  the drafting prompt, enforcing the security discipline at the point it matters.
- Ties together all three earlier primitives: fuelix routing, autonomy matrix,
  memory (search-before-draft, capture-signal-after).

## 2026-07 — Connector layer + morning brief
- **Swappable WorkspaceConnector interface** with two implementations behind it:
  `McpWorkspaceConnector` (Google managed MCP servers) and
  `DirectOAuthConnector` (direct google-api-python-client). Selected by
  `config.ConnectorMode` via `make_connector` — a TELUS "no" on MCP is a config
  change, not a rewrite. MCP connector is real; direct-OAuth was a documented
  stub at this point (implemented later — see "DirectOAuthConnector" below).
- **Send is not a default capability.** The managed Gmail MCP server exposes
  create_draft + labeling but NOT send, so the MCP connector structurally can't
  send. `send_reply` is refused by default (`SendNotPermitted`); only the
  direct-OAuth path can send, and only with an explicit gmail.send scope +
  autonomy grant + `send_enabled` flag. Safe default (draft, human sends) is
  structural, not disciplinary.
- **Provenance tagged at the boundary.** Every fetched thread is marked
  `Provenance.FETCHED` (untrusted) where external data enters, so downstream
  layers can't forget it. Escalating OAuth scopes (readonly → compose → send)
  documented on the direct path; never request send "to avoid re-auth."
- **Morning brief** (`brief.py`) is the first end-to-end deliverable: read-only
  (unread mail + today's events → summary via CONVERSE model), writes nothing,
  frames mail as untrusted. The safe first thing to ship.

## 2026-07 — Slack channel (Socket Mode, thin door)
- **Slack via Bolt Socket Mode** (outbound WebSocket, no inbound port) — the
  channel is a thin surface over the one orchestrator: it renders briefs and
  approval cards and translates button clicks into `Command(resume=...)` on the
  paused graph. No assistant logic lives in the channel ("one brain, many doors").
- **Approval card carries the workflow thread_id** in each button's value, so a
  click routes back to resume the exact paused LangGraph workflow. Approve →
  resume approved; Reject → resume rejected; Edit → modal → resume edited.
- **Block builders are pure functions** in `channels/blocks.py`, testable
  without Slack and reusable for Google Chat cards later.
- slack_bolt is a lazy optional import; graph/app injected so the button→resume
  wiring is tested with a fake Bolt app, no live Slack.
- No-inbound-port transport is the concrete OpenClaw-class mitigation (design 8.1).

## 2026-07 — Google Chat channel (Cards v2, thin-door pattern)
- **Transport contract matches the no-inbound-port rule (rule 5).** Button
  clicks arrive as HTTP POSTs at a thin republisher outside the main process
  (same pattern as Gmail Pub/Sub ingestion). The republisher decodes the event
  and calls `GoogleChatChannel.handle_interaction(event)`; the return value is
  forwarded as the HTTP 200 response body back to Google Chat. The channel
  never opens a port.
- **`send_fn(space, payload)` is injected**, keeping `GoogleChatChannel` free
  of Google auth dependencies. `make_chat_send_fn(credentials)` is provided as
  a convenience for production assembly; tests inject a fake lambda.
- **Cards v2 format.** `gchat_cards.py` is the Cards v2 counterpart to
  `blocks.py`: pure functions, no I/O, testable without credentials. The
  approval card carries `thread_id` as an action parameter on every button
  (not as a block/widget identifier), which is how `handle_interaction`
  routes clicks back to the right paused workflow.
- **Action name strings are shared with `blocks.py`** (`"adc_approve"`,
  `"adc_edit"`, `"adc_reject"`). In Slack these are `action_id` values; in
  Chat they're `onClick.action.function` names and appear as
  `action.actionMethodName` in CARD_CLICKED events. Using the same strings
  means the orchestrator never needs to branch on which channel posted a card.
- **Edit action deferred** (dialog UI), same handling as Slack's modal: the
  button returns an `actionResponse.type: DIALOG` stub; the dialog submit
  path (which calls `resume("edited", text)`) is a wiring task, not a design
  question.
- **Still open (now answered):** Google Chat uses synchronous card interaction
  events (HTTP POST → thin republisher → `handle_interaction`) for the
  approval flow. Workspace Events API (Pub/Sub) is the right path for
  proactive space/message event ingestion (new @mentions, etc.) — deferred to
  the ingestion phase.

## 2026-07 — DirectOAuthConnector (google-api-python-client)
- **All five interface methods implemented** against the Google REST APIs:
  `list_threads` (threads.list + threads.get metadata), `get_thread`
  (threads.get full), `create_draft` (drafts.create with RFC 2822 base64url),
  `list_events` (events.list), `create_hold` (events.insert tentative).
  `add_label` resolves label names to IDs and creates the label if absent.
- **Send gate unchanged.** `send_reply` still raises `SendNotPermitted` unless
  `send_enabled=True`; the flag must be set alongside a real `gmail.send` scope
  and an autonomy grant. This is untouched from the stub, per the rule.
- **`list_threads` uses metadata format** for efficiency (no body data in the
  list pass); `get_thread` fetches full format to decode the MIME body tree.
  This means `list_threads` sets `body=snippet`, while `get_thread` decodes
  the actual plain-text part (recursively through multipart containers).
- **Services are injected** (`gmail_service=`, `calendar_service=`) so all
  tests run without credentials or network calls; services are lazy-built from
  `credentials` only when not injected. `make_connector` passes both kwargs
  through. `google-api-python-client>=2` added as the `[google]` optional extra.
- **Provenance is tagged at entry.** `_thread_from_metadata` and
  `_thread_from_full` both set `provenance=Provenance.FETCHED`; no caller can
  forget it.

## 2026-07 — Runtime assembly (app.py)
- **`AppContext` dataclass** holds the compiled graph, client, store, and
  settings together. It is a context manager that closes the SQLite connection
  on `__exit__`, so the connection lifecycle is handled correctly without
  requiring callers to track the `sqlite3.connect` handle directly.
- **`build_app()`** accepts optional overrides for every collaborator
  (`client`, `store`, `checkpointer`, `matrix`). When an override is absent the
  real implementation is constructed (Fuel iX client from env token, Mem0Store
  from `build_mem0_config()`, `SqliteSaver` backed by
  `settings.checkpointer_db_path`). The lazy-import convention is preserved:
  `SqliteSaver` is only imported inside `build_app` when no checkpointer is
  injected, so the package loads without `langgraph-checkpoint-sqlite`.
- **`settings.checkpointer_db_path`** added to `Settings` (env var
  `ADC_DB_PATH`, default `./aidedecamp.db`). `langgraph-checkpoint-sqlite>=2`
  added to the `orchestrator` optional extra.
- The SQLite connection is opened with `check_same_thread=False` because
  LangGraph resumes may arrive from a different thread than the one that created
  the saver.

## 2026-07 — Ingestion (Gmail watch → Pub/Sub → history)
- **Gmail push via users.watch → Pub/Sub → users.history.list.** Watch expires
  ≤7 days and stops *silently* on lapse, so renewal is a first-class daily
  scheduled op (`ensure_watch`) with explicit expiry tracking; renews
  proactively at <48h remaining.
- **Reconcile from the STORED historyId, not the notification's.** The push
  payload carries the LATEST historyId; using it as the start point would skip
  every change. `process_notification` uses the stored baseline as
  startHistoryId and advances it only on success.
- **Three silent-data-loss traps handled explicitly**: (1) latest-vs-start
  historyId, (2) stale historyId → 404 surfaced as `HistoryExpired` re-sync
  signal, (3) messages duplicated across history records → deduped by threadId.
- **No inbound port on the credential box** (design 4.6): this code takes an
  already-decoded notification; the Pub/Sub HTTP receipt + base64url decode runs
  in a thin republisher outside the process. `decode_pubsub_message` provided
  for that republisher.
- Transport-agnostic: Gmail client + watch-state store injected, so the whole
  reconcile/renew logic is tested without GCP, Pub/Sub, or credentials.
- Note: Google's agent-tool quota/tiering (still-open) governs watch/poll
  cadence; daily renewal + event-driven reads (not polling) keeps call volume low.

## Still open
- Google's agent-tool quota/tiering impact on Gmail/Calendar watch + poll cadence.

(Google Chat action-layer API design is answered — see "Google Chat channel"
above. Current still-open / next-steps list lives in `CLAUDE.md`, not here, to
avoid this list drifting out of sync with that one.)
