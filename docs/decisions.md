# Decisions

A running log of settled architectural decisions, so the reasoning survives even
when the design doc gets long. Newest first.

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
  change, not a rewrite. MCP connector is real; direct-OAuth is a documented stub.
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
- Google Chat action-layer API design (sync events only vs full Workspace Events
  pull pattern for v1).
- Google's agent-tool quota/tiering impact on Gmail/Calendar watch + poll cadence.
