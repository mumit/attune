# 16 — Calendar write actions: design first, then holds-at-PROPOSE

**Milestone:** M5 · **Depends on:** 12 (persisted matrix) · **Two-phase task**

---

Read `CLAUDE.md` (next-steps item 1 — this is the deliberately-deferred
feature, and the conditions it set for building it), `docs/decisions.md`
("Scheduling conflict detection" entry), design §1.3/§3.2, and
`docs/roadmap.md`. Run `pytest` before and after.

## Problem

Calendar is read-only by explicit decision: conflict detection notifies but
cannot offer a fix, and invites can't be responded to. The deferral was
principled — no well-defined trigger, and a write layer needs its own
autonomy-ladder design rather than riding along with conflict detection.
This prompt honors that: **phase 1 is the design decision, phase 2
implements only what phase 1 settles.** Do not skip to code.

## Phase 1 — settle the design (a `docs/decisions.md` entry, written first)

Answer, concretely, in a new decisions entry:

1. **Triggers.** The two candidate well-defined triggers now available:
   (a) a detected conflict (`handle_calendar_notification`) → offer a
   *resolution hold* ("move the overlapped 1:1? I can hold Tue 14:00");
   (b) an incoming invite among the changed events → offer accept/decline
   (needs an invite-response verb — see 3). Decide which to build now
   (recommendation: (a) only — it reuses `create_hold`, an existing
   connector verb on both implementations, and has the clearest user value;
   (b) requires new API surface and RSVP semantics — defer it again,
   explicitly).
2. **Autonomy shape.** `CREATE_HOLD`/`CALENDAR` already sits at `PROPOSE`
   in `default_matrix()`. Confirm the flow enters through the standard
   draft-approve graph (gate → interrupt → card), what `ACT_NOTIFY`
   graduation would mean here (auto-create tentative hold + notify —
   tentative holds are reversible, the canonical rung-3 property), and what
   is *excluded* (any hold on events with external attendees at rung 3,
   matching design §3.2's own example).
3. **What stays deferred**: invite accept/decline, rescheduling,
   negotiating times with counterparties. Name them so scope creep has to
   argue with a written decision.

## Phase 2 — implement the settled slice (assuming recommendation (a))

1. A hold-proposal path: on conflict detection, in addition to the existing
   `notify`, compute up to 2 candidate free slots near the conflict (pure
   function over `list_events` for that day, offline-testable) and start a
   draft-approve workflow — `action=Action.CREATE_HOLD`,
   `domain="calendar"`, `source_ref=<event id>`, proposed "draft" text being
   the human-readable hold proposal. The apply step (prompt 01's `apply_fn`)
   grows a calendar branch: on approval, `connector.create_hold(...)` for
   the chosen slot; confirmation names the created tentative hold.
2. Cards reuse the prompt-15 header extension ("Scheduling conflict —
   proposed hold"). Pending-registry, IGNORED decay, audit events — all
   inherited by construction.
3. A settings kill-switch is unnecessary — an absent grant already gates
   nothing-happens-without-approval; do not add parallel toggles (the
   matrix is the single source of authority, rule 3).

## Constraints

- Phase 1's decisions entry must exist in the same PR/change as phase 2's
  code, and phase 2 must not exceed it.
- `create_hold` only — no event mutation, no attendee invitations on the
  hold, no RSVP calls. Holds are created `tentative` (both connectors
  already do).
- All the usual: injected collaborators, offline tests, UNTRUSTED framing
  for any event-derived text entering prompts (rule 2).

## Acceptance

- Offline tests: free-slot candidate math (edges: back-to-back days, no
  free slot → notify-only fallback); workflow invoked with CREATE_HOLD and
  gated at PROPOSE (no grant → interrupt, always); apply branch calls
  `create_hold` with the approved slot; end-to-end fake flow from
  conflict → card → approve → hold-created audit event.
- CLAUDE.md next-steps item 1 rewritten to reflect what's now built vs.
  still deferred.
