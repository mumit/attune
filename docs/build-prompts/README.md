# Build prompts

**Prompts 01–23 are a completed archive.** They are historical implementation
artifacts describing the repository state and test baseline that existed when
each was run; do not use their setup instructions as current deployment
documentation. Use the root `README.md`, `.env.example`, and
`docs/deployment.md` for the maintained operator path.

**Prompts 24–36 are open work**, implementing
[`docs/plan-2026-h2.md`](../plan-2026-h2.md). Their evidence base is the
[2026-07-31 landscape review](../landscape-2026.md); the earlier product review
is [`current-state.md`](../current-state.md) and its
[gap analysis](../gap-analysis.md).

Baseline at the time 24–36 were written (2026-07-31): **1,921 passing, 8
failing, 56 skipped**, plus the standalone republisher suite. The 8 failures are
prompt 24's first task — the suite is not hermetic and `main` is red. Any prompt
run before 24 lands should record the failure count first so it can prove it did
not add to it.

Self-contained prompts, one per work item, written to be run with Claude Code
(Sonnet) from the repo root:

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
| 17 | `17-principal-allowlist.md` | M6 Stabilization | — |
| 18 | `18-email-safety.md` | M6 | — |
| 19 | `19-live-policy-rungs.md` | M6 | — |
| 20 | `20-resume-audit.md` | M6 | 17 |
| 21 | `21-freshness-idempotency.md` | M6 | — |
| 22 | `22-verified-consolidation.md` | M6 | — |
| 23 | `23-calendar-bootstrap.md` | M6 | — |

Open work — [`docs/plan-2026-h2.md`](../plan-2026-h2.md). P0→P1→P2 are strictly
serial; P3 may run alongside P2; 30/31 and 32 are independent of each other; do
not start 35 before 30 lands, because the capability registry is the seam it
converges onto.

| # | File | Phase | Depends on |
|---|---|---|---|
| 24 | `24-repair.md` | P0 Repair | — |
| 25 | `25-reconnect-learning.md` | P1 Reconnect the loop | 24 |
| 26 | `26-decision-ledger.md` | P2 Measure | 25 |
| 27 | `27-eval-harness.md` | P2 | 26 |
| 28 | `28-model-layer.md` | P3 Model layer floor | 24 |
| 29 | `29-playbook.md` | P4 Compound | 26, 27, 28 |
| 30 | `30-capability-registry.md` | P5 Act | 28 |
| 31 | `31-reversibility.md` | P5 | 26, 30 |
| 32 | `32-attention-budget.md` | P6 Attention budget | 26 |
| 33 | `33-performance.md` | P7 Perform | 28 |
| 34 | `34-mcp-server.md` | P8 Interoperate | 30, 31 |
| 35 | `35-converge-planes.md` | P9 Converge | 30 |
| 36 | `36-prompt-optimization.md` | P10 Optimize | 26, 27, 28, 29 |

House rules every prompt inherits (do not skip):

1. Read `CLAUDE.md` and `docs/decisions.md` before changing anything; the
   non-negotiable rules are inviolable.
2. `pytest` must pass before you start and after you finish, with
   new offline tests (injected fakes, no credentials/network) for everything
   you add — match the style in `tests/`. Until prompt 24 lands the suite is
   red: record the exact failure count first and prove you did not add to it.
3. Keep collaborators injected and optional heavy deps lazy-imported. Inject
   clocks — a bare `datetime.now()` inside a retention window is what made the
   suite non-hermetic (prompt 24, task 1).
4. Finish by appending a `docs/decisions.md` entry (newest first, existing
   format) recording what was settled and why. If your change moves anything
   `CLAUDE.md` or `docs/design.md` asserts, update those too.

Three additional rules for 24–36, from `docs/plan-2026-h2.md`:

5. **No new capability without a signal.** Every action must emit a
   decision-ledger row with `context_attribution`, or it is invisible to
   learning and must not be built.
6. **No learning mechanism without a metric and a guardrail.** Edit burden is
   the north star; coverage is the mandatory paired denominator.
7. **Build once.** Nothing new is implemented separately for the local and
   hosted planes. Where a shared seam does not exist, create it in the same
   change — `hosted/intelligence.py` is the pattern that works.
