"""The eval harness (build prompt 27, ``docs/plan-2026-h2.md`` P2): pairwise
judging against what the human actually sent, trajectory-level assertions,
a triage regression set, an injection-resistance suite, and the CI
regression gate that ties them together. See each submodule's docstring for
its slice; ``runner.run_eval`` assembles the whole
:class:`~.report.EvalReport`.
"""

from .agreement import (
    AGREEMENT_THRESHOLD,
    LabelRecord,
    compute_agreement,
    domain_gates,
    load_agreement,
    load_labels,
    run_label_session,
    save_agreement,
)
from .injection import (
    InjectionCase,
    InjectionOutcome,
    InjectionReport,
    load_injection_corpus,
    run_injection_suite,
)
from .judge import PairwiseResult, judge_pairwise
from .report import DomainPairwise, EvalReport, render_report_text
from .runner import load_cases, run_eval
from .schema import NO_REPLY_GOLD, CaseKind, EvalCase, redact
from .trajectory import (
    RecordingMemoryStore,
    TrajectoryViolation,
    assert_autonomy_rung_respected,
    assert_capability_selected,
    assert_freshness_checked_before_apply,
    assert_no_write_on_read_only,
    assert_retrieval_requested_score_floor,
    run_trajectory_assertions,
)
from .triage_eval import (
    TriageCase,
    TriageEvalReport,
    load_triage_cases,
    run_triage_eval,
)

__all__ = [
    "AGREEMENT_THRESHOLD",
    "LabelRecord",
    "compute_agreement",
    "domain_gates",
    "load_agreement",
    "load_labels",
    "run_label_session",
    "save_agreement",
    "InjectionCase",
    "InjectionOutcome",
    "InjectionReport",
    "load_injection_corpus",
    "run_injection_suite",
    "PairwiseResult",
    "judge_pairwise",
    "DomainPairwise",
    "EvalReport",
    "render_report_text",
    "load_cases",
    "run_eval",
    "NO_REPLY_GOLD",
    "CaseKind",
    "EvalCase",
    "redact",
    "RecordingMemoryStore",
    "TrajectoryViolation",
    "assert_autonomy_rung_respected",
    "assert_capability_selected",
    "assert_freshness_checked_before_apply",
    "assert_no_write_on_read_only",
    "assert_retrieval_requested_score_floor",
    "run_trajectory_assertions",
    "TriageCase",
    "TriageEvalReport",
    "load_triage_cases",
    "run_triage_eval",
]
