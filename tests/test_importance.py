"""Tests for orchestrator/importance.py — the deterministic per-sender
importance profile (Phase 1, docs/future-state.md; gaps G5/G6). All offline:
file-backed profile in tmp_path, injected clocks, fakes for the CLI's
collaborators.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from attune.memory.signals import ActionSignal
from attune.orchestrator.importance import (
    DECAY_DAYS,
    HIGH_MIN_SIGNALS,
    LOW_RUN_THRESHOLD,
    MAX_SIGNALS,
    ImportanceTier,
    JsonImportanceProfile,
    TierAssessment,
)

T0 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _profile(tmp_path):
    return JsonImportanceProfile(str(tmp_path / "importance.json"))


# ---------------------------------------------------------------------------
# Unknown sender / normalization
# ---------------------------------------------------------------------------


def test_unknown_sender_is_normal_with_no_signals_reason(tmp_path):
    profile = _profile(tmp_path)
    assessment = profile.assess("nobody@example.com", now=T0)
    assert assessment == TierAssessment(ImportanceTier.NORMAL, "no recorded signals", False)


def test_sender_key_is_normalized_case_and_whitespace(tmp_path):
    profile = _profile(tmp_path)
    profile.record_signal("  Sender@Example.com ", ActionSignal.APPROVED, ts=T0)

    assert profile.senders() == ["sender@example.com"]
    # A differently-cased/spaced lookup hits the same entry.
    assessment = profile.assess("SENDER@example.com", now=T0)
    assert "1 of last 1" in assessment.reason


# ---------------------------------------------------------------------------
# LOW: consecutive ignore/reject demotion
# ---------------------------------------------------------------------------


def test_two_ignores_do_not_yet_demote(tmp_path):
    profile = _profile(tmp_path)
    for i in range(2):
        profile.record_signal(
            "newsletter@example.com", ActionSignal.IGNORED, ts=T0 + timedelta(days=i)
        )
    assessment = profile.assess("newsletter@example.com", now=T0 + timedelta(days=2))
    assert assessment.tier == ImportanceTier.NORMAL


def test_exactly_three_consecutive_ignores_demotes_to_low(tmp_path):
    profile = _profile(tmp_path)
    for i in range(LOW_RUN_THRESHOLD):
        profile.record_signal(
            "newsletter@example.com", ActionSignal.IGNORED, ts=T0 + timedelta(days=i)
        )
    assessment = profile.assess(
        "newsletter@example.com", now=T0 + timedelta(days=LOW_RUN_THRESHOLD)
    )
    assert assessment.tier == ImportanceTier.LOW
    assert assessment.reason == "sender ignored 3 of last 3 proposals"
    assert assessment.pinned is False


def test_mixed_ignore_and_reject_run_also_demotes(tmp_path):
    profile = _profile(tmp_path)
    profile.record_signal("x@example.com", ActionSignal.IGNORED, ts=T0)
    profile.record_signal(
        "x@example.com", ActionSignal.REJECTED, ts=T0 + timedelta(days=1)
    )
    profile.record_signal(
        "x@example.com", ActionSignal.IGNORED, ts=T0 + timedelta(days=2)
    )
    assessment = profile.assess("x@example.com", now=T0 + timedelta(days=3))
    assert assessment.tier == ImportanceTier.LOW
    assert "ignored or rejected" in assessment.reason


def test_a_later_approval_breaks_the_consecutive_run(tmp_path):
    profile = _profile(tmp_path)
    profile.record_signal("x@example.com", ActionSignal.IGNORED, ts=T0)
    profile.record_signal(
        "x@example.com", ActionSignal.IGNORED, ts=T0 + timedelta(days=1)
    )
    profile.record_signal(
        "x@example.com", ActionSignal.APPROVED, ts=T0 + timedelta(days=2)
    )
    assessment = profile.assess("x@example.com", now=T0 + timedelta(days=3))
    assert assessment.tier == ImportanceTier.NORMAL


# ---------------------------------------------------------------------------
# HIGH: approval-ratio promotion
# ---------------------------------------------------------------------------


def test_high_promotion_needs_min_signal_count(tmp_path):
    profile = _profile(tmp_path)
    for i in range(HIGH_MIN_SIGNALS - 1):
        profile.record_signal(
            "vip@example.com", ActionSignal.APPROVED, ts=T0 + timedelta(days=i)
        )
    assessment = profile.assess("vip@example.com", now=T0 + timedelta(days=10))
    assert assessment.tier == ImportanceTier.NORMAL


def test_high_promotion_at_bar(tmp_path):
    profile = _profile(tmp_path)
    for i in range(HIGH_MIN_SIGNALS):
        profile.record_signal(
            "vip@example.com", ActionSignal.APPROVED, ts=T0 + timedelta(days=i)
        )
    assessment = profile.assess("vip@example.com", now=T0 + timedelta(days=10))
    assert assessment.tier == ImportanceTier.HIGH
    assert "100%" in assessment.reason


def test_high_promotion_counts_edited_as_positive(tmp_path):
    profile = _profile(tmp_path)
    signals = [ActionSignal.APPROVED, ActionSignal.EDITED] * 3  # 6 signals, 100%
    for i, sig in enumerate(signals):
        profile.record_signal("vip@example.com", sig, ts=T0 + timedelta(days=i))
    assessment = profile.assess("vip@example.com", now=T0 + timedelta(days=10))
    assert assessment.tier == ImportanceTier.HIGH


def test_below_approval_rate_stays_normal(tmp_path):
    profile = _profile(tmp_path)
    # 5 signals, 3 approved (60%) -> below the 80% bar.
    signals = [ActionSignal.APPROVED] * 3 + [ActionSignal.EDITED, ActionSignal.APPROVED]
    signals[3] = ActionSignal.IGNORED
    signals[4] = ActionSignal.IGNORED
    for i, sig in enumerate(signals):
        profile.record_signal("mixed@example.com", sig, ts=T0 + timedelta(days=i))
    assessment = profile.assess("mixed@example.com", now=T0 + timedelta(days=10))
    assert assessment.tier == ImportanceTier.NORMAL


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


def test_decay_expiry_flips_low_back_to_normal(tmp_path):
    profile = _profile(tmp_path)
    for i in range(LOW_RUN_THRESHOLD):
        profile.record_signal(
            "newsletter@example.com", ActionSignal.IGNORED, ts=T0 + timedelta(days=i)
        )
    just_after = T0 + timedelta(days=LOW_RUN_THRESHOLD)
    assert profile.assess("newsletter@example.com", now=just_after).tier == (
        ImportanceTier.LOW
    )

    # Once the recorded signals are all older than DECAY_DAYS relative to
    # "now", they're no longer effective — the demotion heals.
    long_later = T0 + timedelta(days=DECAY_DAYS + LOW_RUN_THRESHOLD + 1)
    assessment = profile.assess("newsletter@example.com", now=long_later)
    assert assessment.tier == ImportanceTier.NORMAL
    assert assessment.reason == "no recorded signals"


# ---------------------------------------------------------------------------
# Pin wins over everything
# ---------------------------------------------------------------------------


def test_pin_wins_over_a_demoting_signal_history(tmp_path):
    profile = _profile(tmp_path)
    for i in range(5):
        profile.record_signal(
            "newsletter@example.com", ActionSignal.IGNORED, ts=T0 + timedelta(days=i)
        )
    profile.pin("newsletter@example.com", ImportanceTier.HIGH)

    assessment = profile.assess("newsletter@example.com", now=T0 + timedelta(days=10))
    assert assessment.tier == ImportanceTier.HIGH
    assert assessment.pinned is True
    assert "pinned" in assessment.reason


def test_unpin_restores_computed_tier(tmp_path):
    profile = _profile(tmp_path)
    for i in range(LOW_RUN_THRESHOLD):
        profile.record_signal(
            "newsletter@example.com", ActionSignal.IGNORED, ts=T0 + timedelta(days=i)
        )
    profile.pin("newsletter@example.com", ImportanceTier.HIGH)
    now = T0 + timedelta(days=LOW_RUN_THRESHOLD)
    assert profile.assess("newsletter@example.com", now=now).tier == ImportanceTier.HIGH

    assert profile.unpin("newsletter@example.com") is True
    assert profile.assess("newsletter@example.com", now=now).tier == ImportanceTier.LOW


def test_unpin_is_false_when_nothing_was_pinned(tmp_path):
    profile = _profile(tmp_path)
    assert profile.unpin("nobody@example.com") is False
    profile.record_signal("x@example.com", ActionSignal.APPROVED, ts=T0)
    assert profile.unpin("x@example.com") is False


# ---------------------------------------------------------------------------
# Bounded storage
# ---------------------------------------------------------------------------


def test_signal_cap_keeps_only_the_most_recent(tmp_path):
    profile = _profile(tmp_path)
    for i in range(MAX_SIGNALS + 5):
        profile.record_signal("x@example.com", ActionSignal.APPROVED, ts=T0 + timedelta(days=i))

    signals = profile.recent_signals("x@example.com", now=T0 + timedelta(days=MAX_SIGNALS + 10))
    assert len(signals) == MAX_SIGNALS
    # the earliest 5 were dropped, so the recorded window starts at day 5
    assert signals[0][1] == T0 + timedelta(days=5)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_persistence_roundtrip_through_a_fresh_instance(tmp_path):
    path = str(tmp_path / "importance.json")
    JsonImportanceProfile(path).record_signal(
        "x@example.com", ActionSignal.APPROVED, ts=T0
    )
    reloaded = JsonImportanceProfile(path)
    assert reloaded.senders() == ["x@example.com"]
    assert reloaded.assess("x@example.com", now=T0).tier == ImportanceTier.NORMAL


def test_fslock_file_is_created_alongside_state(tmp_path):
    path = tmp_path / "importance.json"
    JsonImportanceProfile(str(path)).record_signal(
        "x@example.com", ActionSignal.APPROVED, ts=T0
    )
    assert path.exists()
    assert (tmp_path / "importance.json.lock").exists()


def test_senders_lists_pin_only_entries_too(tmp_path):
    profile = _profile(tmp_path)
    profile.pin("pinned-only@example.com", ImportanceTier.HIGH)
    assert profile.senders() == ["pinned-only@example.com"]


# ---------------------------------------------------------------------------
# capture_action_signal dual-write (memory/signals.py)
# ---------------------------------------------------------------------------


class _FakeMemoryStore:
    def __init__(self):
        self.added: list[dict] = []

    def add(self, messages, *, user_id, metadata=None, infer=True):
        self.added.append({"messages": messages, "metadata": metadata, "infer": infer})
        return []

    def search(self, query, *, user_id, limit=8, min_score=None):
        return []

    def get_all(self, *, user_id, limit=100):
        return []

    def delete(self, memory_id):
        pass


class _FailingProfile:
    def record_signal(self, sender, signal, *, ts=None):
        raise RuntimeError("boom")


def test_capture_action_signal_dual_writes_to_profile(tmp_path):
    from attune.memory.signals import capture_action_signal

    store = _FakeMemoryStore()
    profile = _profile(tmp_path)

    capture_action_signal(
        store,
        user_id="u1",
        domain="mail",
        signal=ActionSignal.IGNORED,
        summary="ignored 2d",
        importance_profile=profile,
        sender="Newsletter@Example.com",
    )

    assert store.added  # memory write still happened
    assert profile.senders() == ["newsletter@example.com"]


def test_capture_action_signal_without_profile_or_sender_is_unchanged(tmp_path):
    from attune.memory.signals import capture_action_signal

    store = _FakeMemoryStore()
    capture_action_signal(
        store, user_id="u1", domain="mail",
        signal=ActionSignal.APPROVED, summary="ok",
    )
    assert len(store.added) == 1

    profile = _profile(tmp_path)
    capture_action_signal(
        store, user_id="u1", domain="mail",
        signal=ActionSignal.APPROVED, summary="ok",
        importance_profile=profile, sender=None,
    )
    assert len(store.added) == 2
    assert profile.senders() == []  # no sender -> profile untouched


def test_capture_action_signal_profile_failure_never_breaks_memory_write(tmp_path):
    from attune.memory.signals import capture_action_signal

    store = _FakeMemoryStore()
    capture_action_signal(
        store, user_id="u1", domain="mail",
        signal=ActionSignal.APPROVED, summary="ok",
        importance_profile=_FailingProfile(), sender="x@example.com",
    )
    assert len(store.added) == 1  # memory write happened despite the profile blowing up


# ---------------------------------------------------------------------------
# sweep_ignored / PendingApprovals wiring (orchestrator/pending.py)
# ---------------------------------------------------------------------------


def test_sweep_ignored_passes_sender_to_profile_when_present(tmp_path):
    from attune.orchestrator import JsonPendingApprovals, sweep_ignored

    reg = JsonPendingApprovals(str(tmp_path / "pending.json"))
    reg.register(
        lg_tid="gmail:t1:100", source_ref="t1", domain="mail",
        posted_at=T0, sender="newsletter@example.com",
    )
    profile = _profile(tmp_path)
    store = _FakeMemoryStore()

    swept = sweep_ignored(
        reg, store, user_id="u1", now=T0 + timedelta(hours=49),
        importance_profile=profile,
    )

    assert swept == 1
    assert profile.senders() == ["newsletter@example.com"]


def test_sweep_ignored_skips_profile_when_sender_absent(tmp_path):
    """Legacy entries registered before the sender field existed parse back
    with sender=None; capture_action_signal already no-ops the profile
    write in that case, so the memory write must still succeed."""
    from attune.orchestrator import JsonPendingApprovals, sweep_ignored

    reg = JsonPendingApprovals(str(tmp_path / "pending.json"))
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)
    profile = _profile(tmp_path)
    store = _FakeMemoryStore()

    swept = sweep_ignored(
        reg, store, user_id="u1", now=T0 + timedelta(hours=49),
        importance_profile=profile,
    )

    assert swept == 1
    assert len(store.added) == 1
    assert profile.senders() == []


def test_register_and_pending_are_backward_compatible_with_old_json(tmp_path):
    """A JSON file written before the ``sender`` field existed must still
    parse: PendingApproval.sender defaults to None."""
    import json

    from attune.orchestrator import JsonPendingApprovals

    path = tmp_path / "pending.json"
    path.write_text(json.dumps({
        "gmail:t1:100": {
            "source_ref": "t1",
            "domain": "mail",
            "posted_at": T0.isoformat(),
            "status": "pending",
        }
    }))
    reg = JsonPendingApprovals(str(path))
    entries = reg.pending()
    assert len(entries) == 1
    assert entries[0].sender is None


# ---------------------------------------------------------------------------
# CLI (attune importance)
# ---------------------------------------------------------------------------


def test_cli_list_shows_tier_and_reason(tmp_path, capsys):
    from attune.cli.importance_cmd import run_importance_list

    profile = _profile(tmp_path)
    profile.record_signal("vip@example.com", ActionSignal.APPROVED, ts=T0)

    code = run_importance_list(importance_profile=profile)
    assert code == 0
    out = capsys.readouterr().out
    assert "vip@example.com" in out
    assert "normal" in out


def test_cli_list_empty_profile(tmp_path, capsys):
    from attune.cli.importance_cmd import run_importance_list

    code = run_importance_list(importance_profile=_profile(tmp_path))
    assert code == 0
    assert "No senders recorded yet." in capsys.readouterr().out


def test_cli_show_prints_assessment_and_signals(tmp_path, capsys):
    from attune.cli.importance_cmd import run_importance_show

    profile = _profile(tmp_path)
    profile.record_signal("vip@example.com", ActionSignal.APPROVED, ts=T0)

    code = run_importance_show("vip@example.com", importance_profile=profile)
    assert code == 0
    out = capsys.readouterr().out
    assert "vip@example.com: normal" in out
    assert "approved" in out


def test_cli_pin_then_show_round_trips(tmp_path, capsys):
    from attune.cli.importance_cmd import run_importance_pin, run_importance_show

    profile = _profile(tmp_path)
    code = run_importance_pin("vip@example.com", "high", importance_profile=profile)
    assert code == 0
    assert "Pinned" in capsys.readouterr().out

    run_importance_show("vip@example.com", importance_profile=profile)
    out = capsys.readouterr().out
    assert "vip@example.com: high (pinned)" in out


def test_cli_pin_rejects_unknown_tier(tmp_path, capsys):
    from attune.cli.importance_cmd import run_importance_pin

    code = run_importance_pin(
        "vip@example.com", "urgent", importance_profile=_profile(tmp_path)
    )
    assert code == 2
    assert "Unknown tier" in capsys.readouterr().out


def test_cli_unpin(tmp_path, capsys):
    from attune.cli.importance_cmd import run_importance_pin, run_importance_unpin

    profile = _profile(tmp_path)
    run_importance_pin("vip@example.com", "high", importance_profile=profile)
    capsys.readouterr()

    code = run_importance_unpin("vip@example.com", importance_profile=profile)
    assert code == 0
    assert "Unpinned" in capsys.readouterr().out

    code = run_importance_unpin("vip@example.com", importance_profile=profile)
    assert "had no pin set" in capsys.readouterr().out
