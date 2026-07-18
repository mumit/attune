"""Runtime assembly for Attune (design doc next-steps §1).

Wires the three collaborators built in earlier phases into one runnable process:

    make_client()          -> official OpenAI SDK client (llm.py)
    Mem0Store(config)      -> memory substrate    (memory/mem0_store.py)
    SqliteSaver(conn)      -> durable checkpointer (langgraph-checkpoint-sqlite)

and compiles the canonical draft-approve graph over them.

All collaborators remain injected — pass overrides to build_app() for tests
(InMemorySaver, FakeStore, FakeClient) or to swap substrates without touching
the assembly logic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .audit.log import AuditLog, JsonlAuditLog
from .config import Settings
from .llm import make_client
from .memory.base import MemoryStore
from .memory.mem0_store import Mem0Store, build_mem0_config
from .orchestrator import (
    PermissionMatrix,
    archive_draft_fn,
    build_draft_approve_graph,
    calendar_action_draft_fn,
)


@dataclass
class AppContext:
    """Assembled runtime: compiled graph plus its wired collaborators.

    Use as a context manager so the SQLite connection is closed on shutdown::

        with build_app() as app:
            app.graph.invoke(state, {"configurable": {"thread_id": tid}})
    """

    graph: Any
    client: Any
    store: MemoryStore
    settings: Settings
    audit_log: AuditLog
    # Phase 3 stage 1 (docs/future-state.md, G9): the archive-proposal graph.
    # Same collaborators (client/store/matrix/checkpointer/importance_profile)
    # as ``graph``, compiled separately because its draft_fn (deterministic,
    # no model call) and apply_fn (``label_thread``, not ``create_draft``)
    # differ — see ``orchestrator.draft_approve.archive_draft_fn``. None
    # until ``build_app`` builds it; the dispatcher only reaches for it once
    # all three label gates (matrix rung, connector probe, opt-in flag) hold.
    label_graph: Any = None
    # Phase 3 stage 2: the decline-invite/reschedule proposal graph. Same
    # collaborators as ``graph``/``label_graph``, compiled separately for the
    # same reason ``label_graph`` was — a deterministic draft_fn (no model
    # call — see ``orchestrator.draft_approve.calendar_action_draft_fn``)
    # and an apply_fn that materializes via ``connector.decline_invite``/
    # ``reschedule_event``, never ``create_draft``. None until ``build_app``
    # builds it; the dispatcher only reaches for it once all three calendar-
    # write gates (matrix rung, connector probe, opt-in flag) hold for the
    # relevant action.
    calendar_action_graph: Any = None
    matrix: PermissionMatrix | None = None
    # Live policy source (prompt 19): the gate and every posture surface
    # read through this so grants/revocations bite without a restart.
    matrix_provider: Any = None
    # Phase 1 (docs/future-state.md, G5/G6): the deterministic per-sender
    # importance profile, wired the same way the memory store and audit log
    # are — injected here so the graph's capture node can dual-write.
    importance_profile: Any = None
    _db_conn: Any = field(default=None, repr=False)

    def current_matrix(self) -> PermissionMatrix:
        """The live autonomy posture (falls back to the boot snapshot)."""
        if self.matrix_provider is not None:
            return self.matrix_provider()
        from .orchestrator import default_matrix

        return self.matrix or default_matrix()

    def close(self) -> None:
        """Close the SQLite connection if one was opened by build_app."""
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None

    def __enter__(self) -> "AppContext":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def build_app(
    settings: Settings | None = None,
    *,
    client: Any = None,
    store: MemoryStore | None = None,
    checkpointer: Any = None,
    matrix: PermissionMatrix | None = None,
    audit_log: AuditLog | None = None,
    apply_fn: Any = None,
    importance_profile: Any = None,
    label_apply_fn: Any = None,
    calendar_action_apply_fn: Any = None,
) -> AppContext:
    """Assemble the runtime from config and optional overrides.

    When overrides are absent the real implementations are constructed:

    - *client*       via ``make_client()`` (reads ATTUNE_LLM_API_KEY from env)
    - *store*        via ``Mem0Store(build_mem0_config())`` (requires mem0ai +
                     a running Qdrant instance)
    - *checkpointer* via ``SqliteSaver`` backed by
                     ``settings.checkpointer_db_path`` (requires
                     langgraph-checkpoint-sqlite)
    - *audit_log*    via ``JsonlAuditLog(settings.audit_log_path)`` — the
                     structured reason-for-action log (design rule 4.7)
    - *apply_fn*     passed through to the draft-approve graph; production
                     (``runtime.build_runtime``) binds
                     ``make_connector_apply_fn(connector)`` so approved drafts
                     materialize as Gmail drafts. Absent, apply is a no-op —
                     this module has no connector to bind.
    - *importance_profile* via
                     ``JsonImportanceProfile(settings.importance_profile_path)``
                     (Phase 1, G5/G6) — the deterministic per-sender profile
                     the capture node dual-writes to alongside memory.
    - *label_apply_fn* (Phase 3 stage 1, G9) — the archive-proposal
                     counterpart to ``apply_fn``, compiled into a SEPARATE
                     graph (``AppContext.label_graph``) sharing every other
                     collaborator. Production (``runtime.build_runtime``)
                     binds ``orchestrator.make_label_apply_fn(connector)``.
                     Absent, label apply is a no-op — matches ``apply_fn``'s
                     own default.
    - *calendar_action_apply_fn* (Phase 3 stage 2) — the decline-invite/
                     reschedule counterpart to ``apply_fn``, compiled into
                     its own SEPARATE graph (``AppContext.calendar_action_graph``)
                     sharing every other collaborator. Production binds
                     ``orchestrator.make_calendar_action_apply_fn(connector)``.
                     Absent, apply is a no-op — matches ``apply_fn``'s own
                     default.

    Pass fakes for all five in tests to keep the suite offline::

        build_app(client=FakeClient(), store=FakeStore(), checkpointer=InMemorySaver(),
                  audit_log=FakeAuditLog(), importance_profile=FakeImportanceProfile())
    """
    settings = settings or Settings.from_env()

    resolved_client = client or make_client(settings=settings)

    # Autonomy posture: an injected matrix (tests) is static; otherwise the
    # gate reads LIVE through an mtime-checked provider over the persisted
    # store, so grant/revoke operations bite without a restart (prompt 19).
    resolved_matrix = matrix
    resolved_provider = None
    if resolved_matrix is None:
        from .orchestrator.grants import (
            JsonPermissionMatrixStore,
            make_matrix_provider,
        )

        resolved_provider = make_matrix_provider(
            JsonPermissionMatrixStore(settings.autonomy_state_path)
        )
        resolved_matrix = resolved_provider()

    db_conn: Any = None
    if checkpointer is None:
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise ImportError(
                "SqliteSaver requires langgraph-checkpoint-sqlite. "
                "Install it with: pip install langgraph-checkpoint-sqlite"
            ) from exc
        db_conn = sqlite3.connect(
            settings.checkpointer_db_path, check_same_thread=False
        )
        resolved_checkpointer = SqliteSaver(db_conn)
    else:
        resolved_checkpointer = checkpointer

    # The client rides into the store for the nightly consolidation pass
    # (Task.CONSOLIDATE on the strong model — design 2.2).
    resolved_store: MemoryStore = store or Mem0Store(
        build_mem0_config(settings=settings), client=resolved_client
    )
    resolved_audit_log: AuditLog = audit_log or JsonlAuditLog(settings.audit_log_path)

    resolved_importance_profile = importance_profile
    if resolved_importance_profile is None:
        from .orchestrator.importance import JsonImportanceProfile

        resolved_importance_profile = JsonImportanceProfile(
            settings.importance_profile_path
        )

    graph = build_draft_approve_graph(
        client=resolved_client,
        store=resolved_store,
        matrix=resolved_matrix,
        checkpointer=resolved_checkpointer,
        apply_fn=apply_fn,
        matrix_provider=resolved_provider,
        importance_profile=resolved_importance_profile,
    )
    # The archive-proposal graph (Phase 3 stage 1, G9): same collaborators,
    # a deterministic draft_fn (no model call — see archive_draft_fn) and a
    # label-specific apply_fn. Sharing ``resolved_checkpointer`` is safe —
    # LangGraph keys checkpoints by thread_id, and label proposals use their
    # own "archive:..." namespace (dispatcher._offer_archive_proposal), so
    # the two graphs never collide over the same checkpoint row.
    label_graph = build_draft_approve_graph(
        client=resolved_client,
        store=resolved_store,
        matrix=resolved_matrix,
        checkpointer=resolved_checkpointer,
        draft_fn=archive_draft_fn,
        apply_fn=label_apply_fn,
        matrix_provider=resolved_provider,
        importance_profile=resolved_importance_profile,
    )
    # The decline-invite/reschedule proposal graph (Phase 3 stage 2): same
    # collaborators and disjoint thread-id namespaces ("decline:..."/
    # "calendar:reschedule:...") as every other graph sharing
    # ``resolved_checkpointer``, so nothing about them can collide.
    calendar_action_graph = build_draft_approve_graph(
        client=resolved_client,
        store=resolved_store,
        matrix=resolved_matrix,
        checkpointer=resolved_checkpointer,
        draft_fn=calendar_action_draft_fn,
        apply_fn=calendar_action_apply_fn,
        matrix_provider=resolved_provider,
        importance_profile=resolved_importance_profile,
    )

    from .orchestrator import default_matrix as _default_matrix

    return AppContext(
        graph=graph,
        client=resolved_client,
        store=resolved_store,
        settings=settings,
        audit_log=resolved_audit_log,
        label_graph=label_graph,
        calendar_action_graph=calendar_action_graph,
        matrix=resolved_matrix or _default_matrix(),
        matrix_provider=resolved_provider,
        importance_profile=resolved_importance_profile,
        _db_conn=db_conn,
    )
