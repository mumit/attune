"""``attune eval`` — the eval harness CLI (build prompt 27,
``docs/plan-2026-h2.md`` P2).

    attune eval run       assemble the full report (pairwise, triage,
                           injection, edit-burden proxy) and print/write it
    attune eval capture   turn decided ledger rows into redacted case files
    attune eval label     hand-label a sample for judge-human agreement

Heavy imports (langgraph, the memory substrate) stay inside each function so
``attune --help``/``attune eval --help`` work in a bare install, the same
discipline every other subcommand module in this package follows.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable


def run_eval_run(
    *,
    offline: bool = False,
    seed: int = 0,
    output: str | None = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..config import Settings
    from ..llm import Task, create_chat_completion, make_client, model_for
    from ..orchestrator.importance import ImportanceTier, TierAssessment
    from ..orchestrator.triage import triage_thread
    from .. import evals as ev
    from ..evals import offline_fakes
    from ..evals.injection import InjectionOutcome

    settings = settings or Settings.from_env()

    cases = ev.load_cases(settings.eval_cases_dir)
    triage_cases = (
        ev.load_triage_cases(settings.eval_triage_cases_path)
        if os.path.exists(settings.eval_triage_cases_path) else []
    )
    injection_cases = (
        ev.load_injection_corpus(settings.eval_injection_corpus_path)
        if os.path.exists(settings.eval_injection_corpus_path) else []
    )

    if offline:
        judge_client = offline_fakes.deterministic_judge_client()
        triage_client = offline_fakes.deterministic_triage_client()
        draft_client = offline_fakes.deterministic_draft_client()
    else:
        client = make_client(settings=settings)
        judge_client = client
        triage_client = client
        draft_client = client

    def candidate_fn(case: Any) -> str:
        resp = create_chat_completion(
            draft_client,
            model=model_for(Task.DRAFT, settings),
            messages=[
                {"role": "system", "content": "Draft a reply to the following message."},
                {"role": "user", "content": case.inputs.get("incoming_summary", "")},
            ],
        )
        return resp.choices[0].message.content or ""

    class _FixedTierProfile:
        """A profile stub reporting a fixed tier for every sender — the
        triage regression set's ``tier`` field simulates "the principal's
        recorded importance profile already says this sender is X",
        deliberately isolated from real profile persistence."""

        def __init__(self, tier: str):
            self._tier = ImportanceTier(tier)

        def assess(self, sender: str, *, now=None) -> TierAssessment:
            return TierAssessment(tier=self._tier, reason=f"fixed tier {self._tier.value} (eval)", pinned=False)

    def triage_fn(case: Any):
        profile = _FixedTierProfile(case.tier) if case.tier else None
        return triage_thread(
            triage_client, case.incoming_summary,
            sender=case.sender, importance_profile=profile,
        )

    def injection_probe(case: Any) -> InjectionOutcome:
        from ..evals.injection_probes import probe_injection_case

        return probe_injection_case(case, client=triage_client)

    report = ev.run_eval(
        cases=cases,
        candidate_fn=candidate_fn,
        judge_client=judge_client,
        agreement_path=settings.eval_agreement_path,
        triage_cases=triage_cases,
        triage_fn=triage_fn if triage_cases else None,
        injection_cases=injection_cases,
        injection_probe=injection_probe if injection_cases else None,
        seed=seed,
    )

    out(ev.render_report_text(report))
    if output:
        parent = os.path.dirname(output)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(output, "w") as f:
            json.dump(report.to_json(), f, indent=2, sort_keys=True)
            f.write("\n")
    return 0


def run_eval_capture(
    *,
    since_days: int | None = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from datetime import datetime, timedelta, timezone

    from ..config import Settings
    from ..orchestrator import SqliteDecisionLedger
    from ..evals.capture import run_eval_capture as _run_capture

    settings = settings or Settings.from_env()
    ledger = SqliteDecisionLedger(settings.ledger_db_path)
    since = (
        datetime.now(timezone.utc) - timedelta(days=since_days)
        if since_days is not None else None
    )
    return _run_capture(
        ledger=ledger,
        state_lookup=_checkpoint_state_lookup(settings),
        cases_dir=settings.eval_cases_dir,
        since=since,
        out=out,
    )


def _checkpoint_state_lookup(settings: Any) -> Callable[[str], dict[str, Any] | None]:
    """Build a ``StateLookup`` (``capture.py``'s injected dependency) over
    the real, durable LangGraph checkpointer — the only place a proposal's
    actual text still lives after a decision (see ``capture.py``'s module
    docstring). Best-effort: any failure (checkpoint pruned, langgraph not
    installed, corrupt db) yields ``None`` rather than raising, since a
    capture pass must never crash on one bad thread."""

    def _lookup(thread_id: str) -> dict[str, Any] | None:
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
            import sqlite3

            conn = sqlite3.connect(settings.checkpointer_db_path, check_same_thread=False)
            saver = SqliteSaver(conn)
            tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
            if tup is None or not tup.checkpoint:
                return None
            return dict(tup.checkpoint.get("channel_values") or {})
        except Exception:  # noqa: BLE001 — best-effort, see docstring
            return None

    return _lookup


def run_eval_label(
    *,
    sample: int | None = None,
    labels_path: str | None = None,
    settings: Any = None,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    from ..config import Settings
    from ..llm import make_client
    from .. import evals as ev

    settings = settings or Settings.from_env()
    cases = ev.load_cases(settings.eval_cases_dir)
    if sample is not None:
        cases = cases[:sample]
    if not cases:
        out("No captured cases to label — run `attune eval capture` first.")
        return 1

    client = make_client(settings=settings)

    def judge_fn(case: Any) -> str:
        import random

        result = ev.judge_pairwise(
            client, case_id=case.case_id,
            context=case.inputs.get("incoming_summary", ""),
            candidate_text=case.proposed_text, gold_text=case.gold_text,
            rng=random.Random(hash(case.case_id) & 0xFFFFFFFF),
        )
        return result.reported_winner or "tie"

    def ask_human(case: Any) -> str:
        out(f"\n--- case {case.case_id} ({case.domain}) ---")
        out(f"CONTEXT: {case.inputs.get('incoming_summary', '')}")
        out(f"A: {case.proposed_text}")
        out(f"B: {case.gold_text}")
        answer = ask("Which do you prefer? [a/b/tie]: ").strip().lower()
        return {"a": "candidate", "b": "gold", "tie": "tie"}.get(answer, "tie")

    path = labels_path or os.path.join(settings.eval_labels_dir, "session.jsonl")
    records = ev.run_label_session(cases, judge_fn=judge_fn, ask_human=ask_human, labels_path=path)

    all_records = ev.load_labels(path)
    agreement = ev.compute_agreement(all_records)
    ev.save_agreement(settings.eval_agreement_path, agreement)

    out(f"\nLabeled {len(records)} pair(s); agreement by domain: {agreement}")
    return 0
