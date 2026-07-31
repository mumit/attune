# 26 — The decision ledger and the north-star metric

**Phase P2** · `docs/plan-2026-h2.md` · **Depends on:** 25 · **Blocks:** 27, 29, 36

---

Read `CLAUDE.md`, the P2 section of `docs/plan-2026-h2.md`, and `audit/log.py`.

## Problem

Attune records *that* a human approved, edited, rejected, or ignored a proposal.
It does not record **what was in context when the proposal was made**, so no
learning mechanism can ever attribute a good or bad outcome to a specific
memory, prompt, or rule. And no metric is computed anywhere in the codebase —
there is no number that says whether the assistant is getting better.

The audit log is the right substrate but the wrong shape: it is an
effect-and-decision trail keyed by workflow thread, hash-chained for tamper
evidence, and deliberately content-light. What is needed alongside it is a
**per-proposal analytic row** that can be aggregated.

## Task

1. **A decision ledger.** One append-only row per proposal, written at propose
   time and completed at decision time. Same durability discipline as the other
   local stores (atomic replace, `fslock`, owner-only permissions) but put it in
   the **SQLite database that is already a dependency**, not another JSON file —
   this table gets aggregated, and whole-file JSON reads are already a
   measured problem. Fields:

   ```
   proposal_id, thread_id, domain, action, proposed_at
   autonomy_rung_granted, autonomy_rung_used, scope_matched
   model_id, prompt_version, playbook_commit          # prompt_version lands in 28
   context_attribution: {memory_ids[], playbook_bullet_ids[], skill_ids[]}
   triage_priority, base_priority, sender_importance_tier, profile_reason
   eligible_item_count                                # the coverage denominator
   decision, decided_at, actor_ref, time_to_decision_seconds
   edit_char_distance, edit_distance_normalized, edit_semantic_similarity
   edit_sections_changed[]   # greeting|body|closing|subject|recipients|tone
   applied_ok, apply_skip_reason, undone, undone_at
   ```

2. **`context_attribution` is the point of this prompt.** Thread the ids of
   every retrieved memory record out of the `retrieve` node and into the ledger
   row. Without it, prompts 29 and 36 cannot function — ACE-style per-bullet
   utility accounting, and GEPA's reflection over trajectories, both require
   knowing what was in context. If you build nothing else here, build this.

3. **Edit measurement.** `signals.capture_correction` already computes a diff;
   promote that into structured metrics: raw character distance, normalized
   distance (0.0 = accepted verbatim, 1.0 = fully rewritten), and which sections
   changed. Section classification must be deterministic (line-position and
   header heuristics over the draft structure), **not** a model call — this
   number is a metric and must not itself drift with a model.

4. **`undone` is a first-class column.** Undo does not exist yet (prompt 31
   builds it), but the column and the aggregation land now, because undo is the
   cleanest strong negative signal the product will ever have and it should
   demote a grant when it appears.

5. **`attune metrics`** — a CLI command reporting the north star over a
   configurable window (default 14 days):

   - **`edit_burden`** = `mean(edit_distance_normalized)` over sent proposals
   - **`clean_approval_rate`** = approved-without-edit ÷ decided
   - **`p50_time_to_decision`**
   - **`coverage`** = proposals ÷ eligible items — **mandatory, always rendered
     beside edit burden, never separable**
   - **`undo_rate`**, **`escalation_rate`** (proposals that reached a human when
     a grant would have allowed auto-apply)

   Slice by domain and sender importance tier. Render a plain table; no charts.

6. **Wire the guardrail into graduation.** `grants.suggest_graduations` today
   requires ≥10 decisions, 0 rejections, ≥95% unedited. Add a coverage floor:
   a track record accumulated while coverage was falling is not evidence of
   competence, it is evidence of selection. Also fix the standing bug that
   **scoped grants can never be earned** — `suggest_graduations` calls
   `max_rung(action, domain)` with no scope context, and fail-closed matching
   means a scoped grant never participates. Key track records by
   `(action, domain, priority, tier)` using the `scope_context` the gate already
   records in its `autonomy_gate` audit event.

## Constraints

- The ledger is **analytic state, not the trust root.** The hash-chained audit
  log remains authoritative for authority decisions; a ledger row is derived and
  may be rebuilt. Never let a ledger write failure break a decision path the
  human is waiting on (same best-effort posture as the existing dual-writes).
- Content classification: `edit_sections_changed` and the counters are
  content-free; the diff text itself already lives in memory under
  `frame_memory_text`'s provenance rules and must not be duplicated here.
- Hosted must get the same schema. Follow the `hosted/intelligence.py` pattern:
  shared dataclasses and aggregation functions, per-plane storage. Do not write
  a second, differently-shaped hosted ledger.

## Acceptance

- A test that runs one proposal end to end and asserts the ledger row carries
  the exact memory ids the `retrieve` node used.
- `attune metrics` output over a seeded ledger, asserted exactly — including
  that coverage is present in every rendering.
- A test proving a scoped grant now accumulates a track record and can be
  suggested for graduation.
- A test proving graduation is **withheld** when coverage fell over the window
  even though every other threshold passed.
- `docs/decisions.md` entry recording the north-star choice, the coverage
  guardrail, and the reasoning: an assistant optimizing approve-rate without a
  coverage denominator learns to propose only trivial work and stay silent on
  hard work.
