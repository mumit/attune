from aidedecamp.ingestion.retry_queue import SqliteRetryQueue


def test_retry_queue_round_trip_and_dedupe(tmp_path):
    queue = SqliteRetryQueue(str(tmp_path / "retries.db"))
    queue.enqueue("gmail_thread", "t1", {"history_id": "100"}, error="Timeout")
    queue.enqueue("gmail_thread", "t1", {"history_id": "101"}, error="Again")

    items = queue.pending()
    assert len(items) == 1
    assert items[0].payload == {"history_id": "101"}

    queue.fail(items[0], error="StillDown")
    assert queue.pending()[0].attempts == 1
    queue.complete(queue.pending()[0])
    assert queue.pending() == []
