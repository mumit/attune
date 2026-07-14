"""Backend-neutral polling for WorkspaceConnector implementations."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any


class JsonWorkspacePollState:
    def __init__(self, path: str):
        self.path = path

    def get(self) -> dict[str, Any]:
        try:
            with open(self.path) as fh:
                value = json.load(fh)
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    def put(self, value: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".workspace-poll-", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(value, fh, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _thread_mark(thread: Any) -> str:
    when = getattr(thread, "last_message_at", None)
    return "|".join((
        getattr(thread, "thread_id", ""),
        when.isoformat() if when else "",
        getattr(thread, "last_from_addr", ""),
        getattr(thread, "snippet", ""),
    ))


def _event_mark(event: Any) -> str:
    return "|".join((
        getattr(event, "event_id", ""),
        getattr(event, "start", datetime.min).isoformat(),
        getattr(event, "end", datetime.min).isoformat(),
        getattr(event, "summary", ""),
    ))


def poll_workspace_connector(connector, state: JsonWorkspacePollState, *, now=None):
    """Return changed threads/events, baselining silently on the first call."""
    now = now or datetime.now(timezone.utc)
    threads = connector.list_threads("is:unread", max_results=50)
    events = connector.list_events(time_min=now, time_max=now + timedelta(days=7))
    old = state.get()
    thread_marks = {t.thread_id: _thread_mark(t) for t in threads}
    event_marks = {e.event_id: _event_mark(e) for e in events}
    state.put({"initialized": True, "threads": thread_marks, "events": event_marks})
    if not old.get("initialized"):
        return [], []
    changed_threads = [t for t in threads if old.get("threads", {}).get(t.thread_id) != thread_marks[t.thread_id]]
    changed_events = [e for e in events if old.get("events", {}).get(e.event_id) != event_marks[e.event_id]]
    return changed_threads, changed_events
