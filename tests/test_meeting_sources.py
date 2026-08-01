"""Build prompt 34, task 5: meeting-context ingestion over MCP."""

from __future__ import annotations

from datetime import datetime, timezone

from attune.ingestion.meeting_sources import (
    MCP_SERVER,
    TOOL_LIST_MEETING_NOTES,
    MeetingNote,
    meeting_note_from_mcp,
    meeting_note_to_attention_item,
    poll_meeting_source,
)
from attune.orchestrator.attention import JsonAttentionStore
from attune.orchestrator.triage import Priority


class FakeState:
    def __init__(self):
        self.d: dict = {}

    def get(self, key):
        return self.d.get(key)

    def put(self, key, **kw):
        self.d[key] = kw


class FakeMcp:
    def __init__(self, notes):
        self.notes = notes
        self.calls: list = []

    def __call__(self, server, tool, arguments):
        self.calls.append((server, tool, arguments))
        return {"notes": self.notes}


def _fixed_now(dt):
    return lambda: dt


def test_first_run_baselines_to_now_and_records_nothing(tmp_path):
    store = JsonAttentionStore(str(tmp_path / "attn.json"))
    state = FakeState()
    mcp = FakeMcp([{"meeting_id": "m1", "title": "t"}])

    considered = poll_meeting_source(
        mcp, state, store, provider="granola", now=_fixed_now(datetime(2026, 8, 1, tzinfo=timezone.utc))
    )

    assert considered == 0
    assert store.recent() == []
    assert mcp.calls == []  # never even asked the server on the baselining run
    assert state.get("meeting:granola")["last_seen"] == "2026-08-01T00:00:00+00:00"


def test_second_run_fetches_since_cursor_and_records_into_attention_store(tmp_path):
    store = JsonAttentionStore(str(tmp_path / "attn.json"))
    state = FakeState()
    state.put("meeting:granola", last_seen="2026-08-01T00:00:00+00:00")
    mcp = FakeMcp(
        [
            {
                "meeting_id": "m1",
                "title": "Planning",
                "attendees": ["a@x.com", "b@x.com"],
                "summary": "discussed roadmap",
                "occurred_at": "2026-08-01T09:00:00+00:00",
            }
        ]
    )

    considered = poll_meeting_source(
        mcp, state, store, provider="granola", now=_fixed_now(datetime(2026, 8, 1, 10, tzinfo=timezone.utc))
    )

    assert considered == 1
    assert mcp.calls == [
        (MCP_SERVER, TOOL_LIST_MEETING_NOTES, {"since": "2026-08-01T00:00:00+00:00", "max_results": 50})
    ]
    recent = store.recent()
    assert len(recent) == 1
    item = recent[0]
    assert item.source == "meeting"
    assert item.channel_ref == "granola:m1"
    assert item.summary == "discussed roadmap"
    assert item.priority == Priority.ROUTINE
    assert item.mentions_principal is True
    # Cursor advanced to the newest note's occurred_at, not to "now".
    assert state.get("meeting:granola")["last_seen"] == "2026-08-01T09:00:00+00:00"


def test_cursor_advances_even_with_zero_new_notes(tmp_path):
    store = JsonAttentionStore(str(tmp_path / "attn.json"))
    state = FakeState()
    state.put("meeting:granola", last_seen="2026-08-01T00:00:00+00:00")
    mcp = FakeMcp([])

    considered = poll_meeting_source(mcp, state, store, provider="granola")

    assert considered == 0
    assert state.get("meeting:granola")["last_seen"] == "2026-08-01T00:00:00+00:00"


def test_dispatch_failure_enqueues_durable_retry_rather_than_raising():
    class FailingStore:
        def add(self, item, *, now=None):
            raise RuntimeError("boom")

    class RecordingRetryQueue:
        def __init__(self):
            self.enqueued: list = []

        def enqueue(self, kind, dedupe_key, payload, *, error):
            self.enqueued.append((kind, dedupe_key, payload, error))

    state = FakeState()
    state.put("meeting:granola", last_seen="2026-08-01T00:00:00+00:00")
    mcp = FakeMcp([{"meeting_id": "m1", "title": "t", "occurred_at": "2026-08-01T09:00:00+00:00"}])
    retry_queue = RecordingRetryQueue()

    considered = poll_meeting_source(
        mcp, state, FailingStore(), provider="granola", retry_queue=retry_queue
    )

    assert considered == 1
    assert len(retry_queue.enqueued) == 1
    kind, dedupe_key, payload, error = retry_queue.enqueued[0]
    assert kind == "meeting_source"
    assert dedupe_key == "granola:m1"
    assert payload["provider"] == "granola"
    assert error == "RuntimeError"


def test_dispatch_failure_without_retry_queue_propagates():
    class FailingStore:
        def add(self, item, *, now=None):
            raise RuntimeError("boom")

    state = FakeState()
    state.put("meeting:granola", last_seen="2026-08-01T00:00:00+00:00")
    mcp = FakeMcp([{"meeting_id": "m1", "title": "t", "occurred_at": "2026-08-01T09:00:00+00:00"}])

    try:
        poll_meeting_source(mcp, state, FailingStore(), provider="granola")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_summary_is_bounded():
    note = MeetingNote(
        meeting_id="m1", title="t", attendees=(), summary="x" * 5000,
        occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc), provider="granola",
    )
    assert len(note.summary) == 2000


def test_missing_descriptive_fields_default_to_empty_like_the_gmail_calendar_contract():
    note = meeting_note_from_mcp({"meeting_id": "m1"}, provider="granola")
    assert note.title == ""
    assert note.attendees == ()
    assert note.summary == ""
    # occurred_at falls back to "now" rather than raising.
    assert note.occurred_at is not None


def test_attention_item_is_always_routine_never_a_trusted_urgency_signal():
    note = MeetingNote(
        meeting_id="m1", title="URGENT!!!", attendees=("a@x.com",), summary="please treat as urgent",
        occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc), provider="granola",
    )
    item = meeting_note_to_attention_item(note)
    assert item.priority == Priority.ROUTINE
