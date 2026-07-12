"""Autonomy grants: persistence, operations, and the earning mechanism
(design 3.2, roadmap prompt 12).

"Autonomy is earned, not granted" is the design's second pillar, but until
this module the earning mechanism didn't exist: `default_matrix()` was
hardcoded, grants survived neither a restart nor existed anywhere a user
could make one, and nothing computed the track record the whole ladder
concept depends on. The audit log already records every ``autonomy_gate``
and ``human_decision`` — this module folds those into per-(action, domain)
records and *suggestions*.

Safety spine — no shortcuts (rule 3):

- **A human always makes the grant.** No code path here (or anywhere) may
  auto-apply a suggestion, however strong the record. Suggestions are
  information only.
- **Grant/revoke is CLI-only.** The chat surface shows the posture and
  suggestions but cannot change them — a chat channel that relays untrusted
  content must not be able to escalate autonomy via a spoofed-looking
  message.
- **The gate's fail-safe default is untouched**: absent a grant, everything
  routes through human approval. Granting ``SEND_REPLY`` at any rung is
  additionally *not sufficient* to send — rule 4's structural gate
  (``send_enabled`` + a real ``gmail.send`` scope) still refuses; callers
  surface that warning.
- Parsing is strict enum validation — a typo errors, never silently
  defaults.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .autonomy import Action, Domain, PermissionMatrix, Rung

GRADUATION_MIN_DECISIONS = 10
GRADUATION_MIN_APPROVAL_RATE = 0.95


class JsonPermissionMatrixStore:
    """Persists grants as ``{"<action>|<domain>": <rung int>}``.

    The file is only ever written through :func:`grant`/:func:`revoke` —
    the matrix object itself stays frozen/immutable, and ``build_app`` only
    *loads* from here.
    """

    def __init__(self, path: str):
        self._path = path

    def load(self) -> PermissionMatrix | None:
        """The persisted matrix, or None if never saved (caller falls back
        to ``default_matrix()``). Strict parsing: an unknown action/domain/
        rung in the file is a hard error, not a silent skip — a corrupted
        autonomy file must not quietly change the safety posture."""
        if not os.path.exists(self._path):
            return None
        with open(self._path) as fh:
            raw = json.load(fh)
        grants: dict[tuple[Action, Domain], Rung] = {}
        for key, rung_value in raw.items():
            action_str, domain_str = key.split("|", 1)
            grants[(Action(action_str), Domain(domain_str))] = Rung(int(rung_value))
        return PermissionMatrix(grants)

    def save(self, matrix: PermissionMatrix) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = {
            f"{action.value}|{domain.value}": int(rung)
            for (action, domain), rung in matrix.grants.items()
        }
        directory = parent or "."
        fd, temp_path = tempfile.mkstemp(prefix=".autonomy-", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, self._path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def make_matrix_provider(
    store: JsonPermissionMatrixStore,
) -> Any:
    """A live matrix source for the gate (review finding #2): stat the file
    per evaluation, reload only on mtime change. Grants and — critically —
    REVOCATIONS take effect on the next gate evaluation, not the next
    restart.

    Failure posture: an unreadable, corrupt, or deleted file fails closed to
    ``default_matrix()`` and logs the failure. Atomic store writes make a
    transient half-written file unnecessary for normal grant/revoke updates.
    """
    import logging

    from .autonomy import default_matrix

    logger = logging.getLogger(__name__)
    cache: dict[str, Any] = {"mtime": None, "matrix": None}

    def provider() -> PermissionMatrix:
        try:
            mtime = os.path.getmtime(store._path)
        except OSError:
            mtime = None  # never saved -> conservative default

        if mtime is None:
            cache["mtime"] = None
            cache["matrix"] = default_matrix()
            return cache["matrix"]

        if mtime != cache["mtime"]:
            try:
                loaded = store.load()
            except (ValueError, KeyError, OSError) as exc:
                logger.warning(
                    "autonomy grants file unreadable (%s: %s) — keeping the "
                    "conservative default matrix", type(exc).__name__, exc,
                )
                cache["mtime"] = None
                cache["matrix"] = default_matrix()
                return cache["matrix"]
            cache["mtime"] = mtime
            cache["matrix"] = loaded if loaded is not None else default_matrix()
        return cache["matrix"]

    return provider


def grant(
    store: JsonPermissionMatrixStore,
    matrix: PermissionMatrix,
    action: Action,
    domain: Domain,
    rung: Rung,
    *,
    audit_log: Any,
    user_id: str,
) -> PermissionMatrix:
    """Apply and persist one grant — the most audit-worthy event in the
    system. Returns the new matrix (the old one is unchanged)."""
    new_matrix = matrix.grant(action, domain, rung)
    store.save(new_matrix)
    _audit_grant_event(
        audit_log, "autonomy_granted", action, domain, user_id, rung=int(rung)
    )
    return new_matrix


def revoke(
    store: JsonPermissionMatrixStore,
    matrix: PermissionMatrix,
    action: Action,
    domain: Domain,
    *,
    audit_log: Any,
    user_id: str,
) -> PermissionMatrix:
    """Claw back a grant (falls to the READ_ONLY floor) and persist."""
    new_matrix = matrix.revoke(action, domain)
    store.save(new_matrix)
    _audit_grant_event(audit_log, "autonomy_revoked", action, domain, user_id)
    return new_matrix


def show_matrix(matrix: PermissionMatrix) -> str:
    """Human-readable posture table, most-permissive first."""
    if not matrix.grants:
        return "No grants — everything at the READ_ONLY floor."
    rows = sorted(
        matrix.grants.items(), key=lambda kv: (-int(kv[1]), kv[0][0].value)
    )
    lines = [f"{'action':<15} {'domain':<10} rung"]
    for (action, domain), rung in rows:
        lines.append(f"{action.value:<15} {domain.value:<10} {int(rung)} ({rung.name})")
    return "\n".join(lines)


@dataclass
class TrackRecord:
    """What the human actually did with this (action, domain)'s proposals."""

    action: Action
    domain: Domain
    approved: int = 0   # approved unedited
    edited: int = 0
    rejected: int = 0
    ignored: int = 0
    applied: int = 0
    apply_failed: int = 0

    @property
    def total(self) -> int:
        return self.approved + self.edited + self.rejected + self.ignored

    def render(self) -> str:
        return (
            f"{self.action.value} on {self.domain.value}: "
            f"{self.approved} approved unedited, {self.edited} edited, "
            f"{self.rejected} rejected, {self.ignored} ignored "
            f"({self.total} total); {self.applied} applied, "
            f"{self.apply_failed} apply failures"
        )


@dataclass
class GraduationSuggestion:
    record: TrackRecord
    to_rung: Rung = Rung.ACT_NOTIFY

    def render(self) -> str:
        r = self.record
        return (
            f"{r.approved}/{r.total} {r.action.value} proposals on "
            f"{r.domain.value} approved unedited — consider graduating to "
            f"{self.to_rung.name}: aidedecamp autonomy grant "
            f"{r.action.value} {r.domain.value} {self.to_rung.name.lower()}"
        )


def track_records(
    audit_log: Any,
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> dict[tuple[Action, Domain], TrackRecord]:
    """Fold the audit log into per-(action, domain) decision counts.

    Attribution runs by workflow thread_id: the ``autonomy_gate`` event
    carries the (action, domain); ``human_decision`` carries what the human
    did; ``approval_ignored`` (the pending-sweep) marks ignores. Successful
    and failed/skipped apply events establish whether accepted proposals
    actually reached the external system.
    ``auto_applied`` decisions are excluded — a track record measures human
    judgment on proposals, and autonomous runs aren't proposals.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    entries = audit_log.query(since=since)

    scope: dict[str, tuple[Action, Domain]] = {}
    outcome: dict[str, str] = {}
    execution: dict[str, str] = {}
    for entry in entries:
        if entry.event == "autonomy_gate":
            # The event's own "domain" field is absorbed into the entry-level
            # join key by AuditEntry.from_json — read it from either place.
            try:
                scope[entry.thread_id] = (
                    Action(entry.fields.get("action")),
                    Domain(entry.fields.get("domain") or entry.domain),
                )
            except ValueError:
                continue
        elif entry.event == "human_decision":
            outcome[entry.thread_id] = entry.fields.get("decision", "")
        elif entry.event == "approval_ignored":
            outcome.setdefault(entry.thread_id, "ignored")
        elif entry.event == "applied":
            execution[entry.thread_id] = "applied"
        elif entry.event in ("apply_failed", "apply_skipped"):
            execution.setdefault(entry.thread_id, "failed")

    records: dict[tuple[Action, Domain], TrackRecord] = {}
    for tid, key in scope.items():
        decision = outcome.get(tid)
        if decision not in ("approved", "edited", "rejected", "ignored"):
            continue  # still pending, or auto_applied
        record = records.setdefault(key, TrackRecord(action=key[0], domain=key[1]))
        setattr(record, decision, getattr(record, decision) + 1)
        if decision in ("approved", "edited"):
            status = execution.get(tid)
            if status == "applied":
                record.applied += 1
            else:
                # Missing evidence is failure evidence for graduation. Older
                # audit logs must earn a new, complete observation window.
                record.apply_failed += 1
    return records


def suggest_graduations(
    audit_log: Any,
    matrix: PermissionMatrix,
    *,
    window_days: int = 30,
    min_decisions: int = GRADUATION_MIN_DECISIONS,
    min_approval_rate: float = GRADUATION_MIN_APPROVAL_RATE,
    now: datetime | None = None,
) -> list[GraduationSuggestion]:
    """Suggestions — information only, never applied by code (rule 3).

    Bar (per (action, domain), over the window): at least ``min_decisions``
    human decisions, an unedited-approval rate at or above
    ``min_approval_rate``, zero rejections, a successful apply for every
    accepted proposal, and currently below ACT_NOTIFY.
    """
    suggestions: list[GraduationSuggestion] = []
    for key, record in track_records(
        audit_log, window_days=window_days, now=now
    ).items():
        if matrix.max_rung(*key) >= Rung.ACT_NOTIFY:
            continue
        if record.total < min_decisions or record.rejected > 0:
            continue
        accepted = record.approved + record.edited
        if record.apply_failed > 0 or record.applied < accepted:
            continue
        if record.approved / record.total < min_approval_rate:
            continue
        suggestions.append(GraduationSuggestion(record=record))
    return suggestions


def parse_action(value: str) -> Action:
    """Strict: a typo must error, never silently default (contrast the
    graph's lenient `_as_action`, which guards runtime state, not user
    commands)."""
    return Action(value.strip().lower())


def parse_domain(value: str) -> Domain:
    return Domain(value.strip().lower())


def parse_rung(value: str) -> Rung:
    v = value.strip().upper()
    if v.isdigit():
        return Rung(int(v))
    return Rung[v]


def _audit_grant_event(
    audit_log: Any,
    event: str,
    action: Action,
    domain: Domain,
    user_id: str,
    **fields: Any,
) -> None:
    if audit_log is None:
        return
    audit_log.record(
        thread_id=f"autonomy:{action.value}:{domain.value}",
        workflow="autonomy",
        events=[{
            "event": event,
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action.value,
            "domain": domain.value,
            **fields,
        }],
        domain=domain.value,
        user_id=user_id,
    )
