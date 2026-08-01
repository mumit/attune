"""Judge-human agreement and the 75% CI gate (build prompt 27, task 3):
below-threshold or unmeasured domains never gate — enforced in code."""

from __future__ import annotations

from attune.evals.agreement import (
    AGREEMENT_THRESHOLD,
    LabelRecord,
    compute_agreement,
    domain_gates,
    load_agreement,
    load_labels,
    run_label_session,
    save_agreement,
)


def test_compute_agreement_per_domain():
    records = [
        LabelRecord("c1", "mail", "candidate", "candidate"),
        LabelRecord("c2", "mail", "gold", "candidate"),
        LabelRecord("c3", "mail", "gold", "gold"),
        LabelRecord("c4", "calendar", "tie", "tie"),
    ]
    agreement = compute_agreement(records)
    assert agreement["mail"] == 2 / 3
    assert agreement["calendar"] == 1.0


def test_domain_gates_requires_threshold():
    agreement = {"mail": 0.9, "calendar": 0.5}
    assert domain_gates(agreement, "mail") is True
    assert domain_gates(agreement, "calendar") is False


def test_domain_gates_exactly_at_threshold_gates():
    agreement = {"mail": AGREEMENT_THRESHOLD}
    assert domain_gates(agreement, "mail") is True


def test_unmeasured_domain_never_gates():
    agreement = {"mail": 0.9}
    assert domain_gates(agreement, "chat") is False


def test_save_and_load_agreement_roundtrip(tmp_path):
    path = str(tmp_path / "agreement.json")
    assert load_agreement(path) == {}
    save_agreement(path, {"mail": 0.8, "calendar": 0.6})
    assert load_agreement(path) == {"mail": 0.8, "calendar": 0.6}


class _Case:
    def __init__(self, case_id, domain, proposed_text="p", gold_text="g"):
        self.case_id = case_id
        self.domain = domain
        self.proposed_text = proposed_text
        self.gold_text = gold_text


def test_run_label_session_writes_jsonl_and_appends(tmp_path):
    path = str(tmp_path / "labels" / "mail.jsonl")
    cases = [_Case("c1", "mail"), _Case("c2", "mail")]

    judge_answers = iter(["candidate", "gold"])
    human_answers = iter(["candidate", "candidate"])

    records = run_label_session(
        cases,
        judge_fn=lambda case: next(judge_answers),
        ask_human=lambda case: next(human_answers),
        labels_path=path,
    )
    assert len(records) == 2
    assert records[0].agrees() is True
    assert records[1].agrees() is False

    # A second session appends rather than clobbering the first.
    more_cases = [_Case("c3", "mail")]
    run_label_session(
        more_cases,
        judge_fn=lambda case: "tie",
        ask_human=lambda case: "tie",
        labels_path=path,
    )
    loaded = load_labels(path)
    assert len(loaded) == 3
    assert {r.case_id for r in loaded} == {"c1", "c2", "c3"}


def test_load_labels_missing_file_is_empty(tmp_path):
    assert load_labels(str(tmp_path / "nope.jsonl")) == []
