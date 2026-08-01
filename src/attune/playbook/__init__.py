"""The playbook — a git-backed, self-editing learned policy (build prompt
29, ``docs/plan-2026-h2.md`` P4).

See ``bullets.py`` for the git-backed store and ``reflector.py`` for the
nightly delta-edit reflection pass. Re-exported here as the one import
surface every other module (``draft_approve.py``, ``runtime.py``,
``cli/playbook_cmd.py``) uses — mirrors ``orchestrator/__init__.py``'s own
re-export pattern.
"""

from __future__ import annotations

from .bullets import (
    DOMAINS,
    MAX_BULLETS_PER_FILE,
    MAX_CHARS_PER_BULLET,
    MAX_NEW_BULLETS_PER_DAY,
    Bullet,
    GitPlaybookStore,
)
from .reflector import (
    BULLET_DECAY_DAYS,
    RETIRE_MIN_SAMPLE,
    NewBulletProposal,
    ReflectionReport,
    classify_register,
    propose_bullets,
    record_ledger_outcomes,
    retire_bullets,
    run_nightly_reflection,
)

__all__ = [
    "DOMAINS",
    "MAX_BULLETS_PER_FILE",
    "MAX_CHARS_PER_BULLET",
    "MAX_NEW_BULLETS_PER_DAY",
    "Bullet",
    "GitPlaybookStore",
    "BULLET_DECAY_DAYS",
    "RETIRE_MIN_SAMPLE",
    "NewBulletProposal",
    "ReflectionReport",
    "classify_register",
    "propose_bullets",
    "record_ledger_outcomes",
    "retire_bullets",
    "run_nightly_reflection",
]
