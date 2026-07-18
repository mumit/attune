"""Cross-process advisory file locking (security finding F2).

``JsonPendingApprovals`` and ``JsonlAuditLog`` each guard their read-modify-
write critical section with a ``threading.RLock`` — enough to serialize
threads inside one process, but nothing stops two *processes* (two runtime
instances, a runtime plus a CLI command) from interleaving a load and a save
against the same state file and losing one side's write, e.g. two overlapping
processes both claiming one approval card. :func:`locked` closes that gap with
an OS-level advisory lock on a dedicated lock file next to the guarded state.

It is advisory, not mandatory: a process that never calls :func:`locked` can
still read or write the guarded file underneath it. That is an acceptable
scope for this codebase's boundary (one principal, cooperating processes it
controls) — see ``docs/security-architecture.md``'s data-at-rest section.

``fcntl`` is POSIX-only. On platforms without it (e.g. native Windows), the
import is guarded and :func:`locked` degrades to a plain no-op context
manager, logging one warning per process so the gap is visible rather than
silent. The in-process ``threading.RLock`` callers already hold still applies
in that degraded mode.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only off POSIX
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_warned_no_fcntl = False


@contextmanager
def locked(path: str) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``path`` for the duration of the
    ``with`` block, protecting the caller's read-modify-write of the state
    file it guards from concurrent runtime processes doing the same.

    ``path`` is a dedicated lock file (by convention, the guarded state
    file's path plus ``".lock"``), never the state file itself — so the lock
    holder's own load/rewrite of the real file is unaffected by which flags
    it was opened with. The lock file's parent directory is created if
    missing so this composes with fresh deployments the same way the state
    file's own ``os.makedirs`` does.
    """
    if fcntl is None:
        _warn_once()
        yield
        return

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _warn_once() -> None:
    global _warned_no_fcntl
    if not _warned_no_fcntl:
        _warned_no_fcntl = True
        logger.warning(
            "fcntl unavailable on this platform — cross-process file locking "
            "is disabled; only in-process locking (threading.RLock) protects "
            "this state file, so concurrent runtime processes are unsafe"
        )
