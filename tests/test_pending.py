"""Tests for orchestrator/pending.py — the pending-approvals registry and the
IGNORED-signal sweep (design 2.2, roadmap prompt 03). All offline: file-backed
registry in tmp_path, fake MemoryStore/audit log, injected clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.memory.base import MemoryStore
from attune.orchestrator import JsonPendingApprovals, sweep_ignored
from attune.orchestrator.draft_approve import resume_workflow
from attune.orchestrator.pending import STATUS_EXPIRED, STATUS_IGNORED, sweep_expired


class FakeStore(MemoryStore):
    def __init__(self):
        self.added: list[dict] = []

    def add(self, messages, *, user_id, metadata=None, infer=True):
        self.added.append(
            {"messages": messages, "metadata": metadata, "infer": infer}
        )
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return []

    def get_all(self, *, user_id, limit=100):
        return []

    def delete(self, memory_id):
        pass


class FakeAuditLog:
    def __init__(self):
        self.recorded: list[dict] = []

    def record(self, **kwargs):
        self.recorded.append(kwargs)


T0 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _registry(tmp_path):
    return JsonPendingApprovals(str(tmp_path / "pending.json"))


# ---------------------------------------------------------------------------
# Registry mechanics
# ---------------------------------------------------------------------------


def test_register_and_lookup_by_source(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    entry = reg.get_pending_for_source("t1")
    assert entry is not None
    assert entry.lg_tid == "gmail:t1:100"
    assert entry.posted_at == T0
    assert reg.get_pending_for_source("t2") is None


def test_registry_file_is_chmodded_owner_only(tmp_path):
    """Security finding F5 (Low): pending-approval state must be owner-only
    regardless of the process umask."""
    import os

    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    path = tmp_path / "pending.json"
    assert path.exists()
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_register_stores_sender_when_given(tmp_path):
    reg = _registry(tmp_path)
    reg.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail",
        posted_at=T0, sender="Sender@Example.com",
    )
    entry = reg.get_pending_for_source("t1")
    assert entry.sender == "Sender@Example.com"


def test_register_defaults_sender_to_none(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    entry = reg.get_pending_for_source("t1")
    assert entry.sender is None


def test_register_stores_subject_and_priority_when_given(tmp_path):
    reg = _registry(tmp_path)
    reg.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail",
        posted_at=T0, sender="sender@example.com",
        subject="Re: proposal", priority="routine",
    )
    entry = reg.get_pending_for_source("t1")
    assert entry.subject == "Re: proposal"
    assert entry.priority == "routine"


def test_register_defaults_subject_and_priority_to_none(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    entry = reg.get_pending_for_source("t1")
    assert entry.subject is None
    assert entry.priority is None


def test_legacy_entry_without_subject_or_priority_parses_back_as_none(tmp_path):
    """A JSON file written before ``subject``/``priority`` existed (build
    prompt 25, task 1) must still load — same back-compat posture as
    ``sender``."""
    import json

    path = tmp_path / "pending.json"
    path.write_text(json.dumps({
        "gmail:t1:100": {
            "source_ref": "t1", "domain": "mail",
            "posted_at": T0.isoformat(), "status": "pending",
            "sender": "sender@example.com",
        }
    }))
    entry = JsonPendingApprovals(str(path)).get_pending_for_source("t1")
    assert entry is not None
    assert entry.subject is None
    assert entry.priority is None


def test_legacy_entry_without_sender_field_parses_back_as_none(tmp_path):
    """A JSON file written before ``sender`` existed must still load —
    PendingApproval.sender defaults to None (backward compatibility)."""
    import json

    path = tmp_path / "pending.json"
    path.write_text(json.dumps({
        "gmail:t1:100": {
            "source_ref": "t1", "domain": "mail",
            "posted_at": T0.isoformat(), "status": "pending",
        }
    }))
    entry = JsonPendingApprovals(str(path)).get_pending_for_source("t1")
    assert entry is not None
    assert entry.sender is None


def test_resolve_removes_from_pending(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    reg.resolve("gmail:t1:100")

    assert reg.get_pending_for_source("t1") is None
    assert reg.pending() == []


def test_resolve_unknown_id_is_noop(tmp_path):
    # Resume paths call resolve unconditionally, including for workflows
    # (e.g. chat-domain ones) that were never registered.
    _registry(tmp_path).resolve("never-registered")


def test_claim_is_single_use_and_records_actor(tmp_path):
    import json

    path = tmp_path / "pending.json"
    reg = JsonPendingApprovals(str(path))
    reg.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0
    )

    assert reg.claim("gmail:t1:100", actor="U-OWNER") is True
    assert reg.claim("gmail:t1:100", actor="U-OWNER") is False
    raw = json.loads(path.read_text())["gmail:t1:100"]
    assert raw["resolved_by"] == "U-OWNER"


def test_claim_unknown_workflow_is_unmanaged(tmp_path):
    assert _registry(tmp_path).claim("not-registered", actor="U1") is None


def test_round_trips_through_file(tmp_path):
    path = str(tmp_path / "pending.json")
    JsonPendingApprovals(path).register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0
    )
    # A fresh instance (fresh process, in production) reads the same state —
    # and the sweep's age math consumes the parsed posted_at correctly.
    reloaded = JsonPendingApprovals(path)
    entry = reloaded.get_pending_for_source("t1")
    assert entry.posted_at == T0
    swept = sweep_ignored(
        reloaded, FakeStore(), user_id="u1", now=T0 + timedelta(hours=49)
    )
    assert swept == 1


# ---------------------------------------------------------------------------
# sweep_ignored
# ---------------------------------------------------------------------------


def test_sweep_captures_ignored_after_max_age(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    audit = FakeAuditLog()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_ignored(
        reg, store, user_id="u1", now=T0 + timedelta(hours=49), audit_log=audit
    )

    assert swept == 1
    assert len(store.added) == 1
    meta = store.added[0]["metadata"]
    assert meta["action"] == "ignored"
    assert meta["source_ref"] == "t1"
    assert store.added[0]["infer"] is False  # raw signal, verbatim
    assert audit.recorded[0]["events"][0]["event"] == "approval_ignored"
    assert audit.recorded[0]["thread_id"] == "gmail:t1:100"


def test_sweep_leaves_fresh_entries_alone(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_ignored(reg, store, user_id="u1", now=T0 + timedelta(hours=47))

    assert swept == 0
    assert store.added == []
    assert reg.get_pending_for_source("t1") is not None


def test_sweep_captures_each_entry_exactly_once(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    late = T0 + timedelta(hours=72)
    assert sweep_ignored(reg, store, user_id="u1", now=late) == 1
    assert sweep_ignored(reg, store, user_id="u1", now=late) == 0
    assert len(store.added) == 1


def test_sweep_passes_sender_to_importance_profile(tmp_path):
    from attune.orchestrator.importance import JsonImportanceProfile

    reg = _registry(tmp_path)
    store = FakeStore()
    profile = JsonImportanceProfile(str(tmp_path / "importance.json"))
    reg.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail",
        posted_at=T0, sender="newsletter@example.com",
    )

    swept = sweep_ignored(
        reg, store, user_id="u1", now=T0 + timedelta(hours=49),
        importance_profile=profile,
    )

    assert swept == 1
    assert profile.senders() == ["newsletter@example.com"]


def test_sweep_skips_profile_write_when_sender_absent(tmp_path):
    from attune.orchestrator.importance import JsonImportanceProfile

    reg = _registry(tmp_path)
    store = FakeStore()
    profile = JsonImportanceProfile(str(tmp_path / "importance.json"))
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_ignored(
        reg, store, user_id="u1", now=T0 + timedelta(hours=49),
        importance_profile=profile,
    )

    assert swept == 1          # the memory write still happens
    assert len(store.added) == 1
    assert profile.senders() == []  # nothing to record without a sender


def test_sweep_writes_a_discriminating_summary_not_a_raw_thread_id(tmp_path):
    """The bug build prompt 25 exists to fix: the pre-fix sweep wrote
    ``f"approval card for {entry.source_ref} left untouched Nd"`` — a raw
    Gmail thread id, no sender, no subject. The captured text must now name
    the counterparty and subject, and carry them in metadata too."""
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail",
        posted_at=T0, sender="newsletter@example.com",
        subject="Re: your weekly digest", priority="routine",
    )

    sweep_ignored(reg, store, user_id="u1", now=T0 + timedelta(hours=49))

    assert len(store.added) == 1
    written = store.added[0]
    assert "newsletter@example.com" in written["messages"]
    assert "your weekly digest" in written["messages"]
    assert written["metadata"]["sender"] == "newsletter@example.com"
    assert written["metadata"]["subject"] == "Re: your weekly digest"
    assert written["metadata"]["priority"] == "routine"
    # the raw thread id no longer stands in for the summary itself
    assert written["messages"] != "approval card for t1 left untouched 2d"


def test_sweep_respects_custom_max_age(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_ignored(
        reg, store, user_id="u1", max_age=timedelta(hours=2),
        now=T0 + timedelta(hours=3),
    )
    assert swept == 1


def test_sweep_marks_ignored_not_resolved(tmp_path):
    """Prompt 21: the registry's status is honest — expired-unanswered is
    'ignored', distinct from a human's 'resolved'."""
    import json as _json

    path = tmp_path / "pending.json"
    reg = JsonPendingApprovals(str(path))
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    sweep_ignored(reg, FakeStore(), user_id="u1", now=T0 + timedelta(hours=49))

    raw = _json.loads(path.read_text())
    assert raw["gmail:t1:100"]["status"] == "ignored"
    # a late human click still flips it to resolved
    reg.resolve("gmail:t1:100")
    raw = _json.loads(path.read_text())
    assert raw["gmail:t1:100"]["status"] == "resolved"


def test_resolved_entry_never_swept(tmp_path):
    reg = _registry(tmp_path)
    store = FakeStore()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    reg.resolve("gmail:t1:100")  # user answered the card

    swept = sweep_ignored(reg, store, user_id="u1", now=T0 + timedelta(days=30))
    assert swept == 0
    assert store.added == []


# ---------------------------------------------------------------------------
# sweep_expired (build prompt 31, task 3) — the approval TTL
# ---------------------------------------------------------------------------


def test_sweep_expired_marks_status_and_cancels_workflow(tmp_path):
    reg = _registry(tmp_path)
    audit = FakeAuditLog()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    cancelled: list[str] = []
    swept = sweep_expired(
        reg, now=T0 + timedelta(days=8), audit_log=audit, user_id="u1",
        cancel_workflow=cancelled.append,
    )

    assert swept == 1
    assert cancelled == ["gmail:t1:100"]
    entry = reg.get_entry("gmail:t1:100")
    assert entry.status == STATUS_EXPIRED
    assert audit.recorded[0]["events"][0]["event"] == "approval_expired"


def test_sweep_expired_leaves_fresh_entries_alone(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    swept = sweep_expired(reg, now=T0 + timedelta(days=6))

    assert swept == 0
    assert reg.get_entry("gmail:t1:100").status != STATUS_EXPIRED


def test_sweep_expired_catches_an_already_ignored_entry():
    """A card the 48h ignore-sweep already touched must still be caught
    once it separately crosses the much longer 7-day expiry line —
    ``sweep_expired`` reads BOTH pending and ignored entries, since
    ``registry.pending()`` alone would never see an ignored one again."""

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        reg = JsonPendingApprovals(f"{tmp}/pending.json")
        reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
        sweep_ignored(reg, FakeStore(), user_id="u1", now=T0 + timedelta(hours=49))
        assert reg.get_entry("gmail:t1:100").status == STATUS_IGNORED

        swept = sweep_expired(reg, now=T0 + timedelta(days=8))

        assert swept == 1
        assert reg.get_entry("gmail:t1:100").status == STATUS_EXPIRED


def test_sweep_expired_distinguishable_from_ignored_in_the_audit_trail(tmp_path):
    """STATUS_EXPIRED and STATUS_IGNORED must produce different audit
    events (and therefore different learning signals downstream in
    ``grants.track_records``) — an expiry is a fact about elapsed time,
    never a judgment about the proposal."""
    reg = _registry(tmp_path)
    audit = FakeAuditLog()
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    reg.register(lg_tid="gmail:t2:100", source_ref="t2", domain="mail", posted_at=T0)

    sweep_ignored(reg, FakeStore(), user_id="u1", now=T0 + timedelta(hours=49), audit_log=audit)
    sweep_expired(reg, now=T0 + timedelta(days=8), audit_log=audit, user_id="u1")

    events = {e["events"][0]["event"] for e in audit.recorded}
    assert events == {"approval_ignored", "approval_expired"}
    assert reg.get_entry("gmail:t1:100").status == STATUS_EXPIRED
    assert reg.get_entry("gmail:t2:100").status == STATUS_EXPIRED


def test_expired_card_resume_returns_honest_refusal_without_touching_graph():
    """A click on an expired card must return an honest refusal instead of
    resuming — and never even reach the graph (proven here by a graph
    stub that raises if invoked at all)."""

    class _ExplodingGraph:
        def invoke(self, *a, **kw):
            raise AssertionError("an expired card's workflow must never be resumed")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        reg = JsonPendingApprovals(f"{tmp}/pending.json")
        reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
        sweep_expired(reg, now=T0 + timedelta(days=8))

        result = resume_workflow(_ExplodingGraph(), "gmail:t1:100", "approved", pending=reg)

    assert result["approval_expired"] is True
    assert result["apply_error"] == "expired"


def test_sweep_expired_default_ttl_is_seven_days(tmp_path):
    reg = _registry(tmp_path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    # Just under 7 days: not yet expired.
    assert sweep_expired(reg, now=T0 + timedelta(days=6, hours=23)) == 0
    # Just over 7 days: expired.
    assert sweep_expired(reg, now=T0 + timedelta(days=7, hours=1)) == 1
