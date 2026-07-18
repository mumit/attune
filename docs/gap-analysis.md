# Gap analysis — product goal vs. current state (2026-07-18)

The product goal, in the principal's words: *a personal assistant that
attends to my email, calendar, chat, and Slack; learns what's important;
and suggests actions based on these sources — moving toward measured
autonomy like a real assistant.*

This document scores the current implementation (see
[current-state.md](current-state.md)) against each clause of that goal and
lists the concrete gaps. The [future-state plan](future-state.md) sequences
the remediation.

## Clause 1 — "attends to my email, calendar, chat, and Slack"

**Status: half met.** Gmail and Calendar are genuinely attended: ingestion
cursors, triage (Gmail only), briefs, drafts, conflict detection, and
follow-up nudges all consume them as signal sources.

Chat and Slack, however, are **conversation surfaces, not attended
sources**. They authenticate the principal and carry briefs, approvals, and
Q&A — but no message flowing through a Slack workspace or Chat space is
triaged, remembered as workload signal, correlated with email threads, or
surfaced in the brief. "Attend to my chat and Slack" is currently
unimplemented in both deployment modes.

Gaps:

- G1. No triage or importance pipeline for Chat/Slack content; only the
  principal's own DMs to Attune are processed, and only as commands.
- G2. No calendar triage either — every conflict is surfaced (capped at 3
  per run by arrival order), regardless of meeting importance.
- G3. No cross-source correlation: an urgent email and a same-topic Slack
  thread are never connected into one item.

## Clause 2 — "learns what's important"

**Status: minimal.** The machinery for learning exists (memory store,
signal capture, nightly consolidation) but importance itself is barely
learned and barely used:

- G4. Priority is computed once per Gmail thread (URGENT/ROUTINE/NOISE) and
  then discarded except as a NOISE gate. URGENT changes nothing downstream —
  not notification urgency, brief ordering, draft tone, or autonomy
  eligibility.
- G5. The only learned input to triage is a soft, unstructured memory
  search ("reactions to mail from {sender}") injected into the classify
  prompt. There is no per-sender or per-topic importance profile, no
  deterministic rule (e.g., "ignored 5 times → demote"), and no compounding
  score.
- G6. Real learning (pattern extraction into durable preferences) happens
  only in the nightly consolidation pass with a 3+-repeated-signal bar —
  meaning weeks of identical behavior before anything crystallizes, and
  silence if the pass no-ops. Day-to-day reads are raw-signal searches.
- G7. Retrieval quality against the real Mem0/Qdrant substrate has no CI
  coverage (live eval is manual-only), so learning regressions are
  invisible.
- G8. The hosted platform learns nothing at all: `attune.memories` and
  pgvector are schema-ready but no executor reads or writes them, and each
  hosted conversation is memoryless beyond its turn history.

## Clause 3 — "suggests actions based on these sources"

**Status: narrow.** Two suggestion features exist — follow-up nudges on
quiet sent-threads and same-day conflict hold offers — both riding the
draft-approve graph. That is the entire proactive surface beyond the brief.

- G9. `Action.DECLINE_INVITE`, `Action.RESCHEDULE`, and `Action.LABEL` are
  defined in the autonomy vocabulary with zero implementing code — the
  natural next suggestions (triage my inbox, handle this invite, reschedule
  around this conflict) are aspirational enum members.
- G10. Suggestion volume is capped by count-per-run in arrival order, not
  ranked by importance — on a busy day, which three threads get nudged is
  arbitrary.
- G11. The brief is static in structure: no ranking by learned importance,
  no "what changed since yesterday," no configurable sections.
- G12. Hosted suggests nothing: no briefs, no nudges, no holds — bounded
  read-only Q&A plus mutation refusal is the whole conversational product.

## Clause 4 — "moving toward measured autonomy like a real assistant"

**Status: scaffolded but frozen at the bottom rung.** The ladder
(observe → draft → act-with-notification → act), the per-(action, domain)
grants, the live-reloading gate, and the evidence-based graduation
suggestions are all real and well-tested. But:

- G13. Graduation never happens in-product. Suggestions are computed from
  the audit trail, yet only the CLI (`attune autonomy grant`) can accept
  one; chat is show-only, and there is no one-tap accept in the approval
  channel where the track record was earned.
- G14. Grants cannot be scoped by the very signals the system computes —
  there is no "auto-act on ROUTINE from known senders, always interrupt for
  URGENT." The gate reads only `(action, domain)`.
- G15. `SEND_REPLY` is a dead end: granting it succeeds with a warning while
  remaining structurally inert, which is safe but misleading.
- G16. The trust root for graduation — the local JSONL audit log — is not
  tamper-evident (security finding F1), which matters more as autonomy
  decisions increasingly rest on it.
- G17. Hosted autonomy is fixed at R0 read-only: the typed capability
  gateway that would admit higher tiers is implemented and tested but wired
  to nothing, and no write capability (including draft-and-approve) exists
  for hosted customers.

## Cross-cutting gaps

- G18. **Intelligence divergence.** The hosted path shares no
  triage/memory/brief/autonomy code with the local runtime; every
  improvement toward this goal must currently be built twice.
- G19. **Hosted is not sellable yet.** No production signup, no
  customer-content retention/deletion, incomplete export, no billing or
  quotas, no per-tenant model configuration, dev-sized scale ceilings, and
  job-failure-only monitoring.
- G20. **Setup friction gates adoption.** Self-hosted first value is
  dominated by manual Google Cloud Console ceremony plus a silent 7-day
  Testing-mode token expiry; hosted onboarding requires seven ceremonies
  with ten-minute recency windows.
- G21. **Local-runtime security soft edges** (findings F1–F9): tamperable
  audit, cross-process approval race, discipline-only log redaction,
  CWD state fallback, missing in-class Chat actor guard, unweighted
  correction-memory provenance, no local rate ceilings.

## Summary scorecard

| Goal clause | Verdict |
|---|---|
| Attends to email + calendar | Largely met (triage depth aside) |
| Attends to chat + Slack | Not met — surfaces only, not sources |
| Learns what's important | Minimal — computed once, barely used, slow to crystallize |
| Suggests actions | Narrow — two features, no ranking, three actions unimplemented |
| Measured autonomy | Scaffolded — ladder built, graduation manual, hosted frozen at R0 |
| Foundation (safety, durability, tenancy) | Strong — ahead of the product built on it |
