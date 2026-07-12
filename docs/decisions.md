# Decisions

A running log of settled architectural decisions, so the reasoning survives even
when the design doc gets long. Newest first.

## 2026-07 — Resume-time audit: the earning evidence gets written (roadmap prompt 20, review finding #4)

- **The gap, honestly**: the graph produced `human_decision`/`applied`/
  `signal_captured` on resume, but `resume_workflow` never wrote them to
  the JSONL log — Slack resumes recorded nothing, Chat recorded only a
  `chat_interaction_resumed` marker under domain `"chat"` even for
  mail/calendar work. So `track_records()` could never observe a real human
  decision and **graduation suggestions could never fire in production**.
  Prompt 12's tests built audit files synthetically and certified the fold
  while the pipeline silently wrote nothing — exactly the "validates the
  algorithm, not the data path" failure the review named.
- **The fix lives in the one shared resume path**: `resume_workflow` gains
  `audit_log`/`user_id`/`actor` and records the post-resume events,
  name-filtered by `POST_RESUME_EVENTS` (safe against double-recording:
  dispatch-time records carry only pre-interrupt events, and auto-applied
  runs never resume). Domain comes from the result state — never hardcoded
  per channel — and the actor (prompt 17) is stamped onto `human_decision`.
  Audit failures never break a resume. The runtime's `_bound_resume` and
  the async Chat `_resume_fn` pass all three; `handle_chat_interaction`'s
  own marker now records the workflow's domain and tolerates injected
  3-arg resume fns via a TypeError fallback.
- **`test_audit_pipeline.py` constructs zero audit entries by hand** —
  every entry flows through the real compiled graph and real
  `JsonlAuditLog`: track records count real decisions, graduation fires on
  12 real unedited approvals, each event name appears exactly once per
  workflow, actor/domain are verified on the file, and a calendar card
  clicked in chat audits under `calendar`.

## 2026-07 — Live policy + real rung semantics (roadmap prompt 19, review finding #2)

- **Revocations bite without a restart.** The gate no longer captures a
  matrix at graph-compile time: it consults a `matrix_provider` per
  evaluation. `grants.make_matrix_provider` stats the grants file on each
  call and reloads only on mtime change — no polling thread, no staleness
  window. Failure posture: an unreadable/corrupt file keeps the **last
  good** matrix (never a different posture) and logs; a never-saved file
  yields the conservative default. `AppContext.current_matrix()` gives
  every posture surface (chat `autonomy`, the weekly digest) the live view;
  an injected `matrix` (tests) stays static.
- **Auto-applied runs no longer post phantom cards.** The dispatcher (and
  the follow-up + hold-offer flows) now branch on the gate's own
  `autonomy_gate` audit event (`routed_to == "auto_apply"` + `max_rung`) —
  chosen over checking `__interrupt__` because it's the authoritative
  record of which path ran and keeps every existing fake-graph test valid
  (no gate event = treated as interrupted, the conservative reading).
  Interrupted runs behave exactly as before.
- **The rungs mean what the design says**: ACT_NOTIFY acts, then tells —
  one honest after-the-fact notification ("Acted autonomously (…, act-notify
  grant): drafted a reply to X — done. Revoke with `aidedecamp autonomy
  revoke …`") via the runtime's shared `_notify_all`; AUTONOMOUS acts
  silently. Both audit (`auto_notified`/`auto_silent`, with the rung and
  the applied ref) — silence is a grant level, never an evidence gap.
- No pending-registry entry for auto-applied runs — nothing for the
  ignore-sweep to mislabel as IGNORED.

## 2026-07 — Email-safe ingestion + correct reply envelope (roadmap prompt 18, review finding #3)

- **The owner's own activity is no longer signal.** `gmail_history` skips
  `messagesAdded` records labeled `SENT` or `DRAFT` — sending a mail or
  saving a draft used to trigger triage and could produce a "reply" to
  yourself. A thread now counts as changed only when at least one inbound
  message arrived (a record with no `labelIds` is treated as inbound —
  never silently dropped).
- **`EmailThread.reply_to` is the correct reply target**: the newest message
  NOT authored by the owner, preferring its `Reply-To` header over `From`;
  empty when the thread has no counterparty. `DirectOAuthConnector` gains
  `owner_email` (bound from `settings.user_id` by `make_connector` when
  it's a real address) so the thread builders can tell counterparty
  messages from the owner's own; MCP maps a loose `reply_to` key. Without a
  known owner, the fallback is the newest message's envelope.
- **Apply targeting fixed**: recipient = `reply_to` → `last_from_addr` →
  `from_addr` (it was the FIRST message's sender — which, for M5 follow-ups
  on threads the owner started, meant follow-up drafts addressed back to
  the owner). **An empty or owner recipient refuses to materialize** — the
  assistant never drafts to its own principal
  (`make_connector_apply_fn(connector, owner_email=…)`, bound in
  `build_runtime`).
- **Follow-up candidates require a counterparty**: `find_nudge_candidates`
  drops threads whose `reply_to` is empty or the owner — an owner-only sent
  thread has nobody to nudge.

## 2026-07 — Principal allowlists: authenticate the human (roadmap prompt 17, review finding #1)

- **New non-negotiable rule 7** (numbered after the existing six so no
  cross-reference shifts): every human entry point checks the actor against
  `ADC_SLACK_ALLOWED_USERS` / `ADC_CHAT_ALLOWED_USERS`. Transport signatures
  (Slack request signing, Google's Chat webhook JWT) prove the *platform*
  called — they say nothing about which person typed or clicked. Before
  this, any workspace member who DM'd the bot got the owner's brief, could
  browse/teach/delete the owner's memories, and could approve the owner's
  drafts, all under `settings.user_id`.
- **Empty allowlist = deny-all**, fail-safe. There is deliberately no
  allow-all wildcard. The refusal message echoes the refused actor's *own*
  id (it's theirs; no owner data) so self-allowlisting is one copy-paste;
  `aidedecamp init` asks for the ids when a channel is configured.
- **Enforcement points**: Slack DM handler, approve/reject/edit action
  handlers, and the edit-modal `view_submission` (an unauthorized click
  gets an ephemeral refusal and does NOT replace the card — the owner can
  still act on it); Chat message senders (`handle_chat_message
  allowed_senders`) and card-click actors (`decode_chat_interaction` now
  carries `actor`; `handle_chat_interaction allowed_actors`). `None` means
  no-enforcement for direct/test use; the runtime always passes the
  configured set. Refusals are logged (actor id only) and audited under
  `"ops"` as `unauthorized_actor`.
- **Actor now rides the resume path** (`SlackChannel._resume(..., actor=)`,
  the runtime's `_bound_resume`) so prompt 20 can stamp who decided into
  the audit trail.

## 2026-07 — Calendar write actions: the design decision (roadmap prompt 16, phase 1)

Written *before* the implementation, per the build prompt's own rule: phase 2
must not exceed what this entry settles.

- **Trigger: detected conflicts only.** Of the two now-well-defined
  candidates — (a) a detected conflict offers a *resolution hold*, (b) an
  incoming invite offers accept/decline — **only (a) is built**. It reuses
  `create_hold`, an existing connector verb on both implementations, and has
  the clearest user value ("these two collided — want a hold at 2pm to
  rebook one?"). (b) requires a new RSVP API surface
  (accept/decline-an-existing-invite verb) and its own semantics —
  **deferred again, explicitly**, along with rescheduling and negotiating
  times with counterparties. Scope creep now has a written decision to
  argue with.
- **Autonomy shape**: the flow enters through the standard draft-approve
  graph — `Action.CREATE_HOLD` on `Domain.CALENDAR`, already at PROPOSE in
  `default_matrix()`, so the gate interrupts for human approval absent a
  higher grant. ACT_NOTIFY graduation would mean auto-creating the
  *tentative* hold and notifying — tentative holds are reversible, the
  canonical rung-3 property — but graduation excludes events with external
  attendees (design 3.2's own example of domain scoping); that exclusion
  is enforced at proposal time by only ever creating holds titled `HOLD:`
  with no attendees invited.
- **Mechanics**: the chosen slot rides in graph state
  (`hold_start`/`hold_end`/`hold_summary`, ISO strings — pointers, not
  parsed back out of prose), so approval materializes exactly the slot the
  human saw, regardless of how the model phrased the proposal text. The
  apply step grows a calendar branch calling `create_hold`; the shared
  confirmation becomes domain-aware ("tentative hold created on your
  calendar", never "draft created in Gmail" for a calendar decision).
- **No kill-switch setting**: an absent grant already gates
  nothing-happens-without-approval; parallel toggles would dilute the
  matrix as the single source of authority (rule 3). Hold-proposal cards
  post only when the caller provides `post_approval` (the runtime does).
- **`create_hold` only**: no event mutation, no attendee invitations on the
  hold, no RSVP calls; holds are created tentative (both connectors already
  do).

## 2026-07 — Quiet-thread follow-up nudges (roadmap prompt 15)

- **Design 3.3's fourth interaction pattern exists**: "you haven't heard
  back in N days — want a follow-up drafted?" A nudge is deliberately **an
  approval card for a FOLLOW_UP draft-approve workflow** — the normal gate →
  interrupt → card flow does everything else: approval materializes the
  Gmail draft via the apply node, edits feed correction capture, ignored
  cards decay via the pending sweep, dedupe via the pending registry. No
  new approval surface, no new autonomy path (rule 3 — the nudge offers;
  only the human approval acts).
- **`Action.FOLLOW_UP` is used, not `DRAFT_REPLY`** (the build prompt left
  this open; decided for honesty to the matrix's action-type granularity —
  "may propose follow-ups" and "may propose replies" are separately
  grantable/revocable). `default_matrix()` grants FOLLOW_UP/MAIL at
  PROPOSE; a test pins that the workflow interrupts absent a higher grant.
- **Candidates reuse `brief.find_quiet_threads`** (the single source of
  quiet-thread truth, as the brief-v2 entry demanded) filtered through
  `JsonNudgeState` cooldowns: at most once per thread per
  `ADC_NUDGE_COOLDOWN_DAYS` (7), hard-capped at 3 per run — a proactive
  feature that spams is worse than none (design 8.1's Lindy critique). The
  cooldown records only after a successful card post, so a crashed run
  retries rather than silently burning a thread's nudge budget.
- **Cards read as nudges**: `approval_blocks`/`approval_card` (and both
  channels' `post_approval`) gained an optional `title` — "Follow-up nudge
  — no reply in 5d: <subject>" instead of a reply-draft out of nowhere.
- **Scheduling**: daily at `ADC_NUDGE_TIME` (default 14:00 local —
  deliberately not brief time: the brief lists quiet threads, the nudge is
  the afternoon "want me to act?" follow-through), only when a channel is
  configured AND `user_id` is a real address (quiet-thread detection needs
  one). Audited under a `"followup"` workflow (`nudge_offered` + the
  workflow's own events).

## 2026-07 — Memory-informed triage (roadmap prompt 14)

- **Closes the original triage entry's "fast-follow, not done" flag**:
  design 1.2 lists "your past reactions" as a triage signal, and the system
  now produces exactly that history (IGNORED sweeps, rejection captures,
  consolidated preferences). `triage_thread` gains optional `store` +
  `sender` (+ `user_id`): one narrow search (`"reactions to mail from
  <sender>"`, limit 3) appends a `PAST REACTIONS` block to the
  classification prompt — the user's own captured behavior, trusted
  context, kept in the system prompt while the thread content stays in the
  UNTRUSTED-framed user message. Still exactly one cheap CLASSIFY call.
- **The failure defaults are untouchable and pinned by tests**: parse
  failures still yield ROUTINE (memory input must never change that — a
  dropped real email is worse than a spare draft), memory-retrieval
  failures silently yield an empty block (garnish must never break
  triage), and with no store the prompt is byte-identical to v1
  (regression-pinned).
- **The dispatcher's default path passes `app_ctx.store` + the thread's
  `from_addr` + the deployment user_id** via a sentinel check — callers
  that inject their own `triage_fn` keep the plain `(client, summary)`
  contract unchanged, so every existing test and integration passed
  unmodified.

## 2026-07 — Real consolidation pass + memory-quality regression set (roadmap prompt 13)

- **`Mem0Store.consolidate` is no longer a no-op.** The scheduled deep pass
  (design 2.2's third leg, with a caller since prompt 05) now: gathers raw
  action signals (`infer=False`, stored verbatim precisely so this pass
  could reason over ground truth) and existing facts, capped at 200 each (a
  backlog must never produce a mega-prompt); makes **one**
  `Task.CONSOLIDATE` call (Sonnet 5 — correctness compounds, design 4.5)
  demanding strict JSON (`promotions`/`merges`/`supersessions`, each citing
  the ids it absorbs/supersedes); and applies conservatively.
- **The conservative-apply contract, pinned by tests**: a malformed model
  response mutates *nothing* (a botched consolidation that mangles memory
  is far worse than a skipped night — the report says so and moves on);
  deletions happen only for ids the model explicitly cited AND that
  verifiably exist — never on ambiguity. Supersession is add-new (+
  `metadata.supersedes` breadcrumb) + delete-old, and the report notes that
  true bi-temporal validity windows await the Graphiti migration (Phase 4).
  The consolidation prompt frames all memory text as data, never
  instructions (rule 2 — some of it originated in untrusted mail).
- **The client rides into the store**: `Mem0Store(config, client=…)`,
  wired by `build_app` — without one, consolidate degrades to an honest
  no-op report.
- **The design-2.4 memory eval set exists**: `test_memory_quality.py` +
  `memory_quality_scenarios.json`, LoCoMo/LongMemEval-style categories —
  single-session recall, multi-session recall, preference recall, and
  **knowledge update** (Priya→Marcus ownership change: post-consolidation
  retrieval returns Marcus, the Priya fact is gone, the breadcrumb points
  back). Offline by default against a mem0-shaped fake substrate *under the
  real `Mem0Store` adapter* (the pipeline is the code under test, not
  embedding quality); a live variant (real Mem0/Qdrant/Fuel iX) runs behind
  `ADC_LIVE_MEMORY_EVAL=1`, manual only. Extend the scenario file whenever
  `memory/`, `signals.py`, or the consolidation prompt changes.

## 2026-07 — Autonomy: persistence, grant/revoke, earned graduation (roadmap prompt 12)

- **The earning mechanism finally exists.** "Autonomy is earned, not
  granted" (design pillar 2) had no persistence, no way to grant/revoke
  without editing source, and nothing computing a track record.
  `orchestrator/grants.py` adds all three over the raw material the audit
  log already records (`autonomy_gate` + `human_decision` +
  `approval_ignored` events, joined by workflow thread_id).
- **Persistence**: `JsonPermissionMatrixStore` (`ADC_AUTONOMY_STATE_PATH`,
  data-dir derived) stores `{"action|domain": rung}`. `build_app` loads it
  (else `default_matrix()`); the file is written **only** by explicit
  `grant`/`revoke` operations — the matrix object stays frozen. Loading is
  strict: an unknown action/domain/rung in the file is a hard error, never
  a silent skip — a corrupted autonomy file must not quietly change the
  safety posture. `PermissionMatrix.revoke` added (immutable, like grant).
  `AppContext` now carries the resolved matrix so surfaces can render it.
- **Track record + suggestions**: `track_records` folds the audit log into
  per-(action,domain) approved-unedited/edited/rejected/ignored counts
  (auto-applied runs excluded — a track record measures human judgment on
  proposals). `suggest_graduations` bar: ≥10 decisions, ≥95%
  approved-unedited, zero rejections, currently below ACT_NOTIFY.
  **Suggestions are information only — no code path may auto-apply one.**
- **Surfaces**: CLI `aidedecamp autonomy show/grant/revoke/record` (strict
  enum parsing — a typo exits 2 with the vocabulary, never defaults);
  chat `autonomy` is **show-and-suggest only** — a channel that relays
  untrusted content must not be able to escalate autonomy; a weekly
  `autonomy_digest` scheduler job posts suggestions phrased as the CLI
  command to run. Grants/revokes are audited under an `"autonomy"`
  workflow — the most audit-worthy events in the system.
- **Rule 4 pinned by test**: granting `SEND_REPLY` at any rung (even
  AUTONOMOUS) leaves `DirectOAuthConnector.send_reply` raising
  `SendNotPermitted` — the structural send gate is independent of the
  matrix, the CLI warns so on any send_reply grant, and
  `test_send_gate_survives_send_reply_grant` must never be deleted.

## 2026-07 — Memory transparency: see, correct, teach (roadmap prompt 11)

- **Memory stops being write-only from the user's view.** `get_all`/`delete`
  existed with no surface; `memory/commands.py` is now the engine both
  surfaces render: `list_memories` (numbered listing + a number→id map that
  makes "forget 3" unambiguous against exactly that listing),
  `resolve_memory` (listing number or unique id prefix/suffix — ambiguity
  returns `None`, never a guess), `forget_memory`, `remember_fact`
  (`signal: explicit`, `infer=True`). Every mutation is audited under a
  `"memory"` workflow (`memory_deleted`/`memory_taught`) — corrections to
  the assistant's knowledge are exactly the audit log's business.
- **Chat grammar** (routed *before* brief keywords — "what do you know
  about the morning brief" is a memory command): `what do you know [about
  <topic>]` / `memories …` → list ("about me/you" means everything, not a
  search); `forget <selector>` → **two-step** (shows the memory, requires a
  literal "confirm forget"; stale confirmations are a polite no-op);
  `remember <fact>`. Listing maps and pending confirmations live in a
  per-(channel,user) dict held by the Runtime — deliberately process-local;
  losing it across a restart costs one re-listing.
- **Rule-2 boundary stated where it matters**: the grammar only ever runs
  on the user's own direct messages (Slack DMs user-filtered, Chat events
  HUMAN-sender-filtered upstream) and must never be applied to fetched
  bodies — "remember that X" inside an email is content, not a command.
- **CLI**: `aidedecamp memory list [--query]` / `forget <id> [--yes]` /
  `remember <text>` — same engine, terminal rendering; forget prompts
  unless `--yes`. The `autonomy` group stays a placeholder for prompt 12.
- Deliberately **no bulk delete** — per-memory, explicit, confirmed.

## 2026-07 — Compose stack + quickstart docs (roadmap prompt 10)

- **`deploy/compose.yml` is the canonical stack**: Qdrant always, the
  assistant behind `--profile assistant` (so the substrate can run alone
  during setup while the CLI runs on the host). `deploy/mem0-compose.yml`
  is superseded but kept with a pointer note, since docs and old decisions
  link to it. New `deploy/Dockerfile` (repo-root context — it needs both
  packages) installs only the app extras; the republisher keeps its own
  image per the standalone-deployable convention. No secrets in image or
  compose (rule 6): `.env` via `env_file`; all state in the `adc_data`
  volume through `ADC_DATA_DIR=/data`.
- **`ADC_QDRANT_HOST`/`ADC_QDRANT_PORT`**: `build_mem0_config`'s default
  vector store now honors these, because inside the compose network Qdrant
  isn't localhost — the compose file sets `ADC_QDRANT_HOST=qdrant`. Unset
  keeps mem0's default behavior. Also: `Settings.from_env` now
  `expanduser`s `ADC_DATA_DIR` so a hand-edited `~/.aidedecamp` works.
- **README leads with a ~15-minute quickstart** (clone → compose up →
  `aidedecamp init` → `doctor` → `brief`), dev setup demoted to a
  subsection. **`docs/deployment.md` is restructured into two tracks**:
  Track A (poll mode — no GCP, with its explicit trade-offs: poll-cadence
  latency, and Chat approval buttons need the republisher) and Track B
  (the existing hardened GCP/push content). The "unexercised" honesty
  flag stays, now covering both tracks. `.env.example` rewritten
  poll-mode-first with every setting added since it was written.
- **Verification**: `docker compose config` validates; the image build is
  a documented manual step (`docker build -f
  packages/aidedecamp/deploy/Dockerfile .`) — the Docker daemon wasn't
  running in the dev environment at the time, matching this project's
  convention of flagging unexercised steps rather than claiming them.

## 2026-07 — Polling ingestion mode is the new default (roadmap prompt 09)

- **`ADC_INGESTION_MODE=poll|push`, default `poll`.** Push ingestion needs
  four Pub/Sub topic+subscription pairs, a deployed Cloud Run republisher,
  and watch lifecycle management before the first event flows — but every
  reconciliation primitive was already trigger-agnostic, so a timer can
  drive all three sources. Polling is outbound-only (exactly as
  rule-5-clean as pull subscriptions) and deletes all of that from the
  day-one path. Push stays fully supported and remains the hardened
  production posture. No deployment existed yet, so changing the default
  breaks no one.
- **The dispatcher seam did not move** — `ingestion/polling.py`'s steps
  synthesize the same decoded shapes push delivers, so `dispatcher.py`
  never learns which mode fed it: `poll_gmail_step` compares the profile
  `historyId` to the stored baseline (one cheap `getProfile` per tick;
  default cadence 120s, floored at 30s per the open Google quota concern)
  and synthesizes the push-shaped notification only on advance — the
  baseline still advances inside `process_notification`, so a failed
  reconcile re-synthesizes next tick; `calendar_poll_notification()` is a
  labeled no-payload trigger (the handler only ever reconciles the sync
  token); `poll_chat_step` lists messages past a stored high-water mark
  (`JsonChatPollState`) and wraps them in Workspace-Events shape — the
  mark advances **only after successful dispatch**, so a crash mid-batch
  redelivers rather than drops (mirroring Pub/Sub's redelivery semantics).
- **First run = baseline now, never replay** (both Gmail and Chat), the
  same semantic as push mode's initial watch registration.
- **Runtime**: `poll_once()` (testable, per-source failure isolation) +
  `run_poll_loop()` (thin supervised timer shell reusing prompt 06's
  backoff/heartbeat). `run()` branches: poll mode starts one timer thread,
  skips startup renewals, and `build_scheduler` drops the `renew_watches`
  job (nothing to renew). **The one caveat**: Chat card-click interactions
  can't be polled (Google POSTs them), so that single pull loop still runs
  in either mode when its subscription is configured; without it, Chat
  approval buttons don't resolve — Slack approvals work fully in poll mode
  (Socket Mode), and `run()` logs the limitation.
- **New config**: `ingestion_mode`, `poll_seconds` (`ADC_POLL_SECONDS`),
  `chat_poll_state_path` (data-dir derived).

## 2026-07 — CLI: init wizard, doctor, brief, run (roadmap prompt 08)

- **`aidedecamp` console script** (`cli/` package, `[project.scripts]`),
  stdlib argparse — five subcommands don't justify click/typer, and heavy
  imports live inside subcommands so `--help` works in a bare install.
  `memory`/`autonomy` groups are placeholders until roadmap M4.
- **`init`** — interactive wizard writing a grouped, commented, chmod-0600
  `.env` (refuses overwrite without `--force`; secrets via `getpass`, never
  echoed). Defaults `connector=direct_oauth` (works with plain OAuth
  credentials today; MCP stays a config value) and `ingestion=poll`
  (prompt 09's mode — written now so the wizard doesn't need a breaking
  change when it lands). Pointing it at an OAuth *client secret* offers to
  run the consent flow (`InstalledAppFlow.run_local_server`,
  google-auth-oauthlib, already in `[google]`) and saves an authorized-user
  file into the data dir. **The consent flow is the one documented
  exception to rule 5**: a short-lived localhost redirect listener during
  interactive setup, user-initiated, gone when consent completes — not a
  service port. Scopes are `SCOPES_DEFAULT`; `gmail.send` is never
  requested (rule 4).
- **`doctor`** — one PASS/FAIL/SKIP line per check with a fix hint, exit 1
  on any FAIL. Checks are injected `Check(name, fn)` objects so tests fake
  the battery; the default battery (env parse, data-dir writable, Fuel iX
  1-token call surfacing `TokenRejectedError`'s rotation message, Google
  credential load, Gmail/Calendar metadata reads, Mem0 reachability, Slack
  `auth.test`, Pub/Sub subscription existence) does only read-only work.
  `FATAL_CHECKS` (env/data-dir/fuelix/google-credentials) gate
  **`run`** — which otherwise configures logging and calls
  `build_runtime().run()`; `--no-checks` skips the gate.
- **`brief`** — assembles and prints one brief; deliberately builds only
  connector + client (no Mem0, no checkpointer) so "try it in a terminal"
  works before the memory substrate is even running. `--post` goes through
  the full runtime instead.
- **`ADC_DATA_DIR`**: `Settings.data_dir` now derives all eight `*_path`
  defaults (audit log, checkpointer DB, four ingestion state files, pending
  registry, conversation window) — one variable for new users — while any
  explicit per-path env var still wins.

## 2026-07 — Brief v2: local timezone, meeting prep, quiet threads (roadmap prompt 07)

- **The UTC day-boundary bug is fixed** (roadmap defect #7): `assemble_brief`
  computed "today" as the UTC day and rendered event times in UTC — for a
  Pacific user that's the wrong day window and every meeting seven hours
  off. It now takes `tz` (from `ADC_TIMEZONE`, stdlib `zoneinfo` — no new
  dependency): day boundaries are computed in the user's timezone and
  converted to UTC for the API window; rendered times are local, labeled
  `(times in <tz>)`.
- **Meeting prep** (design 3.3's "prep notes pulled from the last thread on
  each"): per event (capped at 8), up to two remembered facts via
  `store.search` plus the most recent related thread via **one**
  metadata-level `list_threads` query (`"<summary>" OR from:<attendee>`,
  `max_results=1` — read volume stays low per the still-open Google quota
  concern). Prep lines ride inside the existing untrusted block; still
  exactly one model call per brief, pinned by a test. Prep failures are
  garnish, never fatal.
- **`find_quiet_threads`** (design 3.3's "gone quiet"): threads where the
  user sent the last message ≥ N days ago (default 3). Deliberately the
  single source of quiet-thread truth — the follow-up nudge flow (roadmap
  prompt 15) must reuse it, not reimplement it. This needed latest-message
  metadata the thread dataclass didn't carry: `EmailThread` gained
  `last_from_addr`/`last_message_at` (first-message fields unchanged),
  implemented in both connectors (`messages[-1]` on the direct path; loose
  `last_from`/`last_message_at` keys on MCP).
- **`Brief` is structured now**: `meetings: list[MeetingPrep]`,
  `waiting_on: list[EmailThread]`, `timezone` — so the CLI (prompt 08) and
  future surfaces render parts without re-parsing prose. The quiet section
  only exists when a real `user_email` is available to match the last
  sender against; `runtime._assemble_runtime_brief` is the one place brief
  arguments derive from settings (all three brief surfaces share it), and
  passes `user_id` as the email only when it contains `@` (the Gmail
  `"me"` alias matches nothing).

## 2026-07 — Loop supervision + structured logging (roadmap prompt 06)

- **The silent-thread-death defect is closed.** Every pull loop was a bare
  `while True` on a daemon thread: one transient network error, one
  malformed payload into `json.loads`, and that ingestion source was dead
  until a human noticed mail had gone quiet. The four loops now share one
  supervised `_pull_loop(name, subscription, handler)`: transport errors
  back off exponentially (1s → 60s cap via `next_backoff`, reset on
  success; `DeadlineExceeded` on an empty pull is idleness, not failure),
  and the per-message body is a plain testable method,
  `_handle_pulled_message`, per the testable/live split discipline.
- **Poison messages are acked, not redelivered.** A message whose handler
  raises (or that isn't JSON) is logged *by Pub/Sub message id — never its
  payload* (rule 6, pinned by a caplog redaction test), audited under the
  `"ops"` workflow (`message_failed`), and acked — Pub/Sub redelivery of a
  deterministic failure is an infinite loop. Exception preserved:
  `HistoryExpired` still force-renews the Gmail watch (in
  `_handle_gmail_message`) and counts as handled.
- **Heartbeat**: `LoopStats` emits one log line per loop every ~5 minutes
  (pulled/handled/failed since last beat), so "is it alive?" is one
  `journalctl | grep heartbeat` away.
- **`logging_setup.configure(level, json_mode)`** — stdlib logging only
  (a metrics endpoint would be an inbound port, rule 5; logs are the
  observability surface at this scale). Plain lines by default, one JSON
  object per line under `ADC_LOG_JSON=1`; `ADC_LOG_LEVEL` sets the level;
  `__main__.py` wires it. Seam logging added where decisions happen
  (dispatcher triage skip / card posted, scheduler job failures, loop
  lifecycle) — identifiers only, never bodies or tokens.

## 2026-07 — Scheduler: the always-on process finally schedules things (roadmap prompt 05)

- **`scheduler.py`** — a deliberately hand-rolled in-process scheduler (~60
  lines, injected clock, fully deterministic under test) rather than
  APScheduler: four jobs on fixed cadences don't justify a dependency.
  `Job(name, next_run_fn, action)` + `daily_at("HH:MM", tz)` / `every(...)`
  helpers + `Scheduler.run_pending(now)` which fires due jobs, reschedules,
  and isolates failures (one failing job logs into `last_error` and never
  blocks siblings — and stays on cadence). Only `run_loop()` is a thin
  threaded shell (`pragma: no cover`, the pull-loop precedent).
- **First tick schedules, never fires.** Startup work that must happen
  immediately is the caller's job — `Runtime.run()` now calls
  `renew_all_watches()` once at boot (a fresh deployment must not wait a day
  for its first watch registration), and a fire-on-boot rule would repost
  the brief on every restart.
- **The standard job set** (`Runtime.build_scheduler()`): `daily_brief` at
  `ADC_BRIEF_TIME` in `ADC_TIMEZONE` (only when a channel is configured to
  carry it), `renew_watches` every 24h, `sweep_pending` every 6h (prompt
  03's registered TODO, now closed), `consolidate` at
  `ADC_CONSOLIDATE_TIME` — the nightly design-2.2 pass now has a caller;
  the substrate impl is still the no-op report until roadmap prompt 13.
- **Renewals are audited per-target under an `"ops"` workflow**
  (`watch_renewed`/`renewal_failed`) — a lapsed Gmail watch doesn't error,
  mail just quietly stops arriving, which is exactly the silent-failure
  class the audit log exists for. `renew_all_watches` only attempts
  renewals whose settings are configured, and one failure never skips the
  rest. `run_consolidation` audits its report the same way.
- **New config**: `timezone` (`ADC_TIMEZONE`, IANA name, default UTC —
  prompt 07's brief reuses this), `brief_time` (`ADC_BRIEF_TIME`, default
  07:30), `consolidate_time` (`ADC_CONSOLIDATE_TIME`, default 02:00).

## 2026-07 — Conversation context for Q&A (roadmap prompt 04)

- **`conversation.py`** — a `ConversationLog` Protocol + `JsonConversationLog`
  (the `ingestion/state.py` pattern): a rolling window of recent turns keyed
  by `(channel, user_id)`, capped at `ADC_CONVERSE_WINDOW_TURNS` (default 10
  messages) and expired past `ADC_CONVERSE_TTL_MINUTES` (default 120 — TTL is
  enforced on *read* too, so a window that sat on disk overnight comes back
  empty without a rewrite). `dispatcher._converse` replays the window between
  the system prompt and the current message, so "when is the second one?"
  finally works; brief-request exchanges are recorded too, so follow-ups
  right after a brief work the same way.
- **Working memory is a hard boundary from MemoryStore** (design 2.1's first
  row vs. everything `memory/` handles). Nothing here calls `store.add` — no
  fact extraction, no learning, no retrieval. If a Q&A exchange ever deserves
  to become durable memory, that's an explicit capture decision elsewhere,
  never a side effect of chatting. Stated in the module docstring because
  this is exactly the kind of boundary that erodes.
- **Provenance survives replay (rule 2)**: incoming chat text is stored
  *with* its `[UNTRUSTED chat]` frame and replayed verbatim as user/assistant
  turns only — history is never promoted into system/instruction content.
- **Backward compatible**: `conversation=None` (the default on
  `handle_chat_message`/`handle_slack_message`) preserves the old
  single-shot behavior byte-identically; `runtime.build_runtime` wires the
  real file-backed window into both channels' message paths
  (Slack `channel="slack"`, Chat `channel="chat"` — isolated windows).
- **New config**: `conversation_state_path` (`ADC_CONVERSATION_STATE_PATH`),
  `converse_window_turns`, `converse_ttl_minutes`.

## 2026-07 — Pending-approvals registry: card dedupe + the IGNORED signal (roadmap prompt 03)

- **`orchestrator/pending.py`** — a `PendingApprovals` Protocol +
  `JsonPendingApprovals` (the `ingestion/state.py` pattern), tracking each
  posted approval card as `{lg_tid: source_ref, domain, posted_at, status}`.
  Two consumers:
- **Dedupe**: `dispatcher.handle_gmail_notification` now skips any Gmail
  thread that already has an unanswered card — no triage call, no draft, no
  second card — recording a `superseded_notification` audit event against
  the *existing* card's workflow so "why didn't I get another card" stays
  answerable. Newly posted cards are registered after `post_approval`.
- **The IGNORED signal finally fires** (design 2.2 named it one of the two
  most underused capture signals; `ActionSignal.IGNORED` existed with zero
  writers). `sweep_ignored(registry, store, …, max_age, now)` resolves
  entries pending longer than `ADC_APPROVAL_IGNORE_HOURS` (default 48) and
  captures an IGNORED action signal (`infer=False`, verbatim, like all raw
  action signals) plus an `approval_ignored` audit event. Exposed as
  `Runtime.sweep_pending_ignored()`; cadence arrives with the scheduler
  (roadmap prompt 05).
- **Resolution lives in the one shared resume path.** `resume_workflow`
  gained an optional `pending` registry and marks the card resolved after
  every decision — `build_runtime` binds one `_bound_resume` closure into
  both channels and the async Chat path, so no per-channel bookkeeping
  exists to drift. `resolve()` is a no-op for never-registered workflows.
- **Deliberately signals-and-hygiene only**: a swept entry's workflow stays
  paused and resumable in the checkpointer (a very late click still works),
  and nothing writes to the underlying mail (rule 3 — IGNORED capture is a
  memory write, not an action).
- **New config**: `pending_state_path` (`ADC_PENDING_STATE_PATH`),
  `approval_ignore_hours` (`ADC_APPROVAL_IGNORE_HOURS`).

## 2026-07 — Edit flow wired end to end on both channels (roadmap prompt 02)

- **This closes the design's flagship learning-signal gap**: edit-before-send
  is the richest capture signal in the design (§2.2, the correction diff),
  and `capture_correction` was fully built and wired into the graph's
  `capture` node — but no production surface could produce an `edited`
  decision. Slack's Edit button was a literal `pass`; Chat's dialog submit
  was an unwired stub. After this change, a real edit on either channel
  fires `capture_correction`.
- **Slack**: Edit opens a modal (`blocks.edit_modal_view`, pure builder)
  prefilled with the draft **extracted from the approval card itself**
  (`extract_draft_from_blocks` — the card is the single source of what the
  user saw and chose to edit; no state lookup, no checkpointer read from the
  channel layer). `thread_id` + originating channel ride in
  `private_metadata`; the `view_submission` handler resumes
  `("edited", text)` and posts prompt 01's honest confirmation via
  `chat_postMessage`. The modal's `callback_id` is `ACTION_EDIT_SUBMIT`
  (`adc_edit_submit`) — a new shared action name in `blocks.py`, distinct
  from `ACTION_EDIT` (which only opens the editor and never touches the
  graph).
- **Chat**: the dialog-open click stays synchronous at the republisher
  (unchanged trust model — opening a dialog touches no state), but now
  returns a real dialog prefilled from the card echoed in the CARD_CLICKED
  event (the republisher is stateless, so the event is the only possible
  source). The dialog's **submit** is a real graph resume, so it rides the
  existing async path: republisher verifies + publishes it to the same
  interaction topic (sync response = an `actionStatus: OK` that closes the
  dialog), and `decode_chat_interaction` now decodes it to
  `ChatInteraction(decision="edited", text=…)` — dialog-open still
  deliberately decodes to `None`, and an edit submit with no text is dropped
  rather than resumed empty. `dispatcher.handle_chat_interaction` passes the
  text through; **no new plumbing** — the third resume-able decision reuses
  the approve/reject pipe wholesale.
- **New mirrored strings**: `adc_edit_submit` and the dialog field name
  `adc_edit_text` (`gchat_cards.EDIT_DIALOG_FIELD`) are duplicated (not
  imported) in `ingestion/chat_interactions.py` and `deploy/republisher/`,
  per the existing no-cross-dependency rule, pinned by equality tests on the
  aidedecamp side and by literal-string assertions in the republisher's own
  suite (22 tests, run separately with its own deps — verified in a scratch
  venv).

## 2026-07 — Apply step: approvals materialize as Gmail drafts (roadmap prompt 01)

- **The draft-approve graph gained the apply node its own docstring always
  claimed** (`retrieve → draft → gate → approve → apply → capture`). Until
  now, approving a draft captured a memory signal and stopped —
  `connector.create_draft` had zero callers, so an approved reply never
  existed anywhere the user could send it from. Apply materializes an
  `approved`/`edited` decision via an injected `apply_fn(state) -> ref |
  None`; `rejected` skips it entirely.
- **`apply_fn` is injected, not a connector import.**
  `make_connector_apply_fn(connector)` (duck-typed — the orchestrator never
  imports the connector layer) builds the production one: for `domain ==
  "mail"` it re-fetches the thread by `incoming_ref`, builds `Re:` subject +
  recipient from it, and calls `create_draft` — the safe write path; rule 4
  untouched, `send_reply` untouched. Recipient/subject are re-fetched rather
  than carried in checkpoint state, per the pointers-not-payloads state
  discipline. Bound in `runtime.build_runtime` (credentials/connector now
  resolve *before* `build_app` so the graph can close over the real
  connector); `build_app` just passes `apply_fn` through, defaulting to a
  no-op — `app.py` has no connector to bind.
- **Reused the existing `incoming_ref` state field instead of adding the
  `source_ref` the build prompt suggested** — the field ("pointer to the
  source item") already existed with exactly this meaning; the dispatcher
  just never set it. Now it does (`incoming_ref = <gmail thread id>`). New
  state fields: `applied_ref` (the created draft id) and `apply_error`
  (exception class name).
- **Apply never raises.** A `create_draft` failure is recorded in state +
  an `apply_failed` audit event and the flow continues to `capture` — the
  human's decision and its learning signal are never lost to a transport
  error. Success records an `applied` audit event with the draft ref; skips
  record `apply_skipped` with a reason.
- **Confirmations are now honest, and defined once.**
  `apply_confirmation(decision, result)` (orchestrator) is shared by Slack's
  button handlers, `dispatcher.handle_chat_interaction`, and
  `GoogleChatChannel.handle_interaction`: "draft created in Gmail" only when
  `applied_ref` is present, an explicit admission when `apply_error` is set,
  plain "Approved."/"Edited." otherwise. This replaces Slack's false
  "✅ Approved — sending." — nothing sends, and no confirmation may say so
  (pinned by a test asserting "sending" never appears).

## 2026-07 — Roadmap v2 + build prompts (full design/implementation review)

- **A full user-perspective review** (312 tests green at time of review) found
  the safety architecture and test discipline sound, but three product-level
  gaps: the interaction loop is open at both ends (Approve never materializes
  a Gmail draft — `create_draft` had zero callers — and the Edit flow, the
  design's richest learning signal, is a stub on both channels); the runtime
  cannot run unattended (no scheduler ever calls the `renew_*` watch
  renewals, posts the brief, or runs consolidation, and pull-loop daemon
  threads die silently on any exception); and setup requires the full GCP
  push stack before the first brief, though every reconciliation primitive is
  already trigger-agnostic and could be timer-driven.
- **`docs/roadmap.md` is the working execution plan** — five milestones
  ordered by user value (close the loop → runs itself → easy setup → visible
  learning → proactive), each with a "felt as" bar, superseding design.md §6
  as the day-to-day plan while keeping its phases as the long arc. It also
  catalogs 12 specific defects found (e.g., the brief computes "today" in
  UTC; `ActionSignal.IGNORED` is never captured; the Slack approve
  confirmation says "sending", which is false).
- **`docs/build-prompts/` contains 16 self-contained Sonnet prompts**, one
  per roadmap item, each restating the non-negotiable rules it brushes
  against, its dependencies, and offline-test acceptance criteria — designed
  to be run individually with Claude Code from the repo root.
- **Notable directional decisions encoded in the prompts**: polling becomes
  the default ingestion mode (outbound-only, rule-5-clean; push stays the
  hardened production posture); a single `ADC_DATA_DIR` derives all state
  paths; autonomy grants get persistence + an audit-derived track record but
  a human always makes the grant (suggestions are never auto-applied, and
  grant/revoke is CLI-only, not chat); the calendar write layer stays
  design-first (prompt 16 phase 1 is a decisions entry, not code).

## 2026-07 — CI fixed: `deploy/` excluded from the main test collection

- **Real CI failure**: `pytest packages/aidedecamp -q` (CI's actual invocation)
  errored collecting `deploy/republisher/test_main.py` with
  `ModuleNotFoundError: No module named 'flask'`. The republisher was always
  documented as "own dependency set, not part of the main pytest run," but
  that was only ever true by convention — nothing actually told pytest to
  skip it, so plain directory recursion picked it up anyway.
- **Fix**: `norecursedirs = deploy` added to both
  `packages/aidedecamp/pytest.ini` (what CI's `pytest packages/aidedecamp -q`
  step actually loads) and the root `pyproject.toml`'s
  `[tool.pytest.ini_options]` (what a combined
  `pytest packages/aidedecamp packages/bearer-openai` run from the repo root
  loads instead — pytest resolves a different config file depending on
  whether the given paths share a common package-level rootdir or fall back
  to the repo root, so both needed the same exclusion; missing either one
  would silently work only in some invocation shapes and fail in others,
  which is exactly the trap this CI failure walked into).
- **READMEs refreshed.** `README.md` and `packages/aidedecamp/README.md` had
  drifted badly — both still described "Phase 0, only fuelix.py/config/
  autonomy matrix built," when in reality read-only + rung-2 is complete end
  to end (312 tests). Rewritten to match `CLAUDE.md`'s module map and
  "Next steps"/"Still open" framing, with pointers to `docs/decisions.md` and
  `docs/deployment.md` alongside `docs/design.md`.
- `docs/deployment.md` §9 (VM setup) now runs the exact CI test invocation as
  a sanity check before the Qdrant/systemd steps, and §8's republisher test
  instructions note the exclusion is now enforced, not just documented.

## 2026-07 — `verify_chat_request` corrected against Google's actual docs

- **Found and fixed a real bug** while confirming the "audience-claim value
  needs confirming" flag from the previous entry: `verify_chat_request`
  checked `claims.get("iss") == "chat@system.gserviceaccount.com"`. Per
  [Google's documented Python sample](https://developers.google.com/workspace/chat/verify-requests-from-chat),
  the correct check is on the **`email`** claim, not `iss` — a Google-issued
  ID token's `iss` is Google's own generic OIDC issuer
  (`https://accounts.google.com`), not the calling service account; `email`
  is what identifies *which* service account the token was issued to. The
  original check would have rejected every legitimate request, since `iss`
  would never equal a service-account address — this would have failed
  silently as "every Chat approval gets a 403," not as an obvious crash.
- **Audience clarified**: Chat apps configure an "Authentication Audience"
  setting with two modes. This project uses **"HTTP endpoint URL"** mode
  (the right choice for a service, like this one, not using Cloud Run's own
  IAM-based auth) — the `aud` claim is the exact configured endpoint URL
  (`<republisher-url>/chat-interaction`), verified via a Google-signed OIDC
  ID token. The alternative "Project Number" mode uses a different,
  JWT-based check against the numeric project number instead — not
  implemented here, and a genuinely different code path if ever needed.
  `CHAT_APP_AUDIENCE` must be set to that exact endpoint URL string, matching
  what's configured in the Chat app's Connection settings — not the bare
  Cloud Run base URL, not a project number.
- **Still not exercised against a live Chat app** — this is now confirmed
  against Google's current documentation (as opposed to being an assumption
  extrapolated from the general shape of Google's ID-token verification
  APIs), but "matches the docs" and "works against a real running Chat app"
  are different levels of confidence; `docs/deployment.md` §15 flags the
  approval-flow smoke test as the one thing that actually exercises this.

## 2026-07 — Async Chat card-interaction flow (resolves the republisher gap)

- **The problem**: `GoogleChatChannel.handle_interaction()` must return a
  synchronous HTTP response (Chat expects the confirmation text back in the
  response body), which means whatever receives the CARD_CLICKED webhook has
  to call `Command(resume=...)` on the actual compiled graph — needing the
  checkpointer, and by extension the memory store (Mem0, which needs the
  Fuel iX token for embeddings). That's real state, not a stateless forward,
  so "point the republisher at `handle_interaction`" (the framing this
  project used until now) doesn't actually satisfy rule 5. Gmail/Slack never
  hit this: Gmail's republisher only ever forwards to Pub/Sub, and Slack's
  Socket Mode means the same process holding the graph handles button clicks
  in-process — there's no separate internet-facing surface for Slack at all.
- **Options considered and rejected**: (1) a separate "resume service"
  holding only the checkpointer — traced through, it also needs Mem0/Fuel iX
  access (the `capture` node writes memory on every resume) and can't share
  the VM's local SQLite file over a network, forcing a Postgres + networked-
  Qdrant migration for what's a personal-scale system, and still doesn't
  fully close the risk (a compromise can forge approve/reject decisions,
  poisoning memory with fake preference signals even without send access).
  (2) opening a port on the credential-holding VM itself — the literal thing
  rule 5 exists to prevent, reopening the OpenClaw-class threat model
  (design §8.1) for UX convenience. Neither survived scrutiny.
- **Settled: make Chat interactions asynchronous, same shape as
  Gmail/Calendar.** The public endpoint (`/chat-interaction` on the
  republisher) verifies the request is genuinely from Google Chat, forwards
  the decoded click to Pub/Sub, and returns an immediate placeholder ack.
  The main process — the sole authority for resuming workflows, already —
  pulls it, resumes the graph, and posts the real confirmation. Approval
  authority never leaves the main process; the endpoint's worst case matches
  the Calendar republisher's already-accepted risk (forward a bogus message,
  safely re-validated), not a new kind of trust boundary.
- **Edit stays synchronous.** Edit's initial click never calls
  `Command(resume=...)` — it only opens a dialog, which needs no state — so
  it's answered directly by the republisher, no Pub/Sub involved.
  `ingestion/chat_interactions.py::decode_chat_interaction` returns `None`
  for edit clicks specifically so the async path can't accidentally try to
  resume something it shouldn't.
- **New shared `orchestrator.resume_workflow(graph, thread_id, decision,
  text)`** replaces three near-duplicate `Command(resume=...)` implementations
  (`SlackChannel._default_resume`, `GoogleChatChannel._default_resume`, and
  now `dispatcher.handle_chat_interaction`'s resume_fn) with one. Two
  duplicates was tolerable; a third wasn't.
- **`ingestion/chat_interactions.py::decode_chat_interaction`** duplicates
  (doesn't import) the `adc_approve`/`adc_reject` action-name strings from
  `channels/blocks.py` — `ingestion/` doesn't depend on `channels/` anywhere
  else, and dispatcher-facing code deliberately never imports channel code.
  Kept in sync by a test asserting equality, the same technique already used
  to keep Slack's and Chat's own action names in sync.
- **`GoogleChatChannel.handle_interaction()` is retained**, refactored to
  share `decode_chat_interaction` for its approve/reject parsing, but it's no
  longer the production path — it's useful for tests and any direct
  in-process usage, while production goes through the async flow above.
- **The republisher gained request verification for the first time.** The
  Calendar route never needed it (a forged calendar notification just causes
  a harmless extra reconciliation); the Chat interaction route does, since an
  unverified request could forge an approval. `verify_chat_request` checks a
  Google-issued bearer JWT's issuer (`chat@system.gserviceaccount.com`) via
  `google.oauth2.id_token.verify_oauth2_token`. **The exact audience-claim
  value needs confirming against current Google Chat API docs before
  production** — implemented to the documented shape, not yet exercised
  against a live Chat app, flagged the same way this project already flags
  the Gmail/Calendar quota question as unverified.
- **Service renamed `deploy/calendar_republisher/` → `deploy/republisher/`**
  and gained a second route, rather than standing up a second Cloud Run
  service — both routes share the identical trust model (public, stateless,
  forwards to Pub/Sub, no credentials), so one service with two routes is
  less to deploy and monitor than two near-identical ones.
- **New config**: `chat_interaction_pubsub_topic`/`_subscription`
  (`ADC_CHAT_INTERACTION_PUBSUB_TOPIC`/`_SUBSCRIPTION`).

## 2026-07 — Calendar webhook republisher implemented

- **`deploy/calendar_republisher/`** — the piece `docs/deployment.md` §8
  flagged as "not built yet" now exists: a small Flask app (`main.py`) with
  one route, its own `requirements.txt`/`Dockerfile`/`test_main.py`, deployed
  independently of the `aidedecamp` package (same shape as
  `deploy/mem0-compose.yml` — infrastructure, not library code).
- **Waits for publish confirmation before acking.** `publish()` calls
  `future.result(timeout=10)` rather than returning immediately after
  `.publish()` — silently losing a Calendar notification because the webhook
  ack raced ahead of the actual Pub/Sub publish would be worse than the
  extra latency of waiting for confirmation.
- **Tested offline** (Flask's test client + an injected fake publisher via
  `app.config["PUBLISHER"]`/`app.config["TOPIC"]`), matching the rest of the
  codebase's injected-collaborator convention — no live GCP needed to verify
  the header-decoding and publish-wiring logic, only to actually deploy it.
  Flask is intentionally **not** added to the `aidedecamp` package's own
  dependencies — this service has its own `requirements.txt` and is tested/
  deployed independently, verified by installing Flask into a scratch
  environment, running its 8 tests, then uninstalling it and confirming the
  main 288-test suite was unaffected.
- `docs/deployment.md` §8 updated to point at the real path and drop the
  "not built yet" language.

## 2026-07 — Personal deployment moves to GCP; `docs/deployment.md` added

- **Personal now runs on its own GCP project + Compute Engine VM**, not the
  home server design.md §4.6 originally planned — a GCP project became
  available for personal use, so both deployments are now structurally
  identical infrastructure (separate GCP projects, same VM shape), not just
  separate config on different kinds of hardware. The reasons for keeping
  the two deployments fully separate (governance legibility, blast radius,
  differing trust levels) are unchanged — only what personal runs *on*
  changed. `design.md` §4.6 and §7 updated in place to reflect this rather
  than left stale.
- **`docs/deployment.md` is new**: the concrete "how to actually run this"
  guide — GCP project setup, API enablement, the personal-vs-TELUS
  credential-type split (OAuth user credentials for consumer Gmail vs.
  service-account-with-domain-wide-delegation-or-per-user-OAuth for
  Workspace), Secret Manager, Pub/Sub topics/subscriptions, the Calendar
  webhook republisher, the systemd unit, environment variable reference,
  first-run bootstrap, and a verification checklist.
- **Explicitly marked unexercised.** Every step is derived from the code and
  from Google's documented APIs, but nothing in it has been run against a
  real GCP project yet — it's a detailed first draft to execute and correct
  against reality, not a verified runbook. Flagged this way deliberately
  rather than presenting untested steps as settled procedure.
- **The Calendar webhook republisher is documented as a design, not
  implemented.** It's explicitly out of the `aidedecamp` package (a few lines
  of standalone Cloud Run code — receive the webhook, republish onto
  Pub/Sub, return 200) and is called out in the guide as the one piece of
  infrastructure code this repo doesn't contain. Building it is future work,
  not silently assumed to exist.

## 2026-07 — Scheduling conflict detection (design 1.2, 1.4, 4.2)

- **Deliberately narrower than "scheduling graph."** Design 4.2 names a
  scheduling graph as one of the small orchestrator graphs; what's built is
  read-only conflict detection (design 1.4's own example — "a heads-up that
  two meetings just collided"), not an action layer. The connector interface
  only exposes `create_hold` (create a NEW tentative event) — there's no
  accept/decline-an-existing-invite verb, and no well-defined trigger yet for
  "draft a scheduling proposal" the way an incoming email triggers
  draft-and-approve. Building an actual write path needs its own design
  decision about the trigger and how it fits the autonomy ladder (rule 3);
  folding it in unreviewed alongside conflict detection would have been
  scope creep into an under-specified feature, so it's explicitly deferred.
- **`orchestrator/scheduling.py` is a plain function**, `detect_conflict`,
  same reasoning as `triage.py`/`brief.py`: read-only, no HITL interrupt to
  checkpoint around. Two events conflict iff their `[start, end)` intervals
  overlap on the same calendar — `list_events` is already scoped to the
  deployment's own calendar, so no cross-calendar reasoning is needed.
- **New connector method: `get_event(event_id) -> CalendarEvent`**, the
  single-item counterpart to `list_events` (mirroring `get_thread`'s pairing
  with `list_threads`). Calendar ingestion (`CalendarChanges.event_ids`)
  deliberately stays thin — just changed ids, no dependency on `connectors/`
  — so turning an id into the attendees/time details conflict detection
  needs happens at the connector boundary, the same place provenance tagging
  already happens. Implemented in both `McpWorkspaceConnector` (new
  `TOOL_GET_EVENT`) and `DirectOAuthConnector` (`events().get(...)`).
- **`dispatcher.handle_calendar_notification` is new**, mirroring
  `handle_gmail_notification`'s shape: it now owns the sync reconciliation
  (moved from `runtime.py` — see below) *and* the conflict-check-and-notify
  action, consistent with how Gmail's dispatcher function owns both
  reconciliation and the draft-approve invocation. For every conflict found,
  `notify(text)` is called and — when `audit_log` is supplied — the
  detection is recorded under a `"scheduling"` workflow name, same
  transparency discipline as triage's `"triage"`-workflow skip record.
- **`Runtime.process_calendar_notification` is now a thin wrapper** around
  `handle_calendar_notification`, matching `process_gmail_notification`/
  `process_chat_event`'s shape instead of calling ingestion functions
  directly. Its return type changed from `CalendarChanges` to
  `list[ConflictResult]` — a real, deliberate behavior change (existing
  tests were updated to match, not just patched around).

## 2026-07 — Triage step (design 1.2, 4.2) — closes the Task.CLASSIFY gap

- **`orchestrator/triage.py` is a plain function, not a LangGraph graph** —
  same reasoning as `brief.py`: triage has no human-in-the-loop interrupt to
  checkpoint around, so a graph would add machinery with nothing to pause on.
  `triage_thread(client, incoming_summary)` makes one cheap `Task.CLASSIFY`
  (Haiku 4.5) call and parses a two-line `PRIORITY:`/`REASON:` response into
  `Priority.{URGENT,ROUTINE,NOISE}` + a reason string.
- **Parsing failures default to ROUTINE, not NOISE.** A malformed or
  off-format model response still results in the thread being drafted and
  routed through the existing human-approval gate — the safe default,
  because the downstream approval step is what actually protects against bad
  autonomous action. Defaulting to NOISE on a parsing hiccup would silently
  drop real mail with no human ever seeing it, which is a worse failure mode
  than one extra draft the human declines.
- **Deliberately narrow v1: no memory in the triage prompt.** Design 1.2 lists
  "your past reactions" as a triage signal; this classifies from the thread's
  own content only, to keep the pass cheap, single-purpose, and not
  duplicated with the draft node's own memory search. Fast-follow, not done.
- **`dispatcher.handle_gmail_notification` gained a `triage_fn` parameter**,
  defaulting to the real `triage_thread`, so the gap CLAUDE.md flagged
  ("Task.CLASSIFY routed but never called") is actually closed by default,
  not left as another opt-in nobody wires up. NOISE-classified threads never
  reach the draft-approve graph or `post_approval`; the skip itself is
  recorded to `audit_log` under a `"triage"` workflow name, so "why didn't it
  draft a reply" is answerable the same way "why did it draft this" already
  is.
- **Triage is a pure go/no-go gate — it does not act.** No auto-labeling,
  auto-archiving, or any other write on NOISE threads. Adding one would be a
  new autonomous write path outside the existing per-(action,domain)
  autonomy gate (rule 3) — explicitly out of scope here, flagged as a
  possible future addition that would need its own autonomy-gate review, not
  something to slip in unreviewed alongside a "just add triage" task.
- **Backward compatible by construction, verified**: every existing
  `handle_gmail_notification` caller/test that doesn't override `triage_fn`
  now also triggers one classify call through whatever fake client they
  already inject; since the safe default is ROUTINE, none of those threads
  get silently dropped, and the full suite passed unmodified except for the
  new triage-specific tests.

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
