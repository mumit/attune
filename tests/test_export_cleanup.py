"""Tests for bounded delete-only export-attempt cleanup."""

from uuid import UUID

import pytest

from attune.hosted.customer_export_writer import ObjectNotFound
from attune.hosted.export_cleanup import (
    ExportCleanupCandidate,
    ExportExpirationCandidate,
    GoogleDeleteOnlyExportObjects,
    PostgresExportCleanupRepository,
    run_export_cleanup,
)


def _candidate(index):
    return ExportCleanupCandidate(
        UUID(int=1), UUID(int=100 + index), UUID(int=200 + index), UUID(int=300 + index)
    )


def _expiration(index):
    return ExportExpirationCandidate(
        UUID(int=1), UUID(int=400 + index), UUID(int=500 + index), 600 + index
    )


class Repository:
    def __init__(self, batches, expiration_batches=()):
        self.batches = list(batches)
        self.expiration_batches = list(expiration_batches)
        self.completed = []
        self.expirations_completed = []

    def claim(self, *, cleanup_run_id, batch_size):
        return tuple(self.batches.pop(0)) if self.batches else ()

    def complete(self, candidate, *, cleanup_run_id):
        self.completed.append(candidate)
        return True

    def claim_expirations(self, *, cleanup_run_id, batch_size):
        return (
            tuple(self.expiration_batches.pop(0))
            if self.expiration_batches else ()
        )

    def complete_expiration(self, candidate, *, cleanup_run_id):
        self.expirations_completed.append(candidate)
        return True


class Objects:
    def __init__(self, *, missing=(), error=None):
        self.missing = set(missing)
        self.error = error
        self.deleted = []

    def delete(self, name, *, generation=None):
        self.deleted.append((name, generation))
        if self.error:
            raise self.error
        if name in self.missing:
            raise ObjectNotFound()


def test_cleanup_deletes_known_names_and_treats_absence_as_success():
    candidates = [_candidate(1), _candidate(2)]
    missing = {f"objects/{candidates[1].object_id}.bin"}
    repository = Repository([candidates])
    objects = Objects(missing=missing)
    result = run_export_cleanup(repository, objects, batch_size=10)
    assert result == {
        "objects_deleted": 2,
        "attempts_deleted": 2,
        "exports_cleaned": 0,
        "batches": 2,
        "backlog_possible": False,
    }
    assert repository.completed == candidates
    assert all(
        name.startswith("objects/") and name.endswith(".bin") and generation is None
        for name, generation in objects.deleted
    )


def test_cleanup_expires_ready_export_only_after_exact_generation_delete():
    expiration = _expiration(1)
    repository = Repository([], [[expiration]])
    objects = Objects()
    result = run_export_cleanup(repository, objects, batch_size=10)
    assert result == {
        "objects_deleted": 1,
        "attempts_deleted": 0,
        "exports_cleaned": 1,
        "batches": 2,
        "backlog_possible": False,
    }
    assert objects.deleted == [
        (f"objects/{expiration.object_id}.bin", expiration.object_generation)
    ]
    assert repository.expirations_completed == [expiration]


def test_expiry_storage_failure_leaves_database_claim_uncompleted():
    expiration = _expiration(1)
    repository = Repository([], [[expiration]])
    with pytest.raises(RuntimeError, match="storage unavailable"):
        run_export_cleanup(repository, Objects(error=RuntimeError("storage unavailable")))
    assert repository.expirations_completed == []


def test_google_cleanup_binds_expiry_delete_to_exact_generation():
    class Blob:
        def delete(self, **keyword_arguments):
            self.keyword_arguments = keyword_arguments

    class Bucket:
        def __init__(self):
            self.value = Blob()

        def blob(self, name):
            self.name = name
            return self.value

    class Client:
        def __init__(self):
            self.value = Bucket()

        def bucket(self, name):
            self.name = name
            return self.value

    client = Client()
    objects = GoogleDeleteOnlyExportObjects(
        "test-export-bucket", client=client, _not_found=FileNotFoundError
    )
    expiration = _expiration(1)
    name = f"objects/{expiration.object_id}.bin"
    objects.delete(name, generation=expiration.object_generation)
    assert client.value.name == name
    assert client.value.value.keyword_arguments == {
        "if_generation_match": expiration.object_generation
    }
    with pytest.raises(ValueError, match="generation must be positive"):
        objects.delete(name, generation=True)


def test_storage_failure_leaves_database_claim_uncompleted():
    repository = Repository([[_candidate(1)]])
    with pytest.raises(RuntimeError, match="storage unavailable"):
        run_export_cleanup(repository, Objects(error=RuntimeError("storage unavailable")))
    assert repository.completed == []


def test_cleanup_is_bounded_and_reports_possible_backlog():
    repository = Repository([[_candidate(1)], [_candidate(2)]])
    result = run_export_cleanup(repository, Objects(), batch_size=1, max_batches=2)
    assert result == {
        "objects_deleted": 2,
        "attempts_deleted": 2,
        "exports_cleaned": 0,
        "batches": 3,
        "backlog_possible": True,
    }


@pytest.mark.parametrize("batch,max_batches", [(0, 1), (101, 1), (1, 0), (1, 11), (True, 1)])
def test_cleanup_rejects_unbounded_configuration(batch, max_batches):
    with pytest.raises(ValueError):
        run_export_cleanup(Repository([]), Objects(), batch_size=batch, max_batches=max_batches)


def test_repository_supports_pg8000_cursors_without_context_manager():
    class Cursor:
        def execute(self, query, parameters):
            self.query = query

        def fetchall(self):
            return []

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self):
            self.value = Cursor()

        def cursor(self):
            return self.value

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    connection = Connection()
    repository = PostgresExportCleanupRepository(lambda: connection)
    assert repository.claim(cleanup_run_id=UUID(int=9), batch_size=10) == ()
    assert connection.value.closed and connection.committed and connection.closed
