# 32 — Attention budget and user-authored routines

**Phase P6** · `docs/plan-2026-h2.md` · **Depends on:** 26 · **Blocks:** nothing

---

Read `CLAUDE.md`, the P6 section of `docs/plan-2026-h2.md`,
`docs/landscape-2026.md` §1, `brief.py`, and `scheduler.py`.

## Problem

Attune's flagship proactive surface is a fixed-cadence daily brief. That is the
product OpenAI shipped as **Pulse**, measured for nine months at its highest
price tier, and **retired on 2026-06-17** — replacing it with user-authored
Scheduled Tasks, stating that proactive experiences work when they are
*"personalised, action-oriented, and steerable by the user"* and that engagement
concentrated in **tasks** rather than in the brief. Google shipped **Scheduled
Actions** with "daily calendar summaries" as the worked example; Anthropic
shipped scheduled tasks inside Cowork. Three of three converged away from
ambient push.

The quantified failure mode: users tolerate roughly **three to five unsolicited
AI updates per day across all sources combined**, then mute, then uninstall.
Attune's proactive volume is capped per-feature in **arrival order** —
`MAX_NUDGES_PER_RUN=3`, `MAX_HOLD_OFFERS_PER_RUN=3`,
`MAX_LABEL_PROPOSALS_PER_RUN=3`, `MAX_DECLINE_PROPOSALS_PER_RUN=2`, plus a
weekly autonomy digest and the brief itself. On a busy day that is up to a dozen
unsolicited messages, allocated by whichever item happened to arrive first.

And the scheduler that drives all of it is not durable. `Scheduler._next` is an
in-memory dict; a job seen for the first time is only *scheduled*, never fired;
there is no catch-up for missed runs. A process that restarts before brief time
silently skips that day's brief, and nothing reports that it did.

## Task

1. **Routines: proactivity the principal authors.**
   `attune routine add --at "weekday 08:00" --say "unresolved threads from
   HIGH-tier senders, plus today's conflicts"`, with `list`, `show`, `remove`,
   and `run <name>` for a one-off preview. A routine is a stored, named,
   scheduled request expressed in the **existing bounded planner vocabulary** —
   not free-form instructions, and emphatically not a tool loop. Reuse
   `interaction.py`'s intent parsing so a routine can only ever request what a
   principal could already ask for in a DM.

   Ship the current daily brief as one **default routine** created at init, so
   existing behaviour is preserved and is now visible, editable, and removable.
   The brief stops being the architecture and becomes a default.

2. **A global attention budget.** One setting — `ATTUNE_DAILY_ATTENTION_BUDGET`,
   default 5 — bounding **total unsolicited messages per day across every
   proactive feature**. Replace the per-feature `MAX_*_PER_RUN` arrival-order
   caps with one allocator that ranks all candidate interruptions together and
   spends the budget on the highest-ranked. Rank on the signals already
   computed: URGENT priority, importance tier, `mentions_principal`, correlation
   group size, and staleness. Reuse `brief._best_tier_rank` and
   `orchestrator/correlation.py` rather than inventing a third ranking.

3. **URGENT bypasses the budget; nothing else does.** An urgent item must still
   interrupt — that is already the structural rule in
   `autonomy.PermissionMatrix.max_rung`, and the budget must not become a way to
   suppress it. Record every suppressed candidate in the decision ledger as
   `suppressed_by_budget` so the coverage metric from prompt 26 stays honest: a
   budget that silently drops work would otherwise look like an improvement in
   edit burden.

4. **A durable scheduler.** Persist the job ledger (next-run per job, last
   outcome, last error) in the SQLite database. On startup, a job whose next-run
   has passed **fires once with catch-up semantics** — for a brief, that means
   "post today's brief if today's hasn't been posted", not "post every brief you
   missed while down". Distinguish those two cases per job with an explicit
   `catch_up` policy on `Job`. Surface `last_error` and last-success time in
   `attune status` and as a Doctor check; a silently dead recurring job is
   currently invisible.

5. **Make the brief incremental.** `assemble_brief` already writes a
   `BriefSnapshot` and computes a since-yesterday diff. Use the snapshot as the
   read baseline too, so the daily run fetches deltas rather than re-reading 25
   unread threads, 20 sent threads, and 8 events from scratch. This is also
   prompt 33's biggest single win, so coordinate if both are in flight.

## Constraints

- A routine cannot express anything a DM to Attune cannot already express. No
  new capability arrives through this door, and a routine is never a grant.
- The budget bounds **unsolicited** messages only. Replies to the principal's
  own DMs, and approval cards for actions the principal already asked for, are
  not interruptions and are not budgeted.
- Suppression must be observable. A suppressed candidate is logged and countable,
  never silently dropped.
- Routine definitions are principal-authored configuration and may contain
  arbitrary text: treat them as **trusted-by-the-principal but bounded** — parse
  through the existing planner, never interpolate raw routine text into a
  privileged prompt position.
- Keep the scheduler hand-rolled. Its docstring's reasoning still holds: a few
  jobs on fixed cadences with an injected clock is fully deterministic under
  test, and a dependency is not warranted. Durability is a storage change, not a
  library change.

## Acceptance

- A test creating three routines, asserting each fires at the right time under an
  injected clock, and that a routine requesting something outside the planner
  vocabulary is refused at creation time with a clear error.
- A test asserting the default brief routine exists after init and that removing
  it stops the brief.
- A test asserting a budget of 3 with 10 candidates delivers the 3
  highest-ranked, records 7 `suppressed_by_budget` ledger rows, and that an
  URGENT eleventh candidate is still delivered.
- A test asserting a restart after a missed brief window posts exactly one brief,
  and a restart after three missed windows also posts exactly one.
- A test asserting a job that raised on its last run reports through
  `attune status` and Doctor.
- `docs/decisions.md` entry recording the routine model, the budget default and
  its basis, the URGENT bypass, and the catch-up semantics per job.
