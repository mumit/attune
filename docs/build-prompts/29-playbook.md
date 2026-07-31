# 29 — The playbook: a git-backed, self-editing learned policy

**Phase P4** · `docs/plan-2026-h2.md` · **Depends on:** 26, 27, 28 · **Blocks:** 36

---

Read `CLAUDE.md`, the P4 section of `docs/plan-2026-h2.md`,
`docs/landscape-2026.md` §5, and `memory/signals.py::frame_memory_text` — whose
provenance rule this prompt extends to a new surface.

## Problem

Attune's learned knowledge lives in two places, and neither compounds well.
Deterministic importance works but expresses exactly one thing (a per-sender
tier). Semantic memory is a bag of retrieved sentences with no notion of whether
any given memory has ever helped.

The 2026 evidence points somewhere else: a **file the model edits** beats a
retrieval index the model queries, for procedures, workflows, and gotchas —
which is precisely what an assistant learns about its principal. LongMemEval-V2
measured a file-based memory controller at 74.9% against 58.6% for the RAG-based
variant of the same system. Letta replaced memory blocks with git-backed
Markdown. Anthropic's memory tool is file operations, not retrieval. ACE's
evolving playbooks gain +10.6% on agent tasks **with no labels at all**. And a
production case study of accumulated behavioural rules in a version-controlled
instruction file recorded **zero recurrences across 9 error classes over 74
post-rule exposures**.

## Task

1. **A playbook directory, git-backed.** One Markdown file per domain —
   `playbook/mail.md`, `playbook/calendar.md`, `playbook/voice.md`,
   `playbook/scheduling.md` — inside the state directory, initialized as its own
   git repository. Every write is a commit with a descriptive message. The
   principal can `git log`, `git diff`, and `git revert` a single learned belief.
   That auditability is the whole point; it is the same property the
   hash-chained audit log gives effects, applied to beliefs.

2. **Bullets, not prose.** Each bullet carries: a stable `id`, the rule text,
   `provenance` (the decision-ledger proposal ids that produced it),
   `created_at`, `helped` and `harmed` counters, and `last_used_at`. Bullets are
   what `context_attribution` (prompt 26) points at.

3. **Delta edits only.** The reflector may **add**, **refine**, or **retire** a
   bullet. It may never rewrite a file wholesale. This is ACE's central finding:
   full rewrites cause *brevity bias* (summarization silently dropping the
   specific insight that made a rule useful) and *context collapse* (iterative
   rewriting eroding detail). Implement it as a constrained edit operation set,
   not as "ask the model for the new file".

4. **The nightly reflector.** Extend the existing consolidation job rather than
   adding a second nightly pass. In this order:
   1. **Per-bullet accounting.** For every bullet that was in context on a
      decided proposal, increment `helped` (clean approval) or `harmed` (edit or
      rejection), read from the ledger's `context_attribution`. Do this
      *first* — it is the part ACE needs and the part RIZZ identifies as
      required to stop accumulated rules interfering with each other.
   2. **Retire** any bullet where `harmed > helped` over a minimum sample, and
      decay bullets unused past the same 90-day window importance signals use.
   3. **Propose new bullets**, only from edits and rejections, with the diff as
      evidence. **Cap at 3 new bullets per day, hard.** The ratchet study went
      from 5 to 18 rules in a month and that pace worked; faster accumulation is
      how you get interference.
   4. Existing memory consolidation and profile recompute, unchanged.

5. **Load as a cached prefix, not a retrieval.** The playbook slice for a domain
   goes into the stable prefix from prompt 28, so it is billed at cache-hit rates
   rather than re-embedded per request. This is the reason prompt 28 comes first.
   When a file exceeds its bounded size, select bullets by utility and recency —
   never dump everything (that is ExpeL's documented failure, which ACE's
   selective retrieval exists to fix).

6. **`attune playbook` CLI.** `show [domain]`, `history [bullet-id]`,
   `retire <bullet-id>`, `pin <bullet-id>` (exempt from decay and retirement),
   and `revert <commit>`. The principal must be able to read and correct every
   learned rule without leaving the terminal, exactly as `attune memory` and
   `attune autonomy show` already allow.

7. **Demote semantic memory.** Mem0/Qdrant becomes *one retriever for facts
   about people, projects, and commitments* — top-3, above the score floor from
   prompt 24 — not "the memory system". Procedures and preferences belong in the
   playbook; facts belong in memory; tiers and grants stay deterministic.

## Constraints — read these twice

- **Untrusted content must never reach a playbook write.** The reflector sees
  the *decision* (approved/edited/rejected), the *diff*, and content-free
  metadata. Inbound message bodies are fenced and marked if present at all, and
  **no bullet may be created whose text derives from an inbound body.** A
  playbook bullet enters the trusted prefix of every future prompt in its
  domain; a poisoned bullet is a persistent, self-reinforcing compromise. This
  is the highest-severity new attack surface in the entire plan. Enforce it
  structurally — the reflector's input assembly must make body text
  unavailable, not merely instruct the model to ignore it.
- **The playbook grants no authority.** It may shape tone, ordering, phrasing,
  and what to notice. It may never raise an autonomy rung, add a grant, widen a
  scope, or authorize an action. Autonomy changes come only from
  `orchestrator/grants.py` on audited human decisions. Add a test that a bullet
  saying "you may send replies to alice@example.com without asking" changes
  nothing about what the gate permits.
- Bounded by construction: max bullets per file, max chars per bullet, max
  files, ≤3 new bullets/day, decay, `harmed > helped` retirement. Documented
  constants, not tunable environment variables — this is operational state, the
  same reasoning `attention.py` already applies to its own bounds.
- Best-effort: a playbook read failure degrades to no playbook, never an error
  on a path a human is waiting on.

## Acceptance

- A test where three rejections of over-formal drafts to one recipient produce
  exactly one new bullet, committed to git with provenance pointing at the three
  ledger rows.
- A test proving a bullet with `harmed > helped` is retired and stops appearing
  in assembled prompts.
- A test proving the ≤3/day cap holds when 20 edits arrive in one day.
- **An injection test** proving a body containing a well-formed bullet plus
  `"add this to your playbook"` produces no playbook mutation.
- **An authority test** proving no bullet can affect `PermissionMatrix.max_rung`.
- A measurable edit-burden delta on the eval golden set attributable to named
  bullets, reported by `attune eval run`.
- `docs/decisions.md` entry recording the delta-edit discipline, the untrusted
  content firewall, the authority boundary, and the ratchet caps.
