"""Hosted migration contract plus opt-in PostgreSQL isolation tests."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from attune.hosted.migrate import (
    RUNTIME_ROLES,
    Migration,
    _dispatch_function_invariants_hold,
    apply_migrations,
    bind_runtime_roles,
    load_migrations,
    main,
    verify_database_boundary,
)
from attune.hosted.durable import (
    PostgresAutonomyRepository,
    PostgresConversationRepository,
    PostgresLifecycleRepository,
    PostgresProviderEventRepository,
    PostgresWorkflowRepository,
)
from attune.hosted.dispatch import (
    PostgresDispatchBrokerRepository,
    PostgresDispatchProducerRepository,
)
from attune.hosted.audit import (
    PostgresAuditProducerRepository,
    PostgresAuditWriterRepository,
    PostgresDispatchAuditRepository,
)
from attune.hosted.repositories import (
    PostgresApprovalRepository,
    PostgresJobRepository,
    PostgresMemoryRepository,
)
from attune.hosted.tenant import TenantContext, tenant_transaction

TENANT_A = UUID("10000000-0000-4000-8000-000000000001")
TENANT_B = UUID("20000000-0000-4000-8000-000000000002")
PRINCIPAL_A = UUID("10000000-0000-4000-8000-000000000011")
PRINCIPAL_B = UUID("20000000-0000-4000-8000-000000000012")
MEMORY_A = UUID("10000000-0000-4000-8000-000000000021")
MEMORY_B = UUID("20000000-0000-4000-8000-000000000022")
INSTALLATION_A = UUID("10000000-0000-4000-8000-000000000031")
CONNECTOR_A = UUID("10000000-0000-4000-8000-000000000041")

ROLE_BINDINGS = {
    "attune_control_plane": "attune_test_control",
    "attune_dispatch_broker": "attune_test_dispatch",
    "attune_worker": "attune_test_worker",
    "attune_secret_broker": "attune_test_broker",
    "attune_audit_writer": "attune_test_audit",
}


def test_packaged_migrations_are_ordered_and_checksum_pinned():
    migrations = load_migrations()
    assert [migration.name for migration in migrations] == sorted(
        migration.name for migration in migrations
    )
    assert migrations[0].name == "0001_tenant_boundary.sql"
    assert migrations[-1].name == "0009_privileged_function_owners.sql"
    assert all(
        migration.checksum == hashlib.sha256(migration.sql.encode()).hexdigest()
        for migration in migrations
    )


def test_tenant_context_rejects_non_uuid_values():
    with pytest.raises(ValueError):
        TenantContext.parse("model-picked-tenant")
    with pytest.raises(TypeError, match="must be a UUID"):
        TenantContext("10000000-0000-4000-8000-000000000001")  # type: ignore[arg-type]


def test_runtime_role_binding_contract_is_fixed():
    assert set(RUNTIME_ROLES) == set(ROLE_BINDINGS)
    with pytest.raises(ValueError, match="distinct IAM login"):
        duplicated = dict(ROLE_BINDINGS)
        duplicated["attune_worker"] = duplicated["attune_control_plane"]
        bind_runtime_roles(None, duplicated)


def test_dispatch_verifier_accepts_pg8000_list_rows():
    assert _dispatch_function_invariants_hold([True, True, True, True])
    assert not _dispatch_function_invariants_hold([True, True, False, True])


def test_hosted_migrator_refuses_runtime_overrides():
    with pytest.raises(ValueError, match="accepts no runtime arguments"):
        main(["--database-url=forbidden"])


def test_repositories_reject_invalid_data_before_connecting():
    def forbidden_connection():
        raise AssertionError("invalid input must not reach the database")

    context = TenantContext(TENANT_A)
    jobs = PostgresJobRepository(forbidden_connection)
    memories = PostgresMemoryRepository(forbidden_connection)
    audit = PostgresAuditProducerRepository(
        forbidden_connection, producer_kind="worker"
    )

    with pytest.raises(ValueError, match="exactly 32 bytes"):
        jobs.enqueue(
            context,
            kind="test",
            capability="read",
            payload={},
            idempotency_key=b"short",
        )
    with pytest.raises(ValueError, match="finite"):
        memories.add(
            context,
            principal_id=PRINCIPAL_A,
            creator_id=PRINCIPAL_A,
            content="memory",
            provenance={},
            source_class="user_taught",
            confidence=1,
            model="test",
            embedding=[float("nan")],
        )
    with pytest.raises(ValueError, match="audit outcome"):
        audit.request(
            context,
            idempotency_key=hashlib.sha256(b"invalid-audit").digest(),
            actor_type="worker",
            action="read",
            outcome="invented",
        )


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("ATTUNE_TEST_DATABASE_URL")
    if not value:
        pytest.skip("set ATTUNE_TEST_DATABASE_URL for PostgreSQL isolation tests")
    return value


@pytest.fixture(scope="module")
def initialized_database(database_url: str):
    psycopg = pytest.importorskip("psycopg")
    admin = psycopg.connect(database_url)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS attune_meta CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS attune CASCADE")
        cursor.execute("DROP SCHEMA IF EXISTS attune_ext CASCADE")
        for role in (*ROLE_BINDINGS.values(), "attune_test_stale_member"):
            cursor.execute(f'DROP ROLE IF EXISTS "{role}"')
            cursor.execute(f'CREATE ROLE "{role}" NOLOGIN INHERIT')
    admin.autocommit = False

    assert apply_migrations(admin) == 9
    with admin.cursor() as cursor:
        cursor.execute(
            "GRANT attune_worker TO attune_test_stale_member"
        )
    admin.commit()
    bind_runtime_roles(admin, ROLE_BINDINGS)
    verify_database_boundary(admin, ROLE_BINDINGS)
    with admin.cursor() as cursor:
        cursor.execute(
            "SELECT pg_has_role('attune_test_stale_member', 'attune_worker', 'MEMBER')"
        )
        assert cursor.fetchone() == (False,)
    assert apply_migrations(admin) == 0

    with admin.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attune.tenants (id, slug, region) "
            "VALUES (%s, %s, %s), (%s, %s, %s)",
            (TENANT_A, "tenant-a", "test", TENANT_B, "tenant-b", "test"),
        )
        cursor.execute(
            """
            INSERT INTO attune.principals (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, 'test'),
                   (%s, %s, %s, 'test')
            """,
            (
                TENANT_A,
                PRINCIPAL_A,
                hashlib.sha256(b"a").digest(),
                TENANT_B,
                PRINCIPAL_B,
                hashlib.sha256(b"b").digest(),
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.installations
                (tenant_id, id, provider, kind, external_ref_hash)
            VALUES (%s, %s, 'google', 'workspace', %s)
            """,
            (TENANT_A, INSTALLATION_A, hashlib.sha256(b"installation-a").digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.connectors
                (tenant_id, id, principal_id, installation_id, provider,
                 credential_ref, status)
            VALUES (%s, %s, %s, %s, 'google', %s, 'active')
            """,
            (
                TENANT_A,
                CONNECTOR_A,
                PRINCIPAL_A,
                INSTALLATION_A,
                UUID("10000000-0000-4000-8000-000000000051"),
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.memories
                (tenant_id, id, principal_id, content, provenance,
                 source_class, confidence)
            VALUES (%s, %s, %s, 'tenant A memory', '{}', 'user_taught', 1),
                   (%s, %s, %s, 'tenant B memory', '{}', 'user_taught', 1)
            """,
            (TENANT_A, MEMORY_A, PRINCIPAL_A, TENANT_B, MEMORY_B, PRINCIPAL_B),
        )
        cursor.execute(
            """
            INSERT INTO attune.memory_embeddings
                (tenant_id, memory_id, model, dimensions, embedding)
            VALUES (%s, %s, 'test', 3, '[1,0,0]'),
                   (%s, %s, 'test', 3, '[1,0,0]')
            """,
            (TENANT_A, MEMORY_A, TENANT_B, MEMORY_B),
        )
    admin.commit()
    yield admin
    admin.close()


def _set_role(connection, role: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f'SET ROLE "{role}"')
    connection.commit()


def _reset_role(connection) -> None:
    connection.rollback()
    with connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
    connection.commit()


def _role_connection_factory(database_url: str, role: str):
    psycopg = pytest.importorskip("psycopg")

    def connect():
        connection = psycopg.connect(database_url)
        with connection.cursor() as cursor:
            cursor.execute(f'SET ROLE "{role}"')
        connection.commit()
        return connection

    return connect


def test_missing_tenant_context_is_an_error(initialized_database):
    psycopg = pytest.importorskip("psycopg")
    _set_role(initialized_database, ROLE_BINDINGS["attune_worker"])
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with initialized_database.cursor() as cursor:
                cursor.execute("SELECT id FROM attune.memories")
        initialized_database.rollback()
    finally:
        _reset_role(initialized_database)


def test_rls_hides_other_tenant_rows_and_vectors(initialized_database):
    _set_role(initialized_database, ROLE_BINDINGS["attune_worker"])
    try:
        with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
            cursor.execute("SELECT id FROM attune.memories ORDER BY id")
            assert cursor.fetchall() == [(MEMORY_A,)]
            cursor.execute(
                """
                SELECT memory_id FROM attune.memory_embeddings
                ORDER BY embedding OPERATOR(attune_ext.<=>) '[1,0,0]'::attune_ext.vector
                """
            )
            assert cursor.fetchall() == [(MEMORY_A,)]
            cursor.execute(
                "SELECT id FROM attune.memories WHERE id = %s", (MEMORY_B,)
            )
            assert cursor.fetchone() is None
    finally:
        _reset_role(initialized_database)


def test_rls_rejects_cross_tenant_write(initialized_database):
    psycopg = pytest.importorskip("psycopg")
    _set_role(initialized_database, ROLE_BINDINGS["attune_worker"])
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_transaction(
                initialized_database, TenantContext(TENANT_A)
            ) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.memories
                        (tenant_id, principal_id, content, provenance,
                         source_class, confidence)
                    VALUES (%s, %s, 'attack', '{}', 'provider', 0.5)
                    """,
                    (TENANT_B, PRINCIPAL_B),
                )
    finally:
        _reset_role(initialized_database)


def test_tenant_setting_does_not_survive_transaction(initialized_database):
    psycopg = pytest.importorskip("psycopg")
    _set_role(initialized_database, ROLE_BINDINGS["attune_worker"])
    try:
        with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
            cursor.execute("SELECT count(*) FROM attune.memories")
            assert cursor.fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with initialized_database.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM attune.memories")
        initialized_database.rollback()
    finally:
        _reset_role(initialized_database)


def test_audit_is_tenant_bound_and_append_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    producer = PostgresAuditProducerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"]),
        producer_kind="worker",
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_audit_writer"]
        )
    )
    intent = producer.request(
        TenantContext(TENANT_A),
        idempotency_key=hashlib.sha256(b"append-only-audit").digest(),
        actor_type="worker",
        actor_ref_hash=hashlib.sha256(b"actor").digest(),
        action="memory.read",
        outcome="allowed",
        target_type="memory",
        target_ref_hash=hashlib.sha256(b"target").digest(),
        metadata={"content_free": True},
    )
    event_id = writer.write(intent.id)
    assert event_id
    assert writer.write(intent.id) == event_id

    _set_role(initialized_database, ROLE_BINDINGS["attune_audit_writer"])
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with initialized_database.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attune.append_audit_event(
                        %s, 'worker', NULL, 'memory.read', 'allowed',
                        NULL, NULL, '{}'::jsonb)
                    """,
                    (TENANT_A,),
                )
        initialized_database.rollback()
    finally:
        _reset_role(initialized_database)

    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT previous_hash, event_hash FROM attune.audit_events "
            "WHERE tenant_id = %s",
            (TENANT_A,),
        )
        previous_hash, event_hash = cursor.fetchone()
        assert previous_hash == bytes(32)
        assert len(event_hash) == 32 and event_hash != previous_hash
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cursor.execute(
                "UPDATE attune.audit_events SET action = 'tampered' WHERE tenant_id = %s",
                (TENANT_A,),
            )
    initialized_database.rollback()


def test_migration_checksum_mismatch_fails(initialized_database):
    migration = load_migrations()[0]
    changed = Migration(migration.name, migration.sql + "\n-- changed", "0" * 64)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        apply_migrations(initialized_database, [changed])


def test_job_repository_is_idempotent_and_tenant_scoped(
    initialized_database, database_url
):
    repository = PostgresJobRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    )
    key = hashlib.sha256(b"job-1").digest()
    first = repository.enqueue(
        TenantContext(TENANT_A),
        kind="gmail.reconcile",
        capability="gmail.read",
        payload={"source_ref": "opaque"},
        idempotency_key=key,
    )
    duplicate = repository.enqueue(
        TenantContext(TENANT_A),
        kind="gmail.reconcile",
        capability="gmail.read",
        payload={"source_ref": "opaque"},
        idempotency_key=key,
    )
    assert duplicate.id == first.id
    assert repository.get(TenantContext(TENANT_B), first.id) is None
    claimed = repository.claim(
        TenantContext(TENANT_A),
        first.id,
        expected_kind="gmail.reconcile",
        expected_capability="gmail.read",
    )
    assert claimed is not None and claimed.state == "leased" and claimed.attempts == 1
    assert repository.claim(
        TenantContext(TENANT_A),
        first.id,
        expected_kind="gmail.reconcile",
        expected_capability="gmail.read",
    ) is None
    retried = repository.schedule_retry(
        TenantContext(TENANT_A),
        first.id,
        expected_attempt=1,
        error_code="provider_timeout",
        available_at=datetime.now(timezone.utc),
    )
    assert retried is not None and retried.state == "queued"
    claimed = repository.claim(
        TenantContext(TENANT_A),
        first.id,
        expected_kind="gmail.reconcile",
        expected_capability="gmail.read",
    )
    assert claimed is not None and claimed.attempts == 2
    assert repository.schedule_retry(
        TenantContext(TENANT_A),
        first.id,
        expected_attempt=1,
        error_code="replay",
        available_at=datetime.now(timezone.utc),
    ) is None
    assert repository.finish(TenantContext(TENANT_A), first.id, outcome="succeeded")


def test_memory_repository_scopes_vector_search_and_soft_delete(
    initialized_database, database_url
):
    repository = PostgresMemoryRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    )
    memory = repository.add(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        creator_id=PRINCIPAL_A,
        content="repository memory",
        provenance={"source": "test"},
        source_class="user_taught",
        confidence=1,
        model="repository-test",
        embedding=[1, 0, 0],
    )
    results = repository.search(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        model="repository-test",
        embedding=[1, 0, 0],
    )
    assert [result.id for result in results] == [memory.id]
    assert repository.search(
        TenantContext(TENANT_B),
        principal_id=PRINCIPAL_B,
        model="repository-test",
        embedding=[1, 0, 0],
    ) == []
    assert repository.soft_delete(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        memory_id=memory.id,
    )
    assert repository.search(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        model="repository-test",
        embedding=[1, 0, 0],
    ) == []


def test_audit_outbox_is_idempotent_and_writer_accepts_only_intent_ids(
    initialized_database, database_url
):
    producer = PostgresAuditProducerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"]),
        producer_kind="worker",
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_audit_writer"]
        )
    )
    key = hashlib.sha256(b"audit-outbox-idempotency").digest()
    intent = producer.request(
        TenantContext(TENANT_A),
        idempotency_key=key,
        actor_type="worker",
        actor_ref_hash=hashlib.sha256(b"actor").digest(),
        action="job.complete",
        outcome="observed",
        target_type="job",
        target_ref_hash=hashlib.sha256(b"job").digest(),
        metadata={"content_free": True},
    )
    duplicate = producer.request(
        TenantContext(TENANT_A),
        idempotency_key=key,
        actor_type="worker",
        actor_ref_hash=hashlib.sha256(b"actor").digest(),
        action="job.complete",
        outcome="observed",
        target_type="job",
        target_ref_hash=hashlib.sha256(b"job").digest(),
        metadata={"content_free": True},
    )
    assert duplicate.id == intent.id
    event_id = writer.write(intent.id)
    assert event_id and writer.write(intent.id) == event_id
    assert writer.write(UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")) is None

    with pytest.raises(RuntimeError, match="idempotency key reused"):
        producer.request(
            TenantContext(TENANT_A),
            idempotency_key=key,
            actor_type="worker",
            action="job.fail",
            outcome="failed",
        )

    connection = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_worker"]
    )()
    try:
        with tenant_transaction(connection, TenantContext(TENANT_B)) as cursor:
            cursor.execute(
                "SELECT id FROM attune.audit_intents WHERE id = %s",
                (intent.id,),
            )
            assert cursor.fetchone() is None
    finally:
        connection.close()


def test_approval_repository_binds_actor_action_version_and_single_use(
    initialized_database, database_url
):
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    jobs = PostgresJobRepository(factory)
    approvals = PostgresApprovalRepository(factory)
    job = jobs.enqueue(
        TenantContext(TENANT_A),
        kind="gmail.draft",
        capability="gmail.draft",
        payload={"proposal": "opaque"},
        idempotency_key=hashlib.sha256(b"approval-job").digest(),
    )
    opaque_hash = hashlib.sha256(b"opaque-approval-reference").digest()
    action_hash = hashlib.sha256(b"canonical-action").digest()
    destination_hash = hashlib.sha256(b"destination").digest()
    approval = approvals.propose(
        TenantContext(TENANT_A),
        job_id=job.id,
        approver_id=PRINCIPAL_A,
        connector_id=CONNECTOR_A,
        opaque_ref_hash=opaque_hash,
        action_hash=action_hash,
        capability="gmail.draft",
        destination_hash=destination_hash,
        source_version="history-123",
        policy_version=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert approvals.decide(
        TenantContext(TENANT_B),
        opaque_ref_hash=opaque_hash,
        approver_id=PRINCIPAL_A,
        decision="approved",
    ) is None
    decided = approvals.decide(
        TenantContext(TENANT_A),
        opaque_ref_hash=opaque_hash,
        approver_id=PRINCIPAL_A,
        decision="approved",
    )
    assert decided is not None and decided.id == approval.id
    assert approvals.consume(
        TenantContext(TENANT_A),
        approval_id=approval.id,
        expected_action_hash=hashlib.sha256(b"changed-action").digest(),
        expected_source_version="history-123",
        expected_policy_version=1,
    ) is None
    consumed = approvals.consume(
        TenantContext(TENANT_A),
        approval_id=approval.id,
        expected_action_hash=action_hash,
        expected_source_version="history-123",
        expected_policy_version=1,
    )
    assert consumed is not None and consumed.status == "consumed"
    assert approvals.consume(
        TenantContext(TENANT_A),
        approval_id=approval.id,
        expected_action_hash=action_hash,
        expected_source_version="history-123",
        expected_policy_version=1,
    ) is None


def test_provider_events_and_checkpoints_are_idempotent_and_tenant_scoped(
    initialized_database, database_url
):
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    events = PostgresProviderEventRepository(factory)
    workflows = PostgresWorkflowRepository(factory)
    key = hashlib.sha256(b"provider-event").digest()
    event = events.record(
        TenantContext(TENANT_A),
        installation_id=INSTALLATION_A,
        provider="google",
        kind="gmail.changed",
        deduplication_key=key,
        signal={"history_id": "opaque-123"},
    )
    duplicate = events.record(
        TenantContext(TENANT_A),
        installation_id=INSTALLATION_A,
        provider="google",
        kind="gmail.changed",
        deduplication_key=key,
        signal={"history_id": "opaque-123"},
    )
    assert duplicate.id == event.id
    assert events.mark_processed(TenantContext(TENANT_B), event.id) is None
    assert events.mark_processed(TenantContext(TENANT_A), event.id) is not None
    assert events.mark_processed(TenantContext(TENANT_A), event.id) is None

    workflow_id = UUID("10000000-0000-4000-8000-000000000061")
    first = workflows.checkpoint(
        TenantContext(TENANT_A),
        workflow_id=workflow_id,
        state={"step": "planned"},
        status="running",
        expected_version=0,
    )
    assert first.version == 1
    with pytest.raises(RuntimeError, match="version conflict"):
        workflows.checkpoint(
            TenantContext(TENANT_A),
            workflow_id=workflow_id,
            state={"step": "stale"},
            status="running",
            expected_version=0,
        )
    assert workflows.latest(TenantContext(TENANT_B), workflow_id) is None


def test_conversation_sequences_are_atomic_and_tenant_scoped(
    initialized_database, database_url
):
    repository = PostgresConversationRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    )
    external_hash = hashlib.sha256(b"conversation-a").digest()
    conversation = repository.get_or_create(
        TenantContext(TENANT_A),
        installation_id=INSTALLATION_A,
        principal_id=PRINCIPAL_A,
        surface="google_chat",
        external_ref_hash=external_hash,
    )
    duplicate = repository.get_or_create(
        TenantContext(TENANT_A),
        installation_id=INSTALLATION_A,
        principal_id=PRINCIPAL_A,
        surface="google_chat",
        external_ref_hash=external_hash,
    )
    assert duplicate.id == conversation.id
    first = repository.append_turn(
        TenantContext(TENANT_A),
        conversation_id=conversation.id,
        actor_type="user",
        content="bounded content",
        provenance={"source": "verified_chat"},
    )
    second = repository.append_turn(
        TenantContext(TENANT_A),
        conversation_id=conversation.id,
        actor_type="assistant",
        content="bounded response",
    )
    assert (first.sequence, second.sequence) == (1, 2)
    assert [turn.sequence for turn in repository.recent(
        TenantContext(TENANT_A), conversation.id
    )] == [1, 2]
    assert repository.recent(TenantContext(TENANT_B), conversation.id) == []


def test_autonomy_and_lifecycle_objects_fail_closed_across_tenants(
    initialized_database, database_url
):
    control_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    autonomy = PostgresAutonomyRepository(control_factory)
    lifecycle = PostgresLifecycleRepository(control_factory)
    grant = autonomy.grant(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        capability="gmail.read",
        domain="private",
        maximum_risk=0,
        policy_version=1,
        granted_by=PRINCIPAL_A,
    )
    assert autonomy.find_active(
        TenantContext(TENANT_B),
        principal_id=PRINCIPAL_A,
        capability="gmail.read",
        domain="private",
    ) is None
    assert autonomy.revoke(TenantContext(TENANT_B), grant.id) is None
    assert autonomy.revoke(TenantContext(TENANT_A), grant.id) is not None

    export = lifecycle.request_export(
        TenantContext(TENANT_A),
        requested_by=PRINCIPAL_A,
        scope={"object": "account"},
    )
    assert lifecycle.transition_export(
        TenantContext(TENANT_B),
        export.id,
        expected_state="requested",
        state="running",
    ) is None
    assert lifecycle.transition_export(
        TenantContext(TENANT_A),
        export.id,
        expected_state="requested",
        state="running",
    ) is not None

    object_hash = hashlib.sha256(b"memory-object").digest()
    marker = lifecycle.request_deletion(
        TenantContext(TENANT_A),
        requested_by=PRINCIPAL_A,
        object_type="memory",
        object_ref_hash=object_hash,
        suppress_restore_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    duplicate = lifecycle.request_deletion(
        TenantContext(TENANT_A),
        requested_by=PRINCIPAL_A,
        object_type="memory",
        object_ref_hash=object_hash,
        suppress_restore_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    assert duplicate.id == marker.id
    assert lifecycle.transition_deletion(
        TenantContext(TENANT_B),
        marker.id,
        expected_state="requested",
        state="running",
    ) is None

    worker_lifecycle = PostgresLifecycleRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    )
    usage_id = worker_lifecycle.record_usage(
        TenantContext(TENANT_A),
        category="model_tokens",
        provider="approved_gateway",
        units=Decimal("12.5"),
        attributes={"model": "approved-model"},
    )
    assert usage_id


def test_dispatch_intent_is_canonical_idempotent_and_broker_only(
    initialized_database, database_url
):
    producer = PostgresDispatchProducerRepository(
        _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_control_plane"]
        ),
        producer_kind="control_plane",
    )
    broker = PostgresDispatchBrokerRepository(
        _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_dispatch_broker"]
        )
    )
    dispatch_audit = PostgresDispatchAuditRepository(
        _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_dispatch_broker"]
        )
    )
    audit_writer = PostgresAuditWriterRepository(
        _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_audit_writer"]
        )
    )
    key = hashlib.sha256(b"dispatch-intent-canonical").digest()
    dispatch = producer.enqueue(
        TenantContext(TENANT_A),
        kind="gmail.reconcile",
        capability="gmail.read",
        payload={"canonical_ref": "opaque"},
        idempotency_key=key,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    duplicate = producer.enqueue(
        TenantContext(TENANT_A),
        kind="gmail.reconcile",
        capability="gmail.read",
        payload={"canonical_ref": "opaque"},
        idempotency_key=key,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert duplicate.job.id == dispatch.job.id
    assert duplicate.intent.id == dispatch.intent.id
    assert dispatch.intent.task_id == f"attune-{dispatch.intent.id.hex}"

    assert broker.lease(
        dispatch.intent.id, producer_kind="worker"
    ) is None
    leased = broker.lease(
        dispatch.intent.id,
        producer_kind="control_plane",
        lease_seconds=30,
    )
    assert leased is not None
    assert leased.tenant == TenantContext(TENANT_A)
    assert leased.job_id == dispatch.job.id
    assert leased.purpose == "gmail.reconcile"
    assert leased.capability == "gmail.read"
    assert leased.task_id == dispatch.intent.task_id
    assert broker.lease(
        dispatch.intent.id, producer_kind="control_plane"
    ) is None
    pre_audit_intent_id = dispatch_audit.request(
        dispatch.intent.id, outcome="allowed"
    )
    assert pre_audit_intent_id
    assert audit_writer.write(pre_audit_intent_id)
    assert broker.finalize(
        dispatch.intent.id,
        producer_kind="control_plane",
        outcome="dispatched",
    )
    assert broker.finalize(
        dispatch.intent.id,
        producer_kind="control_plane",
        outcome="dispatched",
    )
    replay = broker.lease(
        dispatch.intent.id, producer_kind="control_plane"
    )
    assert replay is not None and replay.state == "dispatched"
    assert replay.task_id == dispatch.intent.task_id
    audit_intent_id = dispatch_audit.request(
        dispatch.intent.id, outcome="observed"
    )
    assert audit_intent_id
    assert dispatch_audit.request(
        dispatch.intent.id, outcome="observed"
    ) == audit_intent_id
    assert audit_writer.write(audit_intent_id)


def test_dispatch_intent_rejects_producer_substitution_and_cross_tenant_reads(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    jobs = PostgresJobRepository(factory)
    job = jobs.enqueue(
        TenantContext(TENANT_A),
        kind="calendar.reconcile",
        capability="calendar.read",
        payload={},
        idempotency_key=hashlib.sha256(b"dispatch-substitution").digest(),
    )
    connection = factory()
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_transaction(connection, TenantContext(TENANT_A)) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.dispatch_intents
                        (tenant_id, job_id, producer_kind, purpose, capability,
                         expires_at)
                    VALUES (%s, %s, 'control_plane', %s, %s, %s)
                    """,
                    (
                        TENANT_A,
                        job.id,
                        job.kind,
                        job.capability,
                        datetime.now(timezone.utc) + timedelta(minutes=5),
                    ),
                )
    finally:
        connection.close()

    producer = PostgresDispatchProducerRepository(
        factory, producer_kind="worker"
    )
    dispatch = producer.enqueue(
        TenantContext(TENANT_A),
        kind="calendar.reconcile",
        capability="calendar.read",
        payload={"version": 2},
        idempotency_key=hashlib.sha256(b"dispatch-tenant-read").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    connection = factory()
    try:
        with tenant_transaction(connection, TenantContext(TENANT_B)) as cursor:
            cursor.execute(
                "SELECT id FROM attune.dispatch_intents WHERE id = %s",
                (dispatch.intent.id,),
            )
            assert cursor.fetchone() is None
    finally:
        connection.close()


def test_dispatch_lease_recovers_and_expired_intent_never_leases(
    initialized_database, database_url
):
    producer = PostgresDispatchProducerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"]),
        producer_kind="worker",
    )
    broker = PostgresDispatchBrokerRepository(
        _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_dispatch_broker"]
        )
    )
    recoverable = producer.enqueue(
        TenantContext(TENANT_A),
        kind="memory.reconcile",
        capability="memory.read",
        payload={},
        idempotency_key=hashlib.sha256(b"dispatch-lease-recovery").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    first = broker.lease(
        recoverable.intent.id, producer_kind="worker", lease_seconds=30
    )
    assert first is not None and first.attempts == 1
    with initialized_database.cursor() as cursor:
        cursor.execute(
            """
            UPDATE attune.dispatch_intents
               SET lease_expires_at = clock_timestamp() - interval '1 second'
             WHERE id = %s
            """,
            (recoverable.intent.id,),
        )
    initialized_database.commit()
    recovered = broker.lease(
        recoverable.intent.id, producer_kind="worker", lease_seconds=30
    )
    assert recovered is not None and recovered.attempts == 2

    expiring = producer.enqueue(
        TenantContext(TENANT_A),
        kind="memory.reconcile",
        capability="memory.read",
        payload={"expiry": True},
        idempotency_key=hashlib.sha256(b"dispatch-expiry").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=30),
    )
    time.sleep(0.05)
    assert broker.lease(
        expiring.intent.id, producer_kind="worker"
    ) is None


def test_credential_intents_are_tenant_bound_and_broker_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    key = hashlib.sha256(b"credential-install-intent").digest()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                """
                INSERT INTO attune.credential_intents
                    (tenant_id, connector_id, producer_kind, operation,
                     capability, idempotency_key, expires_at)
                VALUES (%s, %s, 'control_plane', 'install',
                        'connector.manage', %s, %s)
                RETURNING id
                """,
                (
                    TENANT_A,
                    CONNECTOR_A,
                    key,
                    datetime.now(timezone.utc) + timedelta(minutes=5),
                ),
            )
            intent_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO attune.credential_intents
                    (tenant_id, connector_id, producer_kind, operation,
                     capability, idempotency_key, expires_at)
                VALUES (%s, %s, 'control_plane', 'install',
                        'connector.manage', %s, %s)
                RETURNING id
                """,
                (
                    TENANT_A,
                    CONNECTOR_A,
                    hashlib.sha256(b"credential-concurrent-install").digest(),
                    datetime.now(timezone.utc) + timedelta(minutes=5),
                ),
            )
            concurrent_intent_id = cursor.fetchone()[0]
        with tenant_transaction(control, TenantContext(TENANT_B)) as cursor:
            cursor.execute(
                "SELECT id FROM attune.credential_intents WHERE id = %s",
                (intent_id,),
            )
            assert cursor.fetchone() is None
    finally:
        control.close()

    worker = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_worker"]
    )()
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_transaction(worker, TenantContext(TENANT_A)) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.credential_intents
                        (tenant_id, connector_id, producer_kind, operation,
                         capability, idempotency_key, expires_at)
                    VALUES (%s, %s, 'worker', 'revoke',
                            'connector.manage', %s, %s)
                    """,
                    (
                        TENANT_A,
                        CONNECTOR_A,
                        hashlib.sha256(b"credential-substitution").digest(),
                        datetime.now(timezone.utc) + timedelta(minutes=5),
                    ),
                )
    finally:
        worker.close()

    broker = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_secret_broker"]
    )()
    try:
        with broker.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.lease_credential_intent(%s, %s, %s)",
                (intent_id, "control_plane", 30),
            )
            leased = cursor.fetchone()
            assert leased[0:6] == (
                intent_id,
                TENANT_A,
                CONNECTOR_A,
                "google",
                "install",
                "connector.manage",
            )
            assert all(value is None for value in leased[6:])
            cursor.execute(
                "SELECT * FROM attune.lease_credential_intent(%s, %s, 30)",
                (concurrent_intent_id, "control_plane"),
            )
            assert cursor.fetchone() is None
            cursor.execute(
                """
                SELECT * FROM attune.store_connector_credential(
                    %s, %s, %s, %s, %s, 1)
                """,
                (
                    intent_id,
                    b"ciphertext-with-tag",
                    bytes(12),
                    b"wrapped-dek",
                    "projects/test/locations/test/keyRings/test/cryptoKeys/connectors",
                ),
            )
            credential_id, credential_version = cursor.fetchone()
            assert credential_id and credential_version == 1
        broker.commit()
        with broker.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT id FROM attune.connector_credentials")
        broker.rollback()
    finally:
        broker.close()

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                """
                INSERT INTO attune.credential_intents
                    (tenant_id, connector_id, producer_kind, operation,
                     capability, idempotency_key, expires_at)
                VALUES (%s, %s, 'control_plane', 'revoke',
                        'connector.manage', %s, %s)
                RETURNING id
                """,
                (
                    TENANT_A,
                    CONNECTOR_A,
                    hashlib.sha256(b"credential-revoke-intent").digest(),
                    datetime.now(timezone.utc) + timedelta(minutes=5),
                ),
            )
            revoke_id = cursor.fetchone()[0]
    finally:
        control.close()
    broker = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_secret_broker"]
    )()
    try:
        with broker.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.lease_credential_intent(%s, %s, 30)",
                (revoke_id, "control_plane"),
            )
            assert cursor.fetchone()[6] == credential_id
            cursor.execute(
                "SELECT attune.revoke_connector_credential(%s)", (revoke_id,)
            )
            assert cursor.fetchone() == (True,)
        broker.commit()
    finally:
        broker.close()
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM attune.connector_credentials WHERE id = %s",
            (credential_id,),
        )
        assert cursor.fetchone() == ("revoked",)
        cursor.execute(
            "SELECT status, credential_ref FROM attune.connectors WHERE id = %s",
            (CONNECTOR_A,),
        )
        assert cursor.fetchone() == ("revoked", credential_id)
    initialized_database.rollback()
