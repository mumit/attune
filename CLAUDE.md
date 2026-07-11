# CLAUDE.md — Aide-de-camp

Standing context for Claude Code. Read this and `docs/decisions.md` at the start
of every session before making changes. `docs/design.md` is the deeper reference
for architecture, memory model, autonomy ladder, and roadmap. `docs/deployment.md`
covers the concrete GCP setup for personal and TELUS deployments.

## What this is

A self-learning workspace assistant over Gmail, Calendar, Google Chat, and Slack,
running on Fuel iX (a TELUS OpenAI-compatible gateway, bearer-token auth). It
reads mail/calendar, drafts replies, asks for approval, and learns from what the
user does with its drafts. Named after the "trusted officer who acts within
delegated authority" metaphor — that maps directly to the earned-autonomy ladder.

## Repo shape

Monorepo, two independently-publishable packages:

- `packages/bearer-openai/` — generic, **vendor-neutral** OpenAI-compatible
  client for bearer-token gateways. Knows nothing about Fuel iX or this app.
  Intended to be split into its own repo later.
- `packages/aidedecamp/` — the assistant. Depends on bearer-openai. Holds all
  Fuel iX specifics, orchestration, memory, connectors, channels, ingestion.

Dev setup and full test run:

```bash
pip install -e "packages/bearer-openai[dev]"
pip install -e "packages/aidedecamp[dev]"
pytest        # 312 tests should pass as a baseline before you change anything
              # (the republisher under packages/aidedecamp/deploy/republisher/
              # has its own separate test suite — see its own docstring)
```

Optional extras are lazy-imported so the package loads without them:
`[memory]` (mem0+qdrant), `[orchestrator]` (langgraph), `[slack]` (slack-bolt),
`[google]` (google-api-python-client + google-auth, for DirectOAuthConnector
and `credentials.py`).

## Module map (aidedecamp)

- `fuelix.py` — verified base_url + model IDs + task-shape routing + embedding
  models. The ONLY place Fuel iX specifics live.
- `config/` — per-deployment Settings from env (personal vs TELUS are separate
  deployments, not in-code branches).
- `credentials.py` — Google credential loading (service account / OAuth user /
  ADC), scoped for Gmail/Calendar/Chat.
- `orchestrator/` — LangGraph. `autonomy.py` (permission matrix), `state.py`,
  `draft_approve.py` (the canonical retrieve→draft→gate→approve→capture loop;
  also `resume_workflow(graph, thread_id, decision, text)`, the one shared
  `Command(resume=...)` invoke used by Slack, Chat, and the async Chat-
  interaction path — don't reintroduce a fourth copy of this), `triage.py`
  (plain function, not a graph — one `Task.CLASSIFY` call deciding
  URGENT/ROUTINE/NOISE; see `dispatcher.py` below for where it gates drafting),
  `scheduling.py` (plain function — `detect_conflict`, read-only overlap check;
  no hold-creation/accept-decline action layer built, deliberately — see
  `docs/decisions.md`).
- `memory/` — substrate-agnostic `MemoryStore` (`base.py`), Mem0 impl
  (`mem0_store.py`), capture signals (`signals.py`).
- `connectors/` — swappable `WorkspaceConnector`: `mcp.py` (real, Google managed
  MCP), `direct_oauth.py` (real, google-api-python-client). `make_connector`
  selects by config. `get_event(event_id)` is the single-item counterpart to
  `list_events` (mirrors `get_thread`/`list_threads`), used by
  `scheduling.detect_conflict`.
- `channels/` — `slack.py` (Socket Mode: approval buttons + `message.im`
  conversational DMs via `message_fn`, `make_slack_say` for proactive posts) +
  `gchat.py`/`gchat_cards.py` (Cards v2, thin-door, `post_text`).
  `GoogleChatChannel.handle_interaction` is retained for tests/direct
  in-process use, but is **not** the production path for approve/reject — see
  `dispatcher.handle_chat_interaction` below — plus pure `blocks.py` builders
  shared by both channels.
- `ingestion/` — `gmail_watch.py` + `gmail_history.py` (Gmail watch lifecycle,
  Pub/Sub notification reconciliation); `chat_events.py` (Workspace Events
  subscription lifecycle + message parsing); `chat_interactions.py`
  (`decode_chat_interaction` — parses a CARD_CLICKED event into
  approve/reject only; edit is deliberately excluded, see module docstring);
  `calendar_watch.py` + `calendar_sync.py` (Calendar channel lifecycle +
  sync-token reconciliation); `state.py`
  (`JsonGmailWatchState`/`JsonChatSubscriptionState`/`JsonCalendarChannelState`/
  `JsonCalendarSyncState` — concrete, file-backed persistence for all four
  protocols above). Calendar *and* Chat card-interactions are the two sources
  needing a real inbound webhook — see rule 5 below.
- `dispatcher.py` — the routing seam: turns a decoded Gmail/Calendar
  notification, a Chat card-click, or a Chat/Slack message into a graph
  invocation, a conflict-check-and-notify, or a brief/converse reply.
  Channel-agnostic (`post_approval`/`post_text`/`notify`/`resume_fn` are
  injected callables); `handle_chat_message` and `handle_slack_message` share
  the brief-vs-converse routing via `_respond_to_message`.
  `handle_gmail_notification` triages every thread first (`triage_fn`,
  defaults to `orchestrator.triage_thread`) and skips drafting entirely for
  NOISE — a pure go/no-go gate, no auto-label or other write action.
  `handle_calendar_notification` is read-only the same way — it calls
  `notify` on a scheduling conflict, never creates a hold or answers an
  invite. `handle_chat_interaction` is the async half of Chat's approve/
  reject flow (see `docs/decisions.md`) — the republisher forwards a
  verified, decoded click here over Pub/Sub; this is what actually calls
  `resume_fn` and posts the real confirmation.
- `brief.py` — read-only morning brief (first end-to-end deliverable). A plain
  function, not a graph — it has no HITL/interrupt need.
- `app.py` — runtime assembly (`build_app` → `AppContext`): wires the real
  Fuel iX client, Mem0Store, SqliteSaver, and audit log into one process.
- `runtime.py` — the always-on entrypoint (`build_runtime` → `Runtime`): wires
  `AppContext` + connector + credentials + Slack/Chat channels into one
  process; `process_gmail_notification`/`process_chat_event`/
  `process_calendar_notification`/`process_chat_interaction`/`renew_*` are
  tested wiring, `run`/`run_*_pubsub_loop` are thin live loops (pull
  subscriptions, no inbound port) needing real GCP/Slack. `__main__.py` calls
  `build_runtime().run()`.
- `audit/` — `JsonlAuditLog`: structured, queryable reason-for-action log
  (design 4.7). Wired into `dispatcher.handle_gmail_notification` and
  `handle_chat_interaction` (a `chat_interaction_resumed` event under the
  `draft_approve` workflow); Q&A exchanges (Slack/Chat conversational
  replies) still aren't audited, which is probably fine (they're not
  draft/approve/reject decisions) but hasn't been explicitly decided.

## Non-negotiable rules

Do not weaken these to make something "work." They are the whole security and
safety posture; violating one is a bug, not a shortcut.

1. **bearer-openai stays vendor-neutral.** No Fuel iX base URLs, model IDs, or
   routing in it — those belong in `aidedecamp/fuelix.py`. If tempted, stop.
2. **Untrusted provenance tagging stays.** Content fetched from mail/chat/web is
   `Provenance.FETCHED` and must be framed as untrusted to the model, never as
   instructions. This is the indirect-prompt-injection defense (see the OpenClaw
   analysis in design §8). Never strip the "UNTRUSTED" framing.
3. **Autonomy is scoped, never global.** Actions are gated per `(action, domain)`
   in `orchestrator/autonomy.py`. The draft-approve gate fails safe: without an
   explicit grant at `ACT_NOTIFY`+, the graph routes through human approval and
   cannot silently send. Don't add a global-autonomy path.
4. **Send is refused by default.** The managed Gmail MCP has no send tool (draft
   only). `DirectOAuthConnector.send_reply` must stay refused unless
   `send_enabled` is explicitly set alongside a real `gmail.send` scope AND an
   autonomy grant. Enabling send is a deliberate, separately-reviewed change —
   never a step inside another task.
5. **No inbound port on the credential-holding process.** Ingestion is
   pull/outbound (Pub/Sub via an external republisher; Slack Socket Mode). Don't
   add a web server that listens on the box holding Google tokens / memory.
   Two sources need a real inbound webhook (Calendar notifications, Chat
   card-clicks) — both go through `deploy/republisher/`, a separate, minimal,
   credential-free Cloud Run service that only verifies (Chat only — see
   `docs/decisions.md` for why Calendar's route doesn't need to) and forwards
   to Pub/Sub. If a task seems to need the graph/checkpointer reachable from
   an HTTP endpoint, that's the sign to make it async through this same
   pattern, not to open a port on the main process.
6. **Secrets from env / secrets store, never in code or logs.** A rejected Fuel
   iX token raises `TokenRejectedError` ("needs manual rotation"); don't swallow
   it into a retry loop.

## Conventions

- Keep the lazy-optional-import pattern for langgraph / mem0 / slack_bolt.
- Keep collaborators (client, store, matrix, gmail service, checkpointer)
  **injected** so everything stays testable without live services — that's why
  the whole suite runs with fakes and no credentials. New code should follow the
  same shape.
- Keep the memory interface substrate-agnostic (`add`/`search`/`consolidate`) so
  the planned Mem0→Graphiti migration stays an implementation swap.
- Every new module gets tests that run offline (inject fakes). Match the existing
  test style in `packages/aidedecamp/tests/`.
- Log decisions: when you settle something architectural, append it to
  `docs/decisions.md` (newest first) in the same format.

## Environment

Copy `.env.example` to `.env`. `FUELIX_TOKEN` is needed for anything hitting Fuel
iX; Slack (`SLACK_APP_TOKEN`/`SLACK_BOT_TOKEN`) and Google creds come later per
phase. `.env` is gitignored — never commit it.

Fuel iX: `base_url = https://api.fuelix.ai`; models `claude-haiku-4-5`,
`claude-sonnet-4-7`, `claude-sonnet-5`, `gpt-5.4`, `gpt-5.6-luna`,
`gpt-5.6-terra`; embeddings `text-embedding-3-small` (default, 1536),
`text-embedding-3-large` (3072), `ada-002` (1536).

## Next steps (suggested order)

`app.py`, `DirectOAuthConnector`, the Google Chat channel, `credentials.py`,
Chat ingestion, `dispatcher.py`, the audit log, `runtime.py` (the entrypoint),
Slack conversational Q&A, Calendar ingestion, the triage step, Calendar
scheduling-conflict detection, `deploy/republisher/` (Calendar webhook +
Chat interactions, both async), and the async Chat card-interaction flow are
all done (see `docs/decisions.md`). What's left:

1. **A Calendar write-action layer**, if wanted: creating holds or responding
   to invites automatically. Deliberately not built — there's no well-defined
   trigger yet (unlike mail, where an incoming thread triggers draft-approve)
   and it would need its own autonomy-ladder design (rule 3), not something to
   fold in alongside conflict detection. Conflict detection itself
   (`orchestrator/scheduling.py`, `dispatcher.handle_calendar_notification`)
   is done and is read-only by design — see `docs/decisions.md` for why this
   boundary is deliberate, not a shortcut.
2. **Actually deploy it.** `runtime.py`'s wiring is tested, but `run()` and
   the `run_*_pubsub_loop()` methods have never touched a real GCP project or
   Slack workspace. `deploy/republisher/`'s Chat-interaction JWT verification
   (`verify_chat_request`: "HTTP endpoint URL" audience mode, checks the
   `email` claim) is now confirmed against Google's current docs — an
   earlier version checked the wrong claim (`iss`) entirely; see
   `docs/decisions.md` — but still hasn't been exercised against a live Chat
   app (`docs/deployment.md` §15, step 7, is the one thing that actually
   tests it). See `docs/deployment.md` for the concrete steps, configuration,
   and GCP resources this needs.

## Still open (verify before relying on)

- Google agent-tool quota/tiering vs. the Gmail watch-renewal + read cadence —
  confirm against current Google quota docs before production.
- No live deployment yet — Phase 0's "brief for a full week without
  babysitting" bar (design.md §6) is unverified; everything above is
  code-complete-and-tested, not run-in-production-complete.

(Google Chat's action-layer API design — sync events vs. Workspace Events pull
— is answered; see `docs/decisions.md`, "Google Chat channel" and "Credentials,
Chat ingestion, and dispatcher".)
