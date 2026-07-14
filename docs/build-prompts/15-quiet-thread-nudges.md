# 15 — Quiet-thread nudges: proactive follow-up offers

**Milestone:** M5 · **Depends on:** 05 (scheduler), 07 (`find_quiet_threads`)

---

Read `CLAUDE.md`, `docs/decisions.md`, design §3.3 ("quiet-thread nudge"),
and `docs/roadmap.md` §1. Run `pytest` before and after.

## Problem

Design §3.3's fourth interaction pattern — "you haven't heard back from
Marcus in 4 days on the contract redline, want a follow-up drafted?" — is
unbuilt. Prompt 07 surfaces quiet threads passively inside the brief; this
prompt makes them actionable: a proactive nudge whose "yes" flows into the
existing draft-approve machinery. It's the first genuinely *proactive*
action offer in the system, so it must enter the autonomy ladder correctly.

## Task

1. New module `src/attune/orchestrator/followup.py`:
   - `find_nudge_candidates(connector, nudge_state, *, now, min_age_days)` —
     wraps prompt 07's `find_quiet_threads`, then filters through a
     `JsonNudgeState(path)` (the usual pattern) recording already-nudged
     thread ids + nudge time, so a thread is nudged at most once per
     `ATTUNE_NUDGE_COOLDOWN_DAYS` (default 7). Settings additions follow the
     existing conventions (`ATTUNE_NUDGE_MIN_AGE_DAYS` default 4, state path
     data-dir-derived).
2. **Nudge = an approval card for a follow-up draft.** For each candidate
   (cap 3/day), start the existing draft-approve graph with
   `action="draft_reply"`, `domain="mail"`, `source_ref=<gmail thread id>`,
   and an `incoming_summary` built from the quiet thread (UNTRUSTED-framed,
   as ever) plus an explicit instruction context: "the user sent the last
   message N days ago; draft a brief, polite follow-up." The normal gate →
   interrupt → card flow then does everything else — approval materializes
   a Gmail draft via prompt 01's apply node, edits feed correction capture,
   ignored cards decay via prompt 03. **No new approval surface, no new
   autonomy path.** Register cards in the pending-approvals registry like
   any other.
3. **Scheduler job**: daily at `ATTUNE_NUDGE_TIME` (default "14:00" local —
   deliberately not brief time; the brief already lists quiet threads, the
   nudge is the afternoon "want me to act?" follow-through). Audit each
   nudge under a `"followup"` workflow (thread id, age, resulting lg_tid).
4. The card should read as a nudge, not a reply-draft-out-of-nowhere: pass
   a `title`/context line through to `post_approval` (extend
   `blocks.py`/`gchat_cards.py` builders with an optional header line —
   pure-function change, both channels).

## Constraints

- **Rule 3:** the nudge *offers*; only the existing human-approval interrupt
  can turn it into a Gmail draft. `FOLLOW_UP` exists in the `Action` enum —
  decide (and record in the decisions entry) whether the graph invocation
  uses `Action.FOLLOW_UP` with a `PROPOSE` grant added to `default_matrix()`,
  or reuses `DRAFT_REPLY`; the former is more honest to the matrix's
  action-type granularity.
- **Rule 2:** quiet-thread content is FETCHED/untrusted in the drafting
  prompt, same as any inbound mail.
- Nudge caps and cooldowns are hard limits — a proactive feature that spams
  is worse than none (design §8's Lindy critique).

## Acceptance

- Offline tests: candidate filtering (age, authorship, cooldown, daily cap)
  with injected clock/state; graph invoked with the expected state incl.
  `source_ref`; card header renders in both builders; audit + pending
  registration; cooldown survives restart (JSON round-trip through the
  consuming path).
- `docs/decisions.md` entry (FOLLOW_UP action decision, caps, why nudges
  reuse draft-approve wholesale) + CLAUDE.md module map.
