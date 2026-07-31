# 31 — Reversibility, expiry, and batch review

**Phase P5** · `docs/plan-2026-h2.md` · **Depends on:** 26, 30

---

Read `CLAUDE.md`, the P5 section of `docs/plan-2026-h2.md`,
`orchestrator/pending.py` (especially its module docstring), and
`hosted/capability_admission.py`.

## Problem

Three gaps in the approval spine, all of which get worse as autonomy rises.

**No undo.** `grep -rn "undo" src/` returns **zero matches**. Approving an
archive removes `INBOX` with no re-add path. Approving a reschedule patches the
event with no record of the prior time. A sent reply is irreversible by
construction. The one recovery mechanism is a human doing it manually in Gmail.
For a product whose entire pitch is *earned* autonomy, the absence of an undo
path is the strongest argument against ever granting a rung above PROPOSE — and
the trust literature models reversibility as a first-class dimension of how much
autonomy an action should be allowed to carry.

**No expiry.** `pending.py`'s docstring is explicit: *"an expired entry's
workflow stays paused in the checkpointer and can still be resumed late —
nothing here kills or times out workflows."* A click six months later still
resumes; only the apply-time freshness check stands between that and a wrong
effect. `sweep_ignored` marks an entry IGNORED after 48h and records a learning
signal, but the workflow itself remains live. The hosted plane already does this
correctly: `APPROVAL_LIFETIME = 15 minutes`, `DISPATCH_INTENT_LIFETIME = 10
minutes`.

**No batching.** One card, one `lg_tid`, three buttons, no aggregate handler. On
a busy morning that is a stream of individually-trivial decisions, and the named
failure mode of approval gates is precisely that *"the control breaks when
approval stops being a real decision and becomes a reflex."*

## Task

1. **Compensating actions as part of the capability contract.** Prompt 30's
   `Capability` descriptor gains a real `compensate` function or an explicit
   `irreversible=True`. Implement:
   - `LABEL` → re-add `INBOX`, remove the applied label
   - `CREATE_HOLD` → delete the created event (id is already known)
   - `RESCHEDULE` → restore the prior start/end, which must now be **captured
     into the workflow state before the patch**, not re-derived afterwards
   - `DECLINE_INVITE` → reset the principal's own `responseStatus` to
     `needsAction`
   - `DRAFT_REPLY` → delete the created draft
   - `SEND_REPLY` → **`irreversible=True`.** Do not fake it; a follow-up
     "please ignore that" email is not an undo and must not be modelled as one.

2. **Undo as a first-class audited effect.** `attune undo <effect-id>` plus an
   Undo affordance on the post-apply notification (a bounded window, e.g. 1h,
   documented not configurable). Undo is itself an audited effect with its own
   audit event and its own freshness check — the world may have moved since the
   apply. Undo writes `undone=True` to the decision ledger, and an undo is the
   **strongest negative signal in the system**: feed it to
   `grants.suggest_demotions` as a single-occurrence trigger, the same weight the
   existing rule already gives a rejection against an auto-applied effect.

3. **Approval TTL with an explicit expired state.** `PendingApprovals` gains
   `STATUS_EXPIRED` distinct from `STATUS_IGNORED`, and the sweep **cancels the
   underlying workflow** rather than leaving it paused. A click on an expired
   card returns an honest "this proposal expired, ask me again" instead of
   resuming. Default lifetime configurable, defaulting well beyond hosted's
   15 minutes (a person's approval channel is not a request/response cycle) —
   propose 7 days and justify the number. Expiry must remain distinguishable
   from ignore in the ledger, because they mean different things about the
   principal.

4. **Batch cards.** When several proposals of the same capability are pending,
   render one grouped card with per-item accept/reject plus an "approve all"
   that expands to N individual audited resumes — never one aggregate effect with
   one audit row. Each item keeps its own `lg_tid`, its own freshness check, its
   own `pending.claim`, and its own ledger row. Adopt the vocabulary the category
   has settled on — **accept / edit / respond / ignore** — since it is now shared
   language with LangChain's Agent Inbox and will be legible to anyone arriving
   from elsewhere.

5. **A per-item claim under batch.** `pending.claim` is already
   load-check-mutate-save under an advisory lock and returns `False` for an
   already-resolved entry. Batch resumes must claim each item individually so a
   partially-processed batch is safe to retry, and a double-click on "approve
   all" cannot double-apply anything.

## Constraints

- Undo may never exceed the authority of the original action. If the rung that
  authorized the apply has since been revoked, undo is still permitted (it only
  ever reduces effect), but it is audited with the actor who requested it.
- A compensating action is a **capability**, not a bypass: it goes through the
  same registry, the same audit path, and the same freshness check. It does not
  need its own grant — undoing is not a new authority — but it must be recorded
  as an effect.
- Batch approval must not become a way to approve something the principal did
  not see. Every item in a grouped card is individually rendered with its
  subject and recipient; an approve-all over a truncated list is forbidden.
- Expiry cancels the workflow. Verify the checkpointer actually releases it;
  a "cancelled" flag that leaves a resumable checkpoint is the current bug with
  extra steps.

## Acceptance

- A test per compensating action asserting the inverse effect and its audit
  event, including that `RESCHEDULE`'s prior time was captured pre-patch.
- A test asserting `SEND_REPLY` reports itself irreversible and offers no undo
  affordance anywhere.
- A test asserting an expired card returns the honest refusal and that its
  workflow cannot be resumed afterwards.
- A test asserting an undo triggers a demotion suggestion on a single occurrence.
- A test asserting "approve all" over 5 items produces 5 audited applies, 5
  ledger rows, and 5 freshness checks — and that a repeated click applies nothing
  further.
- A test asserting `STATUS_EXPIRED` and `STATUS_IGNORED` are distinguishable in
  the ledger and produce different learning signals.
- `docs/decisions.md` entry recording the reversibility contract, the chosen TTL
  and its justification, and why `SEND_REPLY` has no undo.
