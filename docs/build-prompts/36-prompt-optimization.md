# 36 — Offline prompt optimization, and weights last

**Phase P10** · `docs/plan-2026-h2.md` · **Depends on:** 26, 27, 28, 29

---

Read `CLAUDE.md`, the P10 section of `docs/plan-2026-h2.md`, and
`docs/landscape-2026.md` §5. Do not start this prompt until the decision ledger
records `context_attribution`, the eval harness reports a judge agreement rate,
prompts are versioned, and the playbook has been accumulating for at least a
month. Every prerequisite is load-bearing: an optimizer without a trustworthy
metric optimizes the metric's flaws.

## Problem

Attune's prompts are hand-written and have never been optimized against
anything, because until now there was nothing to optimize against. Now there is:
a golden set generated from real edits and rejections, and a metric with a
guardrail.

The technique choice is settled by evidence. **GEPA** — reflective prompt
evolution that samples full trajectories, diagnoses failures in natural
language, and merges complementary lessons from a Pareto frontier of its own
attempts — beats GRPO by ~10% average and up to 20%, **with up to 35× fewer
rollouts**, matching GRPO's best validation score in 306–1,179 rollouts (up to
**78× sample efficiency**). One principal genuinely produces that much signal in
a month or two. Reinforcement learning on weights does not become available at
this data scale, and it does not need to.

## Task

1. **A weekly optimization job, run offline and never in the request path.**
   Optimize the `triage`, `draft`, and `brief` prompts from the registry (prompt
   28) against the golden set (prompt 27). Use a reflective optimizer over
   trajectories — GEPA, via DSPy if you take the dependency, or a direct
   implementation of the same loop: sample trajectories, reflect on failures in
   natural language, propose prompt edits, keep a Pareto frontier rather than a
   single best, merge complementary wins. Optimize the **prefix** only; the
   volatile suffix is data assembly and is not the optimizer's business.

2. **Choose the optimizer by the feedback you have.** Use a reflective optimizer
   where rich textual feedback exists — and user edits *are* textual feedback,
   which is the most valuable asset Attune owns. Use a scalar-metric optimizer
   (MIPROv2-style bootstrapped instruction search) only where the only signal is
   a number, e.g. triage accuracy against labels. Do **not** use blind
   evolutionary prompt search; it is dominated by reflective methods on every
   sample-efficiency comparison available.

3. **Promotion is a gated, reviewable change — never automatic.** A candidate
   prompt version is promoted only when the eval harness shows improvement on the
   north star **and** no regression beyond budget on: triage accuracy, coverage,
   injection success rate, and every trajectory assertion. A prompt version is a
   committed artifact with a version identifier already stamped into every audit
   event and ledger row, so any output can be traced to the prompt that produced
   it and any regression can be attributed and reverted. Land promotions as pull
   requests with the per-scorer delta table in the body.

4. **Refuse to optimize against an untrustworthy judge.** If judge–human
   agreement for a domain is below the 75% threshold prompt 27 enforces, that
   domain is excluded from optimization, not optimized against a bad signal.
   Enforce this in the job, not in a comment.

5. **Guard against metric collapse, explicitly.** The optimizer will find that
   the cheapest way to reduce edit burden is to propose less. Coverage is a hard
   constraint, not a reported number: a candidate that improves edit burden while
   reducing coverage is **rejected**. The RLUF study is the precedent —
   aggressively optimizing one implicit user signal won +28% on the target metric
   while making the model end conversations ("bye" in 2.8% of responses vs 0.72%
   baseline), dropping helpfulness 4–16%, and producing a reward model that
   penalized valid safety refusals. Attune's version of that failure is an
   assistant that drafts only the easy replies and stays silent on the hard ones.
   Add an explicit test that a coverage-reducing candidate cannot be promoted.

6. **Then, and only then, weights — for voice and speed only.** Two narrow uses,
   in this order:
   - **A style adapter** on accepted and sent drafts. Roughly $20 for a
     50M-token LoRA run on current per-token training services. This targets the
     one thing context engineering measurably fails at: 5-shot style imitation
     reaches ~96% authorship verification on **email** (the easy domain) but
     16–66% on informal text, and going from 2 to 10 exemplars adds little — so
     exemplar retrieval plateaus and a small adapter is the remaining lever.
   - **A distilled small model** for triage, trained on the strong model's
     *accepted* outputs, feeding prompt 33's cascade.

   For binary approve/reject data use **KTO, not DPO** — KTO takes unpaired
   binary labels, which is exactly the shape of an approval; DPO needs pairs.

## Do not

- **Do not pursue RL on weights** (GRPO/PPO). GEPA beats it at ~1/35th the data,
  and this product will never have GRPO-scale data from one person.
- **Do not pursue OpenAI RFT.** It is `o4-mini`-only, its fine-tuning platform is
  winding down, it bills by training wall-clock hour, and its stated
  preconditions — unambiguous tasks where experts agree, a non-zero baseline
  success rate, non-saturated evals — do not describe email drafting.
- **Do not chase LoCoMo or any public memory leaderboard.** 6.4% of LoCoMo's
  answer key is wrong, its standard judge accepts 62.81% of
  wrong-but-topically-adjacent answers, and a full-context baseline beats the
  memory systems on it. There is no neutral maintained memory leaderboard.
- **Do not put any optimizer in the request path.** This is offline batch work,
  the same posture as nightly consolidation.
- **Do not let the optimizer touch the playbook.** The playbook is learned from
  the principal's own decisions under the reflector's provenance and
  untrusted-content rules (prompt 29); an optimizer rewriting it would launder
  those rules away. Prompts and playbook are separate artifacts with separate
  authorship.

## Constraints

- Any optimizer dependency is dev/optional-extra only and lazy-imported. The
  runtime must not require it.
- The golden set never leaves the principal's control. If optimization calls a
  hosted model, it sends the same class of content the product already sends —
  and that must be stated in the decisions entry, not assumed.
- Prompt versions are immutable and auditable. A promoted prompt is a new
  version, never an edit to an existing one.

## Acceptance

- One completed optimization run recorded end to end: candidate versions,
  per-scorer deltas, the promotion decision, and the resulting prompt version id.
- A test asserting a candidate that improves edit burden while reducing coverage
  is rejected.
- A test asserting a domain below the judge-agreement threshold is excluded from
  optimization.
- A test asserting a promoted prompt version is traceable from a ledger row back
  to the exact prompt text, and that reverting the version reverts behaviour.
- `docs/decisions.md` entry recording the optimizer choice, the promotion gate,
  the coverage constraint and its RLUF rationale, and the explicit rejection of
  weight-based RL and RFT.
