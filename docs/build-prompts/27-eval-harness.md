# 27 — Eval harness: pairwise-against-sent, trajectory assertions, CI gate

**Phase P2** · `docs/plan-2026-h2.md` · **Depends on:** 26 · **Blocks:** 29, 36

---

Read `CLAUDE.md`, the P2 section of `docs/plan-2026-h2.md`,
`tests/test_memory_quality.py`, and `.github/workflows/memory-eval.yml`.

## Problem

Attune has 1,900+ tests and **no evals**. `test_memory_quality.py` computes no
metric — no precision, recall, nDCG, threshold, or baseline; it makes substring
assertions against a `FakeMemory` whose search is lowercase token overlap. There
is no test anywhere that answers "is the drafting getting better", "is triage
accurate", or "did that prompt change help".

There is also no external benchmark to lean on. There is no maintained
leaderboard for email triage, prioritization, or reply-draft quality anywhere in
the industry — so an internal suite is the only possible evidence, and
therefore a differentiator rather than table stakes.

## Task

1. **A golden set that grows from real decisions.** Every edited or rejected
   proposal in the decision ledger becomes a regression case: the inputs (thread
   metadata, retrieved context ids, prompt version) plus **the human's actual
   sent text as gold**. Store cases as files under a `evals/cases/` directory,
   redacted and reviewable, opt-in via an explicit `attune eval capture` step —
   never automatically harvesting a principal's mail into a checked-in fixture.

2. **Pairwise judging only.** Compare a candidate draft against what the human
   actually sent and ask which a reader would prefer. **Never** an absolute 1–5
   score: the 2026 judge literature ("reliability without validity" — judges are
   internally consistent but show low inter-judge agreement and systematic
   length/style bias; position bias is *worst* on close calls, which is exactly
   where you need the judge) makes absolute Likert judging indefensible as a
   release gate. Randomize position on every comparison and report the
   position-swap disagreement rate.

3. **Publish the judge's agreement rate, per domain.** Provide
   `attune eval label` to hand-label a sample (target 150–200 pairs), compute
   judge–human agreement, and write it into the eval report. **Agreement below
   75% in a domain means that domain's judge result is not a gate** — enforce
   this in code, not in a comment.

4. **Trajectory-level assertions, not just output.** Agents fail at the step
   level — wrong tool, wrong arguments, state not propagated, goal drift.
   Assert per case: the right capability was selected; the freshness check ran
   before apply; the autonomy rung was respected; no write occurred on a
   read-only route; the retrieved context met the score floor.

5. **A triage regression set with labels.** ~50 hand-labelled threads
   (URGENT/ROUTINE/NOISE) plus the deterministic importance adjustment's
   expected effect. Report accuracy, per-class confusion, and — separately —
   **whether the learned adjustment moved the answer in the right direction**.
   That last number is the one thing that proves "learns what's important".

6. **An injection-resistance suite.** Attune's controls here are genuinely good
   (`trusted_context` out-of-band provider facts, `[UNTRUSTED mail]` fencing,
   `frame_memory_text` provenance weighting, no write path on the source-message
   route) and completely unmeasured. Build a corpus of adversarial bodies:
   forged `mentions_principal` lines, instructions to approve or send, attempts
   to write a memory or a playbook bullet, attempts to escalate an autonomy
   rung, zero-click exfiltration patterns via markdown links and images.
   **Report a success rate, not a pass/fail** — Anthropic publishes 23.6%
   without mitigations and 11.2% with them, and a number you can watch move is
   worth more than a green tick. Use adversarial simulated users **only here**;
   do not use simulated users for quality measurement (measured benevolence
   bias: 31 simulators vs 451 real participants found many create an "easy
   mode", and 22% of conversations had the simulator acting off-instruction).

7. **CI.** `pytest` stays the fast offline gate. Add an `evals` job that runs
   the offline-judgeable suite on every PR and posts per-scorer deltas against
   the base branch, and fails on a regression beyond a declared budget. The
   live-substrate suite stays scheduled (and now actually runs, per prompt 24).
   Traces go to a self-hostable sink — Langfuse is MIT-licensed and matches the
   "runtime holding credentials exposes no public port" posture; do not take a
   hard dependency on a hosted eval vendor for a product that holds a person's
   mail.

## Constraints

- Offline by default. The judge is an injected client like every other model
  collaborator; the whole suite must run against fakes with no network.
- No principal's real mail is ever committed. Case capture is explicit,
  redacting, and local.
- Do not chase LoCoMo. 6.4% of its answer key is wrong, its standard judge
  accepts 62.81% of deliberately wrong-but-on-topic answers, its conversations
  are 16–26K tokens, and a full-context baseline beats the memory systems on it.
  If you want an external checklist, use **LongMemEval-V2's** ability taxonomy —
  static recall, dynamic state tracking, workflow knowledge, environment
  gotchas, premise awareness — as headings for your own suite.

## Acceptance

- `attune eval run` produces a report with: edit-burden proxy, pairwise win
  rate vs. gold, triage accuracy and learned-adjustment direction, injection
  success rate, and the judge agreement rate per domain.
- A test proving a domain with sub-75% judge agreement is reported but **cannot
  fail the gate**.
- A test proving position-swapped judging is actually performed and disagreement
  is reported.
- The `evals` CI job runs on PR and demonstrably fails on a seeded regression.
- `docs/decisions.md` entry recording pairwise-only judging, the agreement
  threshold, and why simulated users are confined to the injection suite.
