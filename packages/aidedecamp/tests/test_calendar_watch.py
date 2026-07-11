"""Tests for ingestion/calendar_watch.py — Calendar notification channel
lifecycle. No live Google Calendar; the calendar client is a fake exposing
events().watch(...) and channels().stop(...).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aidedecamp.ingestion.calendar_watch import (
    RENEW_WHEN_HOURS_LEFT,
    ChannelResult,
    ensure_calendar_watch,
    stop_calendar_channel,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeState:
    def __init__(self, initial: dict | None = None):
        self._store: dict = dict(initial or {})

    def get(self, calendar_id: str):
        return self._store.get(calendar_id)

    def put(self, calendar_id: str, *, channel_id, resource_id, expiration):
        self._store[calendar_id] = {
            "channel_id": channel_id,
            "resource_id": resource_id,
            "expiration": expiration,
        }


class _FakeCalendarService:
    def __init__(self, *, resource_id="res-1", expire_hours=168):
        self.watch_calls: list[dict] = []
        self.stop_calls: list[dict] = []
        self._resource_id = resource_id
        self._expire_ms = str(
            int((_now() + timedelta(hours=expire_hours)).timestamp() * 1000)
        )

    def events(self):
        svc = self

        class _Events:
            def watch(self, calendarId, body):
                svc.watch_calls.append({"calendarId": calendarId, "body": body})

                class _Req:
                    def execute(self_):
                        return {"resourceId": svc._resource_id, "expiration": svc._expire_ms}
                return _Req()
        return _Events()

    def channels(self):
        svc = self

        class _Channels:
            def stop(self, body):
                svc.stop_calls.append(body)

                class _Req:
                    def execute(self_):
                        return {}
                return _Req()
        return _Channels()


# ---------------------------------------------------------------------------
# ensure_calendar_watch — creation
# ---------------------------------------------------------------------------


def test_creates_channel_when_no_state():
    svc = _FakeCalendarService(resource_id="res-42")
    state = _FakeState()

    result = ensure_calendar_watch(
        svc, state, calendar_id="primary", address="https://republisher/hook",
        channel_id_factory=lambda: "chan-1",
    )

    assert isinstance(result, ChannelResult)
    assert result.renewed is True
    assert result.channel_id == "chan-1"
    assert result.resource_id == "res-42"
    assert len(svc.watch_calls) == 1
    assert svc.watch_calls[0]["body"] == {
        "id": "chan-1", "type": "web_hook", "address": "https://republisher/hook"
    }


def test_state_updated_after_creation():
    svc = _FakeCalendarService()
    state = _FakeState()

    ensure_calendar_watch(
        svc, state, calendar_id="primary", address="https://x/hook",
        channel_id_factory=lambda: "chan-1",
    )
    stored = state.get("primary")

    assert stored["channel_id"] == "chan-1"
    assert isinstance(stored["expiration"], datetime)


def test_default_channel_id_factory_produces_uuid():
    svc = _FakeCalendarService()
    state = _FakeState()

    result = ensure_calendar_watch(svc, state, address="https://x/hook")

    assert len(result.channel_id) == 36  # uuid4 string length


# ---------------------------------------------------------------------------
# ensure_calendar_watch — skip when healthy
# ---------------------------------------------------------------------------


def test_skips_renewal_when_far_from_expiry():
    svc = _FakeCalendarService()
    state = _FakeState({
        "primary": {
            "channel_id": "old-chan",
            "resource_id": "old-res",
            "expiration": _now() + timedelta(hours=RENEW_WHEN_HOURS_LEFT + 24),
        }
    })

    result = ensure_calendar_watch(svc, state, address="https://x/hook")

    assert result.renewed is False
    assert result.channel_id == "old-chan"
    assert svc.watch_calls == []


def test_renews_when_near_expiry():
    svc = _FakeCalendarService()
    state = _FakeState({
        "primary": {
            "channel_id": "old-chan",
            "resource_id": "old-res",
            "expiration": _now() + timedelta(hours=RENEW_WHEN_HOURS_LEFT - 1),
        }
    })

    result = ensure_calendar_watch(
        svc, state, address="https://x/hook", channel_id_factory=lambda: "new-chan"
    )

    assert result.renewed is True
    assert result.channel_id == "new-chan"


def test_force_renews_even_when_healthy():
    svc = _FakeCalendarService()
    state = _FakeState({
        "primary": {
            "channel_id": "old-chan",
            "resource_id": "old-res",
            "expiration": _now() + timedelta(days=6),
        }
    })

    result = ensure_calendar_watch(
        svc, state, address="https://x/hook", force=True,
        channel_id_factory=lambda: "forced-chan",
    )

    assert result.renewed is True
    assert result.channel_id == "forced-chan"


# ---------------------------------------------------------------------------
# ensure_calendar_watch — stops the superseded channel on renewal
# ---------------------------------------------------------------------------


def test_renewal_stops_old_channel():
    svc = _FakeCalendarService()
    state = _FakeState({
        "primary": {
            "channel_id": "old-chan",
            "resource_id": "old-res",
            "expiration": _now() + timedelta(hours=RENEW_WHEN_HOURS_LEFT - 1),
        }
    })

    ensure_calendar_watch(
        svc, state, address="https://x/hook", channel_id_factory=lambda: "new-chan"
    )

    assert svc.stop_calls == [{"id": "old-chan", "resourceId": "old-res"}]


def test_no_stop_call_on_first_creation():
    svc = _FakeCalendarService()
    state = _FakeState()

    ensure_calendar_watch(svc, state, address="https://x/hook")

    assert svc.stop_calls == []


# ---------------------------------------------------------------------------
# ensure_calendar_watch — string expiration in state
# ---------------------------------------------------------------------------


def test_parses_epoch_ms_string_expiration_in_state():
    svc = _FakeCalendarService()
    exp_ms = str(int((_now() + timedelta(days=6)).timestamp() * 1000))
    state = _FakeState({
        "primary": {"channel_id": "c1", "resource_id": "r1", "expiration": exp_ms}
    })

    result = ensure_calendar_watch(svc, state, address="https://x/hook")

    assert result.renewed is False
    assert svc.watch_calls == []


# ---------------------------------------------------------------------------
# stop_calendar_channel
# ---------------------------------------------------------------------------


def test_stop_calendar_channel_calls_stop():
    svc = _FakeCalendarService()
    stop_calendar_channel(svc, channel_id="c1", resource_id="r1")
    assert svc.stop_calls == [{"id": "c1", "resourceId": "r1"}]
