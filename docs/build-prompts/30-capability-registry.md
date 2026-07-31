# 30 — Capability registry: collapse the eleven-file ceremony

**Phase P5** · `docs/plan-2026-h2.md` · **Depends on:** 28 · **Blocks:** 31, 35

---

Read `CLAUDE.md`, the P5 section of `docs/plan-2026-h2.md`,
`orchestrator/draft_approve.py`, `app.py`, `hosted/capability_gateway.py`, and
`hosted/gmail_draft_capability.py`.

## Problem

Adding one action to Attune touches **11–13 files**: the `Action` enum and
`default_matrix()`, the connector base plus its `supports_*()` probe plus a
`*NotPermitted` exception, the Google implementation, the MCP non-implementation,
a `draft_fn` and an `apply_fn` and an `apply_confirmation` branch, a new compiled
graph plus an `AppContext` field plus a `build_app` kwarg, a `_gates_pass` and an
`_offer_proposal` and a `_rank` and a `MAX_*_PER_RUN` in `dispatcher.py`, a
`_graph_for_thread_id` namespace branch plus apply-fn wiring in `runtime.py`, a
settings field, `.env.example`, exports, scopes, Doctor, and docs.

There is no registry, no dispatch table, no plugin seam. `app.py` compiles
**three** LangGraph instances solely because `draft_fn` differs — and two of
those three `draft_fn`s return their input unchanged and make no model call. The
three graphs share one checkpointer and are selected at resume time by
**string-prefix matching on `thread_id`** in `runtime._graph_for_thread_id`. That
mechanism has already caused one live bug: before it existed, approving an
archive card ran the draft graph's apply function and created a Gmail draft
instead of archiving.

Meanwhile the graph itself contributes exactly one thing: durable
`interrupt`/`resume`. It is a 7-node linear chain with one branch.
`MAX_ITERATIONS = 10` is defined and `iteration_count` is incremented but the
value is **never read anywhere** — there is no loop to bound.

And the hosted plane already has the right abstraction. `CapabilityRegistry` +
`CapabilityDefinition` + `ArgumentContract` + `RiskTier` is exactly this design,
with exactly one capability registered in it, dormant.

Six actions is not an assistant. This ceremony is why.

## Task

1. **One declarative descriptor.** A `Capability` dataclass carrying everything
   that currently varies per action:

   ```
   action, domain, risk_tier
   connector_probe        # e.g. connector.supports_sending
   enabled_flag           # the deployment opt-in setting
   propose                # build the proposal (may be identity, may call a model)
   apply                  # perform the effect
   compensate             # the inverse effect, or None + irreversible=True (prompt 31)
   freshness_check        # re-verify the source before applying
   render_card            # title, body, buttons
   confirmation_text      # what actually happened, post-apply
   rank                   # importance ordering for the per-run cap
   max_per_run
   thread_namespace
   ```

   Register all six existing capabilities without changing their behaviour:
   `DRAFT_REPLY`, `SEND_REPLY`, `LABEL`, `CREATE_HOLD`, `DECLINE_INVITE`,
   `RESCHEDULE`, plus `FOLLOW_UP`.

2. **One graph, generic dispatch.** Collapse the three compiled graphs into one
   whose `apply` node dispatches through the registry on the capability recorded
   in the workflow state. Delete `_graph_for_thread_id`'s prefix matching and the
   duplicated apply-fn wiring in `runtime.py`'s two resume paths. Keep the state
   `TypedDict` explicit — the `label_name` bug (an undeclared key silently
   dropped across the interrupt boundary, which meant archiving had probably
   never worked against a real deployment) is what happens when it isn't.

3. **Fix the auto-apply side channel.** `dispatcher._auto_rung` discovers whether
   the gate auto-applied by **string-matching over the audit event stream**. Make
   the gate return its routing decision as a typed value.

4. **Delete the dead loop bound.** Remove `MAX_ITERATIONS` and
   `iteration_count`, or implement an actual bounded loop that uses them. Do not
   leave a safety-shaped constant that nothing reads.

5. **Converge on one risk vocabulary.** The local plane has `Rung 1-4`
   (READ_ONLY/PROPOSE/ACT_NOTIFY/AUTONOMOUS); hosted has `RiskTier R0-R4`. They
   describe the same thing from two directions — how much autonomy is granted,
   and how dangerous the action is. Keep both concepts but define one explicitly
   in terms of the other, in one place, with a test pinning the mapping. Prompt
   35 depends on this.

6. **Then add the missing capabilities**, in this dependency order, each as one
   descriptor plus tests:
   1. generic `add_label` / `remove_label` / `mark_read` (`add_label` already
      exists on the connector with no call site)
   2. `RSVP_ACCEPT` / `RSVP_TENTATIVE` — today only decline exists, which makes
      the calendar surface structurally negative
   3. `freebusy` reads and cross-attendee find-time — `propose_free_slots`
      currently reads only `calendarId="primary"` inside a hardcoded 08:00–18:00
      window on the event's own day
   4. `CREATE_EVENT` **with attendees** — `_apply_calendar_hold` currently forces
      `attendees=[]`
   5. `CANCEL_EVENT`
   6. recurring-event awareness — `list_events(singleEvents=True)` flattens
      recurrence and `reschedule_event` patches a single instance blindly
   7. `SEND_NEW` (non-reply) and reply-all/CC
   8. **a real write capability for `Domain.CHAT` and `Domain.SLACK`** — both
      hold `DRAFT_REPLY` grants at PROPOSE today with no connector method, no
      apply function, and no call site anywhere

   Ship 1–3 in this prompt; 4–8 are follow-on work once the registry proves
   itself. Do not ship a capability without a compensating action or an explicit
   `irreversible=True`.

## Constraints

- Every existing gate stays: matrix rung, connector probe, deployment flag —
  three independent checks, all required. The URGENT-interrupt rule and
  fail-closed scope matching are unchanged.
- `SEND_REPLY` keeps every one of its current protections, including its
  exclusion from graduation cards. Nothing here may make an external send easier
  to reach.
- MCP backends keep their structural refusals; a capability the MCP contract does
  not cover must refuse, not silently no-op.
- Default matrix still tops out at PROPOSE. New capabilities are not granted by
  arriving.
- This is a refactor with additions. Behaviour for the six existing capabilities
  must be identical; prove it by keeping their existing tests unmodified.

## Acceptance

- Existing capability tests pass **unmodified** — that is the evidence the
  refactor is behaviour-preserving.
- A test asserting one compiled graph handles all seven registered capabilities
  and that a resume can never run another capability's apply function (the class
  of bug the prefix matching caused).
- A new capability added in the test suite as one descriptor plus a fake
  connector method, with no production file touched outside the registry.
- A test pinning the `Rung` ↔ `RiskTier` mapping.
- A test asserting `mark_read` and `RSVP_ACCEPT` refuse when their flag is off,
  when the connector lacks support, and when the rung is below PROPOSE —
  independently.
- `docs/decisions.md` entry recording the registry design, the graph collapse,
  the risk-vocabulary unification, and the `MAX_ITERATIONS` removal.
