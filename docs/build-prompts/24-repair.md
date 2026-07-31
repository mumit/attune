# 24 — Repair: green suite, hermetic clocks, retrieval precision

**Phase P0** · `docs/plan-2026-h2.md` · **Depends on:** nothing · **Blocks:** everything

---

Read `CLAUDE.md` and the P0 section of `docs/plan-2026-h2.md`. Run `pytest -q`
first and record the exact failure count — it is **not** green right now, and
knowing the starting number is how you prove you fixed only what you meant to.

Nine independent defects. Land them as separate commits; several are
load-bearing for later phases.

## 1. `main` is red — a wall clock inside a retention window

`JsonAttentionStore._bounded` (`orchestrator/attention.py`) calls
`datetime.now(timezone.utc)` directly and drops items older than
`RETENTION_DAYS = 7`. `tests/test_attention.py` pins `T0 = datetime(2026, 7, 18)`.
Once real time passed 2026-07-25 every seeded item was pruned on write and 8
tests began failing. Nothing in production is broken; the **test suite is not
hermetic** and `CLAUDE.md`'s "inject … clocks" rule was violated.

Give `add` (and `_bounded`) an optional `now: datetime | None = None` following
the `now or datetime.now(timezone.utc)` pattern already used in
`followup.py`/`importance.py`, thread it from the dispatcher, and pass explicit
times from the tests. Do the same for the other non-injectable clocks:
`draft_approve.py`, `grants.py`, `importance.py`, `pending.py` (one each).

Then add a **regression guard**: a test that fails if any module under
`src/attune/orchestrator/` calls `datetime.now(` outside a
`now or datetime.now(` expression. A grep-based AST check is fine; the point is
that the next time-bomb is caught at authoring time.

## 2. CI never re-runs, so nobody noticed

`.github/workflows/ci.yml` fires only on push-to-main and pull_request. Add a
`schedule:` trigger (daily) so a time-dependent regression surfaces within 24h
rather than on the next PR.

## 3. The live memory eval cannot execute

`.github/workflows/memory-eval.yml` sets six secrets but never
`ATTUNE_EMBEDDING_DIMENSIONS`, and `build_mem0_config` (`memory/mem0_store.py`)
raises `ValueError` without it (`config/__init__.py` defaults it to `None`). The
only live memory-quality signal in the project has never run. Add it to both the
secret gate and the env block.

## 4. Retrieval has no relevance floor

`min_score` exists on the `MemoryStore` interface (`memory/base.py`), is
implemented in `Mem0Store.search`, and is passed by **no production caller** —
only by a test. Context rot means low-precision retrieval is actively harmful,
not neutral: semantically-similar-but-irrelevant memories degrade output beyond
what context length alone explains.

Add an `ATTUNE_MEMORY_MIN_SCORE` setting (documented in `.env.example`) and pass
it at all four sites: `draft_approve.py` retrieve, `triage.py`
`_past_reactions`, `dispatcher.py` `_converse`, `brief.py` `_meeting_prep`.
Drop `draft_approve`'s retrieve `limit` from 8 to 3.

## 5. The email body is an unbounded embedding query and prompt input

`dispatcher.submit_gmail_thread` builds `incoming_summary` from the fully
decoded body with **no cap** (`connectors/google_oauth.py` returns it whole),
and that string reaches three prompts per message: the CLASSIFY call, the
retrieve embedding, and the DRAFT call. The conversational path already caps at
1,600 chars in `dispatcher._source_text` — the triage/draft path has no
equivalent. Cost, latency, embedding input limits, and injection surface all
scale with attacker-chosen length.

Bound `incoming_summary` the way `_source_text` does. Separately, the retrieve
node must **not** use the whole body as its query — extract subject plus sender
plus the first N chars.

## 6. The audit log is O(n²) on append

`audit/log.py::_last_hash` linearly scans the entire JSONL on **every** append
to find the previous `entry_hash`. This is the trust root for autonomy
graduation; it will get slow exactly as the evidence base gets valuable. Cache
the tail hash in memory behind the existing lock, and re-derive it from the file
on first use and after any external change (compare size/mtime). Do not change
the on-disk format or the chain semantics.

## 7. `importance.assess` reloads the whole profile per call

`JsonImportanceProfile.assess` does a full JSON load under `fslock` every call,
and `brief.py` calls it 40+ times per run inside `sorted()` key functions for
maybe 20 distinct senders. `PostgresImportanceProfile.assess`
(`hosted/intelligence.py`) opens a **new DB connection** per call.

Add a short-lived per-instance cache to both (invalidated on `record_signal`
and on file mtime change). Do not add a global cache — these objects are
per-runtime.

## 8. One retrieval site skips provenance framing

`brief._meeting_prep` extends its notes with raw `m.text`, the only retrieval
site that never calls `frame_memory_text` — directly against that function's own
documented rule ("call this at every site that turns a retrieved MemoryRecord
into prompt text"). Fix it.

## 9. A full audit scan per grant per digest

`grants._recent_decisions` calls `audit_log.query()` with no `since`, inside a
loop over every grant entry in `suggest_demotions`. Hoist one bounded query out
of the loop.

## Constraints

- No behaviour change other than the ones named. In particular: retention
  semantics, the audit chain format, and the importance rule engine's outputs
  must be identical for identical inputs.
- Every new setting goes in `.env.example` with a comment; `attune init` stays
  an in-place, line-preserving editor.
- Never read, print, or modify a populated `.env`.

## Acceptance

- `pytest -q` is green, and green again with a system clock set 60 days ahead
  (state how you verified this).
- The anti-wall-clock guard test fails if you re-introduce the bug.
- `memory-eval.yml` reaches the eval step in a dry run instead of raising.
- A test asserts `min_score` and the bounded query reach `Mem0Store.search`
  from each of the four production call sites.
- Benchmarks recorded in the decisions entry: audit appends/sec before and
  after (10k-entry log), and `assess` call count vs. file reads for one brief.
- `docs/decisions.md` entry, newest first, recording the hermeticity rule and
  the retrieval-precision rationale.
