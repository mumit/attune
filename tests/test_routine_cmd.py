"""Tests for ``attune routine`` (cli/routine_cmd.py, build prompt 32, task 1)."""

from __future__ import annotations

from attune.cli.routine_cmd import (
    run_routine_add,
    run_routine_list,
    run_routine_remove,
    run_routine_run,
    run_routine_show,
)
from attune.config import Settings


class _Client:
    def __init__(self, reply: str):
        self.reply = reply

    def chat_completions_create(self, **kwargs):
        class _Message:
            content = self.reply

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _settings(tmp_path):
    return Settings.from_env({
        "ATTUNE_ROUTINE_STATE_PATH": str(tmp_path / "routines.json"),
    })


def test_add_list_show_remove_round_trip(tmp_path):
    settings = _settings(tmp_path)
    client = _Client("INTENT: MAIL\nGMAIL_QUERY: is:unread\nSTART: NONE\nEND: NONE")
    lines: list[str] = []

    code = run_routine_add(
        "unresolved threads from HIGH-tier senders", schedule="weekday 08:00",
        name="hightier", settings=settings, client=client, out=lines.append,
    )
    assert code == 0
    assert "hightier" in lines[0]

    lines.clear()
    assert run_routine_list(settings=settings, out=lines.append) == 0
    assert any("hightier" in line for line in lines)

    lines.clear()
    assert run_routine_show("hightier", settings=settings, out=lines.append) == 0
    assert any("weekday 08:00" in line for line in lines)

    lines.clear()
    assert run_routine_remove("hightier", settings=settings, out=lines.append) == 0
    assert run_routine_show("hightier", settings=settings, out=lambda s: None) == 1


def test_add_auto_derives_a_name_when_omitted(tmp_path):
    settings = _settings(tmp_path)
    client = _Client("INTENT: BRIEF\nGMAIL_QUERY: NONE\nSTART: NONE\nEND: NONE")
    lines: list[str] = []

    code = run_routine_add(
        "give me a rundown", schedule="daily 07:00",
        settings=settings, client=client, out=lines.append,
    )
    assert code == 0

    listed: list[str] = []
    run_routine_list(settings=settings, out=listed.append)
    assert any("give_me_a_rundown" in line or "rundown" in line for line in listed)


def test_add_refuses_write_request_with_clear_error(tmp_path):
    settings = _settings(tmp_path)
    client = _Client("INTENT: WRITE\nGMAIL_QUERY: NONE\nSTART: NONE\nEND: NONE")
    lines: list[str] = []

    code = run_routine_add(
        "send a reply to my boss every morning", schedule="daily 08:00",
        settings=settings, client=client, out=lines.append,
    )
    assert code == 2
    assert "never a grant" in lines[0]
    assert run_routine_list(settings=settings, out=lambda s: None) == 0  # nothing was added


def test_add_refuses_invalid_schedule(tmp_path):
    settings = _settings(tmp_path)
    client = _Client("INTENT: BRIEF\nGMAIL_QUERY: NONE\nSTART: NONE\nEND: NONE")
    lines: list[str] = []

    code = run_routine_add(
        "give me the morning brief", schedule="someday 08:00",
        settings=settings, client=client, out=lines.append,
    )
    assert code == 2
    assert "invalid schedule" in lines[0] or "unknown day" in lines[0]


def test_add_refuses_duplicate_name(tmp_path):
    settings = _settings(tmp_path)
    client = _Client("INTENT: BRIEF\nGMAIL_QUERY: NONE\nSTART: NONE\nEND: NONE")
    run_routine_add(
        "give me the morning brief", schedule="daily 07:30", name="brief1",
        settings=settings, client=client, out=lambda s: None,
    )
    lines: list[str] = []
    code = run_routine_add(
        "a different request", schedule="daily 08:00", name="brief1",
        settings=settings, client=client, out=lines.append,
    )
    assert code == 2
    assert "already exists" in lines[0]


def test_remove_unknown_routine_reports_not_found(tmp_path):
    settings = _settings(tmp_path)
    lines: list[str] = []
    assert run_routine_remove("nope", settings=settings, out=lines.append) == 1
    assert "No such routine" in lines[0]


def test_run_uses_injected_runtime_factory(tmp_path):
    settings = _settings(tmp_path)
    client = _Client("INTENT: BRIEF\nGMAIL_QUERY: NONE\nSTART: NONE\nEND: NONE")
    run_routine_add(
        "give me the morning brief", schedule="daily 07:30", name="brief1",
        settings=settings, client=client, out=lambda s: None,
    )

    class _FakeRuntime:
        def run_scheduled_routine(self, routine):
            assert routine.name == "brief1"
            return "the brief text"

    lines: list[str] = []
    code = run_routine_run(
        "brief1", runtime_factory=lambda: _FakeRuntime(),
        settings=settings, out=lines.append,
    )
    assert code == 0
    assert lines == ["the brief text"]
