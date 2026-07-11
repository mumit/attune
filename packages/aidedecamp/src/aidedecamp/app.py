"""Runtime assembly for Aide-de-camp (design doc next-steps §1).

Wires the three collaborators built in earlier phases into one runnable process:

    make_client()          -> Fuel iX BearerClient (fuelix.py)
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
from .fuelix import make_client
from .memory.base import MemoryStore
from .memory.mem0_store import Mem0Store, build_mem0_config
from .orchestrator import PermissionMatrix, build_draft_approve_graph


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
    _db_conn: Any = field(default=None, repr=False)

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
) -> AppContext:
    """Assemble the runtime from config and optional overrides.

    When overrides are absent the real implementations are constructed:

    - *client*       via ``make_client()`` (reads FUELIX_TOKEN from env)
    - *store*        via ``Mem0Store(build_mem0_config())`` (requires mem0ai +
                     a running Qdrant instance)
    - *checkpointer* via ``SqliteSaver`` backed by
                     ``settings.checkpointer_db_path`` (requires
                     langgraph-checkpoint-sqlite)
    - *audit_log*    via ``JsonlAuditLog(settings.audit_log_path)`` — the
                     structured reason-for-action log (design rule 4.7)

    Pass fakes for all four in tests to keep the suite offline::

        build_app(client=FakeClient(), store=FakeStore(), checkpointer=InMemorySaver(),
                  audit_log=FakeAuditLog())
    """
    settings = settings or Settings.from_env()

    resolved_client = client or make_client()

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

    resolved_store: MemoryStore = store or Mem0Store(build_mem0_config())
    resolved_audit_log: AuditLog = audit_log or JsonlAuditLog(settings.audit_log_path)

    graph = build_draft_approve_graph(
        client=resolved_client,
        store=resolved_store,
        matrix=matrix,
        checkpointer=resolved_checkpointer,
    )

    return AppContext(
        graph=graph,
        client=resolved_client,
        store=resolved_store,
        settings=settings,
        audit_log=resolved_audit_log,
        _db_conn=db_conn,
    )
