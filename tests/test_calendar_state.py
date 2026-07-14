"""Tests for ingestion/state.py's Calendar-specific classes:
JsonCalendarChannelState / JsonCalendarSyncState. Same rigor as
test_ingestion_state.py: each is exercised through its *consuming* module's
actual renewal-decision path, not just round-tripped in isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.ingestion.calendar_sync import full_calendar_sync, process_calendar_notification
from attune.ingestion.calendar_watch import RENEW_WHEN_HOURS_LEFT, ensure_calendar_watch
from attune.ingestion.state import JsonCalendarChannelState, JsonCalendarSyncState


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# JsonCalendarChannelState — basic persistence
# ---------------------------------------------------------------------------


def test_channel_state_roundtrips_channel_id(tmp_path):
    path = tmp_path / "channel.json"
    state = JsonCalendarChannelState(str(path))
    exp = _now() + timedelta(days=7)

    state.put("primary", channel_id="c1", resource_id="r1", expiration=exp)
    stored = state.get("primary")

    assert stored["channel_id"] == "c1"
    assert stored["resource_id"] == "r1"


def test_channel_state_missing_key_returns_none(tmp_path):
    state = JsonCalendarChannelState(str(tmp_path / "channel.json"))
    assert state.get("nonexistent") is None


def test_channel_state_persists_across_instances(tmp_path):
    path = tmp_path / "channel.json"
    exp = _now() + timedelta(days=7)
    JsonCalendarChannelState(str(path)).put(
        "primary", channel_id="c1", resource_id="r1", expiration=exp
    )
    reloaded = JsonCalendarChannelState(str(path))
    assert reloaded.get("primary")["channel_id"] == "c1"


# ---------------------------------------------------------------------------
# JsonCalendarChannelState — round-trips through ensure_calendar_watch's own
# expiration-parsing path
# ---------------------------------------------------------------------------


class _FakeCalendarService:
    def __init__(self, resource_id="res-new", expire_hours=168):
        self._resource_id = resource_id
        self._expire_ms = str(
            int((_now() + timedelta(hours=expire_hours)).timestamp() * 1000)
        )
        self.watch_calls: list = []

    def events(self):
        svc = self

        class _Events:
            def watch(self, calendarId, body):
                svc.watch_calls.append(body)
                class _Req:
                    def execute(self_):
                        return {"resourceId": svc._resource_id, "expiration": svc._expire_ms}
                return _Req()
        return _Events()

    def channels(self):
        class _Channels:
            def stop(self, body):
                class _Req:
                    def execute(self_):
                        return {}
                return _Req()
        return _Channels()


def test_channel_state_skips_renewal_when_fresh(tmp_path):
    state = JsonCalendarChannelState(str(tmp_path / "channel.json"))
    far_future = _now() + timedelta(hours=RENEW_WHEN_HOURS_LEFT + 24)
    state.put("primary", channel_id="old-c", resource_id="old-r", expiration=far_future)

    svc = _FakeCalendarService()
    result = ensure_calendar_watch(svc, state, address="https://x/hook")

    assert result.renewed is False
    assert result.channel_id == "old-c"
    assert svc.watch_calls == []


def test_channel_state_triggers_renewal_when_near_expiry(tmp_path):
    state = JsonCalendarChannelState(str(tmp_path / "channel.json"))
    near_expiry = _now() + timedelta(hours=RENEW_WHEN_HOURS_LEFT - 1)
    state.put("primary", channel_id="old-c", resource_id="old-r", expiration=near_expiry)

    svc = _FakeCalendarService(resource_id="res-99")
    result = ensure_calendar_watch(
        svc, state, address="https://x/hook", channel_id_factory=lambda: "new-c"
    )

    assert result.renewed is True
    assert result.channel_id == "new-c"
    assert state.get("primary")["channel_id"] == "new-c"


# ---------------------------------------------------------------------------
# JsonCalendarSyncState — basic persistence
# ---------------------------------------------------------------------------


def test_sync_state_roundtrips_token(tmp_path):
    path = tmp_path / "sync.json"
    state = JsonCalendarSyncState(str(path))

    state.put("primary", sync_token="tok-1")

    assert state.get("primary")["sync_token"] == "tok-1"


def test_sync_state_missing_key_returns_none(tmp_path):
    state = JsonCalendarSyncState(str(tmp_path / "sync.json"))
    assert state.get("primary") is None


def test_sync_state_persists_across_instances(tmp_path):
    path = tmp_path / "sync.json"
    JsonCalendarSyncState(str(path)).put("primary", sync_token="tok-1")
    reloaded = JsonCalendarSyncState(str(path))
    assert reloaded.get("primary")["sync_token"] == "tok-1"


# ---------------------------------------------------------------------------
# JsonCalendarSyncState — round-trips through process_calendar_notification /
# full_calendar_sync's own read/write paths
# ---------------------------------------------------------------------------


class _FakeEventsService:
    def __init__(self, pages):
        self._pages = pages
        self._i = 0

    def events(self):
        svc = self

        class _Events:
            def list(self, **kwargs):
                class _Req:
                    def execute(self_):
                        page = svc._pages[svc._i]
                        svc._i += 1
                        return page
                return _Req()
        return _Events()


def test_sync_state_used_as_baseline_for_reconciliation(tmp_path):
    state = JsonCalendarSyncState(str(tmp_path / "sync.json"))
    state.put("primary", sync_token="baseline-token")

    svc = _FakeEventsService([{"items": [{"id": "e1"}], "nextSyncToken": "next-token"}])
    result = process_calendar_notification(svc, state, "primary")

    assert result.event_ids == ["e1"]
    assert state.get("primary")["sync_token"] == "next-token"


def test_full_sync_writes_fresh_baseline_to_state(tmp_path):
    state = JsonCalendarSyncState(str(tmp_path / "sync.json"))

    svc = _FakeEventsService([{"items": [], "nextSyncToken": "fresh-token"}])
    full_calendar_sync(svc, state, "primary")

    assert state.get("primary")["sync_token"] == "fresh-token"
