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
    GrantScope,
    JsonPermissionMatrixStore,
    Rung,
    default_matrix,
    grant,
    render_scope,
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


def test_old_format_flat_rung_file_loads_as_unscoped_grant(tmp_path):
    """A file written by the pre-scoping schema — a bare rung int per key —
    must still load, as the unscoped grant. This is a literal fixture of the
    OLD on-disk shape, not round-tripped through the new save()."""
    path = tmp_path / "grants.json"
    path.write_text('{"label|mail": 3, "draft_reply|mail": 2}')

    loaded = JsonPermissionMatrixStore(str(path)).load()
    assert loaded.max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY
    assert loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE
    entries = loaded.grants[(Action.LABEL, Domain.MAIL)]
    assert len(entries) == 1 and entries[0].scope is None


def test_scoped_grant_round_trips_through_store(tmp_path):
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    scope = GrantScope(
        priorities=frozenset({"routine"}), tiers=frozenset({"high", "normal"})
    )
    # default_matrix()'s existing unscoped PROPOSE grant on (DRAFT_REPLY,
    # MAIL) stays, alongside a new scoped ACT_NOTIFY one -- multi-grant
    # serialization for the same (action, domain) pair.
    matrix = default_matrix().grant(
        Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY, scope=scope
    )
    store.save(matrix)

    loaded = JsonPermissionMatrixStore(str(tmp_path / "grants.json")).load()
    assert (
        loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="routine", tier="high")
        == Rung.ACT_NOTIFY
    )
    # unscoped fallback (this pair's other grant) still there too
    assert loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE
    entries = loaded.grants[(Action.DRAFT_REPLY, Domain.MAIL)]
    assert len(entries) == 2
    scoped_entry = next(sg for sg in entries if sg.scope is not None)
    assert scoped_entry.scope == scope


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
    # original is untouched by the grant() call (immutability) — its LABEL
    # rung is whatever default_matrix() ships (Phase 3 stage 1, G9: PROPOSE,
    # not the bare READ_ONLY floor other still-dormant actions default to).
    assert original.max_rung(Action.LABEL, Domain.MAIL) == Rung.PROPOSE
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


def test_suggest_graduations_ignores_scoped_grants(tmp_path):
    """Phase 4 stage 1: track_records/suggest_graduations read the matrix
    through max_rung(action, domain) with NO priority/tier context, so a
    scoped grant on the pair (which needs that context to match at all)
    simply never participates — the earned-track-record bar for the
    UNSCOPED grant is unaffected by scoped grants sitting alongside it."""
    log = _audit_file_with_decisions(tmp_path, ["approved"] * 12)

    # A scoped grant already at ACT_NOTIFY for ROUTINE items must not
    # suppress the suggestion for the unscoped grant, which is still below
    # ACT_NOTIFY.
    matrix = default_matrix().grant(
        Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY,
        scope=GrantScope(priorities=frozenset({"routine"})),
    )
    suggestions = suggest_graduations(log, matrix, now=NOW)
    assert len(suggestions) == 1
    assert "draft_reply mail act_notify" in suggestions[0].render()

    # track_records doesn't take a matrix at all -- it folds the audit log
    # alone, so this is really pinning that suggest_graduations' own
    # matrix.max_rung(*key) check (no priority/tier passed) can't
    # accidentally see the scoped grant either.
    record = track_records(log, now=NOW)[(Action.DRAFT_REPLY, Domain.MAIL)]
    assert record.total == 12 and record.approved == 12
    assert matrix.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE
    assert (
        matrix.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="routine")
        == Rung.ACT_NOTIFY
    )


# ---------------------------------------------------------------------------
# Demotion (Phase 4 item 5, docs/future-state.md) — graduation's mirror image
# ---------------------------------------------------------------------------


def _audit_file_with_routed_decisions(tmp_path, rows, *, action="draft_reply", domain="mail"):
    """Like _audit_file_with_decisions but lets each row control routed_to
    too ("approve"/"auto_apply") — needed for demotion's "any rejection of
    an auto-applied effect" rule, which the fixed routed_to="approve" in
    _audit_file_with_decisions can't express. ``rows`` is a list of
    ``(decision, routed_to)`` pairs."""
    log = JsonlAuditLog(str(tmp_path / "audit.jsonl"))
    for i, (decision, routed_to) in enumerate(rows):
        tid = f"gmail:{action}:{i}:100"
        log.record(
            thread_id=tid, workflow="draft_approve",
            events=[{
                "event": "autonomy_gate", "ts": NOW.isoformat(),
                "action": action, "domain": domain,
                "max_rung": 3, "routed_to": routed_to,
            }],
            domain=domain, user_id="u1",
        )
        if decision is not None:
            log.record(
                thread_id=tid, workflow="draft_approve",
                events=[{
                    "event": "human_decision", "ts": NOW.isoformat(),
                    "decision": decision,
                }],
                domain=domain, user_id="u1",
            )
    return log


def test_demotion_suggested_after_two_rejections(tmp_path):
    from attune.orchestrator import suggest_demotions

    rows = [("rejected", "approve"), ("rejected", "approve")] + [
        ("approved", "approve") for _ in range(8)
    ]
    log = _audit_file_with_routed_decisions(tmp_path, rows)
    matrix = default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)

    suggestions = suggest_demotions(log, matrix)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert (s.action, s.domain, s.from_rung, s.to_rung) == (
        Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY, Rung.PROPOSE,
    )
    assert s.rejected == 2
    assert "attune autonomy grant draft_reply mail propose" in s.render()


def test_no_demotion_with_only_one_ordinary_rejection(tmp_path):
    from attune.orchestrator import suggest_demotions

    rows = [("rejected", "approve")] + [("approved", "approve") for _ in range(9)]
    log = _audit_file_with_routed_decisions(tmp_path, rows)
    matrix = default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)

    assert suggest_demotions(log, matrix) == []


def test_single_auto_applied_rejection_triggers_demotion_alone(tmp_path):
    """The stronger-evidence clause: a single rejection recorded against an
    auto-applied effect (routed_to="auto_apply") triggers demotion even
    though ordinary rejections need 2+. Implemented literally/defensively —
    the live graph cannot produce this combination today (an auto-applied
    run never reaches human_decision at all), but the check is ready for
    the day some future affordance (e.g. "flag this auto-acted effect as
    wrong") produces it for real. See docs/decisions.md."""
    from attune.orchestrator import suggest_demotions

    rows = [("rejected", "auto_apply")] + [("approved", "approve") for _ in range(9)]
    log = _audit_file_with_routed_decisions(tmp_path, rows)
    matrix = default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)

    suggestions = suggest_demotions(log, matrix)
    assert len(suggestions) == 1
    assert suggestions[0].rejected == 1


def test_no_demotion_at_or_below_propose(tmp_path):
    """Only grants ABOVE PROPOSE are ever examined — PROPOSE is already the
    safe, always-approve floor; there's nothing to demote it to."""
    from attune.orchestrator import suggest_demotions

    rows = [("rejected", "approve") for _ in range(5)] + [
        ("approved", "approve") for _ in range(5)
    ]
    log = _audit_file_with_routed_decisions(tmp_path, rows)
    matrix = default_matrix()  # DRAFT_REPLY/MAIL sits at PROPOSE by default

    assert suggest_demotions(log, matrix) == []


def test_demotion_examines_scoped_grants_and_preserves_scope(tmp_path):
    """Unlike suggest_graduations (unscoped only), suggest_demotions walks
    EVERY grant entry above PROPOSE, scoped or not — and the suggestion
    carries the scope through so approving it can re-grant the exact same
    scoped entry at PROPOSE, not the unscoped one."""
    from attune.orchestrator import suggest_demotions

    rows = [("rejected", "approve"), ("rejected", "approve")] + [
        ("approved", "approve") for _ in range(8)
    ]
    log = _audit_file_with_routed_decisions(tmp_path, rows)
    scope = GrantScope(priorities=frozenset({"routine"}))
    matrix = default_matrix().grant(
        Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY, scope=scope,
    )

    suggestions = suggest_demotions(log, matrix)
    assert len(suggestions) == 1
    assert suggestions[0].scope == scope
    assert "routine" in suggestions[0].render()
    assert "--priority routine" in suggestions[0].render()


def test_demotion_window_is_last_ten_decisions_not_full_history(tmp_path):
    """The window is a COUNT (last 10), not a calendar-time window like
    track_records' 30 days — old rejections outside the window don't count."""
    from attune.orchestrator import suggest_demotions

    # Two OLD rejections, then 10 clean approvals -- only the last 10 rows
    # (all approved) should be in the window, so no demotion.
    rows = [("rejected", "approve"), ("rejected", "approve")] + [
        ("approved", "approve") for _ in range(10)
    ]
    log = _audit_file_with_routed_decisions(tmp_path, rows)
    matrix = default_matrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)

    assert suggest_demotions(log, matrix) == []


# ---------------------------------------------------------------------------
# Graduation/demotion approval cards (Phase 4 stage 2, G13)
# ---------------------------------------------------------------------------

from attune.orchestrator.grants import (  # noqa: E402
    GRADUATION_CARD_EXCLUDED_ACTIONS,
    GRADUATION_CARD_MAX_RUNG,
    JsonGraduationState,
    demotion_thread_id,
    graduation_thread_id,
    resolve_autonomy_card,
)


def test_graduation_thread_id_format():
    tid = graduation_thread_id(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    assert tid == "graduation:draft_reply:mail:act_notify"


def test_demotion_thread_id_format():
    tid = demotion_thread_id(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
    assert tid == "demotion:draft_reply:mail:propose"


def test_graduation_state_card_round_trips(tmp_path):
    state = JsonGraduationState(str(tmp_path / "graduation_state.json"))
    scope = GrantScope(priorities=frozenset({"routine"}))
    state.record_card(
        "demotion:draft_reply:mail:propose", kind="demotion",
        action=Action.DRAFT_REPLY, domain=Domain.MAIL, to_rung=Rung.PROPOSE,
        scope=scope,
    )
    card = state.get_card("demotion:draft_reply:mail:propose")
    assert card == {
        "kind": "demotion", "action": Action.DRAFT_REPLY, "domain": Domain.MAIL,
        "to_rung": Rung.PROPOSE, "scope": scope,
    }
    state.remove_card("demotion:draft_reply:mail:propose")
    assert state.get_card("demotion:draft_reply:mail:propose") is None


def test_graduation_state_missing_card_is_none(tmp_path):
    state = JsonGraduationState(str(tmp_path / "graduation_state.json"))
    assert state.get_card("graduation:draft_reply:mail:act_notify") is None


def test_graduation_state_cooldown_round_trips(tmp_path):
    state = JsonGraduationState(str(tmp_path / "graduation_state.json"))
    key = "graduation:draft_reply:mail:act_notify"
    assert state.in_cooldown(key, now=NOW) is False

    state.record_rejection(key, at=NOW)
    assert state.in_cooldown(key, now=NOW) is True
    assert state.in_cooldown(key, now=NOW + timedelta(days=29)) is True
    assert state.in_cooldown(key, now=NOW + timedelta(days=31)) is False


def _card_state_with(tmp_path, thread_id, **card_kwargs):
    state = JsonGraduationState(str(tmp_path / "graduation_state.json"))
    state.record_card(thread_id, **card_kwargs)
    return state


def test_resolve_autonomy_card_approve_grants_and_removes_card(tmp_path):
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    matrix = default_matrix()
    thread_id = graduation_thread_id(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    state = _card_state_with(
        tmp_path, thread_id, kind="graduation",
        action=Action.DRAFT_REPLY, domain=Domain.MAIL, to_rung=Rung.ACT_NOTIFY,
    )
    audit = FakeAuditLog()

    result = resolve_autonomy_card(
        thread_id, "approved", store=store, matrix=matrix,
        cooldown_state=state, audit_log=audit, user_id="u1",
    )

    assert result["resolution"] == "granted"
    loaded = store.load()
    assert loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.ACT_NOTIFY
    assert state.get_card(thread_id) is None  # snapshot cleaned up


def test_resolve_autonomy_card_edit_grants_same_as_approve(tmp_path):
    """Edit doesn't make sense for a suggestion (there's nothing to edit
    free-text) -- it's treated as an approve, same precedent as archive/
    decline/reschedule's deterministic draft_fns."""
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    matrix = default_matrix()
    thread_id = graduation_thread_id(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    state = _card_state_with(
        tmp_path, thread_id, kind="graduation",
        action=Action.DRAFT_REPLY, domain=Domain.MAIL, to_rung=Rung.ACT_NOTIFY,
    )

    result = resolve_autonomy_card(
        thread_id, "edited", store=store, matrix=matrix,
        cooldown_state=state, audit_log=FakeAuditLog(), user_id="u1",
    )
    assert result["resolution"] == "granted"


def test_resolve_autonomy_card_reject_records_cooldown_not_grant(tmp_path):
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    matrix = default_matrix()
    thread_id = graduation_thread_id(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    state = _card_state_with(
        tmp_path, thread_id, kind="graduation",
        action=Action.DRAFT_REPLY, domain=Domain.MAIL, to_rung=Rung.ACT_NOTIFY,
    )

    result = resolve_autonomy_card(
        thread_id, "rejected", store=store, matrix=matrix,
        cooldown_state=state, audit_log=FakeAuditLog(), user_id="u1", now=NOW,
    )

    assert result["resolution"] == "rejected"
    assert store.load() is None  # nothing granted
    assert state.get_card(thread_id) is None
    assert state.in_cooldown(thread_id, now=NOW) is True


def test_resolve_autonomy_card_refuses_send_reply_even_if_snapshot_claims_it(tmp_path):
    """HARD CEILING, defense in depth: a forged/stale GRADUATION card
    snapshot claiming SEND_REPLY must still refuse — re-checked here
    against the PERSISTED snapshot, not just wherever a card was
    originally (correctly) built. (A demotion-kind card is the one
    deliberate exception, because it only lowers; see
    test_resolve_autonomy_card_allows_send_reply_demotion_to_propose.)"""
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    matrix = default_matrix()
    thread_id = "graduation:send_reply:mail:act_notify"
    state = _card_state_with(
        tmp_path, thread_id, kind="graduation",
        action=Action.SEND_REPLY, domain=Domain.MAIL, to_rung=Rung.ACT_NOTIFY,
    )

    result = resolve_autonomy_card(
        thread_id, "approved", store=store, matrix=matrix,
        cooldown_state=state, audit_log=FakeAuditLog(), user_id="u1",
    )

    assert result["resolution"] == "refused_ceiling"
    assert store.load() is None
    assert state.get_card(thread_id) is None


def test_resolve_autonomy_card_refuses_above_act_notify_even_for_demotion_kind(tmp_path):
    """A demotion card's own defense-in-depth check: demotion may only
    LOWER, so a demotion-labeled snapshot somehow claiming a target above
    PROPOSE (here AUTONOMOUS) still refuses."""
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    matrix = default_matrix()
    thread_id = "demotion:draft_reply:mail:autonomous"
    state = _card_state_with(
        tmp_path, thread_id, kind="demotion",
        action=Action.DRAFT_REPLY, domain=Domain.MAIL, to_rung=Rung.AUTONOMOUS,
    )

    result = resolve_autonomy_card(
        thread_id, "approved", store=store, matrix=matrix,
        cooldown_state=state, audit_log=FakeAuditLog(), user_id="u1",
    )

    assert result["resolution"] == "refused_ceiling"
    assert store.load() is None


def test_resolve_autonomy_card_allows_send_reply_demotion_to_propose(tmp_path):
    """The ceiling binds GRADUATIONS (raising authority). A demotion card
    for a CLI-granted SEND_REPLY at ACT_NOTIFY must be approvable — it
    LOWERS autonomy back to human-approval-per-item, and refusing it by
    action would block the human from reducing send autonomy through the
    card, which is backwards from safety."""
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    matrix = default_matrix().grant(
        Action.SEND_REPLY, Domain.MAIL, Rung.ACT_NOTIFY
    )
    thread_id = "demotion:send_reply:mail:propose"
    state = _card_state_with(
        tmp_path, thread_id, kind="demotion",
        action=Action.SEND_REPLY, domain=Domain.MAIL, to_rung=Rung.PROPOSE,
    )

    result = resolve_autonomy_card(
        thread_id, "approved", store=store, matrix=matrix,
        cooldown_state=state, audit_log=FakeAuditLog(), user_id="u1",
    )

    assert result["resolution"] == "granted"
    assert result["to_rung"] == "PROPOSE"
    persisted = store.load()
    assert persisted is not None
    assert persisted.max_rung(Action.SEND_REPLY, Domain.MAIL) == Rung.PROPOSE
    assert state.get_card(thread_id) is None


def test_resolve_autonomy_card_unknown_thread_id_is_handled(tmp_path):
    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    state = JsonGraduationState(str(tmp_path / "graduation_state.json"))

    result = resolve_autonomy_card(
        "graduation:draft_reply:mail:act_notify", "approved",
        store=store, matrix=default_matrix(), cooldown_state=state,
        audit_log=FakeAuditLog(), user_id="u1",
    )

    assert result["resolution"] == "unknown_card"


def test_resolve_autonomy_card_already_handled_via_pending_claim(tmp_path):
    """A double-claim (two clicks on the same card) is refused via the SAME
    pending-registry claim mechanism every other approval card uses."""
    class _FakePending:
        def claim(self, thread_id, *, actor=None):
            return False  # already resolved by someone else

    store = JsonPermissionMatrixStore(str(tmp_path / "grants.json"))
    state = JsonGraduationState(str(tmp_path / "graduation_state.json"))

    result = resolve_autonomy_card(
        "graduation:draft_reply:mail:act_notify", "approved",
        store=store, matrix=default_matrix(), cooldown_state=state,
        audit_log=FakeAuditLog(), user_id="u1", pending=_FakePending(),
    )

    assert result["resolution"] == "already_handled"


def test_ceiling_constants_match_the_module_docstrings():
    assert GRADUATION_CARD_EXCLUDED_ACTIONS == frozenset({Action.SEND_REPLY})
    assert GRADUATION_CARD_MAX_RUNG == Rung.ACT_NOTIFY


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


def test_cli_grant_send_reply_refuses_when_disabled(tmp_path, capsys):
    """Phase 4 stage 2, G15: while ATTUNE_MAIL_SEND_ENABLED is off, granting
    send_reply REFUSES outright (non-zero exit, actionable message) rather
    than warning-but-granting an inert entry — rule 4's "no shortcuts" now
    extends to not even persisting the grant."""
    settings = _settings(tmp_path)
    code = run_autonomy_grant(
        "send_reply", "mail", "propose", settings=settings, audit_log=FakeAuditLog()
    )
    out = capsys.readouterr().out
    assert code != 0
    assert "ATTUNE_MAIL_SEND_ENABLED" in out
    assert "gmail.send" in out
    # and nothing was persisted
    assert JsonPermissionMatrixStore(settings.autonomy_state_path).load() is None


def test_cli_grant_send_reply_proceeds_when_enabled(tmp_path, capsys):
    """Contrast case: with sending structurally enabled, granting send_reply
    proceeds exactly like any other grant — no refusal, no special note."""
    settings = Settings.from_env({
        "ATTUNE_MEM0_URL": "",
        "ATTUNE_AUTONOMY_STATE_PATH": str(tmp_path / "grants.json"),
        "ATTUNE_AUDIT_LOG_PATH": str(tmp_path / "audit.jsonl"),
        "ATTUNE_MAIL_SEND_ENABLED": "1",
    })
    code = run_autonomy_grant(
        "send_reply", "mail", "act_notify", settings=settings, audit_log=FakeAuditLog()
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Granted" in out
    loaded = JsonPermissionMatrixStore(settings.autonomy_state_path).load()
    assert loaded.max_rung(Action.SEND_REPLY, Domain.MAIL) == Rung.ACT_NOTIFY


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


# ---------------------------------------------------------------------------
# CLI scope flags (Phase 4 stage 1, G14)
# ---------------------------------------------------------------------------


def test_cli_grant_with_priority_and_tier_scope(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = run_autonomy_grant(
        "draft_reply", "mail", "act_notify",
        priority="routine", tier="high,normal",
        settings=settings, audit_log=FakeAuditLog(),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "scoped to" in out and "routine" in out and "high,normal" in out

    loaded = JsonPermissionMatrixStore(settings.autonomy_state_path).load()
    assert (
        loaded.max_rung(
            Action.DRAFT_REPLY, Domain.MAIL, priority="routine", tier="high"
        )
        == Rung.ACT_NOTIFY
    )
    # doesn't touch the pre-existing unscoped PROPOSE grant on this pair
    assert loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE


def test_cli_grant_unscoped_act_level_notes_the_urgent_rule(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = run_autonomy_grant(
        "draft_reply", "mail", "act_notify", settings=settings, audit_log=FakeAuditLog(),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "urgent-interrupt rule" in out


def test_cli_grant_rejects_empty_scope_value(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = run_autonomy_grant(
        "draft_reply", "mail", "act_notify", priority="",
        settings=settings, audit_log=FakeAuditLog(),
    )
    # an empty --priority string is falsy -> treated as absent (unscoped);
    # a truly empty SET (e.g. a stray comma) is the actual rejection case
    assert code == 0
    capsys.readouterr()

    code2 = run_autonomy_grant(
        "draft_reply", "mail", "act_notify", priority="routine,",
        settings=settings, audit_log=FakeAuditLog(),
    )
    assert code2 == 2
    out = capsys.readouterr().out
    assert "invalid/empty" in out


def test_cli_grant_rejects_unknown_scope_value(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = run_autonomy_grant(
        "draft_reply", "mail", "act_notify", tier="urgent",
        settings=settings, audit_log=FakeAuditLog(),
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "invalid/empty" in out


def test_cli_revoke_with_scope_removes_only_matching_grant(tmp_path, capsys):
    settings = _settings(tmp_path)
    audit = FakeAuditLog()
    run_autonomy_grant(
        "draft_reply", "mail", "act_notify", priority="routine",
        settings=settings, audit_log=audit,
    )
    code = run_autonomy_revoke(
        "draft_reply", "mail", priority="routine",
        settings=settings, audit_log=audit,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "that grant only" in out

    loaded = JsonPermissionMatrixStore(settings.autonomy_state_path).load()
    # the scoped grant is gone...
    assert (
        loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="routine")
        == Rung.PROPOSE
    )
    # ...but the pre-existing unscoped PROPOSE grant on this pair survives
    assert loaded.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE


def test_show_matrix_renders_scope_readably():
    matrix = default_matrix().grant(
        Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY,
        scope=GrantScope(priorities=frozenset({"routine"}), tiers=frozenset({"high", "normal"})),
    )
    text = show_matrix(matrix)
    assert "[routine; tier: high,normal]" in text
    # this grant is SCOPED, so it does not, by itself, trigger the
    # urgent-interrupt footnote (that's reserved for unscoped act-level
    # grants — see the next test).
    assert "urgent-interrupt rule" not in text


def test_show_matrix_notes_urgent_rule_only_when_unscoped_act_level_present():
    # default_matrix() has no unscoped grant at ACT_NOTIFY+ -> no note
    assert "urgent-interrupt rule" not in show_matrix(default_matrix())
    # granting one, unscoped, at ACT_NOTIFY -> the note appears
    matrix = default_matrix().grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY)
    assert "urgent-interrupt rule" in show_matrix(matrix)


def test_render_scope_matches_show_matrix_format():
    scope = GrantScope(priorities=frozenset({"routine"}), tiers=frozenset({"high", "normal"}))
    assert render_scope(scope) == "[routine; tier: high,normal]"
    assert render_scope(None) == ""


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

    # never saved -> conservative default (Phase 3 stage 1, G9: LABEL/MAIL
    # ships PROPOSE in default_matrix() itself — still conservative, since
    # PROPOSE always interrupts for human approval; it just isn't the bare
    # READ_ONLY floor of a still-dormant action).
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.PROPOSE

    store.save(default_matrix().grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY))
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.ACT_NOTIFY

    # revocation bites too (force a distinct mtime for coarse filesystems)
    store.save(default_matrix())
    _os.utime(store._path, (_time.time() + 2, _time.time() + 2))
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.PROPOSE


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
    # corrupt file -> conservative default (default_matrix()'s own PROPOSE
    # grant for LABEL/MAIL, Phase 3 stage 1 G9), never cached autonomous
    # authority.
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.PROPOSE
    assert provider().max_rung(Action.SEND_REPLY, Domain.MAIL) == Rung.READ_ONLY

    path.unlink()
    assert provider().max_rung(Action.LABEL, Domain.MAIL) == Rung.PROPOSE


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
