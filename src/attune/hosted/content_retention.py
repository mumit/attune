"""Bounded entry point for the hosted customer-content retention job.

Mirrors ``protocol_retention.py`` exactly in shape (bounded batches, a
singleton per-run identifier, content-free aggregate output) but prunes
customer content -- conversation turns/conversations and hosted brief
deliveries -- by the 30-day "conversation turns and derived summaries" window
fixed in docs/data-lifecycle.md, instead of protocol artifacts. See that
document's "Content retention and tenant deletion design" section for why
memories and importance/attention signals are deliberately out of scope here.

Gated by ``ATTUNE_ENABLE_CONTENT_RETENTION`` (default off): the module-level
``run_content_retention`` function has no gate of its own -- it is the bounded
executor itself, exactly like ``run_protocol_retention`` -- but ``main`` (the
Cloud Run job entry point) refuses to call the database at all unless the
gate is explicitly ``"true"``, so a paused-but-accidentally-invoked job stays
a content-free no-op rather than a live prune.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from uuid import uuid4

from .cloud_sql import iam_connection


def run_content_retention(
    *, batch_size: int = 500, max_batches: int = 4
) -> dict[str, int | bool]:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        raise TypeError("batch_size must be an integer")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if not isinstance(max_batches, int) or isinstance(max_batches, bool):
        raise TypeError("max_batches must be an integer")
    if not 1 <= max_batches <= 10:
        raise ValueError("max_batches must be between 1 and 10")
    totals = [0, 0, 0]
    batches = 0
    backlog_possible = False
    with closing(iam_connection()) as connection:
        try:
            with closing(connection.cursor()) as cursor:
                for batch_index in range(max_batches):
                    cursor.execute(
                        "SELECT * FROM "
                        "attune.prune_expired_customer_content(%s, %s)",
                        (uuid4(), batch_size),
                    )
                    row = cursor.fetchone()
                    if row is None or len(row) != 3:
                        raise RuntimeError(
                            "content retention function returned an invalid result"
                        )
                    counts = [int(value) for value in row]
                    if any(value < 0 or value > batch_size for value in counts):
                        raise RuntimeError(
                            "content retention function returned an invalid count"
                        )
                    totals = [total + value for total, value in zip(totals, counts)]
                    batches = batch_index + 1
                    if all(value < batch_size for value in counts):
                        break
                else:
                    backlog_possible = any(value == batch_size for value in counts)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    result = {
        "conversation_turns": totals[0],
        "conversations": totals[1],
        "hosted_brief_deliveries": totals[2],
        "batches": batches,
        "backlog_possible": backlog_possible,
    }
    return result


def main() -> None:
    enabled = os.environ.get("ATTUNE_ENABLE_CONTENT_RETENTION", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_CONTENT_RETENTION must be true or false")
    if enabled != "true":
        print(
            json.dumps(
                {
                    "severity": "INFO",
                    "message": "Attune content retention is disabled",
                    "event": "attune_content_retention_disabled",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return
    raw_batch_size = os.environ.get("ATTUNE_CONTENT_RETENTION_BATCH_SIZE", "500")
    raw_max_batches = os.environ.get("ATTUNE_CONTENT_RETENTION_MAX_BATCHES", "4")
    try:
        batch_size = int(raw_batch_size)
    except ValueError as exc:
        raise ValueError(
            "ATTUNE_CONTENT_RETENTION_BATCH_SIZE must be an integer"
        ) from exc
    try:
        max_batches = int(raw_max_batches)
    except ValueError as exc:
        raise ValueError(
            "ATTUNE_CONTENT_RETENTION_MAX_BATCHES must be an integer"
        ) from exc
    result = run_content_retention(batch_size=batch_size, max_batches=max_batches)
    print(
        json.dumps(
            {
                "severity": "WARNING" if result["backlog_possible"] else "INFO",
                "message": "Attune content retention completed",
                "event": "attune_content_retention",
                **result,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
