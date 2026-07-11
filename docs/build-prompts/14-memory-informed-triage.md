# 14 — Memory-informed triage

**Milestone:** M4 · **Depends on:** none ·
**Fixes roadmap defect #12 (the known triage fast-follow)**

---

Read `CLAUDE.md`, `docs/decisions.md` (triage entry — its "deliberately
narrow v1" paragraph is the contract this prompt fulfills), and
`docs/roadmap.md` §1. Run `pytest` before and after.

## Problem

Design §1.2 lists "your past reactions" as a first-class triage signal;
v1 triage (`orchestrator/triage.py`) deliberately classifies from thread
content alone. Now that ignored-signal capture (prompt 03) and consolidation
(prompt 13) are producing per-sender/per-topic reaction history, triage is
leaving the system's own best signal unused — the assistant keeps drafting
replies to senders whose drafts the user has ignored or rejected every time.

## Task

1. `triage_thread` gains an optional `store: MemoryStore | None = None` and
   a `sender: str | None = None`. When both are present, run one narrow
   search (`store.search(f"reactions to mail from {sender}", …, limit=3)`)
   and append a compact `PAST REACTIONS:` block to the classification
   prompt. Absent either, behavior is byte-identical to today — the
   dispatcher's existing default path must not need a store.
2. Prompt discipline: past-reaction lines are *the user's own captured
   behavior* (trusted, from memory), but the thread content stays in its
   UNTRUSTED frame; keep the two visually separate in the prompt and keep
   the same two-line `PRIORITY:`/`REASON:` output contract so parsing is
   untouched.
3. `dispatcher.handle_gmail_notification` passes `app_ctx.store` and the
   thread's `from_addr` through to the default triage call. The audit
   record for a NOISE skip already includes the reason — ensure the reason
   the model gives can reflect the reaction history ("sender's drafts
   ignored 4× this month"), which it will if the prompt asks for it.
4. Add one memory-eval scenario (prompt 13's set, if landed): repeated
   REJECTED signals for a sender → triage prompt contains the reaction
   block → a canned NOISE response is honored and audited.

## Constraints

- Parsing-failure default stays **ROUTINE** (decisions.md is explicit about
  why — a dropped real email is worse than a spare draft). Memory input
  must not change that failure default.
- Still one cheap `Task.CLASSIFY` call; the memory search adds retrieval,
  not a second model call. Still a pure go/no-go gate — no writes (rule 3).
- `min_score` or result-count limits keep irrelevant memories from
  polluting a cheap model's prompt — three short lines max.

## Acceptance

- Offline tests: with a fake store, reaction lines appear in the captured
  prompt and thread content stays untrusted-framed; without a store,
  identical prompt to today (regression-pin it); dispatcher passes
  sender+store; ROUTINE-on-parse-failure unchanged with memory present.
- `docs/decisions.md` entry closing the "fast-follow, not done" flag from
  the original triage entry + CLAUDE.md touch-up.
