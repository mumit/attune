"""``attune playbook`` — see, correct, and audit the git-backed playbook
from the terminal (build prompt 29, task 6). The principal must be able to
read and correct every learned rule without leaving the terminal, exactly
as ``attune memory`` and ``attune autonomy show`` already allow.
"""

from __future__ import annotations

from typing import Any, Callable


def run_playbook_show(
    domain: str | None = None,
    *,
    playbook: Any = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    playbook, _ = _resolve(playbook, settings)
    out(playbook.show(domain))
    return 0


def run_playbook_history(
    bullet_id: str,
    *,
    playbook: Any = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    playbook, _ = _resolve(playbook, settings)
    entries = playbook.history(bullet_id)
    if not entries:
        out(f"No history for '{bullet_id}' — run `attune playbook show` to find a valid id.")
        return 1
    for line in entries:
        out(line)
    return 0


def run_playbook_retire(
    bullet_id: str,
    *,
    reason: str = "retired by the principal",
    playbook: Any = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    playbook, _ = _resolve(playbook, settings)
    if not playbook.retire_bullet(bullet_id, reason=reason):
        out(f"No such bullet: {bullet_id}")
        return 1
    out(f"Retired: {bullet_id}")
    return 0


def run_playbook_pin(
    bullet_id: str,
    *,
    playbook: Any = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    playbook, _ = _resolve(playbook, settings)
    if not playbook.pin_bullet(bullet_id):
        out(f"No such bullet: {bullet_id}")
        return 1
    out(f"Pinned: {bullet_id} (exempt from decay and harmed>helped retirement)")
    return 0


def run_playbook_revert(
    commit: str,
    *,
    playbook: Any = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    playbook, _ = _resolve(playbook, settings)
    if not playbook.revert(commit):
        out(f"Revert failed for {commit} — check `git -C <playbook dir> log` for a valid commit.")
        return 1
    out(f"Reverted {commit}.")
    return 0


def _resolve(playbook: Any, settings: Any):  # pragma: no cover - live path
    from ..config import Settings
    from ..playbook.bullets import GitPlaybookStore

    resolved_settings = settings or Settings.from_env()
    if playbook is None:
        playbook = GitPlaybookStore(resolved_settings.playbook_dir)
    return playbook, resolved_settings
