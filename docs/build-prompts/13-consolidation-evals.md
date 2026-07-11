# 13 — Real consolidation pass + memory-quality regression set

**Milestone:** M4 · **Depends on:** 05 (cadence exists)

---

Read `CLAUDE.md`, `docs/decisions.md` (memory-layer entry), design §2.2/§2.4,
and `docs/roadmap.md` §1. Run `pytest` before and after.

## Problem

Design §2.2's third leg — consolidate — is a stub: `MemoryStore.consolidate`
returns an empty report, `Mem0Store.consolidate` barely more, and until
prompt 05 nothing even called it. Meanwhile raw action signals
(`infer=False`, deliberately verbatim so consolidation could reason over
ground truth) are accumulating with no pass ever promoting them into the
durable preferences they were stored to become. And §2.4's memory-quality
regression set — the "is it actually learning?" check — doesn't exist.

## Task

1. **Consolidation** in `Mem0Store.consolidate(user_id=…)` (keep the base
   no-op; this is substrate logic):
   - Gather recent raw action signals (`metadata.signal == "action"`) and
     existing preference memories.
   - One `Task.CONSOLIDATE` call (already routed to the strong model in
     `fuelix.py` — correctness compounds, per design §4.5) with a structured
     prompt asking for: (a) repeated patterns worth promoting to a durable
     preference ("3× rejected drafts to <sender>" → a stated preference),
     (b) near-duplicate memories to merge, (c) contradictions where the
     newer fact supersedes the older. Require strict JSON output; on parse
     failure, do nothing and report the failure — a botched consolidation
     that mangles memory is far worse than a skipped night.
   - Apply conservatively: promotions/merges via `add` + `delete` of the
     absorbed items; supersession = add the new fact with
     `metadata.supersedes=<old_id>` and delete the old (Mem0 has no
     validity windows — record in the report that true bi-temporal
     supersession is the Graphiti migration's job, design Phase 4).
   - Fill `ConsolidationReport` honestly (merged/superseded counts, notes)
     and let the scheduler's existing job audit it.
   - Cap work per run (e.g., 200 signals) so a backlog can't produce a
     mega-prompt.
2. **Memory eval set** — `packages/aidedecamp/tests/test_memory_quality.py`
   plus a small YAML/JSON scenario file, LoCoMo/LongMemEval-style categories
   (design §2.4): single-session recall, multi-session recall, preference
   recall, and **knowledge update** (fact stated, later contradicted —
   post-consolidation retrieval must surface the newer fact). Runs offline
   against a scripted fake store/LLM by default (regression-checks the
   *pipeline logic*: what gets written, what consolidation decides given a
   canned model response, what retrieval is asked). Mark a live variant
   (real Mem0 + Fuel iX) with a skip-unless-env marker
   (`ADC_LIVE_MEMORY_EVAL=1`) — same suite, real substrate, run manually
   after memory-pipeline changes.
3. Document in the module docstring *when* to extend the set: any change to
   `memory/`, `signals.py`, or this consolidation prompt requires a run and,
   for new behavior, a new scenario (per design §2.4).

## Constraints

- **Rule 2:** consolidated inputs originated partly from untrusted content;
  the consolidation prompt frames all reviewed memory text as data, not
  instructions.
- Deletions only for items the model explicitly marked absorbed/superseded
  and that the code re-verifies exist; never delete on ambiguity.
- Everything through the `MemoryStore` interface + injected client; no live
  calls in the default test path.

## Acceptance

- Offline tests: promotion/merge/supersession application given canned
  model JSON; malformed JSON → no mutations + failure note; work cap; the
  four eval categories green against the fake substrate.
- `docs/decisions.md` entry (conservative-apply policy, what's deferred to
  Graphiti, eval-set discipline) + CLAUDE.md updates.
