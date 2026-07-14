from attune.ingestion.retry_queue import RetryItem, SqliteRetryQueue


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


def test_construction_touches_nothing_on_disk(tmp_path):
    """Lazy-init contract: build_runtime constructs the queue unconditionally,
    so construction (and empty reads) must not create the database — the
    defect that littered every test run's CWD with db/wal/shm files."""
    path = tmp_path / "retries.db"
    queue = SqliteRetryQueue(str(path))

    assert not path.exists()
    assert queue.pending() == []          # empty read: still nothing
    assert not path.exists()
    queue.fail(RetryItem("k", "r", {}), error="x")   # no-op, creates nothing
    queue.complete(RetryItem("k", "r", {}))          # likewise
    assert not path.exists()


def test_first_enqueue_creates_the_database_lazily(tmp_path):
    path = tmp_path / "retries.db"
    queue = SqliteRetryQueue(str(path))
    queue.enqueue("gmail_thread", "t1", {"history_id": "1"}, error="Timeout")

    assert path.exists()
    assert len(queue.pending()) == 1
