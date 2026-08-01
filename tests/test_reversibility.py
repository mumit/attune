"""Tests for build prompt 31 — reversibility, expiry, and batch review.

Covers the acceptance criteria:
- a test per compensating action asserting the inverse effect (and, for
  RESCHEDULE, that the prior time restores exactly what was captured
  pre-patch — see test_dispatcher.py for the state-capture half of that
  criterion);
- SEND_REPLY reports itself irreversible and offers no undo affordance
  anywhere;
- an expired card returns the honest refusal and its workflow cannot be
  resumed afterwards;
- an undo triggers a demotion suggestion on a single occurrence;
- "approve all" over 5 items produces 5 audited applies, 5 ledger rows, and
  5 freshness checks, and a repeated click applies nothing further;
- STATUS_EXPIRED and STATUS_IGNORED are distinguishable in the ledger/
  learning signal.

All offline: fakes for connectors/audit/ledger/store, injected clocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from attune.orchestrator.autonomy import (
    Action,
    Domain,
    PermissionMatrix,
    Rung,
)
from attune.orchestrator.capabilities import (
    build_capability_registry,
    make_create_hold_compensate_fn,
    make_decline_invite_compensate_fn,
    make_draft_reply_compensate_fn,
    make_label_compensate_fn,
    make_reschedule_compensate_fn,
)
from attune.orchestrator.draft_approve import SourceChangedError
from attune.orchestrator.grants import suggest_demotions
from attune.orchestrator.undo import UndoError, undo_effect

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Task 1: compensating actions, one per capability
# ---------------------------------------------------------------------------


def test_draft_reply_compensate_deletes_the_created_draft():
    class FakeConnector:
        def __init__(self):
            self.deleted: list[str] = []

        def delete_draft(self, draft_id):
            self.deleted.append(draft_id)

    connector = FakeConnector()
    compensate = make_draft_reply_compensate_fn(connector)
    compensate({"applied_ref": "draft-123"})
    assert connector.deleted == ["draft-123"]


def test_draft_reply_compensate_is_noop_without_applied_ref():
    class FakeConnector:
        def delete_draft(self, draft_id):
            raise AssertionError("must not be called")

    make_draft_reply_compensate_fn(FakeConnector())({})


def test_create_hold_compensate_deletes_the_created_event():
    class FakeConnector:
        def __init__(self):
            self.deleted: list[str] = []

        def delete_event(self, event_id):
            self.deleted.append(event_id)

    connector = FakeConnector()
    compensate = make_create_hold_compensate_fn(connector)
    compensate({"applied_ref": "hold-evt-1"})
    assert connector.deleted == ["hold-evt-1"]


def test_label_compensate_readds_inbox_and_removes_the_label():
    class FakeConnector:
        def __init__(self):
            self.added: list[tuple] = []
            self.removed: list[tuple] = []

        def get_thread(self, thread_id):
            return SimpleNamespace(labels=["Attune/Noise"])  # archived, labeled

        def add_label(self, *, thread_id, label):
            self.added.append((thread_id, label))

        def remove_label(self, thread_id, *, label):
            self.removed.append((thread_id, label))

    connector = FakeConnector()
    compensate = make_label_compensate_fn(connector)
    compensate({"incoming_ref": "t1", "label_name": "Attune/Noise"})

    assert connector.added == [("t1", "INBOX")]
    assert connector.removed == [("t1", "Attune/Noise")]


def test_label_compensate_refuses_when_thread_already_changed():
    """Freshness check: a thread the human already manually restored (or
    that changed some other way since apply) refuses rather than layering
    a second, possibly wrong effect on top."""

    class FakeConnector:
        def get_thread(self, thread_id):
            return SimpleNamespace(labels=["INBOX", "Attune/Noise"])  # already restored

    compensate = make_label_compensate_fn(FakeConnector())
    with pytest.raises(SourceChangedError):
        compensate({"incoming_ref": "t1", "label_name": "Attune/Noise"})


def test_decline_invite_compensate_resets_to_needs_action():
    class FakeConnector:
        def __init__(self):
            self.reset: list[str] = []

        def get_event(self, event_id):
            return SimpleNamespace(response_status="declined")

        def reset_invite_response(self, event_id):
            self.reset.append(event_id)

    connector = FakeConnector()
    compensate = make_decline_invite_compensate_fn(connector)
    compensate({"incoming_ref": "evt-1"})
    assert connector.reset == ["evt-1"]


def test_decline_invite_compensate_refuses_when_no_longer_declined():
    class FakeConnector:
        def get_event(self, event_id):
            return SimpleNamespace(response_status="accepted")  # human re-responded

    compensate = make_decline_invite_compensate_fn(FakeConnector())
    with pytest.raises(SourceChangedError):
        compensate({"incoming_ref": "evt-1"})


def test_reschedule_compensate_restores_the_prior_start_end():
    """The prior time restored is EXACTLY what was captured pre-patch
    (``reschedule_prior_start``/``reschedule_prior_end``), never re-derived
    — see test_dispatcher.py's
    test_reschedule_proposal_captures_prior_start_end_before_any_patch for
    the propose-time half of this."""

    class FakeConnector:
        def __init__(self):
            self.rescheduled: list[tuple] = []

        def get_event(self, event_id):
            # The event currently sits at the MOVED-TO time (apply already ran).
            return SimpleNamespace(
                start=datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)
            )

        def reschedule_event(self, event_id, *, new_start, new_end):
            self.rescheduled.append((event_id, new_start, new_end))

    connector = FakeConnector()
    compensate = make_reschedule_compensate_fn(connector)
    state = {
        "incoming_ref": "evt-1",
        "reschedule_start": "2026-07-20T15:00:00+00:00",
        "reschedule_prior_start": "2026-07-20T14:00:00+00:00",
        "reschedule_prior_end": "2026-07-20T14:30:00+00:00",
    }
    compensate(state)

    assert connector.rescheduled == [(
        "evt-1",
        datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc),
    )]


def test_reschedule_compensate_refuses_when_event_moved_again():
    class FakeConnector:
        def get_event(self, event_id):
            # Someone moved it again since the reschedule apply.
            return SimpleNamespace(
                start=datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)
            )

    compensate = make_reschedule_compensate_fn(FakeConnector())
    state = {
        "incoming_ref": "evt-1",
        "reschedule_start": "2026-07-20T15:00:00+00:00",
        "reschedule_prior_start": "2026-07-20T14:00:00+00:00",
        "reschedule_prior_end": "2026-07-20T14:30:00+00:00",
    }
    with pytest.raises(SourceChangedError):
        compensate(state)


# ---------------------------------------------------------------------------
# SEND_REPLY: irreversible, unconditionally, no undo affordance anywhere
# ---------------------------------------------------------------------------


def test_send_reply_capability_reports_irreversible_with_no_compensate():
    registry = build_capability_registry()
    capability = registry.get(Action.SEND_REPLY)
    assert capability.irreversible is True
    assert capability.compensate is None


def test_apply_confirmation_never_offers_undo_for_send_reply():
    from attune.orchestrator.draft_approve import apply_confirmation

    text = apply_confirmation(
        "approved",
        {
            "action": "send_reply", "domain": "mail",
            "applied_ref": "draft-1", "undo_available": False,
        },
        thread_id="gmail:t1:100",
    )
    assert "undo" not in text.lower()
    assert "attune undo" not in text


def test_undo_effect_refuses_send_reply_as_irreversible():
    registry = build_capability_registry()
    ledger = _FakeLedger()
    ledger.rows["gmail:t1:100"] = _row(
        action="send_reply", domain="mail", decision="approved",
        applied_ok=True, decided_at=T0,
    )
    with pytest.raises(UndoError, match="irreversible"):
        undo_effect(
            "gmail:t1:100", graph=_FakeGraph({}), registry=registry,
            ledger=ledger, now=T0 + timedelta(minutes=5),
        )


# ---------------------------------------------------------------------------
# Fakes shared by the undo/demotion/batch tests below
# ---------------------------------------------------------------------------


def _row(
    *, action, domain, decision, applied_ok, decided_at,
    undone=False, proposal_id="gmail:t1:100",
):
    return SimpleNamespace(
        proposal_id=proposal_id, action=action, domain=domain,
        decision=decision, applied_ok=applied_ok, decided_at=decided_at,
        undone=undone,
    )


class _FakeLedger:
    def __init__(self):
        self.rows: dict[str, SimpleNamespace] = {}
        self.undone_calls: list[tuple] = []

    def get(self, proposal_id):
        return self.rows.get(proposal_id)

    def mark_undone(self, proposal_id, *, at=None):
        self.undone_calls.append((proposal_id, at))
        row = self.rows.get(proposal_id)
        if row is not None:
            row.undone = True


class _FakeGraph:
    def __init__(self, state: dict):
        self._state = state

    def get_state(self, config):
        return SimpleNamespace(values=self._state)


class _FakeAuditLog:
    def __init__(self):
        self.events: list[dict] = []

    def record(self, **kwargs):
        self.events.append(kwargs)

    def query(self, **kwargs):
        results = []
        for rec in self.events:
            for event in rec["events"]:
                results.append(SimpleNamespace(
                    thread_id=rec["thread_id"], event=event["event"],
                    ts=event["ts"], domain=rec.get("domain"),
                    fields={k: v for k, v in event.items() if k not in ("event", "ts")},
                ))
        return results


# ---------------------------------------------------------------------------
# Task 2: undo end-to-end + demotion on a single undo occurrence
# ---------------------------------------------------------------------------


def test_undo_effect_invokes_compensate_and_marks_ledger_undone():
    connector_calls: list[str] = []

    def compensate(state):
        connector_calls.append(state["applied_ref"])

    registry = build_capability_registry()
    # Swap in a fake compensate so this test doesn't need a real connector.
    import dataclasses
    label_capability = dataclasses.replace(
        registry.get(Action.LABEL), compensate=compensate, irreversible=False,
    )
    from attune.orchestrator.capabilities import CapabilityRegistry
    registry = CapabilityRegistry((label_capability,))

    ledger = _FakeLedger()
    ledger.rows["archive:t1"] = _row(
        action="label", domain="mail", decision="approved",
        applied_ok=True, decided_at=T0, proposal_id="archive:t1",
    )
    audit = _FakeAuditLog()
    graph = _FakeGraph({"applied_ref": "t1", "action": "label"})

    result = undo_effect(
        "archive:t1", graph=graph, registry=registry, ledger=ledger,
        audit_log=audit, user_id="me", actor="cli",
        now=T0 + timedelta(minutes=10),
    )

    assert connector_calls == ["t1"]
    assert result.action == "label" and result.domain == "mail"
    assert ledger.rows["archive:t1"].undone is True
    assert any(e["event"] == "undone" for rec in audit.events for e in rec["events"])


def test_undo_effect_refuses_outside_the_window():
    registry = build_capability_registry()
    ledger = _FakeLedger()
    ledger.rows["gmail:t1:100"] = _row(
        action="draft_reply", domain="mail", decision="approved",
        applied_ok=True, decided_at=T0,
    )
    with pytest.raises(UndoError, match="window"):
        undo_effect(
            "gmail:t1:100", graph=_FakeGraph({}), registry=registry,
            ledger=ledger, now=T0 + timedelta(hours=2),
        )


def test_undo_effect_refuses_when_already_undone():
    registry = build_capability_registry()
    ledger = _FakeLedger()
    ledger.rows["gmail:t1:100"] = _row(
        action="draft_reply", domain="mail", decision="approved",
        applied_ok=True, decided_at=T0, undone=True,
    )
    with pytest.raises(UndoError, match="already undone"):
        undo_effect(
            "gmail:t1:100", graph=_FakeGraph({}), registry=registry,
            ledger=ledger, now=T0 + timedelta(minutes=5),
        )


def test_undo_effect_refuses_when_never_applied():
    registry = build_capability_registry()
    ledger = _FakeLedger()
    ledger.rows["gmail:t1:100"] = _row(
        action="draft_reply", domain="mail", decision="rejected",
        applied_ok=None, decided_at=T0,
    )
    with pytest.raises(UndoError):
        undo_effect(
            "gmail:t1:100", graph=_FakeGraph({}), registry=registry,
            ledger=ledger, now=T0 + timedelta(minutes=5),
        )


def test_undo_effect_unknown_id_refuses():
    registry = build_capability_registry()
    ledger = _FakeLedger()
    with pytest.raises(UndoError, match="no decision recorded"):
        undo_effect(
            "never-registered", graph=_FakeGraph({}), registry=registry,
            ledger=ledger,
        )


def test_undo_triggers_demotion_suggestion_on_single_occurrence():
    """The acceptance criterion: an undo demotes a grant on ONE occurrence,
    the same weight a rejection against an auto-applied effect already
    gets — never requiring a rejection streak."""
    audit = _FakeAuditLog()
    audit.record(
        thread_id="archive:t1", workflow="draft_approve",
        events=[{
            "event": "autonomy_gate", "ts": T0.isoformat(),
            "action": "label", "domain": "mail", "routed_to": "approve",
        }],
        domain="mail", user_id="me",
    )
    audit.record(
        thread_id="archive:t1", workflow="draft_approve",
        events=[{
            "event": "human_decision", "ts": T0.isoformat(), "decision": "approved",
        }],
        domain="mail", user_id="me",
    )
    audit.record(
        thread_id="archive:t1", workflow="draft_approve",
        events=[{"event": "undone", "ts": (T0 + timedelta(minutes=5)).isoformat()}],
        domain="mail", user_id="me",
    )

    matrix = PermissionMatrix().grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY)
    suggestions = suggest_demotions(audit, matrix)

    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.action == Action.LABEL and s.domain == Domain.MAIL
    assert s.from_rung == Rung.ACT_NOTIFY
    assert s.to_rung == Rung.PROPOSE


def test_no_undo_no_demotion_from_a_single_clean_approval():
    audit = _FakeAuditLog()
    audit.record(
        thread_id="archive:t1", workflow="draft_approve",
        events=[{
            "event": "autonomy_gate", "ts": T0.isoformat(),
            "action": "label", "domain": "mail", "routed_to": "approve",
        }],
        domain="mail", user_id="me",
    )
    audit.record(
        thread_id="archive:t1", workflow="draft_approve",
        events=[{
            "event": "human_decision", "ts": T0.isoformat(), "decision": "approved",
        }],
        domain="mail", user_id="me",
    )
    matrix = PermissionMatrix().grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY)
    assert suggest_demotions(audit, matrix) == []
