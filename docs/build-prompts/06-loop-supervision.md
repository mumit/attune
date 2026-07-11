# 06 — Loop supervision + structured logging

**Milestone:** M2 · **Depends on:** none ·
**Fixes roadmap defect #6 (deployment blocker)**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1. Run `pytest`
before and after.

## Problem

Every `run_*_pubsub_loop` in `runtime.py` is a bare `while True` on a daemon
thread with no exception handling. One transient network error, one
`DeadlineExceeded` from an empty pull, one malformed Pub/Sub payload
(`json.loads` on arbitrary bytes), and that ingestion source is dead until a
human notices mail has gone quiet and restarts the process. There is also no
logging anywhere in the package — the always-on process is a black box.

## Task

1. **Logging.** Adopt stdlib `logging` package-wide: a
   `aidedecamp.logging_setup.configure(level, json_mode=False)` helper the
   entrypoint calls (plain human format by default; one-JSON-object-per-line
   when `ADC_LOG_JSON=1`, for journald/Cloud Logging). Add loggers at the
   natural seams — dispatcher decisions (thread triaged/skipped/submitted),
   channel posts, renewals, scheduler firings, loop lifecycle. **Rule 6:**
   never log tokens, credential contents, or full message bodies; subjects
   and ids only.
2. **Supervised loops.** Refactor the four pull loops to share one
   `_pull_loop(name, subscription, handler)` implementation: pull → decode →
   handle per message, where a failing message is logged with its Pub/Sub
   message id, **acked** (see constraint below), and counted, and where
   transport-level exceptions back off exponentially (1s → 60s cap, reset on
   success) instead of killing the thread. The loop must be structured so
   the per-message body is a plain testable function
   (`_handle_pulled_message(...)`) even though the `while True` shell stays
   `pragma: no cover`.
3. **Poison messages:** a message whose handler raises is acked after
   logging, not left to redeliver forever (Pub/Sub redelivery of a
   deterministic failure is an infinite loop). Exception: `HistoryExpired`
   keeps its existing special-case (renew + ack). Record handler failures
   as `"ops"` audit events so silent-drop is still answerable after the fact.
4. **Heartbeat:** a `runtime` logger line every ~5 minutes per live loop
   (messages pulled/handled/failed since last beat) so "is it alive?" is one
   `journalctl | grep heartbeat` away.
5. Wire `configure()` into `__main__.py`; `ADC_LOG_LEVEL` (default INFO).

## Constraints

- No new dependencies (stdlib logging only). No metrics server, no
  Prometheus endpoint — that would be an inbound port (rule 5); logs are the
  observability surface at this scale.
- Keep the testable/live split discipline: decode-and-dispatch logic gets
  offline tests; only the outermost `while True` + real subscriber remains
  uncovered.

## Acceptance

- Offline tests: malformed JSON message → logged, acked, loop continues;
  handler exception → acked + audit event; `HistoryExpired` → force-renew
  preserved; backoff sequence math; heartbeat counters.
- A log-redaction test: feed a fake message with a token-looking body
  through the failure path and assert the body never appears in log records
  (use `caplog`).
- `docs/decisions.md` entry (ack-on-poison rationale, logs-not-metrics
  stance) + CLAUDE.md updates.
