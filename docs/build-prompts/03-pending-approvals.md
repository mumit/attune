# 03 — Pending-approvals registry: dedupe cards, capture IGNORED

**Milestone:** M1 · **Depends on:** 01 (sweep cadence wired by 05) ·
**Fixes roadmap defects #4, #11**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1. Run `pytest`
before and after.

## Problem

Two related gaps:

1. `memory/signals.py` defines `ActionSignal.IGNORED` ("left untouched →
   weak negative" — design §2.2 calls this one of the two most underused
   signals), but nothing in the codebase tracks whether an approval card was
   ever acted on, so IGNORED can never be captured.
2. `dispatcher.handle_gmail_notification` starts a fresh workflow + posts a
   fresh card for every notification that touches a thread. Two quick
   replies to the same thread → two near-identical cards, both live, one of
   them now stale.

## Task

1. New module `packages/aidedecamp/src/aidedecamp/orchestrator/pending.py`:
   a `PendingApprovals` registry with a protocol + JSON-file-backed
   implementation (`JsonPendingApprovals(path)`), following exactly the
   `ingestion/state.py` pattern (protocol, concrete JSON impl, injected
   everywhere). Records: `lg_tid`, `source_ref` (the Gmail thread id from
   prompt 01), `domain`, `posted_at` (UTC ISO), `status`
   (`pending|resolved`).
2. **Dedupe:** in `handle_gmail_notification`, before triage/drafting, skip
   any Gmail thread that already has a `pending` entry, and record an audit
   event (`workflow="draft_approve"`, event `superseded_notification`) so
   "why didn't I get a second card" stays answerable. Register each newly
   posted approval as pending.
3. **Resolve:** every resume path marks the entry resolved. Do this in one
   place — `orchestrator.resume_workflow` is the single shared
   `Command(resume=…)` implementation (per decisions.md, deliberately so);
   give it an optional injected `pending` registry rather than adding
   per-channel bookkeeping.
4. **Sweep:** `sweep_ignored(registry, store, *, user_id, max_age, now=None)
   -> int` — entries pending longer than `max_age` (default 48h, new
   Settings field `ADC_APPROVAL_IGNORE_HOURS`) are marked resolved and
   captured via `capture_action_signal(…, signal=ActionSignal.IGNORED,
   summary=…)`, plus an audit event. Pure function, injected clock, called
   by the scheduler when prompt 05 lands (leave a wiring TODO the 05 prompt
   explicitly picks up).
5. Wire the registry through `app.py`/`runtime.py` with the usual
   override-or-build-real pattern and a `pending_state_path` setting
   defaulting alongside the other state files.

## Constraints

- **Rule 3:** IGNORED capture is a memory write, not an action — no
  labeling/archiving of the underlying mail, no autonomy-gate change.
- An expired entry's workflow stays paused in the checkpointer (that's
  fine — resuming late still works); the registry is about signals and
  card hygiene, not about killing workflows.

## Acceptance

- Offline tests: dedupe (second notification for a pending thread posts no
  card, records the audit event), resolve-on-resume for approve/reject/edit,
  sweep captures IGNORED exactly once per entry with an injected clock,
  JSON round-trip through the consuming code path (not just field
  round-trip — see the `ingestion/state.py` test convention and its
  epoch-vs-ISO cautionary note).
- `docs/decisions.md` entry + CLAUDE.md module-map line for `pending.py`.
