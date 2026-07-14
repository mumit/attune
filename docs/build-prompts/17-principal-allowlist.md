# 17 — Principal allowlist: authenticate the human, not just the transport

**Milestone:** M6 stabilization · **Fixes review finding #1 (P0)**

---

Read `CLAUDE.md`, `docs/decisions.md`, and the M6 section of
`docs/roadmap.md`. Run `pytest` before (504+) and after.

## Problem

Every channel surface authenticates the *transport* (Slack signs requests,
Google signs Chat webhooks) but never the *human*. Any Slack workspace
member who DMs the bot gets the owner's brief, can browse/delete/teach the
owner's memories, and can click Approve on the owner's drafts; any human in
the configured Chat space likewise — all executing under
`settings.user_id`. Webhook verification proves Google Chat called; it says
nothing about which person clicked.

## Task

1. **Settings**: `slack_allowed_users` / `chat_allowed_users`
   (`ATTUNE_SLACK_ALLOWED_USERS` / `ATTUNE_CHAT_ALLOWED_USERS`, comma-separated —
   Slack user IDs like `U0123`, Chat user resource names like `users/123`),
   parsed to frozensets. **Empty means deny-all** (fail-safe, rule-3
   spirit): an unconfigured deployment refuses every actor with a message
   that includes the sender's own ID so the owner can allowlist themselves
   in one copy-paste. `attune init` asks for the Slack user ID.
2. **Enforce at every human entry point**, before any dispatch:
   - Slack: the `message` DM handler, `_approve`/`_reject`/`_edit` action
     handlers, and the `view_submission` handler — actor is
     `event["user"]` / `body["user"]["id"]`. `SlackChannel` gains an
     `allowed_users` constructor param; unauthorized actions get an
     ephemeral "not authorized" respond, unauthorized DMs get the
     self-identifying refusal.
   - Chat: `Runtime.process_chat_event` checks `ChatMessage.sender`;
     `decode_chat_interaction` gains an `actor` field (from
     `event["user"]["name"]`) and `Runtime.process_chat_interaction`
     refuses non-allowlisted actors before resuming anything.
3. **Actor rides the resume path**: `SlackChannel._resume` and the bound
   resume closures accept/forward `actor=` so prompt 20 can stamp it into
   the audit trail. Refused attempts are logged (actor id only — no
   message content) and audited under `"ops"` (`unauthorized_actor`).

## Constraints

- Deny-by-default is non-negotiable; no "allow all" wildcard setting.
- The refusal message may echo the actor's own ID (it's theirs) but never
  any owner data.
- All enforcement offline-testable with the existing fake Bolt app / event
  dicts.

## Acceptance

- Tests: unauthorized DM refused with self-ID message + nothing dispatched;
  unauthorized approve/reject/edit-submit click refused + graph NOT
  resumed; unauthorized Chat message/interaction dropped + audited;
  authorized actors pass through unchanged; empty allowlist = deny-all.
- `.env.example` + wizard updated; decisions.md entry; CLAUDE.md rules
  section gains the "authenticate the human" line.
