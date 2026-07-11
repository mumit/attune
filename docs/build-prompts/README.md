# Build prompts

Self-contained prompts for implementing `docs/roadmap.md`, one per work item,
written to be run with Claude Code (Sonnet) from the repo root:

```bash
claude --model sonnet "$(cat docs/build-prompts/01-apply-node.md)"
```

Each prompt assumes `CLAUDE.md` is auto-loaded (it is, in Claude Code) and
restates the non-negotiable rules it brushes against. Run them in order within
a milestone; cross-milestone dependencies are noted at the top of each file.

| # | File | Milestone | Depends on |
|---|---|---|---|
| 01 | `01-apply-node.md` | M1 Close the loop | — |
| 02 | `02-edit-flow.md` | M1 | 01 |
| 03 | `03-pending-approvals.md` | M1 | 01 (sweep cadence wired in 05) |
| 04 | `04-conversation-context.md` | M1 | — |
| 05 | `05-scheduler.md` | M2 Runs itself | 03 recommended |
| 06 | `06-loop-supervision.md` | M2 | — |
| 07 | `07-brief-v2.md` | M2 | — |
| 08 | `08-cli.md` | M3 Easy setup | 05 |
| 09 | `09-polling-mode.md` | M3 | — |
| 10 | `10-compose-quickstart.md` | M3 | 08, 09 |
| 11 | `11-memory-commands.md` | M4 Visible learning | 08 (CLI subcommands) |
| 12 | `12-autonomy-graduation.md` | M4 | 08, 11 |
| 13 | `13-consolidation-evals.md` | M4 | 05 |
| 14 | `14-memory-informed-triage.md` | M4 | — |
| 15 | `15-quiet-thread-nudges.md` | M5 Proactive | 05 |
| 16 | `16-calendar-actions-design.md` | M5 | 12 |

House rules every prompt inherits (do not skip):

1. Read `CLAUDE.md` and `docs/decisions.md` before changing anything; the six
   non-negotiable rules are inviolable.
2. `pytest` must pass before you start (312+ tests) and after you finish, with
   new offline tests (injected fakes, no credentials/network) for everything
   you add — match the style in `packages/aidedecamp/tests/`.
3. Keep collaborators injected and optional heavy deps lazy-imported.
4. Finish by appending a `docs/decisions.md` entry (newest first, existing
   format) recording what was settled and why, and update `CLAUDE.md`'s module
   map / next-steps if your change moves them.
