"""Bounded entry point for the hosted expired-protocol retention job."""

from __future__ import annotations

import json
import logging
import os
from contextlib import closing
from uuid import uuid4

from .cloud_sql import iam_connection

LOG = logging.getLogger("attune.hosted.protocol_retention")


def run_protocol_retention(*, batch_size: int = 500) -> dict[str, int]:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        raise TypeError("batch_size must be an integer")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    run_id = uuid4()
    with closing(iam_connection()) as connection:
        try:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.prune_expired_protocol_records(%s, %s)",
                    (run_id, batch_size),
                )
                row = cursor.fetchone()
                if row is None or len(row) != 4:
                    raise RuntimeError("retention function returned an invalid result")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    result = {
        "oauth_transactions": int(row[0]),
        "channel_setup_transactions": int(row[1]),
        "identity_sessions": int(row[2]),
        "provider_events": int(row[3]),
    }
    LOG.info("protocol retention completed: %s", json.dumps(result, sort_keys=True))
    return result


def main() -> None:
    logging.basicConfig(level=os.environ.get("ATTUNE_LOG_LEVEL", "INFO"))
    raw_batch_size = os.environ.get("ATTUNE_RETENTION_BATCH_SIZE", "500")
    try:
        batch_size = int(raw_batch_size)
    except ValueError as exc:
        raise ValueError("ATTUNE_RETENTION_BATCH_SIZE must be an integer") from exc
    run_protocol_retention(batch_size=batch_size)


if __name__ == "__main__":
    main()
