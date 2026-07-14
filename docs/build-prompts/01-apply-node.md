# 01 — Apply node: materialize approved drafts into Gmail

**Milestone:** M1 · **Depends on:** none · **Fixes roadmap defects #1, #2**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1 before
changing anything. Run `pytest` first — the full suite must pass as your
baseline and after your change.

## Problem

When a user clicks **Approve** on a draft card, the draft-approve graph
(`src/attune/orchestrator/draft_approve.py`) resumes,
sets `decision`/`final_text`, captures a memory signal — and stops.
`connector.create_draft` (implemented on both `McpWorkspaceConnector` and
`DirectOAuthConnector`) has **zero callers**. The user approves a reply and
then has to copy-paste it into Gmail themselves. Worse,
`channels/slack.py`'s `_approve` handler responds "✅ Approved — sending."
— which is false (nothing sends; per rule 4 nothing *can* send).

## Task

1. Add an **apply** step to the draft-approve flow: after a decision of
   `approved` or `edited` (both the human path and `auto_apply`), call an
   injected `apply_fn(state) -> str | None` that materializes `final_text`.
   The default implementation calls `connector.create_draft(thread_id=…,
   body=final_text)` for `domain == "mail"` and returns the created draft id;
   for domains with no materialization (chat/slack) it's a no-op returning
   `None`. On `rejected`, apply is skipped entirely.
   - The graph currently has no access to a connector — thread it through
     `build_draft_approve_graph(…, apply_fn=…)` the same way `draft_fn` is
     injected, and bind the real connector in `app.py`/`runtime.py` at
     assembly time. Keep the graph free of any direct google/mcp import.
   - The Gmail thread id is not currently in graph state — `dispatcher.
     handle_gmail_notification` builds `lg_tid = "gmail:<tid>:<historyId>"`.
     Add an explicit `source_ref` field to `DraftApproveState`
     (`orchestrator/state.py`) set by the dispatcher, rather than parsing it
     back out of the LangGraph thread id.
2. Record the outcome in the audit trail: an `applied` audit event with the
   draft id (or `apply: skipped` with a reason), same `_audit()` shape as the
   existing events.
3. Fix the dishonest confirmations. Slack `_approve` and
   `dispatcher.handle_chat_interaction`'s approved-branch must state what
   actually happened: "✅ Approved — draft created in Gmail." when apply
   produced a draft, "✅ Approved." when there was nothing to materialize.
   The resume path returns the final graph state — use it to know which.
4. Failure honesty: if `create_draft` raises, the confirmation must say the
   approval was recorded but draft creation failed (and the audit event
   records the error class) — never a false success.

## Constraints (non-negotiable)

- **Rule 4:** `create_draft` only. Do not touch `send_reply`, its gate, or
  `send_enabled`. The word "sending" must not appear in any confirmation.
- **Rule 3:** apply happens only after the existing gate/approve path
  produced an approved/edited decision — no new autonomy shortcut.
- Apply must be injected and offline-testable (a fake `apply_fn` / fake
  connector in tests; no live services).

## Acceptance

- New tests: apply called with `final_text` on approved and edited paths;
  skipped on rejected; audit event recorded; failure path produces the
  honest-failure confirmation; existing suite untouched and green.
- `docs/decisions.md` entry: why apply is an injected fn, why `source_ref`
  is explicit state, and the confirmation-honesty rule.
- Update the flow diagram lines in module docstrings that say
  `approve -> apply -> capture` so the docstring finally matches reality.
