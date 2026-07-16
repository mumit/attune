"""Bounded entry point for the hosted expired-protocol retention job."""

from __future__ import annotations

import json
import os
from contextlib import closing
from uuid import uuid4

from .cloud_sql import iam_connection

def run_protocol_retention(
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
    totals = [0, 0, 0, 0]
    batches = 0
    backlog_possible = False
    with closing(iam_connection()) as connection:
        try:
            with closing(connection.cursor()) as cursor:
                for batch_index in range(max_batches):
                    cursor.execute(
                        "SELECT * FROM "
                        "attune.prune_expired_protocol_records(%s, %s)",
                        (uuid4(), batch_size),
                    )
                    row = cursor.fetchone()
                    if row is None or len(row) != 4:
                        raise RuntimeError(
                            "retention function returned an invalid result"
                        )
                    counts = [int(value) for value in row]
                    if any(value < 0 or value > batch_size for value in counts):
                        raise RuntimeError(
                            "retention function returned an invalid count"
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
        "oauth_transactions": totals[0],
        "channel_setup_transactions": totals[1],
        "identity_sessions": totals[2],
        "provider_events": totals[3],
        "batches": batches,
        "backlog_possible": backlog_possible,
    }
    return result


def main() -> None:
    raw_batch_size = os.environ.get("ATTUNE_RETENTION_BATCH_SIZE", "500")
    raw_max_batches = os.environ.get("ATTUNE_RETENTION_MAX_BATCHES", "4")
    try:
        batch_size = int(raw_batch_size)
    except ValueError as exc:
        raise ValueError("ATTUNE_RETENTION_BATCH_SIZE must be an integer") from exc
    try:
        max_batches = int(raw_max_batches)
    except ValueError as exc:
        raise ValueError("ATTUNE_RETENTION_MAX_BATCHES must be an integer") from exc
    result = run_protocol_retention(
        batch_size=batch_size, max_batches=max_batches
    )
    print(
        json.dumps(
            {
                "severity": "WARNING" if result["backlog_possible"] else "INFO",
                "message": "Attune protocol retention completed",
                "event": "attune_protocol_retention",
                **result,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
