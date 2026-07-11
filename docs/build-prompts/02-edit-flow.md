# 02 — Edit flow: Slack modal + Chat dialog, end to end

**Milestone:** M1 · **Depends on:** 01 · **Fixes roadmap defect #3**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1. Run `pytest`
before and after (all green, plus new tests).

## Problem

Edit-before-send is the single richest learning signal in the design
(design §2.2 — the correction diff), and `capture_correction` is fully built
and wired into the graph's `capture` node. But no production surface can
trigger it: `channels/slack.py`'s `_edit` handler is a literal `pass` stub,
and Google Chat's edit dialog submit path was explicitly deferred ("a wiring
task, not a design question" — see decisions.md, Google Chat channel entry).
The `edited` decision can currently only occur in tests.

## Task

1. **Slack modal.** In `SlackChannel._register`, implement `_edit`:
   `views_open` a modal (via the `client` Bolt hands the action handler)
   prefilled with the proposed draft in a `plain_text_input`, carrying the
   workflow `thread_id` in the modal's `private_metadata`. Register a
   `view_submission` handler that extracts the edited text and calls
   `self._resume(thread_id, "edited", edited_text)`, then confirms in
   channel ("✏️ Edited and applied — draft created in Gmail." — reuse
   prompt 01's honest-confirmation logic).
   - The proposed draft is available in the approval card's context; carry
     it into the modal from the button's message (or re-read it from the
     interrupt payload via the graph state if cleaner — pick one, justify in
     the decisions entry).
   - Build the modal payload as a pure function in `channels/blocks.py`
     (same pattern as `approval_blocks`), testable without Slack.
2. **Chat dialog.** Complete the deferred path: the republisher already
   answers edit's dialog-open click synchronously
   (`deploy/republisher/main.py`) with a DIALOG `actionResponse`. Implement
   the real dialog card (pure builder in `channels/gchat_cards.py`,
   prefilled draft, `thread_id` as an action parameter) and handle the
   dialog-submit CARD_CLICKED event: extend
   `ingestion/chat_interactions.py::decode_chat_interaction` to also decode
   a dialog submit into `("edited", text)` — keeping its current contract
   that a dialog *open* click still returns `None` — so the existing async
   path (`dispatcher.handle_chat_interaction` → `resume_fn`) handles edits
   with **no new plumbing**. Update `handle_chat_interaction` to pass the
   edited text and post the honest confirmation.
   - The republisher's dialog-open response must now return the real dialog
     card. The republisher stays credential-free and stateless: the draft
     text it prefills must come from the incoming event payload (Chat echoes
     the card), never from any store. Its tests live in
     `deploy/republisher/` and run separately (see CLAUDE.md).
3. Keep action-name strings in sync the existing way: any new action name
   is defined in `channels/blocks.py`, mirrored where needed, and pinned by
   an equality test (the technique already used for `adc_approve`).

## Constraints

- **Rule 5:** the main process gains no inbound surface; Chat edit rides the
  existing republisher → Pub/Sub → `handle_chat_interaction` path.
- **Rule 2:** edited text is user-authored (trusted), but the surrounding
  thread content remains UNTRUSTED-framed wherever it re-enters a prompt.
- The graph itself needs no changes — `approve` already handles
  `decision == "edited"` with `text`.

## Acceptance

- Offline tests: modal builder output; view_submission → `resume("edited",
  text)`; dialog-submit decode → `ChatInteraction(decision="edited",
  text=…)`; dialog-open still decodes to `None`; sync-pin tests for new
  action names. Republisher tests updated in its own suite.
- After this lands, a real edit on either channel fires
  `capture_correction` — state this explicitly in the `docs/decisions.md`
  entry, since it closes the design's flagship learning-signal gap.
