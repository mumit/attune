# Future state — plan toward the product goal (2026-07-18)

Target: *a personal assistant that attends to email, calendar, chat, and
Slack; learns what's important; suggests actions from those sources; and
moves toward measured autonomy like a real assistant.*

This plan sequences the gaps in [gap-analysis.md](gap-analysis.md)
(G-numbers) and the security findings in
[current-state.md](current-state.md) (F-numbers). It complements
[roadmap.md](roadmap.md): the hosted assurance sequence recorded there is
unchanged; this document adds the product-intelligence dimension and states
where the two converge.

## Invariants that do not change

Every phase below stays inside the existing design principles: the model is
never a security principal; writes remain typed, audited capabilities;
autonomy is granted by the human and is always reversible; the assistant
never self-grants; untrusted content stays provenance-framed; one principal
per instance. "More autonomous" always means "earns and exercises narrow,
revocable authority faster," never "bypasses the ladder."

## Phase 1 — Make importance a first-class, learned signal

The single highest-leverage change: today priority is computed once and
discarded (G4–G6). This phase makes importance flow through the product.

1. **Propagate priority.** Carry `TriageResult.priority` through the
   dispatcher into notification urgency (URGENT interrupts immediately,
   ROUTINE batches), brief ordering, and workflow state so later phases can
   gate on it. (G4)
2. **Deterministic importance profile per sender/topic.** Alongside the
   soft memory hint, maintain an explicit, inspectable profile: approval /
   edit / rejection / ignore counts per sender and topic, decaying over
   time, with deterministic effects ("ignored 5 of last 5 → demote one
   level; always answered within an hour → promote"). Store it as product
   state, expose it via `attune memory`-style inspect/correct commands so
   the principal can see and override what Attune believes is important.
   (G5)
3. **Faster crystallization with a confidence gradient.** Keep the
   conservative nightly consolidation, but let the deterministic profile
   act immediately — learning the principal can see the same week, not
   after three identical nightly promotions. (G6)
4. **Triage calendar and (once ingested) chat.** Score meetings by
   attendee/organizer importance and conflict cost; stop surfacing the
   first three conflicts by arrival order. (G2, G10)
5. **CI evaluation for learning.** Promote the live memory-quality
   evaluation into a scheduled (not per-commit) CI job against a real
   Qdrant container, and add a regression test asserting that repeated
   ignore signals actually change a triage outcome. (G7)

Exit criteria: an URGENT email is visibly treated differently from a
ROUTINE one; the principal can ask "why did you rank this high?" and get an
answer grounded in the profile; ignoring a newsletter three times demotes
it without waiting weeks.

## Phase 2 — Attend to chat and Slack as sources, not just surfaces

Half the goal's source clause is unmet (G1, G3).

1. **Ingest selected Slack channels / Chat spaces as signal sources**,
   with explicit opt-in per channel (respecting the existing allowlist and
   route model). Messages flow through the same cursor → dispatcher →
   triage pipeline as Gmail, with chat-appropriate importance features
   (mentions of the principal, DMs from important senders, thread
   participation).
2. **Cross-source correlation.** Link items about the same topic/thread
   across Gmail, Calendar, Slack, and Chat (participants + subject/entity
   matching first; embedding similarity later) so the brief and
   notifications present one item, not four. (G3)
3. **A unified "what matters now" ranking** replaces per-source sections as
   the brief's spine: one importance-ordered list across all four sources,
   with the current sections as drill-downs. (G11)

Exit criteria: a Slack message from the principal's manager about a topic
that also has an unanswered email appears as one ranked item in the brief.

## Phase 3 — Widen the suggestion surface

With importance flowing (Phase 1) and all sources attended (Phase 2),
suggestions become both broader and better-ranked.

1. **Implement the three dormant actions** — `LABEL` (archive/label noise),
   `DECLINE_INVITE`, and `RESCHEDULE` — as real draft-approve capabilities
   with the same freshness and approval semantics as drafts and holds.
   Remove the enum-without-implementation state. (G9)
2. **Importance-ranked proactive caps.** Keep per-run caps, but rank
   candidates by the Phase-1 score instead of arrival order. (G10)
3. **Richer scheduling negotiation** (already in roadmap "Later"):
   propose specific resolutions with free-slot math, not just holds.
4. **Brief evolution:** "what changed since yesterday," waiting-on
   tracking, and suggested next actions inline with one-tap approval
   routing into the existing approval channel. (G11)

Exit criteria: on a normal day the assistant proposes at least: replies to
draft, a follow-up, an invite decision, and an inbox-hygiene action — each
ranked, each one approval away.

## Phase 4 — Measured autonomy that actually graduates

Prerequisites from the security review come first because autonomy
decisions rest on them:

1. **Harden the trust root** — hash-chain the local JSONL audit log
   (mirroring the hosted SQL construction) and add cross-process locking to
   the approval claim path. (F1, F2, G16)
2. **In-channel graduation acceptance.** Surface graduation suggestions as
   approval cards in the existing (allowlisted, single) approval channel
   with an explicit confirm step. This deliberately amends the recorded
   "CLI is the only grant surface" decision — record the change in
   `decisions.md` with its compensating controls (allowlisted actor,
   two-step confirm, audited, revocable, never self-initiated beyond a
   suggestion). The assistant still never self-grants. (G13)
3. **Signal-scoped grants.** Extend the gate beyond `(action, domain)` to
   optional scope predicates on the signals the system computes: priority
   band, sender profile tier, topic. "Auto-file NOISE newsletters" and
   "auto-draft ROUTINE replies from known senders, always interrupt for
   URGENT" become expressible grants. (G14)
4. **Make send real, behind the full stack of gates.** Wire
   `SEND_REPLY` end-to-end: `gmail.send` scope + `send_enabled` + explicit
   grant + freshness check + act-with-notification rung first. Until
   granted, `attune autonomy grant send_reply` should refuse loudly
   (non-zero exit) instead of warning. (G15)
5. **Demotion symmetry.** Rejections/edits after graduation automatically
   suggest (never apply) demotion, so the ladder is honestly bidirectional.

Exit criteria: after a demonstrable track record, the principal taps
"accept" on a graduation card, and Attune quietly files newsletter noise
and sends routine acknowledgments with notification — every effect audited
and reversible from chat or CLI.

## Phase 5 — Converge hosted onto the same intelligence

Today the hosted product is a secure shell around a memoryless read-only
Q&A (G8, G12, G17, G18). Convergence, in dependency order:

1. **Extract a shared intelligence core** — triage, importance profile,
   brief assembly, suggestion ranking — behind injected connector/memory/
   clock interfaces (the local runtime already injects these), so local
   LangGraph workflows and hosted executors consume one implementation.
2. **Wire hosted memory.** Connect the dormant `PostgresMemoryRepository`
   to the conversation executors and signal capture, with the tenant filter
   injected in the storage adapter per SEC-201 — designed before code, as
   the security review insists. (F7, G8)
3. **Wire the capability gateway into dispatch** (roadmap step 6's
   remaining half), then introduce the first hosted write capability —
   Gmail draft-and-approve at R1/R2 with the approval ceremony — reusing
   the reconciliation and audit spine that already exists. (G17)
4. **Hosted briefs and nudges** through the existing channel broker and
   delivery routes, honoring the already-built brief/interaction preference
   ceremony. (G12)

The hosted assurance gates in [roadmap.md](roadmap.md) (steps 8–10:
customer-visible audit, adversarial suites, pen test, launch gates) remain
the release authority; this phase gives them a product worth gating.

## Phase 6 — Productization and friction (parallel track)

Run alongside phases 1–5; none of it blocks intelligence work.

- **Self-hosted setup:** script the Google Cloud ceremony where possible
  and inline the remaining steps as a checklist in `attune init`; Doctor
  warns about External/Testing 7-day token expiry; a `--recommended`
  model-selection path; Slack app manifest generation. (G20)
- **Hosted onboarding:** production signup (replacing operator
  provisioning), collapse ceremonies where safety allows, a single step-up
  auth covering one onboarding session, OAuth-style Chat install instead of
  `/link CODE`, push/email fallback for the web panel. (G19, G20)
- **Hosted operations:** customer-content retention and deletion, export
  completion/download/UI, per-tenant model configuration and usage
  metering, SLO/latency monitoring, support/repair tooling, and real
  capacity planning past `max_instance_count = 3`. (G19)
- **Security hardening backlog:** log-redaction filter (F3), live
  republisher OIDC exercise (F4), fail-closed `ATTUNE_DATA_DIR` (F5),
  correction-memory provenance weighting plus the adversarial
  memory-poisoning test (F6), in-class Chat actor guard (F8), local rate
  ceilings (F9). (G21)

## Sequencing rationale

Phase 1 is first because every later phase consumes the importance signal:
ranking suggestions (3), scoping grants (4), and hosted briefs (5) are all
blocked on importance being real. Phase 2 can start in parallel once the
triage pipeline accepts a second source shape. Phase 4's security
prerequisites (F1, F2) are small and should land early even if graduation
UX waits. Phase 5 should begin with the shared-core extraction as soon as
Phase 1 stabilizes the interfaces — every week of delay widens the
duplication (G18). Phase 6 is continuous.

| Phase | Unblocks | Depends on |
|---|---|---|
| 1. Learned importance | 2, 3, 4, 5 | — |
| 2. Chat/Slack as sources | 3 (full breadth) | 1 (partially) |
| 3. Wider suggestions | 4 (track records to graduate on) | 1 |
| 4. Graduating autonomy | real-assistant behavior | 1, 3, F1/F2 |
| 5. Hosted convergence | sellable hosted assistant | 1; roadmap steps 2–7 |
| 6. Productization | adoption | parallel |
