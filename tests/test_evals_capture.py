"""``attune eval capture`` (build prompt 27, task 1): explicit, local, and
idempotent — never an automatic harvest of a principal's mail."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from attune.orchestrator.ledger import SqliteDecisionLedger
from attune.evals.capture import build_case, run_eval_capture
from attune.evals.schema import NO_REPLY_GOLD, CaseKind, redact

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _propose_and_decide(ledger, *, proposal_id, decision, domain="mail", action="draft_reply"):
    from attune.orchestrator.ledger import LedgerRow

    ledger.propose(LedgerRow(
        proposal_id=proposal_id, thread_id=proposal_id, domain=domain, action=action,
        proposed_at=NOW,
    ))
    ledger.complete(proposal_id, decision=decision, decided_at=NOW)


def test_redact_scrubs_emails_urls_and_phones():
    text = "Contact me at jane@example.com or https://example.com/x, call 555-123-4567."
    redacted = redact(text)
    assert "jane@example.com" not in redacted
    assert "https://example.com/x" not in redacted
    assert "555-123-4567" not in redacted
    assert "[REDACTED-EMAIL]" in redacted
    assert "[REDACTED-URL]" in redacted
    assert "[REDACTED-PHONE]" in redacted


def test_build_case_edit_uses_final_text_as_gold():
    from attune.orchestrator.ledger import LedgerRow

    row = LedgerRow(
        proposal_id="p1", thread_id="p1", domain="mail", action="draft_reply",
        proposed_at=NOW, decision="edited", decided_at=NOW,
        triage_priority="routine", sender_importance_tier="high",
    )
    state = {
        "proposed_draft": "Hi, sure thing.",
        "final_text": "Hi Jane, absolutely — I'll send it over by Friday.",
        "incoming_summary": "Can you send the report? jane@example.com",
    }
    case = build_case(row, state)
    assert case.kind is CaseKind.EDIT
    assert case.gold_text == "Hi Jane, absolutely — I'll send it over by Friday."
    assert "jane@example.com" not in case.inputs["incoming_summary"]


def test_build_case_reject_uses_no_reply_gold():
    from attune.orchestrator.ledger import LedgerRow

    row = LedgerRow(
        proposal_id="p2", thread_id="p2", domain="mail", action="draft_reply",
        proposed_at=NOW, decision="rejected", decided_at=NOW,
    )
    state = {"proposed_draft": "Sure, I can do that.", "incoming_summary": "Can you help?"}
    case = build_case(row, state)
    assert case.kind is CaseKind.REJECT
    assert case.gold_text == NO_REPLY_GOLD


def test_build_case_returns_none_for_approved_or_pending():
    from attune.orchestrator.ledger import LedgerRow

    approved = LedgerRow(
        proposal_id="p3", thread_id="p3", domain="mail", action="draft_reply",
        proposed_at=NOW, decision="approved",
    )
    assert build_case(approved, {"proposed_draft": "x", "final_text": "x"}) is None

    pending = LedgerRow(
        proposal_id="p4", thread_id="p4", domain="mail", action="draft_reply", proposed_at=NOW,
    )
    assert build_case(pending, {}) is None


def test_run_eval_capture_writes_files_and_is_idempotent(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    _propose_and_decide(ledger, proposal_id="edit-1", decision="edited")
    _propose_and_decide(ledger, proposal_id="reject-1", decision="rejected")
    _propose_and_decide(ledger, proposal_id="approved-1", decision="approved")

    states = {
        "edit-1": {"proposed_draft": "Hi", "final_text": "Hi there!", "incoming_summary": "s"},
        "reject-1": {"proposed_draft": "Hi", "incoming_summary": "s"},
        "approved-1": {"proposed_draft": "Hi", "final_text": "Hi", "incoming_summary": "s"},
    }
    cases_dir = str(tmp_path / "cases")
    run_eval_capture(ledger=ledger, state_lookup=lambda tid: states.get(tid), cases_dir=cases_dir)

    written = sorted(os.listdir(cases_dir))
    assert written == ["edit-1.json", "reject-1.json"]  # approved never captured

    with open(os.path.join(cases_dir, "edit-1.json")) as f:
        raw = json.load(f)
    assert raw["gold_text"] == "Hi there!"

    # Idempotent: hand-edit the file, re-run, and the edit survives.
    path = os.path.join(cases_dir, "edit-1.json")
    with open(path) as f:
        raw = json.load(f)
    raw["gold_text"] = "hand-reviewed edit"
    with open(path, "w") as f:
        json.dump(raw, f)

    run_eval_capture(ledger=ledger, state_lookup=lambda tid: states.get(tid), cases_dir=cases_dir)
    with open(path) as f:
        raw_after = json.load(f)
    assert raw_after["gold_text"] == "hand-reviewed edit"


def test_run_eval_capture_skips_threads_with_no_checkpoint_state(tmp_path):
    ledger = SqliteDecisionLedger(str(tmp_path / "ledger.db"))
    _propose_and_decide(ledger, proposal_id="orphan-1", decision="edited")

    cases_dir = str(tmp_path / "cases")
    run_eval_capture(ledger=ledger, state_lookup=lambda tid: None, cases_dir=cases_dir)

    assert not os.path.exists(cases_dir) or os.listdir(cases_dir) == []
