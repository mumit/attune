# 05 — Scheduler: brief, renewals, sweeps, consolidation cadence

**Milestone:** M2 · **Depends on:** 03 recommended ·
**Fixes roadmap defect #5 (deployment blocker)**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1. Run `pytest`
before and after.

## Problem

`Runtime.run()` starts pull loops and blocks — and that's all. Nothing ever
calls `renew_gmail_watch` / `renew_chat_subscription` / `renew_calendar_watch`
(Gmail watches lapse silently within 7 days, so ingestion dies within a week
of any deployment); nothing posts the morning brief (the Phase-0
deliverable!); nothing runs `store.consolidate()`; and prompt 03's
`sweep_ignored` has no caller. The docstrings say "called on a daily
schedule" — no such schedule exists.

## Task

1. New module `src/attune/scheduler.py`. Do **not**
   add APScheduler — a small in-process scheduler is enough and keeps the
   dependency set flat: a `Job(name, next_run_fn, action)` list where
   `next_run_fn(now) -> datetime` computes the next firing (daily-at-time
   and every-N-hours helpers cover all current needs), and a
   `Scheduler.run_pending(now=None)` that fires due jobs, reschedules, and
   isolates failures (one failing job logs and never blocks the others).
   Fully deterministic under an injected clock; the only threaded part is a
   thin `run_loop()` (`pragma: no cover`, matching the pull-loop precedent)
   that ticks `run_pending` every ~30s.
2. Standard job set, assembled in `runtime.py`:
   - **Daily brief** at `ATTUNE_BRIEF_TIME` (local time, default `"07:30"`, in
     `ATTUNE_TIMEZONE` — an IANA name, default UTC; prompt 07 reuses this
     setting) posted via the existing `post_brief`/`post_text` surfaces to
     every configured channel.
   - **Watch renewals** daily: all three `renew_*` methods, each wrapped so
     one failure doesn't skip the rest, each recording an audit event under
     an `"ops"` workflow (`watch_renewed` / `renewal_failed`) — renewals are
     exactly the silent-failure class the audit log exists for.
   - **Pending sweep** (prompt 03's `sweep_ignored`) every 6h, if the
     registry is configured.
   - **Consolidation** nightly at `ATTUNE_CONSOLIDATE_TIME` (default `"02:00"`)
     calling `store.consolidate(user_id=…)` and auditing the report — the
     base impl is currently a no-op report; prompt 13 makes it real, this
     prompt gives it its cadence.
3. `Runtime.run()` starts the scheduler loop as one more daemon thread, and
   also calls the three `renew_*` once at startup (bootstrap: a fresh
   deployment must not wait a day for its first watch registration).
4. New Settings fields per above, with env names following the existing
   `ATTUNE_*` convention.

## Constraints

- Jobs run in the main process, outbound-only — no new ports (rule 5), no
  new always-on cloud dependencies.
- Scheduler must be constructible with an empty job list and injected jobs
  for tests; `runtime.py` wiring stays override-or-build-real.

## Acceptance

- Offline tests: daily-at-time and every-N-hours next-run math across day
  boundaries and timezones; `run_pending` fires exactly-due jobs once and
  reschedules; a raising job doesn't prevent sibling jobs; runtime assembles
  the expected job set from settings (assert names/cadences); startup calls
  renewals once.
- `docs/decisions.md` entry (why hand-rolled over APScheduler, the job set,
  the startup-renewal decision) + CLAUDE.md module map/next-steps update.
