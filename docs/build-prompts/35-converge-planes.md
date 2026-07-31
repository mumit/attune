# 35 — Converge the two planes onto one core

**Phase P9** · `docs/plan-2026-h2.md` · **Depends on:** 30 · **Blocks:** nothing

---

Read `CLAUDE.md`, the P9 section of `docs/plan-2026-h2.md`, and
`hosted/intelligence.py` — the one module that already does this correctly and
is the pattern to copy.

## Problem

Attune is two implementations of one product.

```
grep -l '^from \.\.[a-zA-Z]' src/attune/hosted/*.py   →   3
grep -l '^from \.[a-zA-Z]'   src/attune/hosted/*.py   →  94
```

**23,222 LOC across 113 hosted modules share 3 imports with the 19,435-LOC
core — 2.7% reuse.** Every improvement toward the product goal must currently be
built twice, and the hosted plane has consistently received the second, thinner
version or none at all.

The diverged pairs:

| Concern | Local | Hosted | Shared |
|---|---|---|---|
| Google API client | `connectors/google_oauth.py`, 575 ln, SDK, unbounded responses | `hosted/google_provider.py`, 455 ln, hand-rolled `requests`, 32KB cap, field stripping | none |
| Audit | `audit/log.py`, 370 ln, inline field values, hash-chained JSONL | 6 modules, ~495 ln, content-free hashed refs, two-phase intent→writer outbox | none |
| Model routing | `llm.py`, 6 tasks, no validation | `hosted/model_gateway.py`, **3 tasks**, bounded envelopes, tenant profiles | none |
| Approval / risk | LangGraph interrupt + `Rung 1-4` + memory capture | capability registry + `RiskTier R0-R4` + `SECURITY DEFINER` claim + 15-min TTL | none |
| Chat send | `channels/gchat.py` | `hosted/google_chat_provider.py` | none |
| Slack send | `channels/slack.py` | `hosted/slack_provider.py` | none |
| Conversation | `conversation.py`, file-backed TTL window | `hosted/web_conversation.py`, durable rows + sequence numbers | none |
| Importance / attention | `orchestrator/importance.py`, `attention.py` | `hosted/intelligence.py` | **dataclasses, constants, and the `assess_from_signals` rule engine** |
| Recurrence | `scheduler.py` | **nothing** | — |

Plus duplication *inside* hosted: `channel_broker.py` and
`slack_channel_broker.py` are 1,600 lines of near-1:1 mirrored classes with no
shared base, and the app/service/client triplet is hand-written 5 complete times
and partially 12 more — **39 files, 34.5% of `hosted/`**.
`control_plane_service.py` is 1,802 lines with 37 inline route handlers.

`hosted/intelligence.py` proves the right pattern: **storage differs per plane;
the dataclasses, constants, and pure rule engine are imported.** That is what
`assess_from_signals` was made public for. Apply it to everything else.

## Task

Extract shared cores in this order. Each step is independently shippable and must
leave both planes behaviourally identical.

1. **Workspace provider.** One interface, two transports. The hosted version's
   discipline is the better one and should become the shared default — bounded
   response size, `fields=` masks, explicit field stripping — with the local SDK
   client as a second transport behind the same interface. This step also
   delivers the data-minimization the local plane currently lacks (its
   `get_thread` returns an uncapped body, which is prompt 24's finding #5).

2. **Model gateway.** One task vocabulary, one bounded envelope, one usage
   record. Hosted's limits (`MAX_MESSAGES`, `MAX_MESSAGE_CHARS`,
   `MAX_TOTAL_CHARS`, `MAX_RESPONSE_CHARS`) are the better default and should
   apply locally too. Local's six tasks are the correct vocabulary — hosted's
   missing `draft` is why it cannot draft (fixed in prompt 28). The
   prompt registry from prompt 28 is shared by construction.

3. **Capability registry.** Prompt 30 built the local registry against the shape
   hosted already had. Unify them into one registry with one `Capability`
   descriptor and one risk vocabulary (prompt 30 pinned the `Rung`↔`RiskTier`
   mapping). Per-plane differences — Cloud Tasks dispatch vs. LangGraph
   interrupt, `SECURITY DEFINER` claim vs. `pending.claim` — become injected
   collaborators, not parallel architectures. **This is the highest-value step:
   after it, a capability added once appears in both planes.**

4. **Audit interface.** One `record` signature and one event vocabulary; two
   backends (local hash-chained JSONL, hosted two-phase outbox with hashed
   refs). Local should adopt hosted's content-free posture for anything that does
   not need inline values — `audit/log.py`'s own docstring admits its tail
   truncation is undetectable, which the hosted design already solves.

5. **Channel senders.** One `send` interface per provider, two transports.
   Then give the mirrored Slack/Chat brokers inside hosted a shared base — that
   alone is ~800 lines.

6. **Conversation model.** One conversation abstraction with two persistence
   strategies. These solve genuinely different problems (ephemeral replay context
   vs. durable canonical turns), so the shared piece is the turn shape,
   provenance rules, and window policy — not the storage.

7. **Recurrence.** Hosted has no scheduler at all; `hosted/brief_producer.py`
   says recurring scheduling is "explicitly future operator work". Prompt 32's
   durable job ledger should be the shared abstraction, with the hosted plane
   binding it to its own scheduler identity.

8. **Service framework.** Replace the 5 hand-copied app/service/client triplets
   with one shared base implementing the common shape (auth check → bounded
   validate → delegate to injected repository), and split
   `control_plane_service.py`'s 37 handlers into blueprints by concern. Normalize
   the `_app.py` / `_service.py` naming, which currently means the opposite thing
   in 11 of 19 cases.

## Constraints

- **Behaviour-preserving, provably.** Each step keeps both planes' existing tests
  unmodified. If a test must change, the change is the thing to justify in the
  decisions entry.
- Tenancy is not negotiable. Shared code must not make local state
  multi-tenant, and must not let hosted lose RLS, the memberless
  `SECURITY DEFINER` owner pattern, or hashed-reference keying. The shared piece
  is the **rule engine and the shape**; the storage and the isolation stay
  per-plane.
- Where the two planes' postures differ, **the stricter one becomes the shared
  default** — bounded responses, field stripping, content-free audit metadata,
  bounded model envelopes, approval TTLs.
- Do not merge the deployment models. One principal per local instance stays
  true; hosted stays tenant-scoped with stateless workers.
- No new `hosted/` module may be created that has no local counterpart.

## Acceptance

- Report the reuse ratio before and after in the decisions entry, using the same
  two `grep` commands quoted above. Target: above 40%.
- A test asserting one `Capability` descriptor is exercised by both planes'
  execution paths.
- Both planes' existing test suites pass unmodified after each step.
- A test asserting hosted still enforces RLS and its privileged-function owners
  after the audit and capability unification.
- A test asserting the local plane now bounds provider response size and strips
  fields, as hosted always did.
- LOC and module-count deltas for `hosted/` recorded per step.
- `docs/decisions.md` entry per step, and an update to `docs/design.md` — its
  "a future hosted service" language is the stalest statement in the docs
  against 113 modules and 47 migrations.
