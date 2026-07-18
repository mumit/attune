"""Tests for fslock.py — the cross-process advisory lock (security finding
F2) and the race in ``JsonPendingApprovals.claim()`` it closes.

The multiprocessing test uses the "fork" context and module-level worker
functions (picklable under "spawn" too, in case a platform's default ever
changes) plus a ``multiprocessing.Barrier`` so every racer calls ``claim()``
at the same instant rather than serializing by accident of start order.
"""

from __future__ import annotations

import multiprocessing
from datetime import datetime, timezone

from attune.fslock import locked
from attune.orchestrator.pending import JsonPendingApprovals

T0 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# locked() as a plain context manager
# ---------------------------------------------------------------------------


def test_locked_is_a_plain_context_manager(tmp_path):
    path = str(tmp_path / "some.lock")
    with locked(path):
        pass
    assert (tmp_path / "some.lock").exists()


def test_locked_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "x.lock")
    with locked(path):
        pass
    assert (tmp_path / "nested" / "dir" / "x.lock").exists()


def test_locked_is_reentrant_safe_across_sequential_uses(tmp_path):
    # Not reentrant within one call, but sequential with/blocks on the same
    # path must not deadlock or leak file descriptors.
    path = str(tmp_path / "x.lock")
    with locked(path):
        pass
    with locked(path):
        pass


def test_fslock_degrades_to_noop_without_fcntl(monkeypatch):
    """Platforms without fcntl (import guarded in fslock.py) still get a
    usable, importable context manager — just without the cross-process
    guarantee, per the module docstring."""
    import attune.fslock as fslock_mod

    monkeypatch.setattr(fslock_mod, "fcntl", None)
    monkeypatch.setattr(fslock_mod, "_warned_no_fcntl", False)

    entered = False
    with fslock_mod.locked("/nonexistent-dir/should-not-be-created.lock"):
        entered = True
    assert entered


# ---------------------------------------------------------------------------
# claim() is cross-process safe (security finding F2)
# ---------------------------------------------------------------------------


def _claim_worker(path: str, lg_tid: str, barrier, results, idx: int) -> None:
    reg = JsonPendingApprovals(path)
    barrier.wait()
    claimed = reg.claim(lg_tid, actor=f"actor-{idx}")
    results[idx] = 1 if claimed is True else (0 if claimed is False else -1)


def test_claim_is_cross_process_safe_under_concurrent_racers(tmp_path):
    path = str(tmp_path / "pending.json")
    reg = JsonPendingApprovals(path)
    reg.register(lg_tid="gmail:t1:100", source_ref="t1", domain="mail", posted_at=T0)

    n = 8
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(n)
    results = ctx.Array("i", [-1] * n)

    procs = [
        ctx.Process(
            target=_claim_worker, args=(path, "gmail:t1:100", barrier, results, i)
        )
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)

    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
    codes = list(results)
    assert codes.count(1) == 1, f"expected exactly one winner, got {codes}"
    assert codes.count(0) == n - 1, f"expected the rest to lose, got {codes}"

    # The file itself agrees: exactly one resolved-by actor recorded.
    import json

    raw = json.loads((tmp_path / "pending.json").read_text())
    assert raw["gmail:t1:100"]["status"] == "resolved"
