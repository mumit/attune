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

## Still open
- Google Chat action-layer API design (sync events only vs full Workspace Events
  pull pattern for v1).
- Google's agent-tool quota/tiering impact on Gmail/Calendar watch + poll cadence.
