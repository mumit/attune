# 11 — Memory transparency: see, correct, and teach memory from chat

**Milestone:** M4 · **Depends on:** 08 (CLI group) ·
**Fixes roadmap defect #10**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1. Run `pytest`
before and after.

## Problem

"Memory is the product" (design §0), and the design's browser phase exists
largely so the user can *audit and correct* what's been learned. But today
memory is write-only from the user's perspective: `MemoryStore.get_all` and
`delete` exist and are called by nothing. A user who notices the assistant
has learned something wrong has no recourse short of wiping the store. The
cheap, high-value version of Phase 5 is chat commands — build those now;
the browser UI later renders the same operations.

## Task

1. New module `packages/aidedecamp/src/aidedecamp/memory/commands.py` with
   pure functions over an injected `MemoryStore`:
   - `list_memories(store, *, user_id, query=None, limit=20)` — formatted,
     **numbered** listing (via `search` when a query is given, else
     `get_all`), each line showing text + signal/domain metadata + a short
     stable id suffix.
   - `forget_memory(store, *, user_id, selector)` — selector is a number
     from the most recent listing or an id prefix; returns what was deleted
     for confirmation. Requires a preceding listing (see step 3) so "forget
     3" is unambiguous.
   - `remember_fact(store, *, user_id, text)` — explicit user-taught fact,
     stored with `metadata={"signal": "explicit"}` and `infer=True`.
2. **Chat routing.** Extend `dispatcher._respond_to_message` from keyword
   soup toward a small command router: messages starting with
   `what do you know` / `memories` (+ optional topic) → list;
   `forget <selector>` → forget (reply asks "Delete: '<text>'? Reply
   'confirm forget'" and only a follow-up confirmation deletes — destructive
   action, two steps); `remember <text>` → remember. Everything else keeps
   the existing brief-keyword/converse behavior. Store the last listing's
   number→id map and any pending forget-confirmation in the prompt-04
   conversation window if it landed (per-user, per-channel), else a small
   injected dict-backed state.
3. **CLI.** Fill prompt 08's `aidedecamp memory` group: `list [--query]`,
   `forget <id>` (with `--yes` for non-interactive), `remember <text>`.
4. Audit every mutation: `memory_deleted` / `memory_taught` events under a
   `"memory"` workflow (user_id, id, text snippet) — corrections to the
   assistant's knowledge are exactly the audit log's business.

## Constraints

- **Rule 2 nuance:** `remember`/`forget` arrive over chat channels that also
  carry untrusted relayed content. Commands must only trigger on the
  user's own direct messages (already guaranteed: Slack DMs are
  user-filtered, Chat events are HUMAN-sender-filtered) — note in the module
  docstring that this routing must never be applied to fetched mail bodies.
- Deletion is per-memory and explicit; no bulk "forget everything" command
  in this prompt.
- Substrate-agnostic: only the `MemoryStore` interface, no Mem0 imports.

## Acceptance

- Offline tests (fake store): listing numbering/format, search-vs-get_all
  branch, two-step forget (no deletion without confirmation; stale
  confirmation with no pending forget is a polite no-op), remember writes
  with explicit-signal metadata, dispatcher routing precedence (memory
  commands before brief keywords), CLI paths, audit events.
- `docs/decisions.md` entry (command grammar, two-step delete, relationship
  to the future browser UI) + CLAUDE.md module map.
