# Gap analysis — product goal vs. current state (2026-07-18)

> **Most of this analysis is dated.** Build prompts 24-35
> (`docs/plan-2026-h2.md`'s Phases P0-P9) have since resolved or
> substantially narrowed most of the G-numbers below — each has an inline
> status note where the evidence is strong (a specific `decisions.md` entry
> or confirmed current code), dated 2026-08-01. Where no note appears, the
> gap's status was not reverified this pass and should not be assumed
> either resolved or still open without checking current code. The summary
> scorecard at the bottom is rewritten to match.

The product goal, in the principal's words: *a personal assistant that
attends to my email, calendar, chat, and Slack; learns what's important;
and suggests actions based on these sources — moving toward measured
autonomy like a real assistant.*

This document scores the 2026-07-18 implementation (see
[current-state.md](current-state.md), itself now dated) against each clause
of that goal and lists the concrete gaps found then. The
[future-state plan](future-state.md) sequenced the remediation; see
`docs/plan-2026-h2.md` for what actually shipped.

## Clause 1 — "attends to my email, calendar, chat, and Slack"

**Status: half met.** Gmail and Calendar are genuinely attended: ingestion
cursors, triage (Gmail only), briefs, drafts, conflict detection, and
follow-up nudges all consume them as signal sources.

Chat and Slack, however, are **conversation surfaces, not attended
sources**. They authenticate the principal and carry briefs, approvals, and
Q&A — but no message flowing through a Slack workspace or Chat space is
triaged, remembered as workload signal, correlated with email threads, or
surfaced in the brief. "Attend to my chat and Slack" is currently
unimplemented in both deployment modes.

Gaps:

- G1. No triage or importance pipeline for Chat/Slack content; only the
  principal's own DMs to Attune are processed, and only as commands.
  **Narrowed (2026-08-01):** `ingestion/sources.py`'s `poll_slack_source`/
  `poll_chat_source` now feed the dispatcher, and `orchestrator/attention.py`'s
  `AttentionItem`/attention-budget machinery (Phase P6, build prompt 32)
  gives Chat/Slack content a priority and a budget-bounded surface — not the
  full triage/memory pipeline email gets, but no longer wholly unattended.
- G2. No calendar triage either — every conflict is surfaced (capped at 3
  per run by arrival order), regardless of meeting importance.
  **Narrowed (2026-08-01):** Phase P6's attention budget replaces arrival-order
  `MAX_*_PER_RUN` caps generally with a ranked, importance-allocated budget —
  see `docs/plan-2026-h2.md`'s P6 entry. Not reverified specifically for
  calendar-conflict surfacing.
- G3. No cross-source correlation: an urgent email and a same-topic Slack
  thread are never connected into one item. Not reverified this pass.

## Clause 2 — "learns what's important"

**Status: minimal.** The machinery for learning exists (memory store,
signal capture, nightly consolidation) but importance itself is barely
learned and barely used:

- G4. Priority is computed once per Gmail thread (URGENT/ROUTINE/NOISE) and
  then discarded except as a NOISE gate. URGENT changes nothing downstream —
  not notification urgency, brief ordering, draft tone, or autonomy
  eligibility.
  **Resolved (2026-08-01):** build prompt 25 ("Reconnect the learning loop")
  propagates priority through the dispatcher; Phase P6's attention budget
  (build prompt 32) ranks by importance rather than discarding it.
- G5. The only learned input to triage is a soft, unstructured memory
  search ("reactions to mail from {sender}") injected into the classify
  prompt. There is no per-sender or per-topic importance profile, no
  deterministic rule (e.g., "ignored 5 times → demote"), and no compounding
  score.
  **Resolved (2026-08-01):** `orchestrator/importance.py`'s
  `assess_from_signals` is a real per-sender importance profile with tier
  thresholds and decay (the rule engine `hosted/intelligence.py` also
  imports, per build prompt 35's convergence work) — not the soft
  memory-search-only signal this gap described.
- G6. Real learning (pattern extraction into durable preferences) happens
  only in the nightly consolidation pass with a 3+-repeated-signal bar —
  meaning weeks of identical behavior before anything crystallizes, and
  silence if the pass no-ops. Day-to-day reads are raw-signal searches.
  **Narrowed (2026-08-01):** build prompt 29's git-backed playbook adds a
  faster-crystallizing, provenance-tracked learning path (bounded bullets,
  ≤3 new/day, helped/harmed counters) alongside nightly consolidation — not
  a replacement for the 3+-repeat bar's own tradeoffs, but no longer the
  only path.
- G7. Retrieval quality against the real Mem0/Qdrant substrate has no CI
  coverage (live eval is manual-only), so learning regressions are
  invisible.
  **Resolved (2026-08-01):** build prompt 27's eval harness runs in CI with
  per-scorer deltas and a published judge-agreement rate; build prompt 24
  fixed `memory-eval.yml` never setting `ATTUNE_EMBEDDING_DIMENSIONS`, which
  had made the one live memory-quality signal structurally broken.
- G8. The hosted platform learns nothing at all: `attune.memories` and
  pgvector are schema-ready but no executor reads or writes them, and each
  hosted conversation is memoryless beyond its turn history.
  **Narrowed (2026-08-01):** `hosted/intelligence.py` now exists —
  `PostgresImportanceProfile`/`PostgresAttentionStore` importing the same
  dataclasses and rule engine as local's importance/attention modules — but
  its own docstring calls it "Dormant": no production entry point
  constructs one yet, so "no executor reads or writes them" is still
  accurate even though storage and rule engine now exist.

## Clause 3 — "suggests actions based on these sources"

**Status: narrow.** Two suggestion features exist — follow-up nudges on
quiet sent-threads and same-day conflict hold offers — both riding the
draft-approve graph. That is the entire proactive surface beyond the brief.

- G9. `Action.DECLINE_INVITE`, `Action.RESCHEDULE`, and `Action.LABEL` are
  defined in the autonomy vocabulary with zero implementing code — the
  natural next suggestions (triage my inbox, handle this invite, reschedule
  around this conflict) are aspirational enum members.
  **Resolved** — already flagged stale by `roadmap.md`'s own banner: all
  three are implemented, and build prompt 30 registered them (plus 6 more
  actions) in the capability registry.
- G10. Suggestion volume is capped by count-per-run in arrival order, not
  ranked by importance — on a busy day, which three threads get nudged is
  arbitrary.
  **Resolved (2026-08-01):** Phase P6's attention budget (build prompt 32)
  replaces the arrival-order `MAX_*_PER_RUN` caps with a hard daily ceiling
  allocated by ranked importance.
- G11. The brief is static in structure: no ranking by learned importance,
  no "what changed since yesterday," no configurable sections.
  **Narrowed (2026-08-01):** build prompt 32 adds user-authored recurring
  routines (`attune routine add "weekday 8am: unresolved threads from
  HIGH-tier senders"`) with the brief kept as one shipped default routine,
  not the whole architecture; build prompt 33 rebuilds the brief
  incrementally with sleep-time precompute. "What changed since yesterday"
  specifically was not confirmed shipped.
- G12. Hosted suggests nothing: no briefs, no nudges, no holds — bounded
  read-only Q&A plus mutation refusal is the whole conversational product.
  **Resolved (2026-08-01):** hosted proactive briefs shipped (2026-07-19,
  "Hosted proactive briefs close Phase 5"); a gated draft-and-approve write
  capability (`google.gmail.draft.create`, R2) also exists, off by default
  via `ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY`.

## Clause 4 — "moving toward measured autonomy like a real assistant"

**Status: scaffolded but frozen at the bottom rung.** The ladder
(observe → draft → act-with-notification → act), the per-(action, domain)
grants, the live-reloading gate, and the evidence-based graduation
suggestions are all real and well-tested. But:

- G13. Graduation never happens in-product. Suggestions are computed from
  the audit trail, yet only the CLI (`attune autonomy grant`) can accept
  one; chat is show-only, and there is no one-tap accept in the approval
  channel where the track record was earned.
  **Resolved (2026-08-01):** `orchestrator/grants.py`'s graduation cards
  (`GRADUATION_CARD_EXCLUDED_ACTIONS`/`GRADUATION_CARD_MAX_RUNG`) accept
  in-channel — see `docs/plan-2026-h2.md`'s success criterion "at least one
  autonomy grant graduates in-product, on evidence, without a terminal
  command."
- G14. Grants cannot be scoped by the very signals the system computes —
  there is no "auto-act on ROUTINE from known senders, always interrupt for
  URGENT." The gate reads only `(action, domain)`. Not reverified this pass.
- G15. `SEND_REPLY` is a dead end: granting it succeeds with a warning while
  remaining structurally inert, which is safe but misleading. Not
  reverified this pass — `SEND_REPLY` is registered in the capability
  registry (build prompt 30) at R3 with a real `apply` function, but
  whether the specific "succeeds with a warning, inert" behavior this gap
  named was ever fixed was not checked directly.
- G16. The trust root for graduation — the local JSONL audit log — is not
  tamper-evident (security finding F1), which matters more as autonomy
  decisions increasingly rest on it.
  **Resolved:** `audit/log.py`'s hash chain (`prev_hash`/`entry_hash` on
  every appended line, `JsonlAuditLog.verify`) closes F1; its own docstring
  names the one honest remaining limitation (pure tail truncation is
  undetectable without an external anchor — hosted's transactional outbox
  has one, this lightweight local file does not).
- G17. Hosted autonomy is fixed at R0 read-only: the typed capability
  gateway that would admit higher tiers is implemented and tested but wired
  to nothing, and no write capability (including draft-and-approve) exists
  for hosted customers.
  **Narrowed (2026-08-01):** `hosted/gmail_draft_capability.py` registers
  `google.gmail.draft.create` at R2, wired through `TypedCapabilityGateway`/
  `CapabilityAdmissionProducer` end to end (gated off by default via
  `ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY`) — "wired to nothing" is no
  longer accurate, though coverage remains one of the twelve actions local
  registers.

## Cross-cutting gaps

- G18. **Intelligence divergence.** The hosted path shares no
  triage/memory/brief/autonomy code with the local runtime; every
  improvement toward this goal must currently be built twice.
  **Narrowed (2026-08-01):** build prompt 35 (Phase P9) is the dedicated
  convergence pass — `RiskTier` unified (build prompt 30, before P9 even
  started), a shared channel-broker base, model-gateway bounds, a
  capability-identity protocol, a shared audit outcome vocabulary, and a
  shared conversation turn shape now exist. Core/hosted reuse moved from
  2.7% to ~6.1% (the prompt's own metric) — real progress, well short of
  "every improvement... built twice" no longer being true, but no longer
  fully true either. See `decisions.md`'s "Converge the two planes" entries
  for exactly what's shared today and what remains genuinely separate.
- G19. **Hosted is not sellable yet.** No production signup, no
  customer-content retention/deletion, incomplete export, no billing or
  quotas, no per-tenant model configuration, dev-sized scale ceilings, and
  job-failure-only monitoring.
  **Substantially resolved (2026-08-01):** production signup (a
  sessionless, function-owned ceremony), owner-initiated tenant deletion
  with content retention, customer export (writer invocation, download,
  cleanup, UI close-out), per-tenant model configuration and usage
  metering, and SLO-grade observability (request/task metrics, alerts, a
  dashboard) have all shipped — see `decisions.md`'s 2026-07-19 entries.
  Billing/quotas were not confirmed shipped; treat as the remaining open
  item pending direct verification.
- G20. **Setup friction gates adoption.** Self-hosted first value is
  dominated by manual Google Cloud Console ceremony plus a silent 7-day
  Testing-mode token expiry; hosted onboarding requires seven ceremonies
  with ten-minute recency windows.
  **Resolved:** the self-hosted setup-friction package shipped
  (`decisions.md`, 2026-07-19), plus hosted onboarding polish (recency
  countdown, reply notifications, first-run hints, terminal polling state).
- G21. **Local-runtime security soft edges** (findings F1–F9): tamperable
  audit, cross-process approval race, discipline-only log redaction,
  CWD state fallback, missing in-class Chat actor guard, unweighted
  correction-memory provenance, no local rate ceilings.
  **Resolved:** `current-state.md`'s own 2026-07-19 status addendum records
  F1-F9 as shipped (Phase 6 security hardening) — see that document and
  `decisions.md`'s "Phase 6 security hardening: F3, F4, F5, F6, F8, F9"
  entry.

## Summary scorecard

The table below is the original 2026-07-18 scoring, kept for its historical
value. A `Status now` column records the 2026-08-01 picture per the notes
above; where a clause spans several G-numbers with mixed status, the note
says so rather than collapsing to one word.

| Goal clause | Verdict (2026-07-18) | Status now (2026-08-01) |
|---|---|---|
| Attends to email + calendar | Largely met (triage depth aside) | Unchanged; G2/G3 not reverified |
| Attends to chat + Slack | Not met — surfaces only, not sources | Narrowed — Chat/Slack now feed the attention budget (G1), still not full triage/memory; G3 (cross-source correlation) not reverified |
| Learns what's important | Minimal — computed once, barely used, slow to crystallize | Largely resolved — priority propagates, a real per-sender importance profile exists, the playbook adds a faster learning path, eval coverage runs in CI (G4-G7); hosted's own learning storage exists but is dormant (G8) |
| Suggests actions | Narrow — two features, no ranking, three actions unimplemented | Substantially resolved — the three actions are implemented (G9), volume is budget-ranked not arrival-order (G10), routines add configurability (G11), hosted suggests via briefs and one gated draft capability (G12) |
| Measured autonomy | Scaffolded — ladder built, graduation manual, hosted frozen at R0 | Narrowed — in-product graduation cards exist (G13), the audit trust root is tamper-evident (G16), hosted has one wired R2 capability, gated off by default (G17); grant scoping by signal (G14) and `SEND_REPLY`'s own behavior (G15) not reverified |
| Foundation (safety, durability, tenancy) | Strong — ahead of the product built on it | Stronger — hosted is substantially sellable now (G19), setup friction resolved (G20), F1-F9 shipped (G21); intelligence divergence narrowed but not closed (G18) |
