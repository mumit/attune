"""Tests for ingestion/calendar_sync.py — incremental sync reconciliation.

No live Google Calendar; the calendar client is a fake exposing
events().list(...) with syncToken/pageToken support.
"""

from __future__ import annotations

import pytest

from aidedecamp.ingestion.calendar_sync import (
    CalendarChanges,
    SyncExpired,
    decode_calendar_headers,
    full_calendar_sync,
    process_calendar_notification,
)


class _FakeSyncState:
    def __init__(self, initial: dict | None = None):
        self._store: dict = dict(initial or {})

    def get(self, calendar_id: str):
        return self._store.get(calendar_id)

    def put(self, calendar_id: str, *, sync_token: str):
        self._store[calendar_id] = {"sync_token": sync_token}


class _Http410(Exception):
    status_code = 410


class _FakeCalendarService:
    """One or more pages of events().list results, keyed by page_token."""

    def __init__(self, pages: list[dict], *, raise_410_on_call: bool = False):
        self._pages = pages
        self._call_index = 0
        self._raise_410 = raise_410_on_call
        self.list_calls: list[dict] = []

    def events(self):
        svc = self

        class _Events:
            def list(self, **kwargs):
                svc.list_calls.append(kwargs)
                if svc._raise_410:
                    raise _Http410("sync token expired")

                class _Req:
                    def execute(self_):
                        page = svc._pages[svc._call_index]
                        svc._call_index += 1
                        return page
                return _Req()
        return _Events()


# ---------------------------------------------------------------------------
# process_calendar_notification — no baseline / expired
# ---------------------------------------------------------------------------


def test_no_stored_sync_token_raises_sync_expired():
    state = _FakeSyncState()
    svc = _FakeCalendarService(pages=[])

    with pytest.raises(SyncExpired):
        process_calendar_notification(svc, state, "primary")


def test_410_raises_sync_expired():
    state = _FakeSyncState({"primary": {"sync_token": "stale-token"}})
    svc = _FakeCalendarService(pages=[], raise_410_on_call=True)

    with pytest.raises(SyncExpired):
        process_calendar_notification(svc, state, "primary")


def test_non_410_error_propagates():
    class _FakeGenericError(Exception):
        status_code = 500

    class _RaisingService:
        def events(self):
            class _Events:
                def list(self, **kwargs):
                    raise _FakeGenericError("boom")
            return _Events()

    state = _FakeSyncState({"primary": {"sync_token": "t1"}})
    with pytest.raises(Exception, match="boom"):
        process_calendar_notification(_RaisingService(), state, "primary")


# ---------------------------------------------------------------------------
# process_calendar_notification — successful reconcile
# ---------------------------------------------------------------------------


def test_reconciles_using_stored_baseline():
    state = _FakeSyncState({"primary": {"sync_token": "old-token"}})
    svc = _FakeCalendarService(pages=[
        {"items": [{"id": "e1"}, {"id": "e2"}], "nextSyncToken": "new-token"}
    ])

    result = process_calendar_notification(svc, state, "primary")

    assert isinstance(result, CalendarChanges)
    assert result.event_ids == ["e1", "e2"]
    assert result.next_sync_token == "new-token"
    assert svc.list_calls[0]["syncToken"] == "old-token"


def test_advances_stored_token_on_success():
    state = _FakeSyncState({"primary": {"sync_token": "old-token"}})
    svc = _FakeCalendarService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "new-token"}
    ])

    process_calendar_notification(svc, state, "primary")

    assert state.get("primary")["sync_token"] == "new-token"


def test_dedupes_event_ids_across_pages():
    state = _FakeSyncState({"primary": {"sync_token": "t0"}})
    svc = _FakeCalendarService(pages=[
        {"items": [{"id": "e1"}, {"id": "e2"}], "nextPageToken": "p2"},
        {"items": [{"id": "e2"}, {"id": "e3"}], "nextSyncToken": "t1"},
    ])

    result = process_calendar_notification(svc, state, "primary")

    assert result.event_ids == ["e1", "e2", "e3"]


def test_includes_cancelled_events():
    state = _FakeSyncState({"primary": {"sync_token": "t0"}})
    svc = _FakeCalendarService(pages=[
        {
            "items": [
                {"id": "e1", "status": "confirmed"},
                {"id": "e2", "status": "cancelled"},
            ],
            "nextSyncToken": "t1",
        }
    ])

    result = process_calendar_notification(svc, state, "primary")

    assert "e2" in result.event_ids


# ---------------------------------------------------------------------------
# full_calendar_sync
# ---------------------------------------------------------------------------


def test_full_sync_does_not_pass_sync_token():
    state = _FakeSyncState()
    svc = _FakeCalendarService(pages=[
        {"items": [{"id": "e1"}], "nextSyncToken": "fresh-token"}
    ])

    result = full_calendar_sync(svc, state, "primary")

    assert result.next_sync_token == "fresh-token"
    assert "syncToken" not in svc.list_calls[0]


def test_full_sync_stores_fresh_baseline():
    state = _FakeSyncState()
    svc = _FakeCalendarService(pages=[
        {"items": [], "nextSyncToken": "fresh-token"}
    ])

    full_calendar_sync(svc, state, "primary")

    assert state.get("primary")["sync_token"] == "fresh-token"


def test_full_sync_paginates_to_final_sync_token():
    state = _FakeSyncState()
    svc = _FakeCalendarService(pages=[
        {"items": [{"id": "e1"}], "nextPageToken": "p2"},
        {"items": [{"id": "e2"}], "nextSyncToken": "final-token"},
    ])

    result = full_calendar_sync(svc, state, "primary")

    assert result.event_ids == ["e1", "e2"]
    assert result.next_sync_token == "final-token"


# ---------------------------------------------------------------------------
# decode_calendar_headers
# ---------------------------------------------------------------------------


def test_decode_calendar_headers_extracts_fields():
    headers = {
        "X-Goog-Channel-ID": "chan-1",
        "X-Goog-Resource-ID": "res-1",
        "X-Goog-Resource-State": "exists",
        "X-Goog-Message-Number": "42",
        "Other-Header": "ignored",
    }
    decoded = decode_calendar_headers(headers)
    assert decoded == {
        "channel_id": "chan-1",
        "resource_id": "res-1",
        "resource_state": "exists",
        "message_number": "42",
    }


def test_decode_calendar_headers_missing_fields_default_empty():
    decoded = decode_calendar_headers({})
    assert decoded == {
        "channel_id": "",
        "resource_id": "",
        "resource_state": "",
        "message_number": "",
    }
