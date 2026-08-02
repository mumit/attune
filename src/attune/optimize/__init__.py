"""Offline prompt optimization (build prompt 36, ``docs/plan-2026-h2.md``
P10): a weekly, never-in-the-request-path job that optimizes the ``draft``
and ``triage`` prompts (``prompts.py``'s registry) against the golden set
(``evals/``) the eval harness accumulates. See each submodule's docstring for
its slice; ``job.run_weekly_optimization`` assembles the whole run.
"""

from .coverage import MIN_SUBSTANTIVE_CHARS, compute_coverage_proxy, is_substantive
from .gepa import Candidate, GepaResult, ReflectionResult, dominates, merge, pareto_frontier, reflect, run_gepa
from .job import OptimizationRunReport, run_weekly_optimization
from .mipro import MiproResult, run_mipro
from .promotion import PromotionDecision, evaluate_promotion
from .scoring import DraftScorecard, Trajectory, losing_trajectories, sample_trajectories, score_draft_candidate, score_trajectories

__all__ = [
    "MIN_SUBSTANTIVE_CHARS",
    "compute_coverage_proxy",
    "is_substantive",
    "Candidate",
    "GepaResult",
    "ReflectionResult",
    "dominates",
    "merge",
    "pareto_frontier",
    "reflect",
    "run_gepa",
    "MiproResult",
    "run_mipro",
    "PromotionDecision",
    "evaluate_promotion",
    "DraftScorecard",
    "Trajectory",
    "losing_trajectories",
    "sample_trajectories",
    "score_draft_candidate",
    "score_trajectories",
    "OptimizationRunReport",
    "run_weekly_optimization",
]
