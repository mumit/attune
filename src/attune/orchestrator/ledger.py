"""The decision ledger and the north-star metric (build prompt 26,
``docs/plan-2026-h2.md`` P2).

Attune's audit log (``audit/log.py``) records *that* a human approved,
edited, rejected, or ignored a proposal — a hash-chained, effect-and-decision
trail keyed by workflow thread, deliberately content-light. It answers "what
happened." It cannot answer "why was this proposal good or bad" because it
never recorded **what was in context when the proposal was made** — which
memory records, which playbook bullets, which model. Without that
attribution, no learning mechanism (ACE-style per-bullet accounting, prompt
29; GEPA reflection over trajectories, prompt 36) has anything to credit or
blame.

This module is the **per-proposal analytic row** that closes that gap: one
row per proposal, written at propose time and completed at decision time,
aggregable into the metrics ``attune metrics`` reports.

**Analytic state, not the trust root.** The hash-chained audit log remains
authoritative for authority decisions. A ledger row is *derived* from the
same audit events (plus a little extra context threaded through graph
state) and may be rebuilt from the audit log at any time — nothing here
carries independent authority. A ledger write failure must never break a
decision path a human is waiting on: every write site in this module is
best-effort (:func:`record_proposal`/:func:`record_decision` swallow and log
exceptions), the same posture ``resume_workflow`` already holds for
``audit_log.record``.

**Content-free by construction.** ``edit_sections_changed`` and the edit
distance counters are derived NUMBERS and CATEGORICAL LABELS, never the
draft text or the diff itself — that text already lives in memory under
``memory.signals.frame_memory_text``'s provenance rules (correction-derived
memories) and must not be duplicated here. :func:`compute_edit_metrics`
takes the raw proposed/sent text as *transient* arguments, computes the
metrics, and discards the text; nothing in :class:`LedgerRow` or the SQLite
schema below ever stores a message body or a diff.

**One shared shape, two storage backends** (rule 3, "Build once"):
:class:`LedgerRow`, :class:`ContextAttribution`, :class:`EditMetrics`, and
every aggregation function (:func:`compute_metrics`,
:func:`render_metrics_table`) are storage-agnostic — plain dataclasses and
pure functions over a ``Sequence[LedgerRow]``. :class:`SqliteDecisionLedger`
(this module) and ``hosted.ledger.PostgresDecisionLedger`` both produce and
consume the exact same :class:`LedgerRow` shape, following the
``hosted/intelligence.py`` pattern: storage differs per plane, the
dataclasses and the rule engine (here, the aggregation math) are imported,
not reimplemented.

**Coverage, the mandatory guardrail.** An assistant graded only on
edit-burden-when-it-proposes can improve that number by proposing only the
easy, obviously-safe cases and staying silent on everything hard — the RLUF
failure mode (``docs/landscape-2026.md`` §5), quantified. ``eligible_item_count``
is the denominator that makes that reward-hack visible: recorded once per
*batch* of eligible items considered (every row from the same batch carries
the same batch's total; see ``batch_id``), never per-proposal, so summing
it across a window double-counts. :func:`compute_metrics` divides total
proposals by the SUM of each DISTINCT batch's ``eligible_item_count`` in the
window — never proposals alone.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Protocol, Sequence

logger = logging.getLogger(__name__)

# The full vocabulary edit_sections_changed may draw from. Today's
# classifier (see classify_edit_sections) only ever emits a subset of these
# — "subject"/"recipients" require an editable-subject/recipients UI that
# doesn't exist yet (P5/prompt 31's batch-approval work); they're part of
# the vocabulary so a future editable field can start emitting them without
# a schema change.
EDIT_SECTIONS = ("greeting", "body", "closing", "subject", "recipients", "tone")

_DECIDED_STATES = ("approved", "edited", "rejected")


# ---------------------------------------------------------------------------
# Shared shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextAttribution:
    """What was actually in context when a proposal was drafted.

    ``memory_ids`` is populated today (threaded from the draft-approve
    graph's ``retrieve`` node — see ``draft_approve.py``). ``playbook_bullet_ids``
    (prompt 29's ACE-style playbook doesn't exist yet) and ``skill_ids``
    (no such registry exists yet either) are forward-compatible empty
    tuples until those prompts land — this schema doesn't change when they
    do, only these fields stop being empty.
    """

    memory_ids: tuple[str, ...] = ()
    playbook_bullet_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()

    def to_json(self) -> dict[str, list[str]]:
        return {
            "memory_ids": list(self.memory_ids),
            "playbook_bullet_ids": list(self.playbook_bullet_ids),
            "skill_ids": list(self.skill_ids),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> "ContextAttribution":
        raw = raw or {}
        return cls(
            memory_ids=tuple(raw.get("memory_ids") or ()),
            playbook_bullet_ids=tuple(raw.get("playbook_bullet_ids") or ()),
            skill_ids=tuple(raw.get("skill_ids") or ()),
        )


@dataclass(frozen=True)
class EditMetrics:
    """Content-free measurement of one edit — never the text itself."""

    char_distance: int
    distance_normalized: float
    semantic_similarity: float
    sections_changed: tuple[str, ...]


@dataclass
class LedgerRow:
    """One append-only decision-ledger row: written (mostly-empty) at
    propose time, completed at decision time. See the module docstring for
    the "analytic state, not the trust root" and "content-free" rules that
    shape which fields exist here and which don't (no draft text, no diff
    text — see ``memory.signals.frame_memory_text`` for where that lives).
    """

    proposal_id: str
    thread_id: str
    domain: str
    action: str
    proposed_at: datetime

    # Autonomy context (autonomy.py's PermissionMatrix.max_rung, ignoring
    # vs. applying the urgent-interrupt cap — see that method's
    # ``ignore_urgent_cap`` parameter).
    autonomy_rung_granted: int | None = None
    autonomy_rung_used: int | None = None
    scope_matched: bool = False

    # Model layer floor (prompt 28): model_id/prompt_version/token usage/
    # cache_hit are now populated from the draft-approve graph's ``drafted``
    # audit event (see _row_from_propose_result). playbook_commit
    # (prompt 29) still isn't -- recorded as None until that prompt lands.
    model_id: str | None = None
    prompt_version: int | None = None
    playbook_commit: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit: bool | None = None

    context_attribution: ContextAttribution = field(default_factory=ContextAttribution)

    triage_priority: str | None = None
    base_priority: str | None = None
    sender_importance_tier: str | None = None
    profile_reason: str | None = None

    # The coverage denominator. See the module docstring: recorded once per
    # BATCH (every row sharing a batch_id carries the batch's own total),
    # never accumulated per-row — aggregation dedupes by batch_id.
    eligible_item_count: int | None = None
    batch_id: str | None = None

    decision: str | None = None
    decided_at: datetime | None = None
    actor_ref: str | None = None
    time_to_decision_seconds: float | None = None

    edit_char_distance: int | None = None
    edit_distance_normalized: float | None = None
    edit_semantic_similarity: float | None = None
    edit_sections_changed: tuple[str, ...] = ()

    applied_ok: bool | None = None
    apply_skip_reason: str | None = None
    undone: bool = False
    undone_at: datetime | None = None


class DecisionLedger(Protocol):
    """The swappable ledger substrate interface — mirrors ``audit.log.AuditLog``'s
    shape (propose/complete instead of a single ``record``, since a ledger
    row is written in two passes)."""

    def propose(self, row: LedgerRow) -> None: ...

    def complete(
        self,
        proposal_id: str,
        *,
        decision: str,
        decided_at: datetime | None = None,
        actor_ref: str | None = None,
        proposed_text: str | None = None,
        final_text: str | None = None,
        applied_ok: bool | None = None,
        apply_skip_reason: str | None = None,
    ) -> None: ...

    def mark_undone(self, proposal_id: str, *, at: datetime | None = None) -> None: ...

    def rows(
        self,
        *,
        since: datetime | None = None,
        domain: str | None = None,
        action: str | None = None,
    ) -> list[LedgerRow]: ...


# ---------------------------------------------------------------------------
# Edit measurement — deterministic, content-free (task 3)
# ---------------------------------------------------------------------------

_GREETING_RE = re.compile(
    r"^(hi|hello|hey|dear|good morning|good afternoon|good evening)\b", re.IGNORECASE,
)
_CLOSING_RE = re.compile(
    r"^(best|regards|thanks|thank you|sincerely|cheers|warmly|cordially|"
    r"talk soon|see you)\b", re.IGNORECASE,
)
_CONTRACTION_RE = re.compile(r"\b\w+'(?:re|ve|ll|d|s|t|m)\b", re.IGNORECASE)


def _nonblank_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _greeting_line(text: str) -> str:
    lines = _nonblank_lines(text)
    if lines and _GREETING_RE.match(lines[0]):
        return lines[0]
    return ""


def _closing_block(text: str, *, max_lines: int = 2) -> tuple[str, ...]:
    lines = _nonblank_lines(text)
    if not lines:
        return ()
    tail = lines[-max_lines:]
    if any(_CLOSING_RE.match(line) for line in tail):
        return tuple(tail)
    return ()


def _body_only(text: str) -> str:
    """The text with a detected greeting/closing stripped — what's left is
    what "body changed" means below."""
    lines = _nonblank_lines(text)
    start = 1 if lines and _GREETING_RE.match(lines[0]) else 0
    end = len(lines)
    closing = _closing_block(text)
    if closing:
        end = len(lines) - len(closing)
    return "\n".join(lines[start:end])


def _tone_signature(text: str) -> tuple[bool, bool]:
    """A coarse, deterministic proxy for register — never a model call.
    (exclamation used, any contraction used)."""
    return ("!" in text, bool(_CONTRACTION_RE.search(text)))


def classify_edit_sections(proposed: str, sent: str) -> tuple[str, ...]:
    """Which parts of the draft changed, by line-position/header heuristics
    over the draft's own text structure — deterministic, no model call (this
    is a METRIC; it must not itself drift with a model).

    Only ever draws from {"greeting", "body", "closing", "tone"} today:
    "subject"/"recipients" (see :data:`EDIT_SECTIONS`) require an editable
    subject/recipients affordance that doesn't exist yet — this function
    only ever sees the draft BODY text.
    """
    proposed, sent = proposed or "", sent or ""
    if proposed.strip() == sent.strip():
        return ()
    changed: list[str] = []
    if _greeting_line(proposed) != _greeting_line(sent):
        changed.append("greeting")
    if _closing_block(proposed) != _closing_block(sent):
        changed.append("closing")
    if _body_only(proposed) != _body_only(sent):
        changed.append("body")
    if _tone_signature(proposed) != _tone_signature(sent):
        changed.append("tone")
    return tuple(changed)


def _levenshtein(a: str, b: str) -> int:
    """Plain O(n*m) edit distance — draft bodies are bounded (a reply, not a
    novel), so the DP table is cheap; no third-party dependency needed for
    one metric."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def compute_edit_metrics(proposed: str, sent: str) -> EditMetrics:
    """Promote ``signals.capture_correction``'s diff into structured,
    content-free metrics: raw character distance, normalized distance (0.0 =
    accepted verbatim, 1.0 = fully rewritten), and which sections changed.

    ``semantic_similarity`` is a deterministic, non-model proxy (a
    word-level sequence-match ratio) for how much meaning survived the
    edit — NOT a real embedding/model-based semantic score. Building a true
    semantic scorer is prompt 27's eval-harness territory; this metric must
    not itself drift with a model (the same discipline the section
    classifier above holds), so it stays a cheap deterministic proxy here.
    """
    proposed, sent = proposed or "", sent or ""
    distance = _levenshtein(proposed, sent)
    denom = max(len(proposed), len(sent), 1)
    normalized = min(1.0, distance / denom)
    similarity = SequenceMatcher(None, proposed.split(), sent.split()).ratio()
    sections = classify_edit_sections(proposed, sent)
    return EditMetrics(
        char_distance=distance,
        distance_normalized=normalized,
        semantic_similarity=similarity,
        sections_changed=sections,
    )


# ---------------------------------------------------------------------------
# SQLite-backed local ledger
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_ledger (
    proposal_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    action TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    autonomy_rung_granted INTEGER,
    autonomy_rung_used INTEGER,
    scope_matched INTEGER NOT NULL DEFAULT 0,
    model_id TEXT,
    prompt_version INTEGER,
    playbook_commit TEXT,
    context_attribution TEXT NOT NULL DEFAULT '{}',
    triage_priority TEXT,
    base_priority TEXT,
    sender_importance_tier TEXT,
    profile_reason TEXT,
    eligible_item_count INTEGER,
    batch_id TEXT,
    decision TEXT,
    decided_at TEXT,
    actor_ref TEXT,
    time_to_decision_seconds REAL,
    edit_char_distance INTEGER,
    edit_distance_normalized REAL,
    edit_semantic_similarity REAL,
    edit_sections_changed TEXT NOT NULL DEFAULT '[]',
    applied_ok INTEGER,
    apply_skip_reason TEXT,
    undone INTEGER NOT NULL DEFAULT 0,
    undone_at TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_hit INTEGER
)
"""

_COLUMNS = (
    "proposal_id", "thread_id", "domain", "action", "proposed_at",
    "autonomy_rung_granted", "autonomy_rung_used", "scope_matched",
    "model_id", "prompt_version", "playbook_commit", "context_attribution",
    "triage_priority", "base_priority", "sender_importance_tier",
    "profile_reason", "eligible_item_count", "batch_id",
    "decision", "decided_at", "actor_ref", "time_to_decision_seconds",
    "edit_char_distance", "edit_distance_normalized", "edit_semantic_similarity",
    "edit_sections_changed", "applied_ok", "apply_skip_reason", "undone", "undone_at",
    "input_tokens", "output_tokens", "cache_hit",
)


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat() if ts is not None else None


def _parse_iso(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def _parse_iso_required(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


class SqliteDecisionLedger:
    """SQLite-backed decision ledger — one row per proposal.

    Lazy initialization, WAL journal mode, owner-only file permissions: the
    exact same discipline ``ingestion.retry_queue.SqliteRetryQueue`` already
    established as this codebase's pattern for a small SQLite-backed local
    store, and the reason this lands in "the SQLite database that is
    already a dependency" rather than another JSON file (this table gets
    aggregated; whole-file JSON reads are already a measured problem, see
    ``docs/plan-2026-h2.md`` P0). Unlike the JSON stores elsewhere, SQLite's
    own file locking (under WAL) serializes concurrent writers, so no
    additional ``fslock`` wrapping is needed here.
    """

    def __init__(self, path: str):
        self._path = path

    def _exists(self) -> bool:
        return os.path.exists(self._path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _initialize(self) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS decision_ledger_proposed_at "
                "ON decision_ledger (proposed_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS decision_ledger_domain_action "
                "ON decision_ledger (domain, action)"
            )
            # Build prompt 28: a database created before token usage/cache
            # tracking existed has ``CREATE TABLE IF NOT EXISTS`` skip these
            # new columns entirely, so a lightweight in-place migration adds
            # them here. Each is independently guarded: "duplicate column"
            # (the fresh-database case, where _SCHEMA above already created
            # it) is the one expected failure and is swallowed; anything
            # else re-raises.
            for column, sqltype in (
                ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"),
                ("cache_hit", "INTEGER"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE decision_ledger ADD COLUMN {column} {sqltype}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        # Security posture (finding F5 elsewhere in this codebase): owner-only
        # regardless of process umask, self-healing on every initialize.
        for suffix in ("", "-wal", "-shm"):
            candidate = self._path + suffix
            if os.path.exists(candidate):
                try:
                    os.chmod(candidate, 0o600)
                except OSError:
                    pass

    def propose(self, row: LedgerRow) -> None:
        """Insert the propose-time row. Idempotent: a re-submitted proposal
        for the same ``proposal_id`` (e.g. a retried dispatcher call) is
        silently ignored rather than clobbering a row that may already
        carry a decision."""
        self._initialize()
        values = (
            row.proposal_id, row.thread_id, row.domain, row.action,
            _iso(row.proposed_at),
            row.autonomy_rung_granted, row.autonomy_rung_used,
            int(row.scope_matched),
            row.model_id, row.prompt_version, row.playbook_commit,
            _json_dumps(row.context_attribution.to_json()),
            row.triage_priority, row.base_priority, row.sender_importance_tier,
            row.profile_reason, row.eligible_item_count, row.batch_id,
            row.decision, _iso(row.decided_at), row.actor_ref,
            row.time_to_decision_seconds,
            row.edit_char_distance, row.edit_distance_normalized,
            row.edit_semantic_similarity,
            _json_dumps(list(row.edit_sections_changed)),
            _as_int_or_none(row.applied_ok), row.apply_skip_reason,
            int(row.undone), _iso(row.undone_at),
            row.input_tokens, row.output_tokens, _as_int_or_none(row.cache_hit),
        )
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO decision_ledger "
                f"({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )

    def complete(
        self,
        proposal_id: str,
        *,
        decision: str,
        decided_at: datetime | None = None,
        actor_ref: str | None = None,
        proposed_text: str | None = None,
        final_text: str | None = None,
        applied_ok: bool | None = None,
        apply_skip_reason: str | None = None,
    ) -> None:
        if not self._exists():
            return
        decided_at = decided_at or datetime.now(timezone.utc)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT proposed_at FROM decision_ledger WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if existing is None:
                return
            proposed_at = _parse_iso(existing[0])
            elapsed = (
                (decided_at.astimezone(timezone.utc) - proposed_at).total_seconds()
                if proposed_at is not None else None
            )
            edit: EditMetrics | None = None
            if (
                decision == "edited"
                and proposed_text is not None
                and final_text is not None
            ):
                edit = compute_edit_metrics(proposed_text, final_text)
            conn.execute(
                """
                UPDATE decision_ledger SET
                    decision = ?, decided_at = ?, actor_ref = ?,
                    time_to_decision_seconds = ?,
                    edit_char_distance = ?, edit_distance_normalized = ?,
                    edit_semantic_similarity = ?, edit_sections_changed = ?,
                    applied_ok = ?, apply_skip_reason = ?
                WHERE proposal_id = ?
                """,
                (
                    decision, _iso(decided_at), actor_ref, elapsed,
                    edit.char_distance if edit else None,
                    edit.distance_normalized if edit else None,
                    edit.semantic_similarity if edit else None,
                    _json_dumps(list(edit.sections_changed)) if edit else "[]",
                    _as_int_or_none(applied_ok), apply_skip_reason,
                    proposal_id,
                ),
            )

    def mark_undone(self, proposal_id: str, *, at: datetime | None = None) -> None:
        """Undo doesn't exist yet as a capability (prompt 31 builds it), but
        the column and this setter land now — undo is the cleanest strong
        negative signal the product will ever produce, and it should demote
        a grant when it appears (see ``grants.suggest_demotions``, a future
        wiring point once prompt 31 lands)."""
        if not self._exists():
            return
        at = at or datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                "UPDATE decision_ledger SET undone = 1, undone_at = ? "
                "WHERE proposal_id = ?",
                (_iso(at), proposal_id),
            )

    def rows(
        self,
        *,
        since: datetime | None = None,
        domain: str | None = None,
        action: str | None = None,
    ) -> list[LedgerRow]:
        if not self._exists():
            return []
        clauses = []
        params: list[Any] = []
        if since is not None:
            clauses.append("proposed_at >= ?")
            params.append(_iso(since))
        if domain is not None:
            clauses.append("domain = ?")
            params.append(domain)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            cursor = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM decision_ledger{where} "
                f"ORDER BY proposed_at ASC",
                params,
            )
            return [_row_from_sqlite(record) for record in cursor.fetchall()]


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def _json_loads(raw: str | None) -> Any:
    import json

    if not raw:
        return None
    return json.loads(raw)


def _as_int_or_none(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _as_bool_or_none(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _row_from_sqlite(record: Sequence[Any]) -> LedgerRow:
    values = dict(zip(_COLUMNS, record))
    return LedgerRow(
        proposal_id=values["proposal_id"],
        thread_id=values["thread_id"],
        domain=values["domain"],
        action=values["action"],
        proposed_at=_parse_iso_required(values["proposed_at"]),
        autonomy_rung_granted=values["autonomy_rung_granted"],
        autonomy_rung_used=values["autonomy_rung_used"],
        scope_matched=bool(values["scope_matched"]),
        model_id=values["model_id"],
        prompt_version=values["prompt_version"],
        playbook_commit=values["playbook_commit"],
        context_attribution=ContextAttribution.from_json(
            _json_loads(values["context_attribution"])
        ),
        triage_priority=values["triage_priority"],
        base_priority=values["base_priority"],
        sender_importance_tier=values["sender_importance_tier"],
        profile_reason=values["profile_reason"],
        eligible_item_count=values["eligible_item_count"],
        batch_id=values["batch_id"],
        decision=values["decision"],
        decided_at=_parse_iso(values["decided_at"]),
        actor_ref=values["actor_ref"],
        time_to_decision_seconds=values["time_to_decision_seconds"],
        edit_char_distance=values["edit_char_distance"],
        edit_distance_normalized=values["edit_distance_normalized"],
        edit_semantic_similarity=values["edit_semantic_similarity"],
        edit_sections_changed=tuple(_json_loads(values["edit_sections_changed"]) or ()),
        applied_ok=_as_bool_or_none(values["applied_ok"]),
        apply_skip_reason=values["apply_skip_reason"],
        undone=bool(values["undone"]),
        undone_at=_parse_iso(values["undone_at"]),
        input_tokens=values["input_tokens"],
        output_tokens=values["output_tokens"],
        cache_hit=_as_bool_or_none(values["cache_hit"]),
    )


# ---------------------------------------------------------------------------
# Graph-state extraction — the shared wiring every dispatcher call site and
# resume_workflow use, so context_attribution/autonomy fields are computed
# exactly once (rule 3, "Build once").
# ---------------------------------------------------------------------------


def record_proposal(
    ledger: Any,
    *,
    thread_id: str,
    domain: str,
    action: str,
    result: dict[str, Any],
    model_id: str | None = None,
    eligible_item_count: int | None = None,
    batch_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Write the propose-time ledger row from a draft-approve graph's
    just-returned state — whether paused at the human-approval interrupt, or
    already decided via the auto-apply path (in which case this also
    completes the row in the same call, since there is no separate resume).

    ``ledger=None`` (not yet wired at this call site) and any write failure
    are both silently swallowed (logged) — analytic state must never break
    a decision path a human is waiting on, the same posture ``audit_log``
    already holds throughout this codebase.
    """
    if ledger is None:
        return
    try:
        row = _row_from_propose_result(
            thread_id=thread_id, domain=domain, action=action, result=result,
            model_id=model_id, eligible_item_count=eligible_item_count,
            batch_id=batch_id, now=now,
        )
        ledger.propose(row)
        if result.get("decision") is not None:
            _complete_from_result(ledger, thread_id, result, now=now)
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        logger.warning("decision ledger propose failed for %s", thread_id, exc_info=True)


def record_decision(
    ledger: Any,
    *,
    thread_id: str,
    result: dict[str, Any],
    actor: str | None = None,
    now: datetime | None = None,
) -> None:
    """Complete a ledger row from a resumed draft-approve workflow's final
    state — called from ``resume_workflow``, the single shared resume path
    for every approval card regardless of action/domain. Best-effort, same
    posture as :func:`record_proposal`."""
    if ledger is None:
        return
    try:
        _complete_from_result(ledger, thread_id, result, actor=actor, now=now)
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        logger.warning("decision ledger complete failed for %s", thread_id, exc_info=True)


def _row_from_propose_result(
    *,
    thread_id: str,
    domain: str,
    action: str,
    result: dict[str, Any],
    model_id: str | None,
    eligible_item_count: int | None,
    batch_id: str | None,
    now: datetime | None,
) -> LedgerRow:
    events = result.get("audit_events") or []
    gate_event = next(
        (e for e in events if e.get("event") == "autonomy_gate"), None
    ) or {}
    scope_context = gate_event.get("scope_context") or {}
    # Build prompt 28: prompt_version and token usage/cache-hit are already
    # on the graph's own "drafted" audit event (draft_approve.py's ``draft``
    # node) -- read from there, the same "Build once" pattern gate_event
    # above already uses, rather than threading new parameters through
    # every one of record_proposal's callers.
    drafted_event = next(
        (e for e in events if e.get("event") == "drafted"), None
    ) or {}
    return LedgerRow(
        proposal_id=thread_id,
        thread_id=thread_id,
        domain=domain,
        action=action,
        proposed_at=now or datetime.now(timezone.utc),
        autonomy_rung_granted=gate_event.get("autonomy_rung_granted"),
        autonomy_rung_used=gate_event.get("max_rung"),
        scope_matched=bool(gate_event.get("scope_matched", False)),
        model_id=model_id,
        prompt_version=drafted_event.get("prompt_version"),
        context_attribution=ContextAttribution(
            memory_ids=tuple(result.get("retrieved_memory_ids") or ()),
        ),
        triage_priority=scope_context.get("priority"),
        base_priority=result.get("base_priority"),
        sender_importance_tier=scope_context.get("tier"),
        profile_reason=gate_event.get("profile_reason"),
        eligible_item_count=eligible_item_count,
        batch_id=batch_id,
        input_tokens=drafted_event.get("input_tokens"),
        output_tokens=drafted_event.get("output_tokens"),
        cache_hit=drafted_event.get("cache_hit"),
    )


def _complete_from_result(
    ledger: Any,
    thread_id: str,
    result: dict[str, Any],
    *,
    actor: str | None = None,
    now: datetime | None = None,
) -> None:
    decision = result.get("decision")
    if decision not in _DECIDED_STATES:
        return  # still pending (paused at the interrupt) — nothing to complete
    now = now or datetime.now(timezone.utc)
    proposed_text = result.get("proposed_draft")
    final_text = result.get("final_text")
    applied_ref = result.get("applied_ref")
    apply_error = result.get("apply_error")
    applied_ok: bool | None = None
    apply_skip_reason: str | None = None
    if decision in ("approved", "edited"):
        applied_ok = bool(applied_ref)
        if not applied_ref:
            apply_skip_reason = apply_error or "nothing_to_materialize"
    ledger.complete(
        thread_id,
        decision=decision,
        decided_at=now,
        actor_ref=actor,
        proposed_text=proposed_text if decision == "edited" else None,
        final_text=final_text if decision == "edited" else None,
        applied_ok=applied_ok,
        apply_skip_reason=apply_skip_reason,
    )


# ---------------------------------------------------------------------------
# Aggregation — the north-star metric + coverage guardrail (shared by local
# CLI and hosted; pure functions over rows, storage-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricsSlice:
    """One row of ``attune metrics`` output — the overall window, or one
    domain/tier slice of it. Coverage renders alongside edit burden always
    (never separable — see the module docstring's coverage rationale)."""

    label: str
    proposals: int
    decided: int
    edit_burden: float | None
    clean_approval_rate: float | None
    p50_time_to_decision_seconds: float | None
    coverage: float | None
    undo_rate: float | None
    escalation_rate: float | None
    # Build prompt 28: token spend and cache-hit rate, over rows that
    # recorded usage (the draft-approve graph's default draft_fn) -- None
    # when no row in the slice ever recorded usage, so the table renders
    # "—" rather than a misleading zero.
    total_input_tokens: int | None
    total_output_tokens: int | None
    cache_hit_rate: float | None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _coverage(rows: Sequence[LedgerRow]) -> float | None:
    """proposals / eligible items, deduped by batch_id — see the module
    docstring for why summing eligible_item_count per-row would double count.
    A row with no batch_id is its own singleton batch (keyed by proposal_id);
    a row with neither an eligible_item_count nor a batch_id contributes 1
    to the denominator (itself), so coverage still renders rather than going
    silently absent for a call site that hasn't been fully wired yet."""
    if not rows:
        return None
    batch_totals: dict[str, int] = {}
    for row in rows:
        key = row.batch_id or f"__row__:{row.proposal_id}"
        count = row.eligible_item_count if row.eligible_item_count is not None else 1
        # Every row from the same batch should carry the same total; the
        # first one seen wins (they're expected to agree).
        batch_totals.setdefault(key, count)
    eligible = sum(batch_totals.values())
    if eligible <= 0:
        return None
    return len(rows) / eligible


def compute_metrics_slice(rows: Sequence[LedgerRow], *, label: str = "all") -> MetricsSlice:
    """The north-star number (mean edit-distance-normalized over SENT
    proposals) plus its mandatory coverage denominator and the rest of the
    supporting metrics, over whatever ``rows`` already represents (the
    caller slices by window/domain/tier before calling this)."""
    decided = [r for r in rows if r.decision in _DECIDED_STATES]
    sent = [r for r in decided if r.decision in ("approved", "edited")]

    edit_burden = (
        _mean([r.edit_distance_normalized or 0.0 for r in sent]) if sent else None
    )
    clean = sum(1 for r in decided if r.decision == "approved")
    clean_rate = clean / len(decided) if decided else None
    p50 = _median([
        r.time_to_decision_seconds for r in decided
        if r.time_to_decision_seconds is not None
    ])
    coverage = _coverage(rows)
    undone = sum(1 for r in rows if r.undone)
    undo_rate = undone / len(sent) if sent else None

    escalations = sum(
        1 for r in rows
        if r.autonomy_rung_granted is not None
        and r.autonomy_rung_used is not None
        and r.autonomy_rung_granted >= 3  # Rung.ACT_NOTIFY
        and r.autonomy_rung_used < r.autonomy_rung_granted
    )
    grant_eligible = sum(
        1 for r in rows
        if r.autonomy_rung_granted is not None and r.autonomy_rung_granted >= 3
    )
    escalation_rate = escalations / grant_eligible if grant_eligible else None

    metered = [r for r in rows if r.input_tokens is not None or r.output_tokens is not None]
    total_input = sum(r.input_tokens or 0 for r in metered) if metered else None
    total_output = sum(r.output_tokens or 0 for r in metered) if metered else None
    cache_reported = [r for r in rows if r.cache_hit is not None]
    cache_hit_rate = (
        sum(1 for r in cache_reported if r.cache_hit) / len(cache_reported)
        if cache_reported else None
    )

    return MetricsSlice(
        label=label,
        proposals=len(rows),
        decided=len(decided),
        edit_burden=edit_burden,
        clean_approval_rate=clean_rate,
        p50_time_to_decision_seconds=p50,
        coverage=coverage,
        undo_rate=undo_rate,
        escalation_rate=escalation_rate,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        cache_hit_rate=cache_hit_rate,
    )


def _mean(values: Sequence[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def window_rows(
    rows: Sequence[LedgerRow], *, window_days: int = 14, now: datetime | None = None,
) -> list[LedgerRow]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    return [r for r in rows if r.proposed_at >= since]


def render_metrics_table(
    rows: Sequence[LedgerRow],
    *,
    window_days: int = 14,
    now: datetime | None = None,
) -> str:
    """The ``attune metrics`` rendering: the overall window, then a slice per
    domain, then a slice per sender importance tier. Plain table, no charts
    (this is a metric surface, not a dashboard) — coverage is always
    rendered beside edit burden, on every row, never separable."""
    now = now or datetime.now(timezone.utc)
    scoped = window_rows(rows, window_days=window_days, now=now)

    lines = [f"Decision ledger metrics — last {window_days} days"]
    if not scoped:
        lines.append("No proposals in this window.")
        return "\n".join(lines)

    header = (
        f"{'slice':<20} {'proposals':>9} {'decided':>7} {'edit_burden':>11} "
        f"{'clean%':>7} {'p50_ttd_s':>10} {'coverage':>8} {'undo%':>7} {'escal%':>7} "
        f"{'in_tok':>8} {'out_tok':>8} {'cache%':>7}"
    )
    lines.append(header)

    def _row_line(m: MetricsSlice) -> str:
        return (
            f"{m.label:<20} {m.proposals:>9} {m.decided:>7} "
            f"{_fmt(m.edit_burden):>11} {_fmt_pct(m.clean_approval_rate):>7} "
            f"{_fmt(m.p50_time_to_decision_seconds):>10} "
            f"{_fmt_pct(m.coverage):>8} {_fmt_pct(m.undo_rate):>7} "
            f"{_fmt_pct(m.escalation_rate):>7} "
            f"{_fmt_int(m.total_input_tokens):>8} {_fmt_int(m.total_output_tokens):>8} "
            f"{_fmt_pct(m.cache_hit_rate):>7}"
        )

    lines.append(_row_line(compute_metrics_slice(scoped, label="(all)")))

    domains = sorted({r.domain for r in scoped})
    if len(domains) > 1:
        lines.append("")
        lines.append("by domain:")
        for domain in domains:
            subset = [r for r in scoped if r.domain == domain]
            lines.append(_row_line(compute_metrics_slice(subset, label=domain)))

    tiers = sorted({r.sender_importance_tier for r in scoped if r.sender_importance_tier})
    if tiers:
        lines.append("")
        lines.append("by sender importance tier:")
        for tier in tiers:
            subset = [r for r in scoped if r.sender_importance_tier == tier]
            lines.append(_row_line(compute_metrics_slice(subset, label=tier)))

    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt_int(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"
