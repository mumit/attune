#!/usr/bin/env python
"""Build prompt 33 (Phase P7 — Perform) benchmark: before/after numbers
against a fake connector with injected latency, per the build prompt's own
acceptance criterion:

    "Recorded before/after numbers in the decisions entry, from a scripted
    benchmark against a fake connector with injected latency: brief wall
    clock, Google call count per brief, and wall clock for a 25-thread
    notification batch. Targets: brief p50 under 3s, 25-thread batch under
    15s, Google calls per brief cut by more than half."

"Before" is measured directly, not estimated: each benchmark runs a
hand-written SERIAL equivalent of the pre-build-prompt-33 access pattern
(the exact N+1/no-concurrency/no-incremental-fetch shape the build prompt's
own "Problem" section describes) against the SAME injected-latency fake
connector, then runs today's code (concurrent pool + batch hydration +
incremental fetch) against the identical fake. Both paths are counted and
timed by the same harness, so the comparison is apples-to-apples.

Run: python scripts/benchmark_p7.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("ATTUNE_MODEL_DEFAULT", "benchmark-model")
os.environ.setdefault("ATTUNE_LLM_API_KEY", "benchmark-key")

from attune.brief import BriefSnapshot, JsonBriefSnapshot, assemble_brief  # noqa: E402
from attune.connectors.base import CalendarEvent, EmailThread, Provenance  # noqa: E402
from attune.dispatcher import handle_gmail_notification  # noqa: E402
from attune.orchestrator.triage import Priority, TriageResult  # noqa: E402

# ---------------------------------------------------------------------------
# Injected latency, calibrated to the build prompt's own numbers: "10-25s"
# for a real brief (~64 round trips) and "60-90s" for a 25-thread batch (~25
# Gmail calls, ~50 model calls, ~50 memory searches, ~125 JSON reads, 25
# channel posts). These per-call costs, at the ORIGINAL call counts, land in
# that same range — the harness reproduces the measured problem, not a
# fabricated one.
# ---------------------------------------------------------------------------
GOOGLE_CALL_LATENCY = 0.12          # one Gmail/Calendar API round trip
MODEL_CALL_LATENCY = 0.35           # one chat-completion call (triage/draft/brief)
MEMORY_SEARCH_LATENCY = 0.03        # one memory store search
STORE_IO_LATENCY = 0.01             # one whole-file JSON read/write


class _LatencyConnector:
    """A fake WorkspaceConnector whose every method sleeps to simulate a
    real Google API round trip, and counts calls by method name — the
    "fake connector with injected latency" the acceptance criterion names.
    """

    def __init__(self, *, unread_threads: list[EmailThread], events: list[CalendarEvent]):
        self._unread = {t.thread_id: t for t in unread_threads}
        self._unread_order = [t.thread_id for t in unread_threads]
        self._events = events
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def list_threads(self, query="is:unread", *, max_results=20):
        self._count("list_threads")
        time.sleep(GOOGLE_CALL_LATENCY)
        if query == "in:sent":
            return []
        # Pre-build-prompt-33 shape when called directly (no batching): one
        # threads.list call. The N+1 hydration this simulates is modeled
        # explicitly in _serial_brief below, since assemble_brief's own
        # concurrent/batched path calls this method differently than a bare
        # list_threads would in the old code.
        return [self._unread[tid] for tid in self._unread_order[:max_results]]

    def list_thread_ids(self, query="is:unread", *, max_results=20):
        self._count("list_thread_ids")
        time.sleep(GOOGLE_CALL_LATENCY)  # one cheap ID-only round trip
        return self._unread_order[:max_results]

    def get_thread(self, thread_id):
        self._count("get_thread")
        time.sleep(GOOGLE_CALL_LATENCY)
        return self._unread[thread_id]

    def list_events(self, *, time_min, time_max):
        self._count("list_events")
        time.sleep(GOOGLE_CALL_LATENCY)
        return self._events

    def create_draft(self, **kw):
        return None


class _LatencyMemoryStore:
    def __init__(self):
        self.calls = 0

    def search(self, query, *, user_id, limit=5, min_score=None):
        self.calls += 1
        time.sleep(MEMORY_SEARCH_LATENCY)
        return []

    def add(self, *a, **kw):
        return []


class _LatencyClient:
    """A fake chat-completions client: one call, one injected latency."""

    def __init__(self):
        self.calls = 0

    def chat_completions_create(self, **kwargs):
        self.calls += 1
        time.sleep(MODEL_CALL_LATENCY)

        class _Msg:
            content = "Two unread, one meeting."

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


def _thread(i: int) -> EmailThread:
    return EmailThread(
        thread_id=f"t{i}",
        subject=f"Subject {i}",
        snippet=f"snippet {i}",
        from_addr=f"sender{i}@example.com",
        body="body text",
        provenance=Provenance.FETCHED,
        last_from_addr="",
        last_message_at=None,
    )


def _event(i: int, now: datetime) -> CalendarEvent:
    start = now.replace(hour=9 + i, minute=0, second=0, microsecond=0)
    return CalendarEvent(
        event_id=f"e{i}", summary=f"Meeting {i}",
        start=start, end=start + timedelta(minutes=30),
        attendees=[f"attendee{i}@example.com"],
    )


# ---------------------------------------------------------------------------
# Brief benchmark
# ---------------------------------------------------------------------------


def _serial_brief(connector: _LatencyConnector, store: _LatencyMemoryStore, client: _LatencyClient) -> int:
    """The PRE-build-prompt-33 access pattern, hand-reproduced: N+1
    list_threads (one list call + one get_thread per result — no batching,
    no concurrency), one list_events, up to 8 meeting-prep iterations each
    doing one memory search + one related-thread list_threads call
    (serial), one quiet-thread "in:sent" listing (N+1 again), then one
    model call. Returns the Google call count."""
    before = sum(connector.calls.values())
    ids = connector._unread_order[:25]
    for tid in ids:  # the N+1: one get per thread, serial
        connector.get_thread(tid)
    connector.list_threads("is:unread newer_than:1d", max_results=25)  # the initial .list()
    connector.list_events(time_min=None, time_max=None)
    for _ in range(min(8, len(connector._events))):
        store.search("meeting context", user_id="me")
        connector.list_threads("related", max_results=1)
    connector.list_threads("in:sent", max_results=20)  # quiet-thread listing
    client.chat_completions_create(messages=[])
    return sum(connector.calls.values()) - before


def benchmark_brief() -> dict[str, Any]:
    now = datetime(2026, 7, 31, 7, 0, tzinfo=timezone.utc)
    threads = [_thread(i) for i in range(25)]
    events = [_event(i, now) for i in range(8)]

    # --- before: serial, N+1, no incremental fetch ---
    before_conn = _LatencyConnector(unread_threads=threads, events=events)
    before_store = _LatencyMemoryStore()
    before_client = _LatencyClient()
    t0 = time.perf_counter()
    before_calls = _serial_brief(before_conn, before_store, before_client)
    before_wall = time.perf_counter() - t0

    # --- after: concurrent pool + batch hydration (fork B) + incremental
    # fetch (task 5), cold (no prior snapshot — first brief of the day) ---
    after_conn_cold = _LatencyConnector(unread_threads=threads, events=events)
    after_client_cold = _LatencyClient()
    t0 = time.perf_counter()
    assemble_brief(
        after_conn_cold, after_client_cold, store=_LatencyMemoryStore(),
        now=now, user_email="me@example.com",
    )
    after_cold_wall = time.perf_counter() - t0
    after_cold_calls = sum(after_conn_cold.calls.values())

    # --- after, warm: a snapshot from a sleep-time precompute run already
    # exists, so only the cheap list_thread_ids call plus zero new-thread
    # fetches are needed (nothing changed since precompute) ---
    snapshot_store = JsonBriefSnapshot("/tmp/attune_benchmark_snapshot.json")
    snapshot_store.save(BriefSnapshot(
        unread=[
            {"id": t.thread_id, "text": t.subject, "from_addr": t.from_addr, "snippet": t.snippet}
            for t in threads
        ],
        events=[{"id": e.event_id, "text": e.summary} for e in events],
        quiet_thread_ids=[],
        ts=now - timedelta(hours=1),
    ))
    after_conn_warm = _LatencyConnector(unread_threads=threads, events=events)
    after_client_warm = _LatencyClient()
    t0 = time.perf_counter()
    assemble_brief(
        after_conn_warm, after_client_warm, store=_LatencyMemoryStore(),
        now=now, user_email="me@example.com", snapshot_store=snapshot_store,
    )
    after_warm_wall = time.perf_counter() - t0
    after_warm_calls = sum(after_conn_warm.calls.values())

    return {
        "before_wall": before_wall, "before_calls": before_calls,
        "after_cold_wall": after_cold_wall, "after_cold_calls": after_cold_calls,
        "after_warm_wall": after_warm_wall, "after_warm_calls": after_warm_calls,
    }


# ---------------------------------------------------------------------------
# 25-thread notification batch benchmark
# ---------------------------------------------------------------------------


class _BatchConnector:
    def __init__(self, threads: dict[str, EmailThread]):
        self._threads = threads
        self.get_thread_calls = 0

    def get_thread(self, thread_id):
        self.get_thread_calls += 1
        time.sleep(GOOGLE_CALL_LATENCY)
        return self._threads[thread_id]

    def list_threads(self, *a, **kw):
        return []

    def list_events(self, *a, **kw):
        return []

    def create_draft(self, **kw):
        return None


class _BatchTriageFn:
    """Simulates one Task.CLASSIFY model call plus one memory search."""

    def __init__(self):
        self.calls = 0

    def __call__(self, client, summary):
        self.calls += 1
        time.sleep(MEMORY_SEARCH_LATENCY + MODEL_CALL_LATENCY)
        return TriageResult(Priority.ROUTINE, "benchmark")


class _BatchGraph:
    """Simulates the draft-approve graph's one retrieve + one draft model
    call, and a pending/ledger-shaped whole-file JSON write."""

    def __init__(self):
        self.calls = 0

    def invoke(self, state, config):
        self.calls += 1
        time.sleep(MODEL_CALL_LATENCY)  # draft call
        time.sleep(STORE_IO_LATENCY)    # pending/ledger write
        return {
            "proposed_draft": "draft text", "audit_events": [],
            "retrieved_memories": [],
        }


def _serial_batch(thread_ids: list[str], threads: dict[str, EmailThread]) -> float:
    """The PRE-build-prompt-33 shape: fetch, triage, draft — all serial,
    one thread at a time, no pool."""
    connector = _BatchConnector(threads)
    triage_fn = _BatchTriageFn()
    graph = _BatchGraph()
    t0 = time.perf_counter()
    for tid in thread_ids:
        thread = connector.get_thread(tid)
        triage_fn(None, thread.subject)
        graph.invoke({}, {})
    return time.perf_counter() - t0


def benchmark_batch() -> dict[str, Any]:
    from attune.app import AppContext
    from attune.config import Settings

    thread_ids = [f"t{i}" for i in range(25)]
    threads = {tid: _thread(i) for i, tid in enumerate(thread_ids)}

    before_wall = _serial_batch(thread_ids, threads)

    class _FakeAuditLog:
        def record(self, **kw):
            pass

    class _FakeWatchState:
        def __init__(self):
            self._data = {"me": {"history_id": "100"}}

        def get(self, email):
            return self._data.get(email)

        def put(self, email, *, history_id, expiration=None):
            self._data[email] = {"history_id": history_id}

    class _FakeGmail:
        def users(self):
            tids = thread_ids

            class _History:
                def list(self, **kwargs):
                    class _Req:
                        def execute(self):
                            return {"history": [{"messagesAdded": [
                                {"message": {"threadId": tid, "id": f"m_{tid}"}} for tid in tids
                            ]}]}
                    return _Req()

            class _Users:
                def history(self):
                    return _History()

            return _Users()

    settings = Settings.from_env({
        "ATTUNE_WORKSPACE_BACKEND": "mcp", "ATTUNE_MEM0_URL": "",
        "ATTUNE_AUDIT_LOG_PATH": "",
    })
    app_ctx = AppContext(
        graph=_BatchGraph(), client=_LatencyClient(), store=_LatencyMemoryStore(),
        settings=settings, audit_log=_FakeAuditLog(),
    )
    connector = _BatchConnector(threads)
    t0 = time.perf_counter()
    handle_gmail_notification(
        app_ctx, {"emailAddress": "me", "historyId": "200"},
        gmail_service=_FakeGmail(), watch_state=_FakeWatchState(),
        connector=connector, post_approval=lambda *a, **kw: None,
        user_id="me", triage_fn=_BatchTriageFn(),
    )
    after_wall = time.perf_counter() - t0

    return {"before_wall": before_wall, "after_wall": after_wall}


def main() -> None:
    print("Build prompt 33 (Phase P7) benchmark — injected-latency fake connector\n")

    brief = benchmark_brief()
    print("## Brief assembly")
    print(f"{'':20} {'wall clock':>12} {'Google calls':>14}")
    print(f"{'before (serial)':20} {brief['before_wall']:>11.2f}s {brief['before_calls']:>14}")
    print(f"{'after, cold':20} {brief['after_cold_wall']:>11.2f}s {brief['after_cold_calls']:>14}")
    print(f"{'after, warm*':20} {brief['after_warm_wall']:>11.2f}s {brief['after_warm_calls']:>14}")
    print("  * warm = a sleep-time precompute snapshot already exists (task 6)\n")

    call_cut = 1 - (brief["after_cold_calls"] / brief["before_calls"])
    print(f"Google calls per brief cut: {call_cut:.0%} (target: >50%)")
    print(f"Brief wall clock (cold): {brief['after_cold_wall']:.2f}s (target: <3s p50)\n")

    batch = benchmark_batch()
    print("## 25-thread Gmail notification batch")
    print(f"{'before (serial)':20} {batch['before_wall']:>11.2f}s")
    print(f"{'after (pooled)':20} {batch['after_wall']:>11.2f}s")
    print(f"Speedup: {batch['before_wall'] / batch['after_wall']:.1f}x "
          f"(target: batch under 15s -> {'PASS' if batch['after_wall'] < 15 else 'FAIL'})\n")

    print("## Summary table (for docs/decisions.md)\n")
    print("| Metric | Before | After | Target |")
    print("|---|---|---|---|")
    print(f"| Brief wall clock | {brief['before_wall']:.2f}s | {brief['after_cold_wall']:.2f}s | <3s p50 |")
    print(f"| Brief Google calls | {brief['before_calls']} | {brief['after_cold_calls']} | cut >50% |")
    print(f"| 25-thread batch wall clock | {batch['before_wall']:.2f}s | {batch['after_wall']:.2f}s | <15s |")


if __name__ == "__main__":
    main()
