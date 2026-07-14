"""Durable source-work retries after an ingestion cursor has advanced."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class RetryItem:
    kind: str
    source_ref: str
    payload: dict[str, Any]
    attempts: int = 0


class SqliteRetryQueue:
    """Small durable queue with one live item per (kind, source_ref).

    Disk initialization is LAZY, per the same convention as every JSON state
    file in this codebase: constructing the queue (which ``build_runtime``
    does unconditionally) touches nothing; the database and its WAL sidecars
    appear only when the first failure is actually enqueued. Read paths on a
    never-created queue short-circuit without creating the file either —
    otherwise the five-minute drain job would recreate it immediately.
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_retries (
                    kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (kind, source_ref)
                )
                """
            )

    def enqueue(
        self, kind: str, source_ref: str, payload: dict[str, Any], *, error: str
    ) -> None:
        self._initialize()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_retries
                    (kind, source_ref, payload, attempts, last_error, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(kind, source_ref) DO UPDATE SET
                    payload=excluded.payload,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (kind, source_ref, json.dumps(payload), error, now),
            )

    def pending(self, *, limit: int = 25) -> list[RetryItem]:
        if not self._exists():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT kind, source_ref, payload, attempts
                FROM source_retries
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [RetryItem(r[0], r[1], json.loads(r[2]), r[3]) for r in rows]

    def complete(self, item: RetryItem) -> None:
        if not self._exists():
            return
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM source_retries WHERE kind=? AND source_ref=?",
                (item.kind, item.source_ref),
            )

    def fail(self, item: RetryItem, *, error: str) -> None:
        if not self._exists():
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE source_retries
                SET attempts=attempts+1, last_error=?, updated_at=?
                WHERE kind=? AND source_ref=?
                """,
                (error, now, item.kind, item.source_ref),
            )
