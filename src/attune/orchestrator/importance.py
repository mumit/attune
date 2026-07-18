"""A deterministic, inspectable per-sender importance profile (Phase 1 of
``docs/future-state.md``, gaps G5/G6).

Today the only *learned* input to triage is the soft memory search in
``triage.py`` — a few retrieved past-reaction lines folded into a model
prompt. That's useful, but it isn't inspectable (the principal can't ask
"why did you rank this sender high?" and get a grounded answer) and it
doesn't *act*: nothing changes until the model happens to weigh the retrieved
lines differently. This module gives importance an explicit, correctable
home that acts immediately, alongside the soft signal rather than replacing
it (Phase 1, step 2): the profile below is deterministic product state, not
a model call, and it can be inspected/corrected the same day, without
waiting on nightly consolidation (step 3).

Deterministic tier rules — the product's inspectable definition of "learned
importance". :func:`JsonImportanceProfile.assess` evaluates them **in this
order**; the first rule that matches wins:

1. **Pin wins.** A principal-set pin (``attune importance pin``) always
   overrides the computed tier, whatever the recorded signals say. This is
   the explicit override the design calls for — the principal can always
   correct what Attune believes.
2. **Decay.** Signals older than :data:`DECAY_DAYS` (90) are ignored before
   any rule below is evaluated — a profile is about the sender's *recent*
   behavior, not a permanent record. This is also how a demotion heals: once
   the ignoring streak ages out, the tier reverts (to LOW's absence, i.e.
   NORMAL, absent a fresh run).
3. **Bounded storage.** At most the last :data:`MAX_SIGNALS` (20) signals
   per sender are kept at all — old entries are dropped as new ones arrive,
   so storage never grows unbounded per sender.
4. **LOW (demotion): a consecutive-ignore/reject run.** When the most
   recent :data:`LOW_RUN_THRESHOLD` (3) or more *effective* (non-decayed)
   signals for a sender are all IGNORED or REJECTED, the sender is demoted
   to LOW. This is what makes "ignoring a newsletter three times demotes
   it" literally true, the same day, without a nightly consolidation pass.
5. **HIGH (promotion): a strong approval ratio.** When there are at least
   :data:`HIGH_MIN_SIGNALS` (5) effective signals and at least
   :data:`HIGH_MIN_RATE` (80%) of them are APPROVED or EDITED, the sender is
   promoted to HIGH.
6. **NORMAL otherwise.** Includes unknown senders (no recorded state at
   all — reason "no recorded signals") and senders whose recent signals
   don't meet either bar above.

This module makes no model calls anywhere — it is pure, fast, deterministic
product state, the same posture as ``orchestrator/grants.py``'s permission
matrix. Persistence follows that module's (and ``pending.py``'s) JSON-file
pattern: atomic temp-file-plus-``os.replace`` writes, a ``threading.RLock``
plus ``fslock.locked`` around every read-modify-write critical section
(finding F2 — this file is state a CLI command and the runtime process can
both touch), and owner-only file permissions on the persisted file (mirrors
``grants.py``'s ``tempfile.mkstemp`` default mode).

Sender keys are normalized (stripped, lowercased) email addresses so
``Sender@Example.com`` and ``  sender@example.com `` are the same profile
entry.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from ..fslock import locked
from ..memory.signals import ActionSignal

DECAY_DAYS = 90
MAX_SIGNALS = 20
LOW_RUN_THRESHOLD = 3
HIGH_MIN_SIGNALS = 5
HIGH_MIN_RATE = 0.8

_NEGATIVE_SIGNALS = (ActionSignal.IGNORED, ActionSignal.REJECTED)
_POSITIVE_SIGNALS = (ActionSignal.APPROVED, ActionSignal.EDITED)


class ImportanceTier(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


@dataclass(frozen=True)
class TierAssessment:
    """One inspectable answer to "why did you rank this sender X?".

    ``reason`` is always one human-readable sentence grounded in the
    recorded signals (or the pin) that produced ``tier`` — this is the
    Phase 1 exit criterion: the principal can ask and get a grounded answer,
    not a black box.
    """

    tier: ImportanceTier
    reason: str
    pinned: bool


class ImportanceProfile(Protocol):
    def record_signal(
        self, sender: str, signal: ActionSignal, *, ts: datetime | None = None
    ) -> None:
        """Record one implicit signal for ``sender`` (defaults ``ts`` to now)."""
        ...

    def assess(self, sender: str, *, now: datetime | None = None) -> TierAssessment:
        """The current tier + grounded reason for ``sender``."""
        ...

    def pin(self, sender: str, tier: ImportanceTier) -> None:
        """Principal override: this tier always wins over computed signals."""
        ...

    def unpin(self, sender: str) -> bool:
        """Remove a pin. Returns whether one was actually removed."""
        ...

    def senders(self) -> list[str]:
        """All senders with any recorded state (signals and/or a pin)."""
        ...


def _normalize_sender(sender: str) -> str:
    return sender.strip().lower()


class JsonImportanceProfile:
    """File-backed profile: ``{sender: {"signals": [...], "pinned": tier?}}``.

    Each signal entry is ``{"signal": ActionSignal.value, "ts": iso8601}``.
    Only the CLI (``attune importance pin/unpin``) may call :meth:`pin`/
    :meth:`unpin` — mirrors the autonomy-grants posture (``grants.py``): a
    chat surface that relays untrusted content must not get a mutation path
    here either, even though nothing about this file is safety-critical the
    way autonomy grants are. This stage wires only the CLI; a future phase
    may add a show-only chat surface the same way autonomy did.
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()

    # --- writes --------------------------------------------------------

    def record_signal(
        self, sender: str, signal: ActionSignal, *, ts: datetime | None = None
    ) -> None:
        key = _normalize_sender(sender)
        stamp = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            entry = data.setdefault(key, {"signals": []})
            signals = entry.setdefault("signals", [])
            signals.append({"signal": signal.value, "ts": stamp.isoformat()})
            # Bounded storage (rule 3): keep only the most recently recorded
            # MAX_SIGNALS entries per sender.
            entry["signals"] = signals[-MAX_SIGNALS:]
            self._save(data)

    def pin(self, sender: str, tier: ImportanceTier) -> None:
        key = _normalize_sender(sender)
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            entry = data.setdefault(key, {"signals": []})
            entry["pinned"] = tier.value
            self._save(data)

    def unpin(self, sender: str) -> bool:
        key = _normalize_sender(sender)
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
            entry = data.get(key)
            if entry is None or not entry.get("pinned"):
                return False
            del entry["pinned"]
            self._save(data)
            return True

    # --- reads -----------------------------------------------------------

    def assess(self, sender: str, *, now: datetime | None = None) -> TierAssessment:
        key = _normalize_sender(sender)
        now = now or datetime.now(timezone.utc)
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        entry = data.get(key)
        if entry is None:
            return TierAssessment(ImportanceTier.NORMAL, "no recorded signals", False)

        pinned_raw = entry.get("pinned")
        if pinned_raw:
            tier = ImportanceTier(pinned_raw)
            return TierAssessment(
                tier, f"pinned {tier.value} by the principal", True
            )

        effective = self._effective_signals(entry.get("signals", []), now)
        if not effective:
            return TierAssessment(ImportanceTier.NORMAL, "no recorded signals", False)

        return _assess_from_signals(effective)

    def senders(self) -> list[str]:
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        return sorted(data.keys())

    def recent_signals(
        self, sender: str, *, now: datetime | None = None
    ) -> list[tuple[ActionSignal, datetime]]:
        """The recorded, non-expired ``(signal, ts)`` pairs for ``sender``,
        oldest first — what backs ``attune importance show``'s "why" answer."""
        key = _normalize_sender(sender)
        now = now or datetime.now(timezone.utc)
        with self._lock, locked(self._path + ".lock"):
            data = self._load()
        entry = data.get(key)
        if entry is None:
            return []
        return self._effective_signals(entry.get("signals", []), now)

    # --- persistence -----------------------------------------------------

    def _effective_signals(
        self, raw_signals: list[dict[str, Any]], now: datetime
    ) -> list[tuple[ActionSignal, datetime]]:
        """Non-decayed, well-formed signals, oldest first (decay, rule 2)."""
        cutoff = now - timedelta(days=DECAY_DAYS)
        effective: list[tuple[ActionSignal, datetime]] = []
        for raw in raw_signals:
            try:
                ts = datetime.fromisoformat(raw["ts"])
                sig = ActionSignal(raw["signal"])
            except (KeyError, ValueError, TypeError):
                continue
            if ts < cutoff:
                continue
            effective.append((sig, ts))
        effective.sort(key=lambda pair: pair[1])
        return effective

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as fh:
            return json.load(fh)

    def _save(self, data: dict[str, Any]) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        directory = parent or "."
        fd, temp_path = tempfile.mkstemp(prefix=".importance-", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, self._path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def _assess_from_signals(
    effective: list[tuple[ActionSignal, datetime]],
) -> TierAssessment:
    """Rules 4-6 over an already-decayed, oldest-first signal list."""
    signals_only = [sig for sig, _ in effective]

    # Rule 4 (LOW): the most recent N (>=3) effective signals are all
    # IGNORED/REJECTED.
    run = 0
    for sig in reversed(signals_only):
        if sig in _NEGATIVE_SIGNALS:
            run += 1
        else:
            break
    if run >= LOW_RUN_THRESHOLD:
        tail_kinds = set(signals_only[-run:])
        if tail_kinds == {ActionSignal.IGNORED}:
            verb = "ignored"
        elif tail_kinds == {ActionSignal.REJECTED}:
            verb = "rejected"
        else:
            verb = "ignored or rejected"
        return TierAssessment(
            ImportanceTier.LOW,
            f"sender {verb} {run} of last {run} proposals",
            False,
        )

    # Rule 5 (HIGH): a strong approval ratio over enough signals.
    total = len(signals_only)
    positive = sum(1 for sig in signals_only if sig in _POSITIVE_SIGNALS)
    if total >= HIGH_MIN_SIGNALS and (positive / total) >= HIGH_MIN_RATE:
        pct = round(positive / total * 100)
        return TierAssessment(
            ImportanceTier.HIGH,
            f"sender approved or edited {positive} of last {total} "
            f"proposals ({pct}%)",
            False,
        )

    # Rule 6 (NORMAL): grounded in whatever signals exist, just not enough
    # to clear either bar above.
    return TierAssessment(
        ImportanceTier.NORMAL,
        f"{positive} of last {total} recent proposals approved or edited "
        "— not enough to change the tier",
        False,
    )
