# 04 — Conversation context for Q&A

**Milestone:** M1 · **Depends on:** none · **Fixes roadmap defect #8**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1. Run `pytest`
before and after.

## Problem

`dispatcher._converse` answers every message in isolation: memory search +
one model call. A user who asks "what's on my plate today?" and follows with
"move the second one's prep earlier — actually, who's attending it?" gets a
non-sequitur, because the assistant literally cannot see its previous turn.
Every chat product trains users to expect follow-ups to work; this is the
gap they'll hit in the first five minutes.

## Task

1. New module `packages/aidedecamp/src/aidedecamp/conversation.py`: a small
   rolling window of recent turns keyed by `(channel, user_id)` — protocol +
   `JsonConversationLog(path)` concrete impl (the `ingestion/state.py`
   pattern). Keep the last N turns (default 10) and drop turns older than a
   TTL (default 2h) so stale context doesn't leak into tomorrow's questions.
   New Settings: `conversation_state_path`, `ADC_CONVERSE_WINDOW_TURNS`,
   `ADC_CONVERSE_TTL_MINUTES`.
2. `_converse` gains the window: prior turns go into the messages list as
   alternating user/assistant turns **between** the system prompt and the
   current message. The current message keeps its `[UNTRUSTED chat]` frame;
   prior user turns keep the frame they were stored with. After the model
   replies, append both turns to the log.
3. Thread it through `_respond_to_message` so Slack DMs and Chat messages
   each get their own window (`channel` = `"slack"` / `"chat"`), injected
   from `runtime.py` with override-or-build-real. A `None` log preserves
   today's stateless behavior for existing callers/tests.
4. Brief requests (the keyword branch) also record their exchange into the
   window, so "expand on the second item" works after a brief.

## Constraints

- **Rule 2:** incoming chat text stays UNTRUSTED-framed on every turn,
  including when replayed as history. Never re-frame stored history as
  system/instruction content.
- Do not conflate this with memory (`MemoryStore`): the window is ephemeral
  working memory (design §2.1's first row), not a learning substrate — no
  `store.add` calls here. Say so in the module docstring; that boundary is
  the kind of thing that erodes.
- Raw bodies stay out of LangGraph state (existing state discipline); this
  log is dispatcher-level, not graph state.

## Acceptance

- Offline tests: follow-up turn includes prior turns in the model call
  (assert on the fake client's captured messages); TTL and N-turn eviction
  with an injected clock; per-(channel,user) isolation; `None` log keeps
  the old single-shot behavior byte-identical.
- `docs/decisions.md` entry: why working-memory-vs-MemoryStore is a hard
  boundary, chosen defaults. CLAUDE.md module map updated.
