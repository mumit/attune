"""Tests for memory transparency (roadmap prompt 11): the command engine
(memory/commands.py), the chat router (dispatcher), and the CLI surface.
All offline with a fake MemoryStore."""

from __future__ import annotations

from attune.memory.base import MemoryRecord, MemoryStore
from attune.memory.commands import (
    forget_memory,
    list_memories,
    remember_fact,
    resolve_memory,
)


class FakeStore(MemoryStore):
    def __init__(self, records=None):
        self.records = records or []
        self.deleted: list[str] = []
        self.added: list[dict] = []

    def add(self, messages, *, user_id, metadata=None, infer=True):
        self.added.append(
            {"messages": messages, "metadata": metadata, "infer": infer}
        )
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return [r for r in self.records if query.lower() in r.text.lower()][:limit]

    def get_all(self, *, user_id, limit=100):
        return self.records[:limit]

    def delete(self, memory_id):
        self.deleted.append(memory_id)
        self.records = [r for r in self.records if r.id != memory_id]


class FakeAuditLog:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, **kwargs):
        self.records.append(kwargs)


def _records():
    return [
        MemoryRecord(id="mem-aaa111", text="Prefers short replies",
                     metadata={"signal": "correction", "domain": "mail"}),
        MemoryRecord(id="mem-bbb222", text="Priya is the PM for Falcon",
                     metadata={"signal": "explicit"}),
        MemoryRecord(id="mem-ccc333", text="[rejected] mail: draft_reply on mail",
                     metadata={"signal": "action", "action": "rejected",
                               "domain": "mail"}),
    ]


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def test_list_memories_numbers_and_maps_ids():
    listing = list_memories(FakeStore(_records()), user_id="u1")
    assert listing.ids == {1: "mem-aaa111", 2: "mem-bbb222", 3: "mem-ccc333"}
    assert "1. Prefers short replies [correction] (mail)" in listing.text
    assert "…aaa111" in listing.text


def test_list_memories_with_query_searches():
    listing = list_memories(FakeStore(_records()), user_id="u1", query="falcon")
    assert listing.ids == {1: "mem-bbb222"}
    assert "Priya" in listing.text


def test_list_memories_empty_store():
    assert "No memories stored yet." in list_memories(FakeStore(), user_id="u1").text


def test_resolve_by_listing_number_and_id_fragment():
    store = FakeStore(_records())
    listing = list_memories(store, user_id="u1")

    by_number = resolve_memory(
        store, user_id="u1", selector="2", listing_ids=listing.ids
    )
    assert by_number.id == "mem-bbb222"

    by_suffix = resolve_memory(store, user_id="u1", selector="aaa111")
    assert by_suffix.id == "mem-aaa111"

    # ambiguous prefix -> None, never guess
    assert resolve_memory(store, user_id="u1", selector="mem-") is None
    assert resolve_memory(store, user_id="u1", selector="nope") is None


def test_forget_deletes_and_audits():
    store = FakeStore(_records())
    audit = FakeAuditLog()
    record = store.records[0]

    forget_memory(store, record, user_id="u1", audit_log=audit)

    assert store.deleted == ["mem-aaa111"]
    event = audit.records[0]["events"][0]
    assert event["event"] == "memory_deleted"
    assert audit.records[0]["workflow"] == "memory"


def test_remember_stores_explicit_signal_and_audits():
    store = FakeStore()
    audit = FakeAuditLog()

    remember_fact(store, user_id="u1", text="Dana is my manager", audit_log=audit)

    assert store.added[0]["metadata"] == {"signal": "explicit"}
    assert store.added[0]["infer"] is True
    assert audit.records[0]["events"][0]["event"] == "memory_taught"


# ---------------------------------------------------------------------------
# chat router (dispatcher)
# ---------------------------------------------------------------------------

from attune.app import AppContext  # noqa: E402
from attune.config import Settings  # noqa: E402
from attune.dispatcher import handle_slack_message  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.calls = []

    def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)

        class _C:
            class message:
                content = "conversational reply"

        class _R:
            choices = [_C]
        return _R()


def _app(store):
    return AppContext(
        graph=None, client=_FakeClient(), store=store,
        settings=Settings.from_env({"ATTUNE_MEM0_URL": ""}),
        audit_log=FakeAuditLog(),
    )


def _dm(app, text, replies, ui):
    handle_slack_message(
        app, text=text, user_id="U1", post_text=replies.append, memory_ui=ui
    )


def test_what_do_you_know_lists_memories():
    store = FakeStore(_records())
    replies: list[str] = []
    _dm(_app(store), "what do you know about me?", replies, {})

    assert "Prefers short replies" in replies[0]
    assert "forget <number>" in replies[0]


def test_memory_command_beats_brief_keyword():
    """'what do you know about the morning brief' is a memory command, not a
    brief request — command routing runs first."""
    store = FakeStore(_records())
    replies: list[str] = []
    handle_slack_message(
        _app(store), text="what do you know about the morning brief",
        user_id="U1", post_text=replies.append,
        brief_fn=lambda: "A BRIEF", memory_ui={},
    )
    assert replies[0] != "A BRIEF"
    assert "what i know" in replies[0].lower()


def test_forget_is_two_step():
    store = FakeStore(_records())
    app = _app(store)
    ui: dict = {}
    replies: list[str] = []

    _dm(app, "what do you know", replies, ui)
    _dm(app, "forget 1", replies, ui)
    assert "confirm forget" in replies[1]
    assert store.deleted == []  # nothing deleted yet

    _dm(app, "confirm forget", replies, ui)
    assert store.deleted == ["mem-aaa111"]
    assert "Forgotten" in replies[2]


def test_confirm_forget_without_pending_is_noop():
    store = FakeStore(_records())
    replies: list[str] = []
    _dm(_app(store), "confirm forget", replies, {})

    assert store.deleted == []
    assert "Nothing pending" in replies[0]


def test_forget_unknown_selector_asks_for_listing():
    store = FakeStore(_records())
    replies: list[str] = []
    _dm(_app(store), "forget 9", replies, {})
    assert store.deleted == []
    assert "numbered list" in replies[0]


def test_remember_via_chat():
    store = FakeStore()
    replies: list[str] = []
    _dm(_app(store), "remember Dana is my manager", replies, {})

    assert store.added[0]["metadata"] == {"signal": "explicit"}
    assert "Dana is my manager" in replies[0]


def test_non_command_still_converses():
    store = FakeStore(_records())
    app = _app(store)
    replies: list[str] = []
    _dm(app, "what's the status of the falcon project?", replies, {})

    assert replies == ["conversational reply"]
    assert len(app.client.calls) == 1


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

from attune.cli.memory_cmd import (  # noqa: E402
    run_memory_forget,
    run_memory_list,
    run_memory_remember,
)


def _settings():
    return Settings.from_env({"ATTUNE_MEM0_URL": ""})


def test_cli_memory_list(capsys):
    code = run_memory_list(store=FakeStore(_records()), settings=_settings())
    assert code == 0
    assert "Priya" in capsys.readouterr().out


def test_cli_memory_forget_with_yes():
    store = FakeStore(_records())
    audit = FakeAuditLog()
    code = run_memory_forget(
        "bbb222", yes=True, store=store, settings=_settings(),
        audit_log=audit, out=lambda s: None,
    )
    assert code == 0
    assert store.deleted == ["mem-bbb222"]


def test_cli_memory_forget_prompts_without_yes():
    store = FakeStore(_records())
    code = run_memory_forget(
        "bbb222", store=store, settings=_settings(), audit_log=FakeAuditLog(),
        ask=lambda p: "n", out=lambda s: None,
    )
    assert code == 0
    assert store.deleted == []


def test_cli_memory_remember():
    store = FakeStore()
    code = run_memory_remember(
        "Dana is my manager", store=store, settings=_settings(),
        audit_log=FakeAuditLog(), out=lambda s: None,
    )
    assert code == 0
    assert store.added[0]["metadata"] == {"signal": "explicit"}
