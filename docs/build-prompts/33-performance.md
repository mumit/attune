# 33 — Performance: concurrency, batching, and sleep-time precompute

**Phase P7** · `docs/plan-2026-h2.md` · **Depends on:** 28 · **Blocks:** nothing

---

Read `CLAUDE.md`, the P7 section of `docs/plan-2026-h2.md`, `brief.py`,
`connectors/google_oauth.py`, and `dispatcher.py`.

## Problem

Measured, not estimated:

- **The brief is ~64 sequential Google HTTP round-trips** plus ~9 memory
  searches plus 1 model call — **10–25 seconds wall clock** — and it is rebuilt
  from scratch on **every** on-demand "give me the brief" request, not only the
  daily post.
- **One Gmail notification carrying 25 changed threads** costs ~25 Gmail calls,
  ~50 model calls, ~50 memory searches, ~125 whole-file JSON/JSONL reads and 25
  channel posts — **all serial, ~60–90 seconds on one thread**.
- `DirectOAuthConnector.list_threads` is **N+1 by construction**: `threads.list`
  followed by one `threads.get` per result. It is called with `max_results=25`
  (brief), 20 twice (quiet threads), and 10 (planner).
- A calendar notification with N changed events costs roughly **3N** API calls;
  `decline_invite` and `reschedule_event` each re-fetch an event the apply
  function already fetched.
- Across all of `src/attune`: **no `asyncio.gather`, no `ThreadPoolExecutor`, no
  `concurrent.futures`, no `anyio`.** `async def` appears in exactly one file.
- No Google `BatchHttpRequest`, no `fields=` partial-response masks, no
  ETag/`If-None-Match`, no connection-pool configuration. The only cache in the
  tree is `_label_id_cache`.

Meanwhile the market has moved to competing on cost-per-answer — Glean shipped a
purpose-built agentic search model advertising ~50% lower latency and ~25% fewer
tokens as the headline feature, not answer quality.

## Task

1. **Concurrency for independent I/O.** A bounded worker pool (not unbounded
   fan-out — Google will rate-limit) for the genuinely independent reads:
   `brief`'s initial `list_threads` and `list_events`; `_meeting_prep`'s 8
   × (memory search + `list_threads`); `list_threads`' own per-thread hydration.
   Keep the public functions synchronous — do not convert the codebase to async
   for this. Threads plus a pool is the right amount of change; every
   collaborator is already injected, so a fake connector stays a fake connector.

2. **Kill the N+1.** Use Google `BatchHttpRequest` for `list_threads`'
   hydration where the client supports it, and add `fields=` masks everywhere so
   metadata reads stop transferring full payloads. Hydration must degrade to the
   current per-thread loop when batching is unavailable (MCP backend, or a client
   that lacks it).

3. **Stop double-fetching events.** `decline_invite`/`reschedule_event` each
   perform their own `events.get` before patching, on top of the apply function's
   `get_event`. Pass the already-fetched event through, and keep exactly one
   authoritative fresh read — the freshness check's read is the one that must
   stay, since its whole purpose is to be late.

4. **Move hot-path state out of whole-file JSON.** `pending`, `importance`,
   `attention`, `nudge`, and `graduation` state are all read-whole-file
   /write-whole-file under advisory locks; a 25-thread batch does ~125 of them.
   Move them into the SQLite database that is already a dependency for the
   checkpointer and the retry queue. Keep the `Protocol` interfaces unchanged so
   `hosted/intelligence.py`'s Postgres implementations and every injected test
   fake are untouched — this is a storage swap behind a stable seam, and the
   existing JSON stores stay available with a documented migration.

5. **Incremental brief.** Use the existing `BriefSnapshot` as a read baseline,
   not only as a since-yesterday diff source, so a daily run fetches deltas.
   Memoize the assembled brief for a short window so a second "give me the
   brief" within minutes is served, not recomputed. Coordinate with prompt 32,
   which touches the same code.

6. **Sleep-time precompute.** Assemble tomorrow's brief overnight in the
   existing nightly job. The evidence is direct: sleep-time compute yields ~5×
   less test-time compute for equal accuracy and 2.5× lower cost per query
   amortized, with benefit proportional to **query predictability** — and a
   personal assistant's mornings are the most predictable workload there is.
   The morning post then validates freshness and patches deltas rather than
   building from nothing.

7. **Cascade triage.** Route the CLASSIFY call to a small model by default and
   escalate to the strong model on low confidence, on HIGH-tier senders, or when
   the deterministic importance adjustment disagrees with the model. This needs
   prompt 28's structured output to have a confidence signal worth reading.
   Report the escalation rate in `attune metrics`.

8. **Prompt caching on the stable prefix**, using prompt 28's registry split.
   Report hit rate. Note the economics before picking a TTL: a longer TTL costs
   more on write and pays back only when predictable cache lifetime exceeds it.

## Constraints

- **No semantic caching on the generation path.** A false cache hit is a
  correctness incident when the output is a draft that gets sent. Exact-match and
  prefix caching only.
- Concurrency must not reorder audited effects or break the
  cursor-advance-then-retry-queue contract: cursors already advance before
  per-item work, so a concurrently-failing item must still reach the retry queue.
  Prove this with a test that fails one item in a concurrent batch.
- Rate limits and quota are real. Bound the pool, honour `Retry-After`, and add
  jitter — `_fetch_with_retry` is currently a bare 3-shot loop with no delay.
- Determinism under test is non-negotiable. Concurrency must not make ordering
  assertions flaky; sort results before returning them.
- No new heavy dependency. Standard library `concurrent.futures` is sufficient.

## Acceptance

- Recorded before/after numbers in the decisions entry, from a scripted
  benchmark against a fake connector with injected latency: brief wall clock,
  Google call count per brief, and wall clock for a 25-thread notification batch.
  Targets: **brief p50 under 3s**, 25-thread batch under 15s, Google calls per
  brief cut by more than half.
- A test asserting a concurrent batch with one failing item still enqueues
  exactly that item to the retry queue and advances the cursor once.
- A test asserting batch hydration falls back to per-thread reads on a client
  without batch support, with identical results.
- A test asserting the SQLite-backed stores satisfy the same `Protocol` tests the
  JSON stores do — run the existing store test suites against both.
- Cache hit rate and triage escalation rate visible in `attune metrics`.
- `docs/decisions.md` entry with the benchmark table and the explicit rejection
  of semantic caching on the generation path.
