"""Tests for autonomy persistence + graduation (roadmap prompt 12).
This is the safety spine — the send-gate-survives-grant test at the bottom
is the one that must never be deleted."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from attune.audit.log import JsonlAuditLog
from attune.orchestrator import (
    Action,
    Domain,
    JsonPermissionMatrixStore,
    Rung,
    default_matrix,
    grant,
    revoke,
    show_matrix,
    suggest_graduations,
    track_records,
)
from attune.orchestrator.grants import parse_action, parse_domain, parse_rung


class FakeAuditLog:
    def __init__(self, entries=None):
        self.records: list[dict] = []
        self._entries = entries or []

    def record(self, **kwargs):
        self.records.append(kwargs)

    def query(self, **kwargs):
        return self._entries


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_matrix_round_trips_through_store(tmp_path):
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    matrix = default_matrix().grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY)
    store.save(matrix)

    loaded = JsonPermissionMatrixStore(str(tmp_path / "grants.json")).load()
    assert loaded is not None
    assert loaded.max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY
    assert loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE
    # untouched keys stay at the floor
    assert loaded.max_rung(Action.SEND_REPLY, Domain.MAIL) == Rung.READ_ONLY


def test_store_load_none_when_never_saved(tmp_path):
    assert JsonPermissionMatrixStore(str(tmp_path / "nope.json")).load() is None


def test_corrupt_grant_file_errors_loudly(tmp_path):
    """An unknown action in the persisted file must be a hard error — a
    corrupted autonomy file silently changing posture is the nightmare."""
    path = tmp_path / "grants.json"
    path.write_text('{"hack_the_planet|mail": 4}')
    with pytest.raises(ValueError):
        JsonPermissionMatrixStore(str(path)).load()


def test_build_app_loads_persisted_matrix(tmp_path):
    langgraph = pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver

    from attune.app import build_app
    from attune.config import Settings

    grants_path = str(tmp_path / "grants.json")
    JsonPermissionMatrixStore(grants_path).save(
        default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    )
    settings = Settings.from_env({
        "ATTUNE_MEM0_URL": "", "ATTUNE_AUTONOMY_STATE_PATH": grants_path,
        "ATTUNE_AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
    })

    class _Store:
        def add(self, *a, **kw): return []
        def search(self, *a, **kw): return []
        def get_all(self, *a, **kw): return []
        def delete(self, *a): pass

    class _Client:
        def chat_completions_create(self, **kw):
            class _C:
                class message:
                    content = "draft"
            class _R:
                choices = [_C]
            return _R()

    app = build_app(
        settings, client=_Client(), store=_Store(), checkpointer=InMemorySaver()
    )
    assert app.matrix.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.ACT_NOTIFY

    # and the graph actually uses it: the granted (action, domain) skips
    # the interrupt (auto-applies)
    out = app.graph.invoke(
        {"user_id": "u1", "domain": "mail", "action": "draft_reply",
         "incoming_ref": "r1", "incoming_summary": "hello",
         "audit_events": [], "iteration_count": 0},
        {"configurable": {"thread_id": "t-persisted"}},
    )
    assert "__interrupt__" not in out
    assert out["decision"] == "approved"


# ---------------------------------------------------------------------------
# grant / revoke operations
# ---------------------------------------------------------------------------


def test_grant_persists_audits_and_leaves_original_immutable(tmp_path):
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    audit = FakeAuditLog()
    original = default_matrix()

    updated = grant(
        store, original, Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY,
        audit_log=audit, user_id="u1",
    )

    assert updated.max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY
    assert original.max_rung(Action.LABEL, Domain.MAIL) == Rung.READ_ONLY
    assert store.load().max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY
    event = audit.records[0]["events"][0]
    assert event["event"] == "autonomy_granted"
    assert audit.records[0]["workflow"] == "autonomy"


def test_revoke_falls_back_to_floor_and_audits(tmp_path):
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    audit = FakeAuditLog()
    matrix = default_matrix()

    updated = revoke(
        store, matrix, Action.DRAFT_REPLY, Domain.MAIL,
        audit_log=audit, user_id="u1",
    )

    assert updated.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.READ_ONLY
    assert store.load().max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.READ_ONLY
    assert audit.records[0]["events"][0]["event"] == "autonomy_revoked"


def test_strict_parsing_rejects_typos():
    with pytest.raises(ValueError):
        parse_action("draft_replyy")
    with pytest.raises(ValueError):
        parse_domain("gmail")
    with pytest.raises(KeyError):
        parse_rung("yolo")
    assert parse_rung("act_notify") == Rung.ACT_NOTIFY
    assert parse_rung("3") == Rung.ACT_NOTIFY


def test_show_matrix_renders_grants():
    text = show_matrix(default_matrix())
    assert "draft_reply" in text
    assert "PROPOSE" in text


# ---------------------------------------------------------------------------
# track record + graduation suggestions
# ---------------------------------------------------------------------------


def _audit_file_with_decisions(tmp_path, decisions):
    """Write a real JSONL audit file: one draft-approve workflow per decision
    ('approved'/'edited'/'rejected'/'ignored'/None for still-pending)."""
    log = JsonlAuditLog(str(tmp_path / "audit.jsonl"))
    for i, decision in enumerate(decisions):
        tid = f"gmail:t{i}:100"
        log.record(
            thread_id=tid, workflow="draft_approve",
            events=[{
                "event": "autonomy_gate", "ts": NOW.isoformat(),
                "action": "draft_reply", "domain": "mail",
                "max_rung": 2, "routed_to": "approve",
            }],
            domain="mail", user_id="u1",
        )
        if decision == "ignored":
            log.record(
                thread_id=tid, workflow="draft_approve",
                events=[{"event": "approval_ignored", "ts": NOW.isoformat()}],
                domain="mail", user_id="u1",
            )
        elif decision is not None:
            events = [{
                "event": "human_decision", "ts": NOW.isoformat(),
                "decision": decision,
            }]
            if decision in ("approved", "edited"):
                events.append({"event": "applied", "ts": NOW.isoformat()})
            log.record(
                thread_id=tid, workflow="draft_approve",
                events=events,
                domain="mail", user_id="u1",
            )
    return log


def test_track_record_counts_by_outcome(tmp_path):
    log = _audit_file_with_decisions(
        tmp_path,
        ["approved", "approved", "edited", "rejected", "ignored", None],
    )
    records = track_records(log, now=NOW)
    record = records[(Action.DRAFT_REPLY, Domain.MAIL)]
    assert (record.approved, record.edited, record.rejected, record.ignored) == (
        2, 1, 1, 1,
    )
    assert record.total == 5  # the still-pending one doesn't count
    assert record.applied == 3
    assert record.apply_failed == 0


def test_graduation_suggested_when_bar_met(tmp_path):
    log = _audit_file_with_decisions(tmp_path, ["approved"] * 12)
    suggestions = suggest_graduations(log, default_matrix(), now=NOW)
    assert len(suggestions) == 1
    rendered = suggestions[0].render()
    assert "12/12" in rendered
    assert "attune autonomy grant draft_reply mail act_notify" in rendered


def test_no_suggestion_below_bar(tmp_path):
    # too few decisions
    log = _audit_file_with_decisions(tmp_path, ["approved"] * 9)
    assert suggest_graduations(log, default_matrix(), now=NOW) == []
    # a rejection disqualifies
    log = _audit_file_with_decisions(tmp_path.joinpath("b"), ["approved"] * 11 + ["rejected"])
    assert suggest_graduations(log, default_matrix(), now=NOW) == []
    # too many edits (approval rate below 95%)
    log = _audit_file_with_decisions(tmp_path.joinpath("c"), ["approved"] * 10 + ["edited"] * 2)
    assert suggest_graduations(log, default_matrix(), now=NOW) == []


def test_no_suggestion_when_already_graduated(tmp_path):
    log = _audit_file_with_decisions(tmp_path, ["approved"] * 12)
    matrix = default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    assert suggest_graduations(log, matrix, now=NOW) == []


def test_no_suggestion_when_an_approved_apply_failed(tmp_path):
    log = _audit_file_with_decisions(tmp_path, ["approved"] * 12)
    tid = "gmail:t11:100"
    path = tmp_path / "audit.jsonl"
    lines = path.read_text().splitlines()
    path.write_text(
        "\n".join(
            line.replace('"event": "applied"', '"event": "apply_failed"')
            if tid in line
            else line
            for line in lines
        )
        + "\n"
    )

    assert suggest_graduations(log, default_matrix(), now=NOW) == []
    record = track_records(log, now=NOW)[(Action.DRAFT_REPLY, Domain.MAIL)]
    assert (record.applied, record.apply_failed) == (11, 1)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

from attune.cli.autonomy_cmd import (  # noqa: E402
    run_autonomy_grant,
    run_autonomy_record,
    run_autonomy_revoke,
    run_autonomy_show,
)
from attune.config import Settings  # noqa: E402


def _settings(tmp_path):
    return Settings.from_env({
        "ATTUNE_MEM0_URL": "",
        "ATTUNE_AUTONOMY_STATE_PATH": str(tmp_path / "grants.json"),
        "ATTUNE_AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
    })


def test_cli_grant_then_show_round_trip(tmp_path, capsys):
    settings = _settings(tmp_path)
    audit = FakeAuditLog()

    code = run_autonomy_grant(
        "label", "mail", "act_notify", settings=settings, audit_log=audit
    )
    assert code == 0

    run_autonomy_show(settings=settings, audit_log=audit)
    out = capsys.readouterr().out
    assert "label" in out and "ACT_NOTIFY" in out
    # and it persisted for real
    loaded = JsonPermissionMatrixStore(settings.autonomy_state_path).load()
    assert loaded.max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY


def test_cli_grant_send_reply_warns_about_structural_gate(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = run_autonomy_grant(
        "send_reply", "mail", "propose", settings=settings, audit_log=FakeAuditLog()
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "structurally disabled" in out
    assert "gmail.send" in out


def test_cli_grant_typo_errors_with_vocabulary(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = run_autonomy_grant(
        "draft_replyy", "mail", "propose", settings=settings, audit_log=FakeAuditLog()
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "actions:" in out  # the vocabulary hint
    # and nothing was persisted
    assert JsonPermissionMatrixStore(settings.autonomy_state_path).load() is None


def test_cli_revoke(tmp_path, capsys):
    settings = _settings(tmp_path)
    audit = FakeAuditLog()
    run_autonomy_grant("label", "mail", "act_notify", settings=settings, audit_log=audit)
    code = run_autonomy_revoke("label", "mail", settings=settings, audit_log=audit)
    assert code == 0
    loaded = JsonPermissionMatrixStore(settings.autonomy_state_path).load()
    assert loaded.max_rung(Action.LABEL, Domain.MAIL) == Rung.READ_ONLY


def test_cli_record_renders_track_record(tmp_path, capsys):
    settings = _settings(tmp_path)
    log = _audit_file_with_decisions(tmp_path, ["approved", "edited"])
    code = run_autonomy_record(settings=settings, audit_log=log)
    assert code == 0
    out = capsys.readouterr().out
    assert "1 approved unedited, 1 edited" in out


# ---------------------------------------------------------------------------
# chat surface (show-only) + weekly digest
# ---------------------------------------------------------------------------


def test_chat_autonomy_command_shows_posture_never_grants(tmp_path):
    from attune.app import AppContext
    from attune.dispatcher import handle_slack_message

    class _Store:
        def add(self, *a, **kw): return []
        def search(self, *a, **kw): return []
        def get_all(self, *a, **kw): return []
        def delete(self, *a): pass

    log = _audit_file_with_decisions(tmp_path, ["approved"] * 12)
    app = AppContext(
        graph=None, client=None, store=_Store(),
        settings=_settings(tmp_path), audit_log=log,
        matrix=default_matrix(),
    )
    replies: list[str] = []
    handle_slack_message(
        app, text="autonomy", user_id="U1", post_text=replies.append,
        audit_log=log, memory_ui={},
    )

    assert "draft_reply" in replies[0]
    assert "12/12" in replies[0]           # the earned suggestion shows
    assert "CLI-only" in replies[0]        # and grants are pointed at the CLI


# (the weekly digest test lives in test_runtime.py, next to the Runtime fakes)


# ---------------------------------------------------------------------------
# THE safety test: a grant alone must never be sufficient to send (rule 4)
# ---------------------------------------------------------------------------


def test_send_gate_survives_send_reply_grant():
    """Granting SEND_REPLY at any rung — even AUTONOMOUS — must not make the
    connector send. Rule 4's structural gate (send_enabled + a real
    gmail.send scope) is independent of the autonomy matrix, and this test
    pins that independence."""
    from attune.connectors.base import SendNotPermitted
    from attune.connectors.google_oauth import DirectOAuthConnector

    matrix = default_matrix().grant(Action.SEND_REPLY, Domain.MAIL, Rung.AUTONOMOUS)
    assert matrix.allows(Action.SEND_REPLY, Domain.MAIL, Rung.AUTONOMOUS)

    connector = DirectOAuthConnector(gmail_service=object(), calendar_service=object())
    with pytest.raises(SendNotPermitted):
        connector.send_reply(draft_id="d-1")


# ---------------------------------------------------------------------------
# Live policy (prompt 19): the gate reads the CURRENT matrix, not a snapshot
# ---------------------------------------------------------------------------

from attune.orchestrator.grants import make_matrix_provider  # noqa: E402


def test_provider_reloads_on_file_change(tmp_path):
    import os as _os
    import time as _time

    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    provider = make_matrix_provider(store)

    # never saved -> conservative default
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.READ_ONLY

    store.save(default_matrix().grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY))
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY

    # revocation bites too (force a distinct mtime for coarse filesystems)
    store.save(default_matrix())
    _os.utime(store._path, (_time.time() + 2, _time.time() + 2))
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.READ_ONLY


def test_provider_fails_closed_on_corrupt_or_deleted_file(tmp_path):
    import os as _os
    import time as _time

    path = tmp_path / "grants.json"
    store = JsonPermissionMatrixStore(str(path))
    store.save(default_matrix().grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY))
    provider = make_matrix_provider(store)
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY

    path.write_text('{"hack_the_planet|mail": 4}')
    _os.utime(str(path), (_time.time() + 2, _time.time() + 2))
    # corrupt file -> conservative default, never cached autonomous authority
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.READ_ONLY
    assert provider().max_rung(Action.SEND_REPLY, Domain.MAIL) == Rung.READ_ONLY

    path.unlink()
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.READ_ONLY


def test_gate_honors_live_grant_and_revocation(tmp_path):
    """End to end: the SAME compiled graph obeys a grant file edited on disk
    — a revocation stops autonomous runs without a restart."""
    import os as _os
    import time as _time

    langgraph = pytest.importorskip("langgraph")

    class _Store:
        def add(self, *a, **kw): return []
        def search(self, *a, **kw): return []
        def get_all(self, *a, **kw): return []
        def delete(self, *a): pass

    class _Client:
        def chat_completions_create(self, **kw):
            class _C:
                class message:
                    content = "draft"
            class _R:
                choices = [_C]
            return _R()

    from attune.orchestrator import build_draft_approve_graph

    matrix_store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    graph = build_draft_approve_graph(
        client=_Client(), store=_Store(),
        matrix_provider=make_matrix_provider(matrix_store),
    )

    def _run(tid):
        return graph.invoke(
            {"user_id": "u1", "domain": "mail", "action": "draft_reply",
             "incoming_ref": "r1", "incoming_summary": "hello",
             "audit_events": [], "iteration_count": 0},
            {"configurable": {"thread_id": tid}},
        )

    # 1. default posture -> interrupt
    assert "__interrupt__" in _run("t-live-1")

    # 2. grant lands on disk -> the same graph auto-applies
    matrix_store.save(
        default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    )
    out = _run("t-live-2")
    assert "__interrupt__" not in out
    assert out["decision"] == "approved"

    # 3. REVOCATION lands on disk -> the same graph interrupts again,
    #    with no rebuild and no restart
    matrix_store.save(default_matrix())
    _os.utime(matrix_store._path, (_time.time() + 2, _time.time() + 2))
    assert "__interrupt__" in _run("t-live-3")
