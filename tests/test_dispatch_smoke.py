from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from attune.hosted.dispatch_smoke import _origin, _row_tuple, _wait_for_success
from attune.hosted.tenant import TenantContext


class Jobs:
    def __init__(self, states):
        self.states = iter(states)

    def get(self, context, job_id):
        return SimpleNamespace(state=next(self.states))


def test_dispatch_smoke_accepts_only_https_origins():
    assert _origin("https://broker.example.run.app/", "url") == (
        "https://broker.example.run.app"
    )
    for value in (
        "http://broker.example.run.app",
        "https://user@broker.example.run.app",
        "https://broker.example.run.app/path",
        "https://broker.example.run.app?next=other",
    ):
        with pytest.raises(ValueError):
            _origin(value, "url")


def test_dispatch_smoke_normalizes_dbapi_rows():
    assert _row_tuple(["slug", "region", "active"]) == (
        "slug",
        "region",
        "active",
    )
    assert _row_tuple(("slug", "region", "active")) == (
        "slug",
        "region",
        "active",
    )
    assert _row_tuple(None) is None


def test_dispatch_smoke_fails_on_ambiguous_terminal_state():
    context = TenantContext(UUID("10000000-0000-4000-8000-000000000721"))
    job_id = UUID("10000000-0000-4000-8000-000000000722")
    with pytest.raises(RuntimeError):
        _wait_for_success(
            Jobs(["reconcile"]),
            context,
            job_id,
            timeout_seconds=1,
        )
