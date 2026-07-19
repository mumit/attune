"""Tests for quiet-thread follow-up nudges (design 3.3, roadmap prompt 15).
All offline: fake connector/graph/state, injected clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.app import AppContext
from attune.config import Settings
from attune.connectors.base import EmailThread, Provenance
from attune.orchestrator import (
    JsonNudgeState,
    find_nudge_candidates,
    run_follow_up_nudges,
)
from attune.orchestrator.followup import MAX_NUDGES_PER_RUN

NOW = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)
ME = "me@example.com"


def _quiet_thread(tid="t1", subject="Contract redline", days_quiet=5,
                  reply_to="marcus@acme.com"):
    return EmailThread(
        thread_id=tid, subject=subject, snippet="any update on this?",
        from_addr=ME, body="...", provenance=Provenance.FETCHED,
        last_from_addr=ME, last_message_at=NOW - timedelta(days=days_quiet),
        reply_to=reply_to,
    )


class _FakeConnector:
    def __init__(self, sent=None):
        self._sent = sent or []

    def list_threads(self, query="is:unread", *, max_results=20):
        return self._sent if query == "in:sent" else []

    def list_events(self, **kw):
        return []


class _FakeGraph:
    def __init__(self):
        self.calls: list[dict] = []

    def invoke(self, state, config):
        self.calls.append({"state": state, "config": config})
        return {
            "proposed_draft": "Just checking in — any update?",
            "retrieved_memories": [],
            "audit_events": [],
        }


class _FakeAuditLog:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, **kwargs):
        self.records.append(kwargs)


class _FakePending:
    def __init__(self):
        self.registered: list[dict] = []

    def register(self, **kw):
        self.registered.append(kw)


def _app(graph=None):
    return AppContext(
        graph=graph or _FakeGraph(), client=None, store=None,
        settings=Settings.from_env({"ATTUNE_MEM0_URL": ""}),
        audit_log=_FakeAuditLog(),
    )


# ---------------------------------------------------------------------------
# candidate filtering
# ---------------------------------------------------------------------------


def test_candidates_reuse_quiet_thread_truth(tmp_path):
    state = JsonNudgeState(str(tmp_path / "nudges.json"))
    conn = _FakeConnector(sent=[
        _quiet_thread("t1", days_quiet=5),
        _quiet_thread("t2", days_quiet=2),  # too fresh
    ])
    candidates = find_nudge_candidates(conn, state, user_email=ME, now=NOW)
    assert [t.thread_id for t in candidates] == ["t1"]


def test_cooldown_blocks_repeat_nudges(tmp_path):
    state = JsonNudgeState(str(tmp_path / "nudges.json"))
    state.record_nudge("t1", at=NOW - timedelta(days=3))  # inside 7d cooldown
    state.record_nudge("t2", at=NOW - timedelta(days=8))  # cooldown elapsed
    conn = _FakeConnector(sent=[
        _quiet_thread("t1", days_quiet=10),
        _quiet_thread("t2", days_quiet=10),
    ])
    candidates = find_nudge_candidates(conn, state, user_email=ME, now=NOW)
    assert [t.thread_id for t in candidates] == ["t2"]


def test_daily_cap_is_hard(tmp_path):
    state = JsonNudgeState(str(tmp_path / "nudges.json"))
    conn = _FakeConnector(
        sent=[_quiet_thread(f"t{i}", days_quiet=6) for i in range(8)]
    )
    candidates = find_nudge_candidates(conn, state, user_email=ME, now=NOW)
    assert len(candidates) == MAX_NUDGES_PER_RUN


def test_owner_only_thread_never_nudged(tmp_path):
    """Prompt 18: a sent thread with no counterparty has nobody to nudge —
    a follow-up would be addressed back to the owner."""
    state = JsonNudgeState(str(tmp_path / "nudges.json"))
    conn = _FakeConnector(sent=[
        _quiet_thread("t1", days_quiet=10, reply_to=""),        # no counterparty
        _quiet_thread("t2", days_quiet=10, reply_to=ME),        # owner "counterparty"
        _quiet_thread("t3", days_quiet=10),                     # real counterparty
    ])
    candidates = find_nudge_candidates(conn, state, user_email=ME, now=NOW)
    assert [t.thread_id for t in candidates] == ["t3"]


def test_cooldown_survives_restart(tmp_path):
    path = str(tmp_path / "nudges.json")
    JsonNudgeState(path).record_nudge("t1", at=NOW)
    # fresh instance = fresh process
    assert JsonNudgeState(path).last_nudged("t1") == NOW


# ---------------------------------------------------------------------------
# importance-ranked candidates (Phase 3 stage 1, docs/future-state.md; G10)
# ---------------------------------------------------------------------------


class _FakeImportanceProfile:
    """Deterministic tier-by-address lookup, no signals/decay logic needed
    for ranking tests."""

    def __init__(self, tiers: dict):
        self._tiers = tiers

    def assess(self, address):
        from attune.orchestrator.importance import ImportanceTier, TierAssessment

        tier = self._tiers.get(address, ImportanceTier.NORMAL)
        return TierAssessment(tier, "fake", False)


def test_high_tier_counterparts_win_the_cap_before_it_binds(tmp_path):
    """5 candidates, cap 3 (MAX_NUDGES_PER_RUN): the HIGH-tier counterparts
    must win the cap even though they arrived later than some LOW/NORMAL
    ones — ranking happens BEFORE max_candidates binds."""
    from attune.orchestrator.importance import ImportanceTier

    state = JsonNudgeState(str(tmp_path / "nudges.json"))
    conn = _FakeConnector(sent=[
        _quiet_thread("low1", days_quiet=10, reply_to="low1@x.com"),
        _quiet_thread("low2", days_quiet=10, reply_to="low2@x.com"),
        _quiet_thread("normal1", days_quiet=10, reply_to="normal1@x.com"),
        _quiet_thread("high1", days_quiet=10, reply_to="high1@x.com"),
        _quiet_thread("high2", days_quiet=10, reply_to="high2@x.com"),
    ])
    profile = _FakeImportanceProfile({
        "low1@x.com": ImportanceTier.LOW,
        "low2@x.com": ImportanceTier.LOW,
        "normal1@x.com": ImportanceTier.NORMAL,
        "high1@x.com": ImportanceTier.HIGH,
        "high2@x.com": ImportanceTier.HIGH,
    })

    candidates = find_nudge_candidates(
        conn, state, user_email=ME, now=NOW, importance_profile=profile
    )

    assert len(candidates) == MAX_NUDGES_PER_RUN
    ids = [t.thread_id for t in candidates]
    assert ids[:2] == ["high1", "high2"]  # HIGH first, stable within tier
    assert "normal1" in ids
    assert "low1" not in ids and "low2" not in ids  # LOW loses the cap


def test_without_profile_ranking_is_arrival_order_backcompat(tmp_path):
    state = JsonNudgeState(str(tmp_path / "nudges.json"))
    conn = _FakeConnector(sent=[
        _quiet_thread(f"t{i}", days_quiet=10, reply_to=f"c{i}@x.com")
        for i in range(5)
    ])

    candidates = find_nudge_candidates(conn, state, user_email=ME, now=NOW)

    assert [t.thread_id for t in candidates] == ["t0", "t1", "t2"]


# ---------------------------------------------------------------------------
# the nudge run: normal draft-approve workflows, nudge-titled cards
# ---------------------------------------------------------------------------


def test_nudge_starts_follow_up_workflow_and_posts_titled_card(tmp_path):
    state = JsonNudgeState(str(tmp_path / "nudges.json"))
    graph = _FakeGraph()
    app = _app(graph)
    pending = _FakePending()
    audit = _FakeAuditLog()
    posted: list[dict] = []

    def post_approval(lg_tid, draft, rationale, *, title=None):
        posted.append({"lg_tid": lg_tid, "draft": draft, "title": title})

    results = run_follow_up_nudges(
        app, _FakeConnector(sent=[_quiet_thread("t1", days_quiet=5)]), state,
        user_email=ME, user_id=ME, post_approval=post_approval,
        pending=pending, audit_log=audit, now=NOW,
    )

    assert len(results) == 1
    # the graph got a FOLLOW_UP workflow with the thread ref for apply
    invoked = graph.calls[0]["state"]
    assert invoked["action"] == "follow_up"
    assert invoked["domain"] == "mail"
    assert invoked["incoming_ref"] == "t1"
    assert "5 days ago" in invoked["incoming_summary"]
    # the card reads as a nudge
    assert posted[0]["title"] == "Follow-up nudge — no reply in 5d: Contract redline"
    # registered pending (dedupe + ignore-sweep see it), audited, cooled down
    assert pending.registered[0]["source_ref"] == "t1"
    assert audit.records[0]["workflow"] == "followup"
    assert audit.records[0]["events"][0]["event"] == "nudge_offered"
    assert state.last_nudged("t1") == NOW


def test_follow_up_gated_at_propose_by_default():
    """The FOLLOW_UP action rides the standard gate: no ACT_NOTIFY grant ->
    the graph interrupts for human approval, never auto-applies (rule 3)."""
    import pytest

    pytest.importorskip("langgraph")
    from attune.memory.base import MemoryRecord, MemoryStore
    from attune.orchestrator import build_draft_approve_graph

    class _Store(MemoryStore):
        def add(self, *a, **kw): return []
        def search(self, *a, **kw): return []
        def get_all(self, *a, **kw): return []
        def delete(self, *a): pass

    class _Client:
        def chat_completions_create(self, **kw):
            class _C:
                class message:
                    content = "follow-up draft"
            class _R:
                choices = [_C]
            return _R()

    graph = build_draft_approve_graph(client=_Client(), store=_Store())
    out = graph.invoke(
        {"user_id": "u1", "domain": "mail", "action": "follow_up",
         "incoming_ref": "t1", "incoming_summary": "quiet thread",
         "audit_events": [], "iteration_count": 0},
        {"configurable": {"thread_id": "t-followup-gate"}},
    )
    assert "__interrupt__" in out  # paused for the human, as it must


def test_failed_post_does_not_consume_nudge_budget(tmp_path):
    """The cooldown records only after a successful post — a crashed run
    retries next time instead of silently burning the thread's nudge."""
    import pytest

    state = JsonNudgeState(str(tmp_path / "nudges.json"))

    def exploding_post(*a, **kw):
        raise RuntimeError("slack down")

    with pytest.raises(RuntimeError):
        run_follow_up_nudges(
            _app(), _FakeConnector(sent=[_quiet_thread("t1")]), state,
            user_email=ME, user_id=ME, post_approval=exploding_post, now=NOW,
        )
    assert state.last_nudged("t1") is None


def test_runtime_noop_without_real_email_or_state():
    from attune.runtime import Runtime

    runtime = Runtime(
        app=_app(), settings=Settings.from_env({"ATTUNE_MEM0_URL": ""}),
        connector=_FakeConnector(), gmail_service=None, watch_state=None,
        chat_state=None, nudge_state=None,
    )
    assert runtime.post_follow_up_nudges(now=NOW) == []  # user_id is "me"
