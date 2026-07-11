# CLAUDE.md — Aide-de-camp

Standing context for Claude Code. Read this and `docs/decisions.md` at the start
of every session before making changes. `docs/design.md` is the deeper reference
for architecture, memory model, autonomy ladder, and roadmap.

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
pytest        # 253 tests should pass as a baseline before you change anything
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
  `draft_approve.py` (the canonical retrieve→draft→gate→approve→capture loop).
- `memory/` — substrate-agnostic `MemoryStore` (`base.py`), Mem0 impl
  (`mem0_store.py`), capture signals (`signals.py`).
- `connectors/` — swappable `WorkspaceConnector`: `mcp.py` (real, Google managed
  MCP), `direct_oauth.py` (real, google-api-python-client). `make_connector`
  selects by config.
- `channels/` — `slack.py` (Socket Mode: approval buttons + `message.im`
  conversational DMs via `message_fn`, `make_slack_say` for proactive posts) +
  `gchat.py`/`gchat_cards.py` (Cards v2, thin-door, approvals +
  `handle_interaction` + `post_text`) + pure `blocks.py` builders shared by both.
- `ingestion/` — `gmail_watch.py` + `gmail_history.py` (Gmail watch lifecycle,
  Pub/Sub notification reconciliation); `chat_events.py` (Workspace Events
  subscription lifecycle + message parsing); `calendar_watch.py` +
  `calendar_sync.py` (Calendar channel lifecycle + sync-token reconciliation —
  the one source with a real inbound webhook, see rule 5 below); `state.py`
  (`JsonGmailWatchState`/`JsonChatSubscriptionState`/`JsonCalendarChannelState`/
  `JsonCalendarSyncState` — concrete, file-backed persistence for all four
  protocols above). No action layer wired to Calendar changes yet (no
  scheduling graph exists).
- `dispatcher.py` — the routing seam: turns a decoded Gmail notification or a
  Chat/Slack message into a graph invocation or a brief/converse reply.
  Channel-agnostic (`post_approval`/`post_text` are injected callables);
  `handle_chat_message` and `handle_slack_message` share the brief-vs-converse
  routing via `_respond_to_message`.
- `brief.py` — read-only morning brief (first end-to-end deliverable). A plain
  function, not a graph — it has no HITL/interrupt need.
- `app.py` — runtime assembly (`build_app` → `AppContext`): wires the real
  Fuel iX client, Mem0Store, SqliteSaver, and audit log into one process.
- `runtime.py` — the always-on entrypoint (`build_runtime` → `Runtime`): wires
  `AppContext` + connector + credentials + Slack/Chat channels into one
  process; `process_gmail_notification`/`process_chat_event`/
  `process_calendar_notification`/`renew_*` are tested wiring,
  `run`/`run_*_pubsub_loop` are thin live loops (pull subscriptions, no
  inbound port) needing real GCP/Slack. `__main__.py` calls
  `build_runtime().run()`.
- `audit/` — `JsonlAuditLog`: structured, queryable reason-for-action log
  (design 4.7). Wired into `dispatcher.handle_gmail_notification` only — Q&A
  exchanges (Slack/Chat) aren't audited, which is probably fine (they're not
  draft/approve/reject actions) but hasn't been explicitly decided.

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
Slack conversational Q&A, and Calendar ingestion are all done (see
`docs/decisions.md`). What's left:

1. **A triage step.** `Task.CLASSIFY` (Haiku 4.5) is routed in `fuelix.py` but
   never called — every new thread goes straight to draft, with no
   urgent/routine/noise pass. Design 4.2 calls this a separate small graph.
2. **A scheduling action layer for Calendar.** Ingestion (`calendar_watch.py`/
   `calendar_sync.py`, `Runtime.process_calendar_notification`) stops at
   "here are the changed/cancelled event ids" — there's no graph that reacts
   to them (propose a hold, flag a conflict, etc.), matching design's
   unbuilt "scheduling graph."
3. **Actually deploy it.** `runtime.py`'s wiring is tested, but `run()` and
   the `run_*_pubsub_loop()` methods have never touched a real GCP project or
   Slack workspace — that requires provisioning the Pub/Sub topics/
   subscriptions (and, for Calendar, the external webhook republisher) design
   4.6 describes, which is infrastructure work, not code.

## Still open (verify before relying on)

- Google agent-tool quota/tiering vs. the Gmail watch-renewal + read cadence —
  confirm against current Google quota docs before production.
- No live deployment yet — Phase 0's "brief for a full week without
  babysitting" bar (design.md §6) is unverified; everything above is
  code-complete-and-tested, not run-in-production-complete.

(Google Chat's action-layer API design — sync events vs. Workspace Events pull
— is answered; see `docs/decisions.md`, "Google Chat channel" and "Credentials,
Chat ingestion, and dispatcher".)
