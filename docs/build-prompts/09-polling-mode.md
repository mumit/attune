# 09 — Polling ingestion mode: zero-infrastructure day one

**Milestone:** M3 · **Depends on:** none (06's supervision helpers welcome)

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1 + §3. Run
`pytest` before and after.

## Problem

The push-ingestion path requires four Pub/Sub topic+subscription pairs, a
deployed Cloud Run republisher, and watch/subscription lifecycle management
— *before the first event flows*. But every reconciliation primitive in this
codebase is already trigger-agnostic: `gmail_history.process_notification`
walks `history.list` from a stored baseline, `calendar_sync` walks a stored
sync token, and Chat's Workspace Events are just one delivery option for
messages `spaces.messages.list` can also return. A timer can drive all
three. Polling is outbound-only — exactly as rule-5-clean as pull
subscriptions — and turns "a weekend of GCP plumbing" into "OAuth + go".

## Task

1. New Settings: `ingestion_mode` (`ADC_INGESTION_MODE`, enum `poll|push`,
   **default `poll`**) and `ADC_POLL_SECONDS` (default 120).
2. New module `packages/aidedecamp/src/aidedecamp/ingestion/polling.py`
   with three pure poll-step functions, each reusing the existing
   reconciliation machinery rather than duplicating it:
   - `poll_gmail_step(gmail_service, watch_state) -> notification | None`:
     fetch the current profile `historyId` and, if it's ahead of the stored
     baseline, synthesize the same `{"emailAddress", "historyId"}` dict a
     Pub/Sub notification carries — so `dispatcher.handle_gmail_notification`
     is invoked *identically* in both modes. First run with no baseline:
     store the current historyId and return `None` (start from now, don't
     replay the mailbox).
   - `poll_calendar_step(...)`: `handle_calendar_notification` already
     ignores the notification body beyond triggering reconciliation — invoke
     it on every tick (sync-token diffing makes empty ticks cheap).
   - `poll_chat_step(chat_service, state) -> list[event]`: `spaces.messages.
     list` filtered by a stored last-seen create time, synthesizing the same
     event shape `process_chat_event` decodes; persist the new high-water
     mark only after successful dispatch.
3. `Runtime.run()` branches on `ingestion_mode`: poll mode starts one
   supervised timer thread (reuse 06's backoff/heartbeat helpers if landed;
   otherwise a minimal equivalent) ticking the three steps; push mode keeps
   today's pull loops. In poll mode, watch-renewal scheduler jobs are
   skipped (nothing to renew) — Chat card-interactions still require the
   republisher, so keep that one pull loop if its subscription is
   configured, and otherwise log that Chat approval buttons need push mode
   or Slack (approve/reject via Slack Socket Mode works fully in poll mode).
4. `doctor` (prompt 08, if landed) gains mode-aware checks: poll mode stops
   requiring any Pub/Sub resources.

## Constraints

- **Rule 5 intact:** polling is outbound-only. No webhook, no port.
- **The dispatcher seam does not move.** Poll functions produce the same
  decoded shapes push produces; `dispatcher.py` must not learn which mode
  fed it.
- Respect the still-open Google quota concern (CLAUDE.md): default cadence
  120s, floor of 30s, and one profile-get/tick for Gmail (cheap) rather
  than message listing.

## Acceptance

- Offline tests (fake services): gmail step synthesizes a notification only
  when historyId advanced, first-run baselining, calendar step delegates to
  the dispatcher path, chat high-water-mark advance-only-on-success, runtime
  assembles poll vs push loop sets per mode.
- `docs/decisions.md` entry: poll-as-default rationale, push as the
  graduated production posture, the Chat-interaction caveat. CLAUDE.md
  module map + next-steps updated.
