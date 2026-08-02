"""``attune optimize`` — the offline prompt-optimization CLI (build prompt
36, ``docs/plan-2026-h2.md`` P10).

    attune optimize run       run GEPA (``draft``) + MIPRO (``triage``)
                               against the golden set, promote any candidate
                               that passes the gate, print/write the report
    attune optimize history   show one prompt's promoted-version history
    attune optimize revert    append a new version reverting to an earlier one

Heavy imports stay inside each function so ``attune --help``/``attune
optimize --help`` work in a bare install, the same discipline
``cli/eval_cmd.py`` already holds. Never wired into any scheduled runtime
job in-process — the weekly cadence is a separate CI workflow
(``.github/workflows/prompt-optimize.yml``) invoking this CLI, the same
"offline, never in the request path" posture ``eval-live.yml`` already holds
for the eval harness itself.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable


def run_optimize_run(
    *,
    offline: bool = False,
    rollout_budget: int = 200,
    minibatch_size: int = 8,
    n_candidates: int = 4,
    seed: int = 0,
    output: str | None = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..config import Settings
    from ..llm import Task, create_chat_completion, make_client, model_for
    from ..orchestrator.triage import triage_thread
    from ..prompts import render_system_message
    from .. import evals as ev
    from ..evals import offline_fakes as eval_fakes
    from ..optimize import offline_fakes as opt_fakes
    from ..optimize.job import run_weekly_optimization

    settings = settings or Settings.from_env()

    draft_cases = ev.load_cases(settings.eval_cases_dir)
    triage_cases = (
        ev.load_triage_cases(settings.eval_triage_cases_path)
        if os.path.exists(settings.eval_triage_cases_path) else []
    )
    agreement_by_domain = ev.load_agreement(settings.eval_agreement_path)

    if offline:
        judge_client = eval_fakes.deterministic_judge_client()
        draft_client = eval_fakes.deterministic_draft_client()
        triage_client_factory = opt_fakes.prefix_sensitive_triage_fn_factory()
        reflection_client = opt_fakes.deterministic_reflection_client()
        instruction_client = opt_fakes.deterministic_instruction_proposer_client()
    else:
        client = make_client(settings=settings)
        judge_client = client
        draft_client = client
        reflection_client = client
        instruction_client = client
        triage_client_factory = None  # built against the real client below

    def draft_candidate_fn_factory(prefix: str) -> Callable[[Any], str]:
        def candidate_fn(case: Any) -> str:
            resp = create_chat_completion(
                draft_client, model=model_for(Task.DRAFT, settings),
                messages=[
                    render_system_message(prefix),
                    {"role": "user", "content": case.inputs.get("incoming_summary", "")},
                ],
            )
            return resp.choices[0].message.content or ""
        return candidate_fn

    def triage_fn_factory(prefix: str) -> Callable[[Any], Any]:
        if triage_client_factory is not None:
            return triage_client_factory(prefix)

        def triage_fn(case: Any) -> Any:
            return triage_thread(
                judge_client, case.incoming_summary, sender=case.sender, stable_prefix=prefix,
            )
        return triage_fn

    report = run_weekly_optimization(
        draft_cases=draft_cases,
        draft_candidate_fn_factory=draft_candidate_fn_factory,
        judge_client=judge_client,
        reflection_client=reflection_client,
        triage_cases=triage_cases,
        triage_fn_factory=triage_fn_factory if triage_cases else None,
        instruction_client=instruction_client,
        agreement_by_domain=agreement_by_domain,
        versions_dir=settings.prompt_versions_dir,
        rollout_budget=rollout_budget,
        minibatch_size=minibatch_size,
        n_candidates=n_candidates,
        seed=seed,
    )

    rendered = json.dumps(report.to_json(), indent=2, sort_keys=True)
    out(rendered)
    if output:
        parent = os.path.dirname(output)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(output, "w") as f:
            f.write(rendered)
            f.write("\n")
    return 0


def run_optimize_history(
    *,
    name: str,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from .. import prompts
    from ..config import Settings

    settings = settings or Settings.from_env()
    records = prompts.history(name, versions_dir=settings.prompt_versions_dir)
    if not records:
        out(f"no promoted versions for {name!r} -- still on the baseline")
        return 0
    for r in records:
        out(
            f"v{r.version} <- v{r.parent_version} ({r.source}, {r.promoted_at}): "
            f"{r.note or '(no note)'} {dict(r.scorer_deltas)}"
        )
    return 0


def run_optimize_revert(
    *,
    name: str,
    to_version: int,
    note: str = "",
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from .. import prompts
    from ..config import Settings

    settings = settings or Settings.from_env()
    base = getattr(prompts, f"PROMPT_{name.upper()}", None)
    if base is None:
        out(f"no such prompt: {name!r}")
        return 1
    reverted = prompts.revert(base, to_version, versions_dir=settings.prompt_versions_dir, note=note)
    out(f"{name} reverted to v{to_version}'s text as new version v{reverted.version}")
    return 0
