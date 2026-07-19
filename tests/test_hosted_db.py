"""Hosted migration contract plus opt-in PostgreSQL isolation tests."""

from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from attune.hosted.data_lifecycle import (
    DataClass,
    DeletionRule,
    RELATIONAL_ASSET_BY_TABLE,
    validate_relational_lifecycle_inventory,
)
from attune.hosted.export_archive import build_export_archive
from attune.hosted.migrate import (
    RUNTIME_ROLES,
    TENANT_TABLES,
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
from attune.hosted.reconciliation import PostgresJobReconciliationRepository
from attune.hosted.oauth import (
    PostgresGoogleConnectorRevocationRepository,
    PostgresGoogleOAuthStartRepository,
    PostgresOAuthExchangeRepository,
    PostgresOAuthTransactionRepository,
)
from attune.hosted.onboarding import PostgresHostedOnboardingRepository
from attune.hosted.hosted_policy import PostgresHostedPolicyRepository
from attune.hosted.hosted_channels import PostgresHostedChannelRepository
from attune.hosted.channel_setup import PostgresHostedChannelSetupRepository
from attune.hosted.channel_broker import PostgresChannelBrokerRepository
from attune.hosted.brief_delivery import (
    BriefDestination,
    BriefWork,
    PostgresHostedBriefRepository,
)
from attune.hosted.google_chat_conversation_executor import (
    PostgresGoogleChatConversationWorkRepository,
)
from attune.hosted.capability_gateway import (
    AuthorizedCapability,
    CapabilityDefinition,
    CapabilityDenied,
    CapabilityRegistry,
    EmptyArguments,
    PostgresCapabilityAuthorityRepository,
    RiskTier,
    TypedCapabilityGateway,
)
from attune.hosted.capability_admission import PostgresCapabilityAdmissionRepository
from attune.hosted.identity import VerifiedIdentity
from attune.hosted.identity_session import (
    IdentitySessionSecrets,
    PostgresIdentitySessionRepository,
)
from attune.hosted.intelligence import (
    IntelligenceReferenceHasher,
    PostgresAttentionStore,
    PostgresImportanceProfile,
)
from attune.orchestrator.attention import RETENTION_DAYS, AttentionItem
from attune.orchestrator.importance import (
    DECAY_DAYS,
    HIGH_MIN_SIGNALS,
    LOW_RUN_THRESHOLD,
    ImportanceTier,
    TierAssessment,
)
from attune.orchestrator.triage import Priority
from attune.memory.signals import ActionSignal
from attune.hosted.tenant import TenantContext, tenant_transaction
from attune.hosted.tenant_deletion import PostgresTenantDeletionRequests
from attune.hosted.tenant_deletion_executor import erasable_relations_in_order
from attune.hosted.vault import (
    PostgresCredentialIntentRepository,
    PostgresSecretBrokerRepository,
)
from attune.hosted.vault_crypto import EncryptedCredential

TENANT_A = UUID("10000000-0000-4000-8000-000000000001")
TENANT_B = UUID("20000000-0000-4000-8000-000000000002")
PRINCIPAL_A = UUID("10000000-0000-4000-8000-000000000011")
PRINCIPAL_B = UUID("20000000-0000-4000-8000-000000000012")
MEMORY_A = UUID("10000000-0000-4000-8000-000000000021")
MEMORY_B = UUID("20000000-0000-4000-8000-000000000022")
INSTALLATION_A = UUID("10000000-0000-4000-8000-000000000031")
CONNECTOR_A = UUID("10000000-0000-4000-8000-000000000041")
OAUTH_CONNECTOR = UUID("10000000-0000-4000-8000-000000000042")
OAUTH_INTENT = UUID("10000000-0000-4000-8000-000000000043")
RATE_CONNECTOR = UUID("10000000-0000-4000-8000-000000000061")
RATE_CREDENTIAL = UUID("10000000-0000-4000-8000-000000000062")
IDENTITY_PRINCIPAL_A = UUID("10000000-0000-4000-8000-000000000071")
IDENTITY_PRINCIPAL_B = UUID("20000000-0000-4000-8000-000000000072")
POLICY_TENANT = UUID("30000000-0000-4000-8000-000000000081")
POLICY_PRINCIPAL = UUID("30000000-0000-4000-8000-000000000082")
POLICY_SESSION = UUID("30000000-0000-4000-8000-000000000083")
CHANNEL_TENANT = UUID("30000000-0000-4000-8000-000000000084")
CHANNEL_PRINCIPAL = UUID("30000000-0000-4000-8000-000000000085")
CHANNEL_SESSION = UUID("30000000-0000-4000-8000-000000000086")

ROLE_BINDINGS = {
    "attune_control_plane": "attune_test_control",
    "attune_channel_broker": "attune_test_channel_broker",
    "attune_dispatch_broker": "attune_test_dispatch",
    "attune_worker": "attune_test_worker",
    "attune_secret_broker": "attune_test_broker",
    "attune_audit_writer": "attune_test_audit",
    "attune_oauth_exchange": "attune_test_oauth_exchange",
    "attune_identity_provisioner": "attune_test_identity_provisioner",
    "attune_retention": "attune_test_retention",
    "attune_export": "attune_test_export",
    "attune_export_cleanup": "attune_test_export_cleanup",
    "attune_export_download": "attune_test_export_download",
    "attune_content_retention": "attune_test_content_retention",
    "attune_deletion": "attune_test_deletion",
}


def test_packaged_migrations_are_ordered_and_checksum_pinned():
    migrations = load_migrations()
    assert [migration.name for migration in migrations] == sorted(
        migration.name for migration in migrations
    )
    sql_by_name = {migration.name: migration.sql for migration in migrations}
    assert migrations[0].name == "0001_tenant_boundary.sql"
    assert migrations[-1].name == "0046_tenant_content_lifecycle.sql"
    download = sql_by_name["0037_customer_export_download.sql"]
    assert "GRANT attune_export_cleanup_coordinator TO %I" in download
    assert "REVOKE attune_export_cleanup_coordinator FROM %I" in download
    assert all(
        migration.checksum == hashlib.sha256(migration.sql.encode()).hexdigest()
        for migration in migrations
    )
    channel_broker = sql_by_name["0023_google_chat_delivery_test.sql"]
    assert "GRANT attune_channel_link_executor TO %I" in channel_broker
    assert "GRANT CREATE ON SCHEMA attune TO attune_channel_link_executor" in channel_broker
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor" in channel_broker
    assert "REVOKE attune_channel_link_executor FROM %I" in channel_broker
    conversation = sql_by_name["0024_google_chat_conversation_acceptance.sql"]
    assert "LIMIT 2" in conversation
    assert "GRANT attune_channel_message_executor TO %I" in conversation
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_message_executor" in conversation
    assert "TO attune_channel_broker" in conversation
    delivery = sql_by_name["0025_google_chat_conversation_delivery.sql"]
    assert "hosted_channel_deliveries" in delivery
    assert "already_delivered boolean" in delivery
    assert "TO attune_channel_broker" in delivery
    lifecycle = sql_by_name["0026_google_chat_destination_lifecycle.sql"]
    assert "attune_channel_lifecycle_executor" in lifecycle
    assert "disconnect_hosted_channel_destination" in lifecycle
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_lifecycle_executor" in lifecycle
    replace_link = lifecycle.index(
        "CREATE OR REPLACE FUNCTION attune.consume_google_chat_link_v2"
    )
    assert lifecycle.index("SET LOCAL ROLE attune_channel_link_executor") < replace_link
    assert lifecycle.index("RESET ROLE", replace_link) > replace_link
    relink_context = sql_by_name["0027_google_chat_relink_route_context.sql"]
    assert "destination.status = 'revoked'" in relink_context
    assert "SET LOCAL ROLE attune_channel_link_executor" in relink_context
    retention = sql_by_name["0028_protocol_retention.sql"]
    assert "CREATE ROLE attune_retention_executor" in retention
    assert "prune_expired_protocol_records" in retention
    assert "pg_try_advisory_xact_lock" in retention
    assert "FOR UPDATE" not in retention
    assert "interval '24 hours'" in retention
    assert "interval '7 days'" in retention
    export = sql_by_name["0029_customer_export_authority.sql"]
    assert "legacy export jobs require explicit reviewed adoption" in export
    assert "request_customer_export" in export
    assert "claim_customer_export" in export
    assert "recent owner session is required" in export
    assert "REVOKE INSERT, UPDATE ON attune.export_jobs" in export
    projection = sql_by_name["0030_customer_export_projections.sql"]
    assert "read_customer_export_records" in projection
    assert "credential_ref" not in projection
    assert "external_ref_hash" not in projection
    assert "policy.document" not in projection
    assert "t.provenance" not in projection
    assert "memory.provenance" not in projection
    assert "a.metadata" not in projection
    assert "u.attributes" not in projection
    assert "lease_run_id = p_run_id" in projection
    assert "owner_state.owner_principal_id = job.requested_by" in projection
    completion = sql_by_name["0031_customer_export_completion.sql"]
    assert "complete_customer_export" in completion
    assert "ciphertext_bytes = archive_bytes + 16" in completion
    assert "interval '24 hours'" in completion
    assert "job.lease_run_id = p_run_id" in completion
    recovery = sql_by_name["0032_customer_export_recovery.sql"]
    assert "reserve_customer_export_object" in recovery
    assert "export_object_attempts" in recovery
    assert "list_customer_export_cleanup_objects" in recovery
    assert "fail_customer_export" in recovery
    assert "job.lease_expires_at <= clock_timestamp()" in recovery
    assert "failure_code" in recovery
    cleanup = sql_by_name["0033_customer_export_cleanup_authority.sql"]
    assert "attune_export_cleanup" in cleanup
    assert "15 minutes" in cleanup
    assert "SKIP LOCKED" in cleanup
    assert "job.state = 'ready'" in cleanup
    expiry = sql_by_name["0034_customer_export_expiry_cleanup.sql"]
    assert "claim_customer_export_expirations" in expiry
    assert "object_generation = p_object_generation" in expiry
    assert "state = 'expired'" in expiry
    assert "wrapped_dek = NULL" in expiry
    task = sql_by_name["0035_customer_export_task_authority.sql"]
    assert "claim_customer_export_for_tenant" in task
    assert "claim_customer_export_task" in task
    assert "finish_customer_export_task" in task
    assert "interval '6 minutes'" in task
    control_plane = sql_by_name["0036_customer_export_control_plane.sql"]
    assert "request_or_read_customer_export" in control_plane
    assert "list_customer_exports" in control_plane
    download = sql_by_name["0037_customer_export_download.sql"]
    assert "issue_customer_export_download" in download
    assert "claim_customer_export_download" in download
    assert "finish_customer_export_download" in download
    assert "state = 'consumed'" in download
    slack = sql_by_name["0038_slack_channel_installation.sql"]
    assert "hosted_channel_credentials" in slack
    assert "purpose = 'slack_bot_token'" in slack
    assert "claim_slack_install" in slack
    assert "consume_slack_install" in slack
    assert "accept_slack_owner_message" in slack
    assert "claim_slack_conversation_delivery" in slack
    assert "disconnect_hosted_channel_destination_v2" in slack
    assert "LIMIT 2" in slack
    assert "TO attune_channel_broker" in slack
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor" in slack
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_message_executor" in slack
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_lifecycle_executor" in slack
    reinstall = sql_by_name["0039_slack_reinstall_installation_reuse.sql"]
    assert "CREATE OR REPLACE FUNCTION attune.consume_slack_install" in reinstall
    assert "v_installation_id := v_existing.installation_id" in reinstall
    assert "SET LOCAL ROLE attune_channel_link_executor" in reinstall
    assert "RESET ROLE" in reinstall
    assert "TO attune_channel_broker" in reinstall
    acknowledgment = sql_by_name["0040_slack_message_acknowledgment.sql"]
    assert "claim_slack_acknowledgment" in acknowledgment
    assert "complete_slack_acknowledgment" in acknowledgment
    assert "fixed_acknowledgment_v1" in acknowledgment
    assert "LIMIT 2" in acknowledgment
    assert "GRANT attune_channel_link_executor TO %I" in acknowledgment
    assert "TO attune_channel_broker" in acknowledgment
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor" in acknowledgment
    web_conversation = sql_by_name["0041_web_conversation.sql"]
    assert "accept_web_owner_message" in web_conversation
    assert "attune_web_message_executor" in web_conversation
    assert "'channel_message'" in web_conversation
    assert "attune_control_plane', 'MEMBER'" in web_conversation
    assert "GRANT attune_web_message_executor TO %I" in web_conversation
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_web_message_executor" in web_conversation
    assert "TO attune_control_plane" in web_conversation
    assert "provider IN ('google', 'slack', 'web')" in web_conversation
    intelligence = sql_by_name["0042_intelligence_persistence.sql"]
    assert "CREATE TABLE attune.importance_signals" in intelligence
    assert "CREATE TABLE attune.attention_items" in intelligence
    assert "importance_signals_one_pin_per_sender" in intelligence
    assert "FORCE ROW LEVEL SECURITY" in intelligence
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON attune.importance_signals, attune.attention_items" in intelligence
    assert "TO attune_worker" in intelligence
    assert "attune_control_plane" not in intelligence
    capability_admission = sql_by_name["0043_capability_admission.sql"]
    assert "CREATE TABLE attune.capability_admissions" in capability_admission
    assert "capability_admissions_no_update_delete" in capability_admission
    assert "ALTER TABLE attune.approvals ALTER COLUMN job_id DROP NOT NULL" in capability_admission
    assert "ADD COLUMN admission_id uuid" in capability_admission
    assert "attune_capability_executor" in capability_admission
    assert "claim_capability_approval" in capability_admission
    assert (
        "REVOKE UPDATE ON attune.approvals FROM attune_worker, attune_control_plane"
        in capability_admission
    )
    brief_delivery = sql_by_name["0044_hosted_brief_delivery.sql"]
    assert "CREATE TABLE attune.hosted_brief_deliveries" in brief_delivery
    assert "PRIMARY KEY (tenant_id, job_id, destination_id)" in brief_delivery
    assert "claim_google_chat_brief_delivery" in brief_delivery
    assert "claim_slack_brief_delivery" in brief_delivery
    assert "'google_chat' = ANY(preference.brief_channels)" in brief_delivery
    assert "'slack' = ANY(preference.brief_channels)" in brief_delivery
    assert "GRANT SELECT, INSERT ON attune.hosted_brief_deliveries TO attune_worker" in brief_delivery
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor" in brief_delivery
    signup = sql_by_name["0045_hosted_signup.sql"]
    assert "CREATE FUNCTION attune.provision_hosted_signup_tenant" in signup
    assert "'tn-' || replace(v_tenant_id::text, '-', '')" in signup
    assert "pg_advisory_xact_lock(214748301)" in signup
    assert "GRANT EXECUTE ON FUNCTION" in signup
    assert "TO attune_control_plane" in signup
    assert "GRANT attune_identity_provisioning_executor TO %I" in signup
    assert "REVOKE CREATE ON SCHEMA attune\nFROM attune_identity_provisioning_executor" in signup
    assert "REVOKE attune_identity_provisioning_executor FROM %I" in signup
    # No new relation, role, or table grant is introduced by this migration:
    # the function reuses 0016's existing owner-role table privileges.
    assert "CREATE TABLE" not in signup
    assert "CREATE ROLE" not in signup
    assert "GRANT SELECT" not in signup
    assert "GRANT INSERT" not in signup


def test_lifecycle_enums_preserve_string_behavior_on_python_310():
    assert str(DataClass.CUSTOMER_CONTENT) == "customer_content"
    assert DataClass.CUSTOMER_CONTENT == "customer_content"
    assert str(DeletionRule.CRYPTO_ERASE) == "crypto_erase"


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


def test_lifecycle_inventory_is_complete_and_fail_closed():
    validate_relational_lifecycle_inventory(TENANT_TABLES)
    assert set(RELATIONAL_ASSET_BY_TABLE) == set(TENANT_TABLES)
    assert RELATIONAL_ASSET_BY_TABLE["conversation_turns"].data_class == (
        DataClass.CUSTOMER_CONTENT
    )
    assert RELATIONAL_ASSET_BY_TABLE["connector_credentials"].deletion_rule == (
        DeletionRule.CRYPTO_ERASE
    )
    assert not RELATIONAL_ASSET_BY_TABLE["connector_credentials"].customer_export
    assert RELATIONAL_ASSET_BY_TABLE["audit_events"].deletion_rule == (
        DeletionRule.DEIDENTIFY
    )
    assert RELATIONAL_ASSET_BY_TABLE["deletion_markers"].deletion_rule == (
        DeletionRule.RETAIN_TOMBSTONE
    )
    assert RELATIONAL_ASSET_BY_TABLE["importance_signals"].data_class == (
        DataClass.CUSTOMER_CONTENT
    )
    assert RELATIONAL_ASSET_BY_TABLE["importance_signals"].deletion_rule == (
        DeletionRule.ERASE
    )
    assert RELATIONAL_ASSET_BY_TABLE["importance_signals"].customer_export
    assert RELATIONAL_ASSET_BY_TABLE["attention_items"].data_class == (
        DataClass.CUSTOMER_CONTENT
    )
    assert RELATIONAL_ASSET_BY_TABLE["attention_items"].customer_export
    with pytest.raises(RuntimeError, match="does not match tenant tables"):
        validate_relational_lifecycle_inventory((*TENANT_TABLES, "unreviewed_table"))


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
    audit = PostgresAuditProducerRepository(forbidden_connection, producer_kind="worker")
    oauth = PostgresOAuthTransactionRepository(forbidden_connection)
    oauth_exchange = PostgresOAuthExchangeRepository(forbidden_connection)

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
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        oauth.create(
            context,
            principal_id=PRINCIPAL_A,
            connector_id=OAUTH_CONNECTOR,
            credential_intent_id=OAUTH_INTENT,
            state_hash=b"short",
            binding_hash=hashlib.sha256(b"binding").digest(),
            nonce_hash=hashlib.sha256(b"nonce").digest(),
            pkce_verifier="a" * 64,
            redirect_uri="https://dev.attune.mumit.org/oauth/google/callback",
            scopes=("openid",),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    with pytest.raises(ValueError, match="between 1 and 60"):
        oauth_exchange.lease(
            state_hash=hashlib.sha256(b"state").digest(),
            binding_hash=hashlib.sha256(b"binding").digest(),
            lease_seconds=61,
        )


_TEST_HMAC_KEY = hashlib.sha256(b"attune-test-intelligence-hmac-key").digest()


def test_intelligence_reference_hasher_is_keyed_deterministic_and_domain_separated():
    with pytest.raises(ValueError, match="32 bytes"):
        IntelligenceReferenceHasher(b"short")

    hasher = IntelligenceReferenceHasher(_TEST_HMAC_KEY)
    first = hasher.hash("sender", "vip@example.com")
    again = hasher.hash("sender", "vip@example.com")
    assert first == again
    assert len(first) == 32

    # Domain separation: the same literal value hashed under a different
    # ``kind`` never collides.
    assert first != hasher.hash("channel", "vip@example.com")

    # A different key must never reproduce the same digest (the whole point
    # of a keyed hash over a plain one -- an attacker without the key cannot
    # dictionary-attack a guessable sender/channel reference).
    other_key = hashlib.sha256(b"a different key").digest()
    assert hasher.hash("sender", "vip@example.com") != (
        IntelligenceReferenceHasher(other_key).hash("sender", "vip@example.com")
    )

    with pytest.raises(ValueError, match="invalid intelligence reference"):
        hasher.hash("sender", "")
    with pytest.raises(ValueError, match="invalid intelligence reference"):
        hasher.hash("sender", "x" * 321)


def test_intelligence_repositories_reject_invalid_data_before_connecting():
    def forbidden_connection():
        raise AssertionError("invalid input must not reach the database")

    context = TenantContext(TENANT_A)
    hasher = IntelligenceReferenceHasher(_TEST_HMAC_KEY)
    importance = PostgresImportanceProfile(
        forbidden_connection, context, PRINCIPAL_A, reference_hasher=hasher
    )
    attention = PostgresAttentionStore(
        forbidden_connection, context, PRINCIPAL_A, reference_hasher=hasher
    )

    with pytest.raises(ValueError, match="signal must be an ActionSignal"):
        importance.record_signal("vip@example.com", "approved")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid intelligence reference"):
        importance.record_signal("", ActionSignal.APPROVED)
    with pytest.raises(ValueError, match="tier must be an ImportanceTier"):
        importance.pin("vip@example.com", "high")  # type: ignore[arg-type]

    def _item(**overrides):
        fields = dict(
            source="slack",
            channel_ref="C1",
            channel_name="general",
            sender_ref="U1",
            sender_display="Alice",
            summary="hello",
            ts=datetime.now(timezone.utc),
            priority=Priority.ROUTINE,
            mentions_principal=False,
            thread_ref=None,
        )
        fields.update(overrides)
        return AttentionItem(**fields)

    with pytest.raises(ValueError, match="source must be slack or google_chat"):
        attention.add(_item(source="teams"))
    with pytest.raises(ValueError, match="priority must be a Priority"):
        attention.add(_item(priority="urgent"))
    with pytest.raises(ValueError, match="between 1 and 200"):
        attention.add(_item(channel_name=""))
    with pytest.raises(ValueError, match="between 1 and 2000"):
        attention.add(_item(summary="x" * 2001))
    with pytest.raises(ValueError, match="non-negative integer"):
        attention.recent(limit=-1)


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

    assert apply_migrations(admin) == 46
    with admin.cursor() as cursor:
        cursor.execute("GRANT attune_worker TO attune_test_stale_member")
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
            INSERT INTO attune.connectors
                (tenant_id, id, principal_id, installation_id, provider,
                 credential_ref, status)
            VALUES (%s, %s, %s, %s, 'google', %s, 'pending')
            """,
            (
                TENANT_A,
                OAUTH_CONNECTOR,
                PRINCIPAL_A,
                INSTALLATION_A,
                UUID("10000000-0000-4000-8000-000000000052"),
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
    with admin.cursor() as cursor:
        cursor.execute(f'SET ROLE "{ROLE_BINDINGS["attune_control_plane"]}"')
    admin.commit()
    with tenant_transaction(admin, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            """
            INSERT INTO attune.credential_intents
                (tenant_id, id, connector_id, producer_kind, operation,
                 capability, idempotency_key, expires_at)
            VALUES (%s, %s, %s, 'control_plane', 'install',
                    'google.oauth.install', %s,
                    clock_timestamp() + interval '1 day')
            """,
            (
                TENANT_A,
                OAUTH_INTENT,
                OAUTH_CONNECTOR,
                hashlib.sha256(b"oauth-install-intent").digest(),
            ),
        )
    with admin.cursor() as cursor:
        cursor.execute("RESET ROLE")
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


def test_capability_gateway_authority_is_one_tenant_snapshot(
    initialized_database, database_url
):
    scopes = (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    )
    capability = CapabilityDefinition(
        name="google.workspace.connection.verify",
        version=1,
        risk=RiskTier.R0,
        maximum_product_risk=RiskTier.R0,
        domain="private_workspace",
        provider="google",
        required_scopes=scopes,
        arguments=EmptyArguments(),
    )
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            """
            INSERT INTO attune.policies
                (tenant_id, version, document, active, created_by)
            VALUES (%s, 900, '{}'::jsonb, true, %s)
            """,
            (TENANT_A, PRINCIPAL_A),
        )
        cursor.execute(
            """
            INSERT INTO attune.autonomy_grants
                (tenant_id, principal_id, capability, domain, maximum_risk,
                 policy_version, granted_by)
            VALUES (%s, %s, %s, %s, 0, 900, %s)
            """,
            (
                TENANT_A,
                PRINCIPAL_A,
                capability.name,
                capability.domain,
                PRINCIPAL_A,
            ),
        )
        cursor.execute(
            """
            UPDATE attune.connectors SET granted_scopes = %s
             WHERE tenant_id = %s AND id = %s
            """,
            (list(scopes), TENANT_A, CONNECTOR_A),
        )

    authority = PostgresCapabilityAuthorityRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    gateway = TypedCapabilityGateway(
        registry=CapabilityRegistry((capability,)), authority=authority
    )
    proposal = {
        "version": 1,
        "capability": capability.name,
        "arguments": {},
    }
    admitted = gateway.authorize(
        TenantContext(TENANT_A), principal_id=PRINCIPAL_A, proposal=proposal
    )
    assert admitted.connector_id == CONNECTOR_A
    assert admitted.policy_version == 900

    with pytest.raises(CapabilityDenied, match="authority_unavailable"):
        gateway.authorize(
            TenantContext(TENANT_B), principal_id=PRINCIPAL_B, proposal=proposal
        )

    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "UPDATE attune.policies SET version = 901 "
            "WHERE tenant_id = %s AND version = 900",
            (TENANT_A,),
        )
    with pytest.raises(CapabilityDenied, match="authority_unavailable"):
        gateway.authorize(
            TenantContext(TENANT_A), principal_id=PRINCIPAL_A, proposal=proposal
        )
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "UPDATE attune.policies SET version = 900 "
            "WHERE tenant_id = %s AND version = 901",
            (TENANT_A,),
        )
        cursor.execute(
            """
            INSERT INTO attune.connectors
                (tenant_id, id, principal_id, provider, credential_ref,
                 granted_scopes, status)
            VALUES (%s, %s, %s, 'google', %s, %s, 'active')
            """,
            (
                TENANT_A,
                UUID("10000000-0000-4000-8000-000000000091"),
                PRINCIPAL_A,
                UUID("10000000-0000-4000-8000-000000000092"),
                list(scopes),
            ),
        )
    with pytest.raises(CapabilityDenied, match="authority_unavailable"):
        gateway.authorize(
            TenantContext(TENANT_A), principal_id=PRINCIPAL_A, proposal=proposal
        )
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "UPDATE attune.connectors SET status = 'revoked' "
            "WHERE tenant_id = %s AND id = %s",
            (TENANT_A, UUID("10000000-0000-4000-8000-000000000091")),
        )
        cursor.execute(
            "UPDATE attune.connectors SET granted_scopes = ARRAY[]::text[] "
            "WHERE tenant_id = %s AND id = %s",
            (TENANT_A, CONNECTOR_A),
        )


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


def test_read_only_policy_activation_is_exact_recent_and_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attune.tenants (id, slug, region) VALUES (%s,%s,%s)",
            (POLICY_TENANT, "policy-tenant", "test"),
        )
        cursor.execute(
            """
            INSERT INTO attune.principals
                (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, 'test')
            """,
            (POLICY_TENANT, POLICY_PRINCIPAL, hashlib.sha256(b"policy").digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.identity_sessions
                (tenant_id, id, principal_id, token_hash, csrf_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s,
                    clock_timestamp() + interval '8 hours')
            """,
            (
                POLICY_TENANT,
                POLICY_SESSION,
                POLICY_PRINCIPAL,
                hashlib.sha256(b"policy-token").digest(),
                hashlib.sha256(b"policy-csrf").digest(),
            ),
        )
    initialized_database.commit()
    control_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    onboarding = PostgresHostedOnboardingRepository(control_factory)
    policies = PostgresHostedPolicyRepository(control_factory)
    context = TenantContext(POLICY_TENANT)
    assert onboarding.start(context, principal_id=POLICY_PRINCIPAL).policy == (
        "not_started"
    )

    first = policies.activate_read_only(
        context, principal_id=POLICY_PRINCIPAL, session_id=POLICY_SESSION
    )
    repeated = policies.activate_read_only(
        context, principal_id=POLICY_PRINCIPAL, session_id=POLICY_SESSION
    )
    assert first == repeated
    assert (first.policy_version, first.onboarding_revision, first.status) == (
        1,
        2,
        "validated",
    )
    assert onboarding.read(context, principal_id=POLICY_PRINCIPAL).policy == "validated"
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "SELECT document FROM attune.policies WHERE tenant_id = %s AND active",
            (POLICY_TENANT,),
        )
        assert cursor.fetchone()[0] == {
            "schema_version": 1,
            "profile": "private_alpha_read_only",
            "maximum_risk": 0,
            "capabilities": ["google.workspace.connection.verify"],
        }
        cursor.execute(
            """
            SELECT capability, domain, maximum_risk, policy_version, granted_by
              FROM attune.autonomy_grants
             WHERE tenant_id = %s AND revoked_at IS NULL
            """,
            (POLICY_TENANT,),
        )
        assert cursor.fetchall() == [
            (
                "google.workspace.connection.verify",
                "private_workspace",
                0,
                1,
                POLICY_PRINCIPAL,
            )
        ]

    control = control_factory()
    try:
        with control.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "INSERT INTO attune.policies "
                    "(tenant_id,version,document,active,created_by) "
                    "VALUES (%s,2,'{}',false,%s)",
                    (POLICY_TENANT, POLICY_PRINCIPAL),
                )
        control.rollback()
    finally:
        control.close()

    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.policies SET document = '{\"tampered\":true}'::jsonb "
            "WHERE tenant_id = %s AND active",
            (POLICY_TENANT,),
        )
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions "
            "SET created_at = clock_timestamp() - interval '11 minutes' "
            "WHERE tenant_id = %s AND id = %s",
            (POLICY_TENANT, POLICY_SESSION),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        policies.activate_read_only(
            context, principal_id=POLICY_PRINCIPAL, session_id=POLICY_SESSION
        )
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions SET created_at = clock_timestamp() "
            "WHERE tenant_id = %s AND id = %s",
            (POLICY_TENANT, POLICY_SESSION),
        )
    changed = policies.activate_read_only(
        context, principal_id=POLICY_PRINCIPAL, session_id=POLICY_SESSION
    )
    assert (changed.onboarding_revision, changed.status) == (
        3,
        "externally_modified",
    )


def test_hosted_channel_preferences_are_tenant_bound_recent_and_effect_free(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attune.tenants (id, slug, region) VALUES (%s,%s,%s)",
            (CHANNEL_TENANT, "channel-tenant", "test"),
        )
        cursor.execute(
            """
            INSERT INTO attune.principals (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, 'test')
            """,
            (CHANNEL_TENANT, CHANNEL_PRINCIPAL, hashlib.sha256(b"channel").digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.identity_sessions
                (tenant_id, id, principal_id, token_hash, csrf_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s,
                    clock_timestamp() + interval '8 hours')
            """,
            (
                CHANNEL_TENANT,
                CHANNEL_SESSION,
                CHANNEL_PRINCIPAL,
                hashlib.sha256(b"channel-token").digest(),
                hashlib.sha256(b"channel-csrf").digest(),
            ),
        )
    initialized_database.commit()
    factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    context = TenantContext(CHANNEL_TENANT)
    onboarding = PostgresHostedOnboardingRepository(factory)
    channels = PostgresHostedChannelRepository(factory)
    onboarding.start(context, principal_id=CHANNEL_PRINCIPAL)

    first = channels.configure(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        interaction_channels=["slack", "google_chat"],
        brief_channels=["slack"],
    )
    repeated = channels.configure(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        interaction_channels=["google_chat", "slack"],
        brief_channels=["slack"],
    )
    assert first == repeated
    assert first.interaction_channels == ("google_chat", "slack")
    assert first.brief_channels == ("slack",)
    assert first.status == "authorized"
    assert channels.read(context, principal_id=CHANNEL_PRINCIPAL) == first
    assert channels.read(TenantContext(TENANT_A), principal_id=PRINCIPAL_A) is None

    control = factory()
    try:
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            with tenant_transaction(control, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.configure_hosted_channels(%s,%s,%s,%s)",
                    (
                        CHANNEL_PRINCIPAL,
                        CHANNEL_SESSION,
                        ["slack", "slack"],
                        [],
                    ),
                )
    finally:
        control.rollback()
        control.close()

    control = factory()
    try:
        with control.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    """
                    INSERT INTO attune.hosted_channel_preferences
                        (tenant_id, owner_principal_id,
                         interaction_channels, brief_channels)
                    VALUES (%s, %s, ARRAY['slack'], ARRAY[]::text[])
                    """,
                    (CHANNEL_TENANT, CHANNEL_PRINCIPAL),
                )
        control.rollback()
    finally:
        control.close()

    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions "
            "SET created_at = clock_timestamp() - interval '11 minutes' "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, CHANNEL_SESSION),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        channels.configure(
            context,
            principal_id=CHANNEL_PRINCIPAL,
            session_id=CHANNEL_SESSION,
            interaction_channels=["slack"],
            brief_channels=["slack"],
        )


def test_hosted_channel_setup_is_recent_single_use_and_not_directly_mutable(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    context = TenantContext(CHANNEL_TENANT)
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions SET created_at = clock_timestamp() "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, CHANNEL_SESSION),
        )
    factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    setups = PostgresHostedChannelSetupRepository(factory)
    first = setups.begin(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="google_chat",
        mechanism="link_code",
        secret_hash=hashlib.sha256(b"first-channel-link").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=9),
    )
    second = setups.begin(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="google_chat",
        mechanism="link_code",
        secret_hash=hashlib.sha256(b"second-channel-link").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=9),
    )
    assert first.state == second.state == "pending"
    assert first.id != second.id
    states = {state.provider: state for state in setups.read(
        context, principal_id=CHANNEL_PRINCIPAL
    )}
    assert states["google_chat"].selected is True
    assert states["google_chat"].setup_state == "pending"
    assert states["google_chat"].destination_state == "not_started"
    assert states["slack"].selected is True
    assert states["slack"].setup_state == "not_started"

    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "SELECT state FROM attune.hosted_channel_setup_transactions "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, first.id),
        )
        assert cursor.fetchone()[0] == "cancelled"

    control = factory()
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_transaction(control, context) as cursor:
                cursor.execute(
                    "UPDATE attune.hosted_channel_setup_transactions "
                    "SET state = 'consumed' WHERE tenant_id = %s",
                    (CHANNEL_TENANT,),
                )
        control.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_transaction(control, context) as cursor:
                cursor.execute(
                    "UPDATE attune.installations SET status = 'revoked' "
                    "WHERE tenant_id = %s",
                    (CHANNEL_TENANT,),
                )
    finally:
        control.rollback()
        control.close()


def test_google_chat_link_broker_is_one_use_audited_and_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    broker = PostgresChannelBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_channel_broker"])
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
    )
    secret_hash = hashlib.sha256(b"second-channel-link").digest()
    claim_hash = hashlib.sha256(b"broker-claim").digest()
    claim = broker.claim(
        secret_hash=secret_hash,
        claim_hash=claim_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert claim.tenant_id == CHANNEL_TENANT
    assert claim.owner_principal_id == CHANNEL_PRINCIPAL
    assert writer.write(claim.pre_audit_intent_id) is not None

    destination_id = broker.resolve_destination_id(
        secret_hash=secret_hash,
        claim_hash=claim_hash,
        candidate_id=UUID("30000000-0000-4000-8000-000000000099"),
    )
    linked = broker.consume(
        secret_hash=secret_hash,
        claim_hash=claim_hash,
        installation_ref_hash=hashlib.sha256(b"google-chat-app").digest(),
        actor_ref_hash=hashlib.sha256(b"google-chat-owner").digest(),
        destination_ref_hash=hashlib.sha256(b"google-chat-dm").digest(),
        destination_id=destination_id,
        encrypted=EncryptedCredential(
            ciphertext=b"c" * 32,
            nonce=b"n" * 12,
            wrapped_dek=b"w" * 32,
            key_resource="projects/test/locations/test/keyRings/test/cryptoKeys/test",
        ),
    )
    assert linked.tenant_id == CHANNEL_TENANT
    assert linked.destination_status == "pending_test"
    assert writer.write(linked.outcome_audit_intent_id) is not None

    with pytest.raises(psycopg.errors.NoDataFound):
        broker.claim(
            secret_hash=secret_hash,
            claim_hash=hashlib.sha256(b"replay").digest(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )

    delivery_claim_hash = hashlib.sha256(b"delivery-claim-one").digest()
    delivery = broker.claim_delivery(
        destination_id=linked.destination_id,
        claim_hash=delivery_claim_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert delivery.tenant_id == CHANNEL_TENANT
    assert delivery.encrypted.ciphertext == b"c" * 32
    assert writer.write(delivery.pre_audit_intent_id) is not None
    failed = broker.complete_delivery(
        destination_id=linked.destination_id,
        claim_hash=delivery_claim_hash,
        succeeded=False,
    )
    assert failed.destination_status == "pending_test"
    assert writer.write(failed.outcome_audit_intent_id) is not None

    delivery_claim_hash = hashlib.sha256(b"delivery-claim-two").digest()
    delivery = broker.claim_delivery(
        destination_id=linked.destination_id,
        claim_hash=delivery_claim_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert writer.write(delivery.pre_audit_intent_id) is not None
    completed = broker.complete_delivery(
        destination_id=linked.destination_id,
        claim_hash=delivery_claim_hash,
        succeeded=True,
    )
    assert completed.destination_status == "active"
    assert writer.write(completed.outcome_audit_intent_id) is not None

    direct = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_channel_broker"]
    )()
    try:
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT * FROM attune.hosted_channel_destinations")
        direct.rollback()
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT * FROM attune.hosted_channel_routes")
        direct.rollback()
    finally:
        direct.close()

    setups = PostgresHostedChannelSetupRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    states = {
        state.provider: state
        for state in setups.read(
            TenantContext(CHANNEL_TENANT), principal_id=CHANNEL_PRINCIPAL
        )
    }
    assert states["google_chat"].setup_state == "consumed"
    assert states["google_chat"].destination_state == "active"


def test_google_chat_conversation_delivery_is_canonical_replay_safe_and_broker_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    conversation_id = UUID("30000000-0000-4000-8000-000000000101")
    connector_id = UUID("30000000-0000-4000-8000-000000000102")
    job_id = UUID("30000000-0000-4000-8000-000000000103")
    event_id = UUID("30000000-0000-4000-8000-000000000105")
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT id, installation_id FROM attune.hosted_channel_destinations "
            "WHERE tenant_id = %s AND provider = 'google_chat' AND status = 'active'",
            (CHANNEL_TENANT,),
        )
        destination_id, installation_id = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO attune.connectors
                (tenant_id, id, principal_id, installation_id, provider,
                 credential_ref, status)
            VALUES (%s, %s, %s, %s, 'google', %s, 'active')
            """,
            (CHANNEL_TENANT, connector_id, CHANNEL_PRINCIPAL, installation_id,
             UUID("30000000-0000-4000-8000-000000000104")),
        )
        cursor.execute(
            "INSERT INTO attune.policies "
            "(tenant_id, version, document, active, created_by) "
            "VALUES (%s, 1, '{}'::jsonb, true, %s)",
            (CHANNEL_TENANT, CHANNEL_PRINCIPAL),
        )
        cursor.execute(
            """
            INSERT INTO attune.conversations
                (tenant_id, id, installation_id, principal_id, surface,
                 external_ref_hash)
            VALUES (%s, %s, %s, %s, 'google_chat', %s)
            """,
            (CHANNEL_TENANT, conversation_id, installation_id, CHANNEL_PRINCIPAL,
             hashlib.sha256(b"conversation-delivery").digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.provider_events
                (tenant_id, id, installation_id, provider, kind,
                 deduplication_key, signal)
            VALUES (%s, %s, %s, 'google', 'google_chat.message', %s,
                    jsonb_build_object('schema_version', 1,
                        'conversation_id', %s::text,
                        'destination_id', %s::text,
                        'principal_id', %s::text,
                        'user_sequence', 1))
            """,
            (CHANNEL_TENANT, event_id, installation_id,
             hashlib.sha256(b"conversation-event").digest(), conversation_id,
             destination_id, CHANNEL_PRINCIPAL),
        )
        cursor.execute(
            """
            INSERT INTO attune.jobs
                (tenant_id, id, kind, state, idempotency_key, capability,
                 payload, attempts, lease_expires_at)
            VALUES (%s, %s, 'channel.google_chat.converse', 'leased', %s,
                    'assistant.conversation.read',
                    jsonb_build_object('schema_version', 1,
                        'conversation_id', %s::text,
                        'destination_id', %s::text,
                        'provider_event_id', %s::text,
                        'user_sequence', 1),
                    1, clock_timestamp() + interval '5 minutes')
            """,
            (CHANNEL_TENANT, job_id, hashlib.sha256(b"conversation-job").digest(),
             conversation_id, destination_id,
             event_id),
        )
        cursor.execute(
            """
            INSERT INTO attune.conversation_turns
                (tenant_id, conversation_id, sequence, actor_type, content, provenance)
            VALUES (%s, %s, 1, 'user', 'What is tomorrow?', '{}'),
                   (%s, %s, 2, 'assistant', 'Canonical answer',
                    jsonb_build_object('schema_version', 1, 'job_id', %s::text))
            """,
            (CHANNEL_TENANT, conversation_id, CHANNEL_TENANT, conversation_id, job_id),
        )
    initialized_database.commit()

    worker_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_worker"]
    )
    canonical_job = PostgresJobRepository(worker_factory).get(
        TenantContext(CHANNEL_TENANT), job_id
    )
    assert canonical_job is not None
    work = PostgresGoogleChatConversationWorkRepository(worker_factory).resolve(
        TenantContext(CHANNEL_TENANT), canonical_job
    )
    assert (work.conversation_id, work.connector_id, work.destination_id) == (
        conversation_id, connector_id, destination_id,
    )

    broker = PostgresChannelBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_channel_broker"])
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
    )
    first_hash = hashlib.sha256(b"conversation-delivery-first").digest()
    first = broker.claim_conversation_delivery(
        destination_id=destination_id, job_id=job_id, claim_hash=first_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert first.reply_text == "Canonical answer" and not first.already_delivered
    assert writer.write(first.pre_audit_intent_id) is not None
    failed = broker.complete_conversation_delivery(
        job_id=job_id, claim_hash=first_hash, succeeded=False,
        provider_message_ref_hash=None,
    )
    assert failed.delivery_state == "failed"
    assert writer.write(failed.outcome_audit_intent_id) is not None

    second_hash = hashlib.sha256(b"conversation-delivery-second").digest()
    second = broker.claim_conversation_delivery(
        destination_id=destination_id, job_id=job_id, claim_hash=second_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert writer.write(second.pre_audit_intent_id) is not None
    completed = broker.complete_conversation_delivery(
        job_id=job_id, claim_hash=second_hash, succeeded=True,
        provider_message_ref_hash=hashlib.sha256(b"provider-message").digest(),
    )
    assert completed.delivery_state == "delivered"
    assert writer.write(completed.outcome_audit_intent_id) is not None
    replay = broker.claim_conversation_delivery(
        destination_id=destination_id, job_id=job_id,
        claim_hash=hashlib.sha256(b"conversation-delivery-replay").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert replay.already_delivered and replay.reply_text is None

    direct = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_channel_broker"]
    )()
    try:
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT * FROM attune.hosted_channel_deliveries")
        direct.rollback()
    finally:
        direct.close()


def test_hosted_brief_job_round_trips_through_real_stores_and_holds_rls(
    initialized_database, database_url
):
    """Gated Postgres round trip (Phase 5 stage 4, G12): a self-contained
    tenant/principal/destination/preference/job fixture, proving
    ``PostgresHostedBriefRepository`` resolves authority and fan-out
    destinations, and the new brief delivery claim/complete functions round-
    trip real rows -- plus the same forced-RLS/function-only isolation every
    other delivery table in this schema holds."""
    psycopg = pytest.importorskip("psycopg")
    tenant_id = UUID("30000000-0000-4000-8000-000000000201")
    principal_id = UUID("30000000-0000-4000-8000-000000000202")
    installation_id = UUID("30000000-0000-4000-8000-000000000203")
    destination_id = UUID("30000000-0000-4000-8000-000000000204")
    connector_id = UUID("30000000-0000-4000-8000-000000000205")
    job_id = UUID("30000000-0000-4000-8000-000000000206")
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attune.tenants (id, slug, region) VALUES (%s,%s,%s)",
            (tenant_id, "hosted-brief-tenant", "test"),
        )
        cursor.execute(
            """
            INSERT INTO attune.principals (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, 'test')
            """,
            (tenant_id, principal_id, hashlib.sha256(b"hosted-brief-principal").digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.installations
                (tenant_id, id, provider, kind, external_ref_hash, status)
            VALUES (%s, %s, 'google', 'channel', %s, 'active')
            """,
            (tenant_id, installation_id, hashlib.sha256(b"hosted-brief-install").digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.connectors
                (tenant_id, id, principal_id, installation_id, provider,
                 credential_ref, status)
            VALUES (%s, %s, %s, %s, 'google', %s, 'active')
            """,
            (tenant_id, connector_id, principal_id, installation_id,
             UUID("30000000-0000-4000-8000-000000000207")),
        )
        cursor.execute(
            "INSERT INTO attune.policies "
            "(tenant_id, version, document, active, created_by) "
            "VALUES (%s, 1, '{}'::jsonb, true, %s)",
            (tenant_id, principal_id),
        )
        cursor.execute(
            """
            INSERT INTO attune.hosted_channel_destinations
                (tenant_id, id, owner_principal_id, installation_id, provider,
                 installation_ref_hash, actor_ref_hash, destination_ref_hash,
                 visibility, status, ingress_verified_at, delivery_verified_at,
                 route_version)
            VALUES (%s, %s, %s, %s, 'google_chat', %s, %s, %s, 'owner_dm',
                    'active', clock_timestamp(), clock_timestamp(), 1)
            """,
            (
                tenant_id, destination_id, principal_id, installation_id,
                hashlib.sha256(b"hosted-brief-install-ref").digest(),
                hashlib.sha256(b"hosted-brief-actor-ref").digest(),
                hashlib.sha256(b"hosted-brief-destination-ref").digest(),
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.hosted_channel_routes
                (tenant_id, destination_id, ciphertext, nonce, wrapped_dek, key_resource)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id, destination_id, b"ciphertext", b"0" * 12, b"wrapped",
                "projects/p/locations/l/keyRings/r/cryptoKeys/k",
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.hosted_channel_preferences
                (tenant_id, owner_principal_id, interaction_channels, brief_channels)
            VALUES (%s, %s, ARRAY[]::text[], ARRAY['google_chat'])
            """,
            (tenant_id, principal_id),
        )
        cursor.execute(
            """
            INSERT INTO attune.jobs
                (tenant_id, id, kind, state, idempotency_key, capability,
                 payload, attempts, lease_expires_at)
            VALUES (%s, %s, 'channel.brief.deliver', 'leased', %s,
                    'assistant.brief.deliver',
                    jsonb_build_object('schema_version', 1,
                        'principal_id', %s::text),
                    1, clock_timestamp() + interval '5 minutes')
            """,
            (tenant_id, job_id, hashlib.sha256(b"hosted-brief-job").digest(),
             principal_id),
        )
    initialized_database.commit()

    worker_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_worker"]
    )
    context = TenantContext(tenant_id)
    canonical_job = PostgresJobRepository(worker_factory).get(context, job_id)
    assert canonical_job is not None
    brief_repository = PostgresHostedBriefRepository(worker_factory)
    work = brief_repository.resolve(context, canonical_job)
    assert work == BriefWork(principal_id, connector_id)
    destinations = brief_repository.list_brief_destinations(
        context, principal_id=principal_id
    )
    assert destinations == (BriefDestination(destination_id, "google_chat"),)
    brief_repository.propose_delivery(
        context, job_id=job_id, destination_id=destination_id,
        brief_text="Hello, this is your brief.",
    )
    # Idempotent: proposing again for the same (job, destination) is a no-op,
    # never an error.
    brief_repository.propose_delivery(
        context, job_id=job_id, destination_id=destination_id,
        brief_text="A different rendering would be ignored.",
    )

    broker = PostgresChannelBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_channel_broker"])
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
    )
    first_hash = hashlib.sha256(b"hosted-brief-delivery-first").digest()
    first = broker.claim_brief_delivery(
        destination_id=destination_id, job_id=job_id, claim_hash=first_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert first.brief_text == "Hello, this is your brief."
    assert not first.already_delivered
    assert writer.write(first.pre_audit_intent_id) is not None
    completed = broker.complete_brief_delivery(
        job_id=job_id, destination_id=destination_id, claim_hash=first_hash,
        succeeded=True,
        provider_message_ref_hash=hashlib.sha256(b"hosted-brief-message").digest(),
    )
    assert completed.delivery_state == "delivered"
    assert writer.write(completed.outcome_audit_intent_id) is not None

    replay = broker.claim_brief_delivery(
        destination_id=destination_id, job_id=job_id,
        claim_hash=hashlib.sha256(b"hosted-brief-delivery-replay").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert replay.already_delivered and replay.brief_text is None

    direct = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_channel_broker"]
    )()
    try:
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT * FROM attune.hosted_brief_deliveries")
        direct.rollback()
    finally:
        direct.close()


def test_google_chat_destination_disconnect_blocks_use_and_allows_explicit_relink(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    context = TenantContext(CHANNEL_TENANT)
    setups = PostgresHostedChannelSetupRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    broker = PostgresChannelBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_channel_broker"])
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
    )
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions SET created_at = clock_timestamp() "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, CHANNEL_SESSION),
        )
        cursor.execute(
            "SELECT id, installation_ref_hash, actor_ref_hash, "
            "destination_ref_hash FROM attune.hosted_channel_destinations "
            "WHERE tenant_id = %s AND provider = 'google_chat'",
            (CHANNEL_TENANT,),
        )
        destination_id, installation_hash, actor_hash, destination_hash = cursor.fetchone()

    assert setups.disconnect(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="google_chat",
    ) is True
    assert setups.disconnect(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="google_chat",
    ) is False
    states = {
        state.provider: state
        for state in setups.read(context, principal_id=CHANNEL_PRINCIPAL)
    }
    assert states["google_chat"].destination_state == "revoked"
    with pytest.raises(psycopg.errors.NoDataFound):
        broker.accept_message(
            installation_ref_hash=installation_hash,
            actor_ref_hash=actor_hash,
            destination_ref_hash=destination_hash,
            message_ref_hash=hashlib.sha256(b"message-after-disconnect").digest(),
            text="This must not be accepted",
        )
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "SELECT count(*) FROM attune.hosted_channel_routes "
            "WHERE tenant_id = %s AND destination_id = %s",
            (CHANNEL_TENANT, destination_id),
        )
        assert cursor.fetchone() == (0,)

    secret = b"explicit-replacement-link"
    started = setups.begin(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="google_chat",
        mechanism="link_code",
        secret_hash=hashlib.sha256(secret).digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=9),
    )
    claim_hash = hashlib.sha256(b"replacement-claim").digest()
    claim = broker.claim(
        secret_hash=hashlib.sha256(secret).digest(),
        claim_hash=claim_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert writer.write(claim.pre_audit_intent_id) is not None
    candidate_id = UUID("30000000-0000-4000-8000-000000000109")
    resolved_id = broker.resolve_destination_id(
        secret_hash=hashlib.sha256(secret).digest(),
        claim_hash=claim_hash,
        candidate_id=candidate_id,
    )
    assert resolved_id == destination_id
    linked = broker.consume(
        secret_hash=hashlib.sha256(secret).digest(),
        claim_hash=claim_hash,
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        destination_id=resolved_id,
        encrypted=EncryptedCredential(
            ciphertext=b"r" * 32,
            nonce=b"q" * 12,
            wrapped_dek=b"k" * 32,
            key_resource="projects/test/locations/test/keyRings/test/cryptoKeys/test",
        ),
    )
    assert started.state == "pending"
    assert linked.destination_id == destination_id
    assert linked.destination_status == "pending_test"
    assert writer.write(linked.outcome_audit_intent_id) is not None
    delivery_claim_hash = hashlib.sha256(b"replacement-delivery-claim").digest()
    delivery = broker.claim_delivery(
        destination_id=destination_id,
        claim_hash=delivery_claim_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert writer.write(delivery.pre_audit_intent_id) is not None
    completed = broker.complete_delivery(
        destination_id=destination_id,
        claim_hash=delivery_claim_hash,
        succeeded=True,
    )
    assert completed.destination_status == "active"
    assert writer.write(completed.outcome_audit_intent_id) is not None
    accepted = broker.accept_message(
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        message_ref_hash=hashlib.sha256(b"message-after-relink").digest(),
        text="This must be accepted after verified relink",
    )
    assert accepted.accepted_new is True


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
            cursor.execute("SELECT id FROM attune.memories WHERE id = %s", (MEMORY_B,))
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


def test_audit_is_tenant_bound_and_append_only(initialized_database, database_url):
    psycopg = pytest.importorskip("psycopg")
    producer = PostgresAuditProducerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"]),
        producer_kind="worker",
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
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
    assert (
        repository.claim(
            TenantContext(TENANT_A),
            first.id,
            expected_kind="gmail.reconcile",
            expected_capability="gmail.read",
        )
        is None
    )
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
    assert (
        repository.schedule_retry(
            TenantContext(TENANT_A),
            first.id,
            expected_attempt=1,
            error_code="replay",
            available_at=datetime.now(timezone.utc),
        )
        is None
    )
    assert repository.finish(TenantContext(TENANT_A), first.id, outcome="succeeded")


def test_reconciliation_atomically_moves_only_the_canonical_leased_job(
    initialized_database, database_url
):
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    jobs = PostgresJobRepository(factory)
    reconciliations = PostgresJobReconciliationRepository(factory)
    job = jobs.enqueue(
        TenantContext(TENANT_A),
        kind="calendar.write",
        capability="calendar.write",
        payload={"canonical_ref": "opaque"},
        idempotency_key=hashlib.sha256(b"reconciliation-job").digest(),
    )
    leased = jobs.claim(
        TenantContext(TENANT_A),
        job.id,
        expected_kind="calendar.write",
        expected_capability="calendar.write",
    )
    assert leased is not None
    opened = reconciliations.open(
        TenantContext(TENANT_A),
        leased,
        reason_code="executor_ambiguous",
        provider_request_ref_hash=hashlib.sha256(b"provider-request").digest(),
    )
    assert opened.job_id == job.id
    assert opened.state == "open"
    assert jobs.get(TenantContext(TENANT_A), job.id).state == "reconcile"
    assert jobs.get(TenantContext(TENANT_B), job.id) is None
    replay = reconciliations.open(
        TenantContext(TENANT_A),
        leased,
        reason_code="executor_ambiguous",
    )
    assert replay.id == opened.id


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
    assert (
        repository.search(
            TenantContext(TENANT_B),
            principal_id=PRINCIPAL_B,
            model="repository-test",
            embedding=[1, 0, 0],
        )
        == []
    )
    assert repository.soft_delete(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        memory_id=memory.id,
    )
    assert (
        repository.search(
            TenantContext(TENANT_A),
            principal_id=PRINCIPAL_A,
            model="repository-test",
            embedding=[1, 0, 0],
        )
        == []
    )


def test_memory_repository_list_recent_and_get_are_tenant_scoped(
    initialized_database, database_url
):
    """docs/hosted-memory.md: the inspect/forget commands' repository
    surface -- recency listing and id-addressable lookup, both tenant- and
    principal-scoped (SEC-201: the predicate comes from the caller's
    verified context, never a selector the user or model typed)."""
    repository = PostgresMemoryRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    )
    older = repository.add(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        creator_id=PRINCIPAL_A,
        content="older memory",
        provenance={},
        source_class="user_taught",
        confidence=1,
        model="list-recent-test",
        embedding=[0, 1, 0],
    )
    newer = repository.add(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        creator_id=PRINCIPAL_A,
        content="newer memory",
        provenance={},
        source_class="user_taught",
        confidence=1,
        model="list-recent-test",
        embedding=[0, 1, 0],
    )
    other_tenant = repository.add(
        TenantContext(TENANT_B),
        principal_id=PRINCIPAL_B,
        creator_id=PRINCIPAL_B,
        content="tenant B memory for listing",
        provenance={},
        source_class="user_taught",
        confidence=1,
        model="list-recent-test",
        embedding=[0, 1, 0],
    )

    listing = repository.list_recent(TenantContext(TENANT_A), principal_id=PRINCIPAL_A)
    ids = [item.id for item in listing]
    assert ids.index(newer.id) < ids.index(older.id)
    assert other_tenant.id not in ids

    assert repository.get(
        TenantContext(TENANT_A), principal_id=PRINCIPAL_A, memory_id=newer.id
    ).content == "newer memory"
    # Cross-tenant get: another tenant's context cannot read tenant A's memory.
    assert repository.get(
        TenantContext(TENANT_B), principal_id=PRINCIPAL_B, memory_id=newer.id
    ) is None
    # Cross-tenant list: tenant B's listing never contains tenant A's rows.
    tenant_b_listing = repository.list_recent(
        TenantContext(TENANT_B), principal_id=PRINCIPAL_B
    )
    assert all(item.id != newer.id and item.id != older.id for item in tenant_b_listing)

    assert repository.soft_delete(
        TenantContext(TENANT_A), principal_id=PRINCIPAL_A, memory_id=older.id
    )
    after_delete = repository.list_recent(TenantContext(TENANT_A), principal_id=PRINCIPAL_A)
    assert older.id not in [item.id for item in after_delete]
    assert repository.get(
        TenantContext(TENANT_A), principal_id=PRINCIPAL_A, memory_id=older.id
    ) is None


def test_audit_outbox_is_idempotent_and_writer_accepts_only_intent_ids(
    initialized_database, database_url
):
    producer = PostgresAuditProducerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"]),
        producer_kind="worker",
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
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

    connection = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])()
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
    """attune.approvals now backs a real security transition (migration
    0043): direct UPDATE is refused for every runtime role, and the sole
    decide/consume path is the actor-bound, one-use, idempotent-replay
    attune.claim_capability_approval SECURITY DEFINER function."""

    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "SELECT version FROM attune.policies WHERE tenant_id = %s AND active",
            (TENANT_A,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO attune.policies "
                "(tenant_id, version, document, active, created_by) "
                "VALUES (%s, 1, '{}'::jsonb, true, %s) RETURNING version",
                (TENANT_A, PRINCIPAL_A),
            )
            row = cursor.fetchone()
        policy_version = row[0]

    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    connection = factory()
    try:
        with tenant_transaction(connection, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                """
                INSERT INTO attune.capability_admissions
                    (tenant_id, principal_id, connector_id, capability,
                     contract_version, risk, policy_version, arguments)
                VALUES (%s, %s, %s, %s, 1, 2, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    TENANT_A,
                    PRINCIPAL_A,
                    CONNECTOR_A,
                    "gmail.draft",
                    policy_version,
                    '{"proposal": "opaque"}',
                ),
            )
            admission_id = cursor.fetchone()[0]
    finally:
        connection.close()

    approvals = PostgresApprovalRepository(factory)
    opaque_hash = hashlib.sha256(b"opaque-approval-reference").digest()
    action_hash = hashlib.sha256(b"canonical-action").digest()
    destination_hash = hashlib.sha256(b"destination").digest()
    approval = approvals.propose(
        TenantContext(TENANT_A),
        admission_id=admission_id,
        approver_id=PRINCIPAL_A,
        connector_id=CONNECTOR_A,
        opaque_ref_hash=opaque_hash,
        action_hash=action_hash,
        capability="gmail.draft",
        destination_hash=destination_hash,
        source_version="history-123",
        policy_version=policy_version,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    assert approval.status == "pending"
    assert approval.surface == "web"
    assert approval.job_id is None
    assert approval.admission_id == admission_id

    worker_connection = factory()
    try:
        with pytest.raises(Exception, match="permission denied"):
            with tenant_transaction(worker_connection, TenantContext(TENANT_A)) as cursor:
                cursor.execute(
                    "UPDATE attune.approvals SET status = 'rejected' WHERE id = %s",
                    (approval.id,),
                )
    finally:
        worker_connection.close()

    control_plane_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    control_plane_connection = control_plane_factory()
    try:
        with pytest.raises(Exception, match="permission denied"):
            with tenant_transaction(
                control_plane_connection, TenantContext(TENANT_A)
            ) as cursor:
                cursor.execute(
                    "UPDATE attune.approvals SET status = 'rejected' WHERE id = %s",
                    (approval.id,),
                )
    finally:
        control_plane_connection.close()

    # Wrong tenant, then wrong approver: neither can claim it.
    assert (
        approvals.claim(
            TenantContext(TENANT_B),
            approval_id=approval.id,
            principal_id=PRINCIPAL_A,
            decision="approved",
        )
        is None
    )
    assert (
        approvals.claim(
            TenantContext(TENANT_A),
            approval_id=approval.id,
            principal_id=PRINCIPAL_B,
            decision="approved",
        )
        is None
    )

    claimed = approvals.claim(
        TenantContext(TENANT_A),
        approval_id=approval.id,
        principal_id=PRINCIPAL_A,
        decision="approved",
    )
    assert claimed is not None
    assert claimed.final_status == "consumed"
    assert claimed.admission_id == admission_id
    assert claimed.capability == "gmail.draft"
    assert dict(claimed.arguments) == {"proposal": "opaque"}
    assert claimed.connector_id == CONNECTOR_A
    assert claimed.policy_version == policy_version

    # One-use: replaying the same decision returns the recorded outcome
    # rather than erroring or mutating anything again (SEC-501).
    replay = approvals.claim(
        TenantContext(TENANT_A),
        approval_id=approval.id,
        principal_id=PRINCIPAL_A,
        decision="approved",
    )
    assert replay is not None
    assert replay.final_status == "consumed"
    assert dict(replay.arguments) == {"proposal": "opaque"}

    # A pending approval that is instead rejected never returns arguments.
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            """
            INSERT INTO attune.approvals
                (tenant_id, admission_id, approver_id, connector_id,
                 opaque_ref_hash, action_hash, capability, destination_hash,
                 source_version, policy_version, surface, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'web', %s)
            RETURNING id
            """,
            (
                TENANT_A,
                admission_id,
                PRINCIPAL_A,
                CONNECTOR_A,
                hashlib.sha256(b"opaque-approval-reference-2").digest(),
                action_hash,
                "gmail.draft",
                destination_hash,
                "history-123",
                policy_version,
                datetime.now(timezone.utc) + timedelta(minutes=10),
            ),
        )
        second_approval_id = cursor.fetchone()[0]
    rejected = approvals.claim(
        TenantContext(TENANT_A),
        approval_id=second_approval_id,
        principal_id=PRINCIPAL_A,
        decision="rejected",
    )
    assert rejected is not None
    assert rejected.final_status == "rejected"
    assert rejected.arguments is None


def test_capability_admission_repository_persists_atomically_and_never_dispatches(
    initialized_database, database_url
):
    """PostgresCapabilityAdmissionRepository.record() -- admission is never
    execution authority: it inserts one immutable capability_admissions row
    and one pending approvals row in the same transaction, and creates no
    job or dispatch intent."""

    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "SELECT version FROM attune.policies WHERE tenant_id = %s AND active",
            (TENANT_A,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO attune.policies "
                "(tenant_id, version, document, active, created_by) "
                "VALUES (%s, 1, '{}'::jsonb, true, %s) RETURNING version",
                (TENANT_A, PRINCIPAL_A),
            )
            row = cursor.fetchone()
        policy_version = row[0]

    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    repository = PostgresCapabilityAdmissionRepository(factory)
    admitted = AuthorizedCapability(
        context=TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        connector_id=CONNECTOR_A,
        capability="google.gmail.draft.create",
        contract_version=1,
        risk=RiskTier.R2,
        policy_version=policy_version,
        arguments={"thread_ref": "thread_9", "body": "Ready to ship."},
    )
    recorded = repository.record(
        TenantContext(TENANT_A),
        authorized=admitted,
        destination_hash=hashlib.sha256(b"thread_9").digest(),
        now=datetime.now(timezone.utc),
    )

    connection = factory()
    try:
        with tenant_transaction(connection, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT capability, contract_version, risk, policy_version, arguments "
                "FROM attune.capability_admissions WHERE tenant_id = %s AND id = %s",
                (TENANT_A, recorded.admission_id),
            )
            admission_row = cursor.fetchone()
            cursor.execute(
                "SELECT status, admission_id, job_id, surface, policy_version "
                "FROM attune.approvals WHERE tenant_id = %s AND id = %s",
                (TENANT_A, recorded.approval_id),
            )
            approval_row = cursor.fetchone()
    finally:
        connection.close()

    assert admission_row[0] == "google.gmail.draft.create"
    assert admission_row[1] == 1
    assert admission_row[2] == 2
    assert admission_row[3] == policy_version
    assert dict(admission_row[4]) == {"thread_ref": "thread_9", "body": "Ready to ship."}
    assert approval_row == (
        "pending", recorded.admission_id, None, "web", policy_version,
    )
    # No job exists yet from this admission alone -- only an approval does.
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "SELECT count(*) FROM attune.jobs "
            "WHERE tenant_id = %s AND capability = 'google.gmail.draft.create'",
            (TENANT_A,),
        )
        assert cursor.fetchone()[0] == 0


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
    assert [
        turn.sequence
        for turn in repository.recent(TenantContext(TENANT_A), conversation.id)
    ] == [1, 2]
    assert repository.recent(TenantContext(TENANT_B), conversation.id) == []


def test_autonomy_and_lifecycle_objects_fail_closed_across_tenants(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    control_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    autonomy = PostgresAutonomyRepository(control_factory)
    lifecycle = PostgresLifecycleRepository(control_factory)
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            """
            INSERT INTO attune.autonomy_grants
                (tenant_id, principal_id, capability, domain, maximum_risk,
                 policy_version, granted_by)
            VALUES (%s, %s, 'gmail.read', 'private', 0, 1, %s)
            RETURNING id
            """,
            (TENANT_A, PRINCIPAL_A, PRINCIPAL_A),
        )
        grant_id = cursor.fetchone()[0]
    assert (
        autonomy.find_active(
            TenantContext(TENANT_A),
            principal_id=PRINCIPAL_A,
            capability="gmail.read",
            domain="private",
        )
        is not None
    )
    assert (
        autonomy.find_active(
            TenantContext(TENANT_B),
            principal_id=PRINCIPAL_A,
            capability="gmail.read",
            domain="private",
        )
        is None
    )
    control = control_factory()
    try:
        with control.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "UPDATE attune.autonomy_grants SET revoked_at = clock_timestamp() "
                    "WHERE tenant_id = %s AND id = %s",
                    (TENANT_A, grant_id),
                )
        control.rollback()
    finally:
        control.close()

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
    assert (
        lifecycle.transition_deletion(
            TenantContext(TENANT_B),
            marker.id,
            expected_state="requested",
            state="running",
        )
        is None
    )

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
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"]),
        producer_kind="control_plane",
    )
    broker = PostgresDispatchBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_dispatch_broker"])
    )
    dispatch_audit = PostgresDispatchAuditRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_dispatch_broker"])
    )
    audit_writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
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

    assert broker.lease(dispatch.intent.id, producer_kind="worker") is None
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
    assert broker.lease(dispatch.intent.id, producer_kind="control_plane") is None
    pre_audit_intent_id = dispatch_audit.request(dispatch.intent.id, outcome="allowed")
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
    replay = broker.lease(dispatch.intent.id, producer_kind="control_plane")
    assert replay is not None and replay.state == "dispatched"
    assert replay.task_id == dispatch.intent.task_id
    audit_intent_id = dispatch_audit.request(dispatch.intent.id, outcome="observed")
    assert audit_intent_id
    assert (
        dispatch_audit.request(dispatch.intent.id, outcome="observed") == audit_intent_id
    )
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

    producer = PostgresDispatchProducerRepository(factory, producer_kind="worker")
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
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_dispatch_broker"])
    )
    recoverable = producer.enqueue(
        TenantContext(TENANT_A),
        kind="memory.reconcile",
        capability="memory.read",
        payload={},
        idempotency_key=hashlib.sha256(b"dispatch-lease-recovery").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    first = broker.lease(recoverable.intent.id, producer_kind="worker", lease_seconds=30)
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
    assert broker.lease(expiring.intent.id, producer_kind="worker") is None


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

    worker = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])()
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
            cursor.execute("SELECT attune.revoke_connector_credential(%s)", (revoke_id,))
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


def test_credential_use_rate_is_atomic_per_tenant_and_capability(
    initialized_database, database_url
):
    with initialized_database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO attune.connectors
                (tenant_id, id, principal_id, installation_id, provider,
                 credential_ref, status)
            VALUES (%s, %s, %s, %s, 'google', %s, 'active')
            """,
            (
                TENANT_A,
                RATE_CONNECTOR,
                PRINCIPAL_A,
                INSTALLATION_A,
                RATE_CREDENTIAL,
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.connector_credentials
                (tenant_id, id, connector_id, credential_version,
                 ciphertext, nonce, wrapped_dek, key_resource)
            VALUES (%s, %s, %s, 1, %s, %s, %s, %s)
            """,
            (
                TENANT_A,
                RATE_CREDENTIAL,
                RATE_CONNECTOR,
                b"ciphertext-with-tag",
                bytes(12),
                b"wrapped-dek",
                "projects/test/locations/test/keyRings/test/cryptoKeys/rate",
            ),
        )
    initialized_database.commit()

    producer = PostgresCredentialIntentRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"]),
        producer_kind="worker",
    )
    broker = PostgresSecretBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_secret_broker"])
    )
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    for index in range(60):
        intent = producer.request(
            TenantContext(TENANT_A),
            connector_id=RATE_CONNECTOR,
            operation="use",
            capability="google.gmail.profile.read",
            idempotency_key=hashlib.sha256(f"rate-{index}".encode()).digest(),
            expires_at=expires,
        )
        assert broker.lease(intent.id, producer_kind="worker") is not None
        assert broker.finalize(intent.id, producer_kind="worker", outcome="failed")

    limited = producer.request(
        TenantContext(TENANT_A),
        connector_id=RATE_CONNECTOR,
        operation="use",
        capability="google.gmail.profile.read",
        idempotency_key=hashlib.sha256(b"rate-limited").digest(),
        expires_at=expires,
    )
    assert broker.lease(limited.id, producer_kind="worker") is None

    separate_capability = producer.request(
        TenantContext(TENANT_A),
        connector_id=RATE_CONNECTOR,
        operation="use",
        capability="google.calendar.profile.read",
        idempotency_key=hashlib.sha256(b"rate-separate").digest(),
        expires_at=expires,
    )
    assert broker.lease(separate_capability.id, producer_kind="worker") is not None


def test_oauth_transaction_is_tenant_bound_one_time_and_exchange_only(
    initialized_database, database_url
):
    state_hash = hashlib.sha256(b"oauth-state").digest()
    binding_hash = hashlib.sha256(b"oauth-binding").digest()
    wrong_binding_hash = hashlib.sha256(b"wrong-binding").digest()
    nonce_hash = hashlib.sha256(b"oauth-nonce").digest()
    producer = PostgresOAuthTransactionRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    exchange = PostgresOAuthExchangeRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_oauth_exchange"])
    )

    transaction = producer.create(
        TenantContext(TENANT_A),
        principal_id=PRINCIPAL_A,
        connector_id=OAUTH_CONNECTOR,
        credential_intent_id=OAUTH_INTENT,
        state_hash=state_hash,
        binding_hash=binding_hash,
        nonce_hash=nonce_hash,
        pkce_verifier="v" * 64,
        redirect_uri="https://dev.attune.mumit.org/oauth/google/callback",
        scopes=("openid", "email"),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert transaction.state == "pending"
    assert exchange.lease(state_hash=state_hash, binding_hash=wrong_binding_hash) is None
    leased = exchange.lease(state_hash=state_hash, binding_hash=binding_hash)
    assert leased is not None
    assert leased.id == transaction.id
    assert leased.context == TenantContext(TENANT_A)
    assert leased.principal_id == PRINCIPAL_A
    assert leased.connector_id == OAUTH_CONNECTOR
    assert leased.credential_intent_id == OAUTH_INTENT
    assert leased.nonce_hash == nonce_hash
    assert "oauth-binding" not in repr(leased)
    assert exchange.lease(state_hash=state_hash, binding_hash=binding_hash) is None
    assert not exchange.finalize(
        transaction.id, binding_hash=wrong_binding_hash, outcome="completed"
    )
    assert exchange.finalize(
        transaction.id, binding_hash=binding_hash, outcome="completed"
    )
    assert not exchange.finalize(
        transaction.id, binding_hash=binding_hash, outcome="completed"
    )

    psycopg = pytest.importorskip("psycopg")
    raw_exchange = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_oauth_exchange"]
    )()
    try:
        with raw_exchange.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT id FROM attune.oauth_transactions")
        raw_exchange.rollback()
    finally:
        raw_exchange.close()


def test_google_oauth_start_is_atomic_principal_bound_and_refuses_replacement(
    initialized_database, database_url
):
    repository = PostgresGoogleOAuthStartRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    context = TenantContext(TENANT_B)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    scopes = (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    )
    first = repository.start(
        context,
        principal_id=PRINCIPAL_B,
        state_hash=hashlib.sha256(b"start-state-one").digest(),
        binding_hash=hashlib.sha256(b"start-binding-one").digest(),
        nonce_hash=hashlib.sha256(b"start-nonce-one").digest(),
        pkce_verifier="v" * 64,
        redirect_uri="https://dev.attune.mumit.org/oauth/google/callback",
        scopes=scopes,
        expires_at=expires_at,
    )
    second = repository.start(
        context,
        principal_id=PRINCIPAL_B,
        state_hash=hashlib.sha256(b"start-state-two").digest(),
        binding_hash=hashlib.sha256(b"start-binding-two").digest(),
        nonce_hash=hashlib.sha256(b"start-nonce-two").digest(),
        pkce_verifier="w" * 64,
        redirect_uri="https://dev.attune.mumit.org/oauth/google/callback",
        scopes=scopes,
        expires_at=expires_at,
    )
    assert first.connector_id == second.connector_id
    assert first.credential_intent_id != second.credential_intent_id
    assert first.transaction_id != second.transaction_id
    assert not repository.is_connected(context, principal_id=PRINCIPAL_B)

    broker = PostgresSecretBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_secret_broker"])
    )
    leased = broker.lease(first.credential_intent_id, producer_kind="control_plane")
    assert leased is not None
    stored = broker.store(
        first.credential_intent_id,
        EncryptedCredential(
            b"ciphertext-with-tag",
            bytes(12),
            b"wrapped-dek",
            "projects/test/locations/test/keyRings/test/cryptoKeys/connectors",
        ),
        granted_scopes=scopes,
    )
    assert stored is not None
    assert repository.is_connected(context, principal_id=PRINCIPAL_B)
    assert (
        repository.active_connector(
            context, principal_id=PRINCIPAL_B, required_scopes=scopes
        )
        == first.connector_id
    )
    assert (
        repository.active_connector(
            context, principal_id=PRINCIPAL_B, required_scopes=scopes[:-1]
        )
        is None
    )
    assert (
        repository.active_connector(
            TenantContext(TENANT_A),
            principal_id=PRINCIPAL_B,
            required_scopes=scopes,
        )
        is None
    )
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT granted_scopes FROM attune.connectors "
            "WHERE tenant_id = %s AND id = %s",
            (TENANT_B, first.connector_id),
        )
        assert tuple(cursor.fetchone()[0]) == scopes
    with pytest.raises(RuntimeError, match="already connected"):
        repository.start(
            context,
            principal_id=PRINCIPAL_B,
            state_hash=hashlib.sha256(b"start-state-three").digest(),
            binding_hash=hashlib.sha256(b"start-binding-three").digest(),
            nonce_hash=hashlib.sha256(b"start-nonce-three").digest(),
            pkce_verifier="x" * 64,
            redirect_uri="https://dev.attune.mumit.org/oauth/google/callback",
            scopes=scopes,
            expires_at=expires_at,
        )

    revocations = PostgresGoogleConnectorRevocationRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    requested = revocations.request(
        context,
        principal_id=PRINCIPAL_B,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert requested is not None
    replay = revocations.request(
        context,
        principal_id=PRINCIPAL_B,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert replay == requested
    revoke_lease = broker.lease(
        requested.credential_intent_id, producer_kind="control_plane"
    )
    assert revoke_lease is not None
    assert revoke_lease.capability == "google.oauth.disconnect"
    assert broker.revoke(requested.credential_intent_id)
    assert not repository.is_connected(context, principal_id=PRINCIPAL_B)
    assert (
        revocations.request(
            context,
            principal_id=PRINCIPAL_B,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        is None
    )


def test_hosted_onboarding_is_tenant_bound_idempotent_and_server_seeded(
    initialized_database, database_url
):
    repository = PostgresHostedOnboardingRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    context = TenantContext(TENANT_A)
    assert repository.read(context, principal_id=PRINCIPAL_A) is None
    first = repository.start(context, principal_id=PRINCIPAL_A)
    second = repository.start(context, principal_id=PRINCIPAL_A)
    assert first == second
    assert first.schema_version == 1
    assert first.revision == 1
    assert first.workspace == "validated"
    assert first.status == "in_progress"
    assert repository.read(TenantContext(TENANT_B), principal_id=PRINCIPAL_A) is None
    with pytest.raises(RuntimeError, match="principal"):
        repository.start(context, principal_id=PRINCIPAL_B)


def test_identity_session_is_unambiguous_csrf_bound_and_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    issuer = "https://securetoken.google.com/attune-development-502421"
    subject_hash = hashlib.sha256(b"identity-platform-user").digest()
    with initialized_database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO attune.principals
                (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, %s)
            """,
            (TENANT_A, IDENTITY_PRINCIPAL_A, subject_hash, issuer),
        )
    initialized_database.commit()

    connection_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    sessions = PostgresIdentitySessionRepository(connection_factory)
    identity = VerifiedIdentity(
        issuer=issuer,
        subject_hash=subject_hash,
        authenticated_at=datetime.now(timezone.utc),
    )
    secrets = IdentitySessionSecrets(token="s" * 43, csrf="c" * 43)
    opened = sessions.open(
        identity,
        secrets,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
    )
    assert opened is not None
    assert opened.context == TenantContext(TENANT_A)
    assert opened.principal_id == IDENTITY_PRINCIPAL_A
    assert sessions.read(secrets.token) == opened
    assert sessions.authorize(secrets.token, "w" * 43) is None
    assert sessions.authorize(secrets.token, secrets.csrf) == opened
    assert sessions.authorize_recent(secrets.token, secrets.csrf) == opened
    stale_secrets = IdentitySessionSecrets(token="u" * 43, csrf="v" * 43)
    stale = sessions.open(
        identity,
        stale_secrets,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
    )
    assert stale is not None
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions "
            "SET created_at = clock_timestamp() - interval '11 minutes' "
            "WHERE tenant_id = %s AND id = %s",
            (TENANT_A, stale.id),
        )
    assert sessions.authorize(stale_secrets.token, stale_secrets.csrf) == stale
    assert sessions.authorize_recent(stale_secrets.token, stale_secrets.csrf) is None
    assert sessions.revoke(stale_secrets.token, stale_secrets.csrf) is True
    assert sessions.revoke(secrets.token, "w" * 43) is False
    assert sessions.revoke(secrets.token, secrets.csrf) is True
    assert sessions.read(secrets.token) is None

    control = connection_factory()
    try:
        with control.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT id FROM attune.identity_sessions")
        control.rollback()
    finally:
        control.close()

    with initialized_database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO attune.principals
                (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, %s)
            """,
            (TENANT_B, IDENTITY_PRINCIPAL_B, subject_hash, issuer),
        )
    initialized_database.commit()
    ambiguous = sessions.open(
        identity,
        IdentitySessionSecrets(token="t" * 43, csrf="d" * 43),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
    )
    assert ambiguous is None


def test_initial_identity_provisioning_is_idempotent_conflict_closed_and_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    subject_hash = hashlib.sha256(b"initial-identity").digest()
    issuer = "https://securetoken.google.com/attune-development-502421"
    connection = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_identity_provisioner"]
    )()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.provision_initial_identity(%s,%s,%s,%s)",
                (subject_hash, issuer, "bootstrap-test", "test-region1"),
            )
            first = cursor.fetchone()
        connection.commit()
        assert first[2] is True

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.provision_initial_identity(%s,%s,%s,%s)",
                (subject_hash, issuer, "bootstrap-test", "test-region1"),
            )
            repeated = cursor.fetchone()
        connection.commit()
        assert repeated[:2] == first[:2]
        assert repeated[2] is False

        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.UniqueViolation):
                cursor.execute(
                    "SELECT * FROM attune.provision_initial_identity(%s,%s,%s,%s)",
                    (subject_hash, issuer, "other-bootstrap", "test-region1"),
                )
        connection.rollback()

        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT id FROM attune.tenants")
        connection.rollback()
    finally:
        connection.close()


def test_hosted_signup_provisioning_is_idempotent_server_slugged_and_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    subject_hash = hashlib.sha256(b"hosted-signup-subject").digest()
    issuer = "https://securetoken.google.com/attune-development-502421"
    connection = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.provision_hosted_signup_tenant(%s,%s,%s)",
                (subject_hash, issuer, "test-region1"),
            )
            first = cursor.fetchone()
        connection.commit()
        assert first[2] is True
        tenant_id = first[0]

        # The control plane never chose the slug: the function derived it
        # from the tenant id it created, never from caller input (there is
        # no slug parameter at all).
        with initialized_database.cursor() as cursor:
            cursor.execute(
                "SELECT slug FROM attune.tenants WHERE id = %s", (tenant_id,)
            )
            (slug,) = cursor.fetchone()
        assert slug == "tn-" + str(tenant_id).replace("-", "")

        # Exact replay by the same subject is idempotent, not a second tenant.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.provision_hosted_signup_tenant(%s,%s,%s)",
                (subject_hash, issuer, "test-region1"),
            )
            repeated = cursor.fetchone()
        connection.commit()
        assert repeated[:2] == first[:2]
        assert repeated[2] is False

        # It cannot join or alter any other tenant: there is no tenant
        # identifier this ceremony ever accepts as input. Two assertions
        # about the OPERATOR ceremony for the same subject: (1) the control
        # plane's runtime role cannot even invoke it — that function is
        # granted only to the operator-job identity, and the denial IS the
        # boundary; (2) a privileged caller attempting it under a different
        # slug is refused with a conflict, not silently merged into the
        # signup tenant.
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "SELECT * FROM attune.provision_initial_identity(%s,%s,%s,%s)",
                    (subject_hash, issuer, "operator-conflict-test", "test-region1"),
                )
        connection.rollback()
        with initialized_database.cursor() as cursor:
            with pytest.raises(psycopg.errors.UniqueViolation):
                cursor.execute(
                    "SELECT * FROM attune.provision_initial_identity(%s,%s,%s,%s)",
                    (subject_hash, issuer, "operator-conflict-test", "test-region1"),
                )
        initialized_database.rollback()

        # Function-only: the control plane's runtime role has no direct
        # table authority, only EXECUTE on the function.
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT id FROM attune.tenants")
        connection.rollback()
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT id FROM attune.principals")
        connection.rollback()
    finally:
        connection.close()


def test_customer_export_request_and_claim_are_fixed_recent_and_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    session_id = UUID("10000000-0000-4000-8000-000000000093")
    request_key = hashlib.sha256(b"export-request-a").digest()
    run_id = UUID("10000000-0000-4000-8000-000000000094")
    _reset_role(initialized_database)
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            """
            INSERT INTO attune.hosted_onboarding_states
                (tenant_id, owner_principal_id)
            VALUES (%s, %s)
            ON CONFLICT (tenant_id) DO NOTHING
            """,
            (TENANT_A, PRINCIPAL_A),
        )
        cursor.execute(
            "SELECT owner_principal_id FROM attune.hosted_onboarding_states "
            "WHERE tenant_id = %s",
            (TENANT_A,),
        )
        assert cursor.fetchone() == (PRINCIPAL_A,)
        cursor.execute(
            """
            INSERT INTO attune.policies
                (tenant_id, version, document, active, created_by)
            VALUES (%s, 777, %s::jsonb, false, %s)
            """,
            (TENANT_A, '{"api_key":"must-not-be-projected"}', PRINCIPAL_A),
        )
        cursor.execute(
            """
            INSERT INTO attune.identity_sessions
                (tenant_id, id, principal_id, token_hash, csrf_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp() + interval '1 hour')
            """,
            (
                TENANT_A,
                session_id,
                PRINCIPAL_A,
                hashlib.sha256(b"export-token").digest(),
                hashlib.sha256(b"export-csrf").digest(),
            ),
        )

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.request_customer_export(%s,%s,%s,%s)",
                (PRINCIPAL_A, session_id, "account", request_key),
            )
            requested = cursor.fetchone()
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.request_customer_export(%s,%s,%s,%s)",
                (PRINCIPAL_A, session_id, "account", request_key),
            )
            assert cursor.fetchone()[0] == requested[0]
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                cursor.execute(
                    "SELECT * FROM attune.request_customer_export(%s,%s,%s,%s)",
                    (PRINCIPAL_A, session_id, "all_tables", hashlib.sha256(b"bad").digest()),
                )
        control.rollback()
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "UPDATE attune.export_jobs SET state = 'failed' WHERE id = %s",
                    (requested[0],),
                )
        control.rollback()
    finally:
        control.close()

    task = PostgresDispatchProducerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"]),
        producer_kind="control_plane",
    ).enqueue(
        TenantContext(TENANT_A),
        kind="customer.export.generate",
        capability="customer.export.generate",
        payload={"export_id": str(requested[0])},
        idempotency_key=hashlib.sha256(b"export-task-a").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    task_broker = PostgresDispatchBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_dispatch_broker"])
    )
    assert task_broker.lease(
        task.intent.id, producer_kind="control_plane"
    ) is not None
    assert task_broker.finalize(
        task.intent.id, producer_kind="control_plane", outcome="dispatched"
    )

    executor = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_export"]
    )()
    object_id = UUID("10000000-0000-4000-8000-000000000097")
    try:
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_task(%s,%s,%s)",
                (TENANT_B, task.job.id, task.intent.delivery_id),
            )
            assert cursor.fetchone() is None
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_task(%s,%s,%s)",
                (TENANT_A, task.job.id, task.intent.delivery_id),
            )
            assert cursor.fetchone() == (requested[0], "claimed")
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_for_tenant(%s,%s,%s)",
                (TENANT_A, requested[0], run_id),
            )
            claimed = cursor.fetchone()
        executor.commit()
        assert claimed[:4] == (TENANT_A, requested[0], PRINCIPAL_A, "account")
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.reserve_customer_export_object(%s,%s,%s)",
                (requested[0], run_id, object_id),
            )
            reservation = cursor.fetchone()
        executor.commit()
        assert reservation == (object_id, requested[3])
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.reserve_customer_export_object(%s,%s,%s)",
                (
                    requested[0], run_id,
                    UUID("10000000-0000-4000-8000-000000000098"),
                ),
            )
            assert cursor.fetchone() == reservation
        executor.commit()
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.read_customer_export_records(%s,%s) "
                "ORDER BY sort_key",
                (requested[0], run_id),
            )
            projected = cursor.fetchall()
        executor.commit()
        assert projected
        assert {row[0] for row in projected} == {"account.jsonl"}
        assert [row[1] for row in projected] == sorted(row[1] for row in projected)
        records = [row[2] for row in projected]
        assert {record["kind"] for record in records} >= {"tenant", "principal"}
        policy_record = next(record for record in records if record["kind"] == "policy")
        assert set(policy_record["data"]) == {
            "id", "version", "active", "created_at"
        }
        connector_records = [
            record for record in records if record["kind"] == "connector"
        ]
        assert connector_records
        assert all("credential_ref" not in record["data"] for record in connector_records)
        archive = build_export_archive(
            export_id=requested[0],
            scope="account",
            requested_at=requested[3],
            generated_at=datetime.now(timezone.utc),
            records=records,
        )
        assert archive.manifest["members"][0]["records"] == len(records)
        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "SELECT * FROM attune.read_customer_export_records(%s,%s)",
                    (
                        requested[0],
                        UUID("10000000-0000-4000-8000-000000000096"),
                    ),
                )
        executor.rollback()
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export(%s,%s)",
                (requested[0], UUID("10000000-0000-4000-8000-000000000095")),
            )
            assert cursor.fetchone() is None
        executor.commit()
        completion = (
            requested[0], run_id, object_id, 7, b"w" * 64, b"n" * 12,
            "projects/test/locations/test/keyRings/test/cryptoKeys/customer-export",
            hashlib.sha256(b"archive").digest(),
            hashlib.sha256(b"ciphertext").digest(), 123, 139, 1,
        )
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.complete_customer_export("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                completion,
            )
            ready = cursor.fetchone()
        executor.commit()
        assert ready[:2] == (requested[0], "ready")
        assert ready[2] <= datetime.now(timezone.utc) + timedelta(hours=24)
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.complete_customer_export("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                completion,
            )
            assert cursor.fetchone() == ready
        executor.commit()
        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "SELECT * FROM attune.complete_customer_export("
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*completion[:3], 8, *completion[4:]),
                )
        executor.rollback()
        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM attune.export_jobs")
        executor.rollback()
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT attune.finish_customer_export_task(%s,%s,%s)",
                (TENANT_A, task.job.id, task.intent.delivery_id),
            )
            assert cursor.fetchone() == ("succeeded",)
        executor.commit()
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_task(%s,%s,%s)",
                (TENANT_A, task.job.id, task.intent.delivery_id),
            )
            assert cursor.fetchone() == (requested[0], "succeeded")
        executor.commit()
        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM attune.jobs")
        executor.rollback()
    finally:
        executor.close()

    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "SELECT action, outcome FROM attune.audit_intents "
            "WHERE target_ref_hash = %s ORDER BY created_at",
            (hashlib.sha256(str(requested[0]).encode()).digest(),),
        )
        # Pre-existing gap fixed in passing (Phase 5 stage 4): this query had
        # no ORDER BY, relying on undefined physical row order -- harmless
        # while few rows existed ahead of it in the shared TENANT_A fixture,
        # but exposed as soon as an earlier test in this module (this
        # stage's own hosted-brief test) added enough preceding audit_intents
        # volume for the planner to stop returning rows in insertion order.
        assert cursor.fetchall() == [
            ("export.requested", "observed"),
            ("export.claimed", "observed"),
            ("export.ready", "observed"),
        ]

    expiry_run = UUID("10000000-0000-4000-8000-000000000099")
    cleanup = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_export_cleanup"]
    )()
    try:
        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_expirations(%s,%s)",
                (expiry_run, 10),
            )
            assert cursor.fetchall() == []
        cleanup.commit()

        _reset_role(initialized_database)
        with initialized_database.cursor() as cursor:
            cursor.execute(
                "UPDATE attune.export_jobs "
                "SET ready_at = clock_timestamp() - interval '2 seconds', "
                "expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = %s",
                (requested[0],),
            )
        initialized_database.commit()

        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_expirations(%s,%s)",
                (expiry_run, 10),
            )
            assert cursor.fetchall() == [(TENANT_A, requested[0], object_id, 7)]
        cleanup.commit()
        with cleanup.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "SELECT attune.complete_customer_export_expiration(%s,%s,%s,%s)",
                    (requested[0], object_id, 8, expiry_run),
                )
        cleanup.rollback()
        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT attune.complete_customer_export_expiration(%s,%s,%s,%s)",
                (requested[0], object_id, 7, expiry_run),
            )
            assert cursor.fetchone() == (True,)
        cleanup.commit()
        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT attune.complete_customer_export_expiration(%s,%s,%s,%s)",
                (requested[0], object_id, 7, expiry_run),
            )
            assert cursor.fetchone() == (False,)
        cleanup.commit()
        with cleanup.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM attune.export_jobs")
        cleanup.rollback()
    finally:
        cleanup.close()

    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT state, object_ref, object_generation, wrapped_dek, nonce, "
            "key_resource, archive_sha256, ciphertext_sha256, archive_bytes, "
            "ciphertext_bytes, encryption_format, ready_at, "
            "expiry_cleanup_run_id FROM attune.export_jobs WHERE id = %s",
            (requested[0],),
        )
        expired = cursor.fetchone()
        assert expired[0] == "expired"
        assert expired[1:] == (None,) * 12
        cursor.execute(
            "SELECT cleanup_pending, cleaned_at IS NOT NULL "
            "FROM attune.export_object_attempts WHERE export_id = %s",
            (requested[0],),
        )
        assert cursor.fetchone() == (False, True)
        cursor.execute(
            "SELECT action, outcome, metadata FROM attune.audit_intents "
            "WHERE target_ref_hash = %s ORDER BY created_at DESC LIMIT 1",
            (hashlib.sha256(str(requested[0]).encode()).digest(),),
        )
        assert cursor.fetchone() == (
            "export.expired", "observed", {"records": 1}
        )
    initialized_database.commit()


def test_customer_export_projection_refuses_a_claim_without_current_owner_status(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    session_id = UUID("20000000-0000-4000-8000-000000000093")
    run_id = UUID("20000000-0000-4000-8000-000000000094")
    _reset_role(initialized_database)
    with tenant_transaction(initialized_database, TenantContext(TENANT_B)) as cursor:
        cursor.execute(
            """
            INSERT INTO attune.identity_sessions
                (tenant_id, id, principal_id, token_hash, csrf_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp() + interval '1 hour')
            """,
            (
                TENANT_B,
                session_id,
                PRINCIPAL_B,
                hashlib.sha256(b"export-token-b").digest(),
                hashlib.sha256(b"export-csrf-b").digest(),
            ),
        )

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_B)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.request_customer_export(%s,%s,%s,%s)",
                (
                    PRINCIPAL_B,
                    session_id,
                    "memories",
                    hashlib.sha256(b"unowned-export-request").digest(),
                ),
            )
            export_id = cursor.fetchone()[0]
    finally:
        control.close()

    executor = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_export"]
    )()
    try:
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export(%s,%s)",
                (export_id, run_id),
            )
            assert cursor.fetchone() is not None
        executor.commit()
        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "SELECT * FROM attune.read_customer_export_records(%s,%s)",
                    (export_id, run_id),
                )
        executor.rollback()
    finally:
        executor.close()


def test_customer_export_control_plane_and_one_time_download_are_exact(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    session_id = UUID("10000000-0000-4000-8000-0000000000d1")
    export_id = None
    object_id = UUID("10000000-0000-4000-8000-0000000000d2")
    grant_hash = hashlib.sha256(b"one-time-download-secret").digest()
    grant_run = UUID("10000000-0000-4000-8000-0000000000d3")
    _reset_role(initialized_database)
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "INSERT INTO attune.identity_sessions "
            "(tenant_id,id,principal_id,token_hash,csrf_hash,expires_at) "
            "VALUES (%s,%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",
            (
                TENANT_A, session_id, PRINCIPAL_A,
                hashlib.sha256(b"download-token").digest(),
                hashlib.sha256(b"download-csrf").digest(),
            ),
        )

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.request_or_read_customer_export(%s,%s,%s,%s)",
                (
                    PRINCIPAL_A, session_id, "conversations",
                    hashlib.sha256(b"download-export-request").digest(),
                ),
            )
            requested = cursor.fetchone()
            assert requested[4] is True
            export_id = requested[0]
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.request_or_read_customer_export(%s,%s,%s,%s)",
                (
                    PRINCIPAL_A, session_id, "conversations",
                    hashlib.sha256(b"double-click-request").digest(),
                ),
            )
            assert cursor.fetchone() == (*requested[:4], False)
            cursor.execute(
                "SELECT export_id,scope_name,export_state "
                "FROM attune.list_customer_exports(%s,%s)",
                (PRINCIPAL_A, 20),
            )
            assert (export_id, "conversations", "requested") in cursor.fetchall()
    finally:
        control.close()

    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "UPDATE attune.export_jobs SET state='ready', object_ref=%s, "
            "object_generation=601, wrapped_dek=%s, nonce=%s, key_resource=%s, "
            "archive_sha256=%s, ciphertext_sha256=%s, archive_bytes=100, "
            "ciphertext_bytes=116, encryption_format=1, ready_at=clock_timestamp(), "
            "expires_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
            (
                object_id, b"w" * 64, b"n" * 12,
                "projects/test/locations/test/keyRings/test/cryptoKeys/customer-export",
                hashlib.sha256(b"plain").digest(),
                hashlib.sha256(b"cipher").digest(), export_id,
            ),
        )
    initialized_database.commit()

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.issue_customer_export_download(%s,%s,%s,%s)",
                (PRINCIPAL_A, session_id, export_id, grant_hash),
            )
            grant_id, grant_expires = cursor.fetchone()
        assert grant_expires <= datetime.now(timezone.utc) + timedelta(seconds=90)
        with tenant_transaction(control, TenantContext(TENANT_B)) as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "SELECT * FROM attune.issue_customer_export_download(%s,%s,%s,%s)",
                    (PRINCIPAL_A, session_id, export_id, hashlib.sha256(b"cross").digest()),
                )
        control.rollback()
    finally:
        control.close()

    download = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_export_download"]
    )()
    try:
        with download.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_download(%s,%s,%s)",
                (grant_id, hashlib.sha256(b"wrong").digest(), grant_run),
            )
            assert cursor.fetchone() is None
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_download(%s,%s,%s)",
                (grant_id, grant_hash, grant_run),
            )
            claimed = cursor.fetchone()
        download.commit()
        assert claimed[:5] == (TENANT_A, export_id, "conversations", object_id, 601)
        with download.cursor() as cursor:
            cursor.execute(
                "SELECT attune.finish_customer_export_download(%s,%s,%s)",
                (grant_id, export_id, UUID(int=999)),
            )
            assert cursor.fetchone() == (False,)
            cursor.execute(
                "SELECT attune.finish_customer_export_download(%s,%s,%s)",
                (grant_id, export_id, grant_run),
            )
            assert cursor.fetchone() == (True,)
        download.commit()
        with download.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_download(%s,%s,%s)",
                (grant_id, grant_hash, UUID(int=1000)),
            )
            assert cursor.fetchone() is None
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM attune.export_jobs")
        download.rollback()
    finally:
        download.close()

    cleanup_run = UUID("10000000-0000-4000-8000-0000000000d4")
    cleanup = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_export_cleanup"]
    )()
    try:
        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_expirations(%s,%s)",
                (cleanup_run, 20),
            )
            assert (TENANT_A, export_id, object_id, 601) in cursor.fetchall()
            cursor.execute(
                "SELECT attune.complete_customer_export_expiration(%s,%s,%s,%s)",
                (export_id, object_id, 601, cleanup_run),
            )
            assert cursor.fetchone() == (True,)
        cleanup.commit()
    finally:
        cleanup.close()

    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT state,object_ref,wrapped_dek FROM attune.export_jobs WHERE id=%s",
            (export_id,),
        )
        assert cursor.fetchone() == ("consumed", None, None)
    initialized_database.commit()


def test_customer_export_download_grant_is_consumed_by_exactly_one_of_two_racers(
    initialized_database, database_url
):
    # Function-owned one-use consumption must hold under real concurrency,
    # not merely under a single-threaded call sequence: race two independent
    # connections claiming the identical grant and prove exactly one obtains
    # plaintext-bound metadata while the other gets the same fixed refusal
    # (None) a wrong secret or a replay would produce.
    session_id = UUID("10000000-0000-4000-8000-0000000000e1")
    object_id = UUID("10000000-0000-4000-8000-0000000000e2")
    export_id = None
    grant_secret = b"race-the-one-time-download-secret"
    grant_hash = hashlib.sha256(grant_secret).digest()
    _reset_role(initialized_database)
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            "INSERT INTO attune.identity_sessions "
            "(tenant_id,id,principal_id,token_hash,csrf_hash,expires_at) "
            "VALUES (%s,%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",
            (
                TENANT_A, session_id, PRINCIPAL_A,
                hashlib.sha256(b"race-token").digest(),
                hashlib.sha256(b"race-csrf").digest(),
            ),
        )

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.request_or_read_customer_export(%s,%s,%s,%s)",
                (
                    PRINCIPAL_A, session_id, "memories",
                    hashlib.sha256(b"race-export-request").digest(),
                ),
            )
            export_id = cursor.fetchone()[0]
    finally:
        control.close()

    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "UPDATE attune.export_jobs SET state='ready', object_ref=%s, "
            "object_generation=701, wrapped_dek=%s, nonce=%s, key_resource=%s, "
            "archive_sha256=%s, ciphertext_sha256=%s, archive_bytes=100, "
            "ciphertext_bytes=116, encryption_format=1, ready_at=clock_timestamp(), "
            "expires_at=clock_timestamp()+interval '1 hour' WHERE id=%s",
            (
                object_id, b"w" * 64, b"n" * 12,
                "projects/test/locations/test/keyRings/test/cryptoKeys/customer-export",
                hashlib.sha256(b"race-plain").digest(),
                hashlib.sha256(b"race-cipher").digest(), export_id,
            ),
        )
    initialized_database.commit()

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.issue_customer_export_download(%s,%s,%s,%s)",
                (PRINCIPAL_A, session_id, export_id, grant_hash),
            )
            grant_id, _grant_expires = cursor.fetchone()
    finally:
        control.close()

    def claim_with_own_connection(run_id: UUID):
        connection = _role_connection_factory(
            database_url, ROLE_BINDINGS["attune_export_download"]
        )()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM attune.claim_customer_export_download(%s,%s,%s)",
                    (grant_id, grant_hash, run_id),
                )
                row = cursor.fetchone()
            connection.commit()
            return row
        finally:
            connection.close()

    run_ids = (
        UUID("10000000-0000-4000-8000-0000000000e3"),
        UUID("10000000-0000-4000-8000-0000000000e4"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim_with_own_connection, run_ids))

    winners = [row for row in results if row is not None]
    losers = [row for row in results if row is None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0][:5] == (TENANT_A, export_id, "memories", object_id, 701)

    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT lease_run_id, consumed_at IS NOT NULL "
            "FROM attune.export_download_grants WHERE id = %s",
            (grant_id,),
        )
        lease_run_id, consumed = cursor.fetchone()
    initialized_database.commit()
    assert consumed is False
    assert lease_run_id in run_ids


def test_customer_export_expired_claim_is_recoverable_and_failure_is_exact(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    session_id = UUID("10000000-0000-4000-8000-0000000000a1")
    first_run = UUID("10000000-0000-4000-8000-0000000000a2")
    replacement_run = UUID("10000000-0000-4000-8000-0000000000a3")
    object_id = UUID("10000000-0000-4000-8000-0000000000a4")
    _reset_role(initialized_database)
    with tenant_transaction(initialized_database, TenantContext(TENANT_A)) as cursor:
        cursor.execute(
            """
            INSERT INTO attune.identity_sessions
                (tenant_id, id, principal_id, token_hash, csrf_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp() + interval '1 hour')
            """,
            (
                TENANT_A, session_id, PRINCIPAL_A,
                hashlib.sha256(b"export-recovery-token").digest(),
                hashlib.sha256(b"export-recovery-csrf").digest(),
            ),
        )

    control = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )()
    try:
        with tenant_transaction(control, TenantContext(TENANT_A)) as cursor:
            cursor.execute(
                "SELECT * FROM attune.request_customer_export(%s,%s,%s,%s)",
                (
                    PRINCIPAL_A, session_id, "activity",
                    hashlib.sha256(b"export-recovery-request").digest(),
                ),
            )
            export_id = cursor.fetchone()[0]
    finally:
        control.close()

    executor = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_export"]
    )()
    try:
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export(%s,%s)",
                (export_id, first_run),
            )
            assert cursor.fetchone() is not None
            cursor.execute(
                "SELECT * FROM attune.reserve_customer_export_object(%s,%s,%s)",
                (export_id, first_run, object_id),
            )
            assert cursor.fetchone()[0] == object_id
        executor.commit()

        _reset_role(initialized_database)
        with initialized_database.cursor() as cursor:
            cursor.execute(
                "UPDATE attune.export_jobs "
                "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = %s",
                (export_id,),
            )
        initialized_database.commit()

        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export(%s,%s)",
                (export_id, replacement_run),
            )
            assert cursor.fetchone() is not None
            cursor.execute(
                "SELECT * FROM attune.list_customer_export_cleanup_objects(%s,%s)",
                (export_id, replacement_run),
            )
            assert cursor.fetchall() == [(first_run, object_id)]
            replacement_object_id = UUID(
                "10000000-0000-4000-8000-0000000000a5"
            )
            cursor.execute(
                "SELECT * FROM attune.reserve_customer_export_object(%s,%s,%s)",
                (export_id, replacement_run, replacement_object_id),
            )
            assert cursor.fetchone()[0] == replacement_object_id
        executor.commit()

        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "SELECT * FROM attune.fail_customer_export(%s,%s,%s)",
                    (export_id, first_run, "upload_failed"),
                )
        executor.rollback()
        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                cursor.execute(
                    "SELECT * FROM attune.fail_customer_export(%s,%s,%s)",
                    (export_id, replacement_run, "raw_exception_text"),
                )
        executor.rollback()
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.fail_customer_export(%s,%s,%s)",
                (export_id, replacement_run, "upload_failed"),
            )
            failed = cursor.fetchone()
        executor.commit()
        assert failed == (export_id, "failed", "upload_failed")
        with executor.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.fail_customer_export(%s,%s,%s)",
                (export_id, replacement_run, "upload_failed"),
            )
            assert cursor.fetchone() == failed
        executor.commit()
        with executor.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM attune.export_jobs")
        executor.rollback()
    finally:
        executor.close()

    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT state, object_ref, failure_code, failure_run_id "
            "FROM attune.export_jobs WHERE id = %s",
            (export_id,),
        )
        assert cursor.fetchone() == (
            "failed", None, "upload_failed", replacement_run
        )
        cursor.execute(
            "SELECT run_id, object_ref, cleanup_pending "
            "FROM attune.export_object_attempts WHERE export_id = %s "
            "ORDER BY created_at, run_id",
            (export_id,),
        )
        assert cursor.fetchall() == [
            (first_run, object_id, True),
            (replacement_run, replacement_object_id, False),
        ]
        cursor.execute(
            "SELECT action, outcome, metadata FROM attune.audit_intents "
            "WHERE target_ref_hash = %s ORDER BY created_at",
            (hashlib.sha256(str(export_id).encode()).digest(),),
        )
        audit = cursor.fetchall()
    initialized_database.commit()
    assert [row[:2] for row in audit] == [
        ("export.requested", "observed"),
        ("export.claimed", "observed"),
        ("export.claimed", "observed"),
        ("export.failed", "failed"),
    ]
    assert audit[-1][2] == {
        "scope": "activity", "failure_code": "upload_failed"
    }

    cleanup_run = UUID("10000000-0000-4000-8000-0000000000a6")
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "UPDATE attune.export_object_attempts "
            "SET created_at = clock_timestamp() - interval '16 minutes' "
            "WHERE export_id = %s AND run_id = %s",
            (export_id, first_run),
        )
    initialized_database.commit()
    cleanup = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_export_cleanup"]
    )()
    try:
        with cleanup.cursor() as cursor:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                cursor.execute(
                    "SELECT * FROM attune.claim_customer_export_attempt_cleanups(%s,%s)",
                    (cleanup_run, 101),
                )
        cleanup.rollback()
        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.claim_customer_export_attempt_cleanups(%s,%s)",
                (cleanup_run, 10),
            )
            assert cursor.fetchall() == [(TENANT_A, export_id, first_run, object_id)]
        cleanup.commit()
        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT attune.complete_customer_export_attempt_cleanup(%s,%s,%s)",
                (export_id, first_run, cleanup_run),
            )
            assert cursor.fetchone() == (True,)
        cleanup.commit()
        with cleanup.cursor() as cursor:
            cursor.execute(
                "SELECT attune.complete_customer_export_attempt_cleanup(%s,%s,%s)",
                (export_id, first_run, cleanup_run),
            )
            assert cursor.fetchone() == (False,)
        cleanup.commit()
        with cleanup.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT * FROM attune.export_object_attempts")
        cleanup.rollback()
    finally:
        cleanup.close()
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT cleanup_pending, cleaned_at IS NOT NULL "
            "FROM attune.export_object_attempts "
            "WHERE export_id = %s AND run_id = %s",
            (export_id, first_run),
        )
        assert cursor.fetchone() == (False, True)
        cursor.execute(
            "SELECT action, metadata FROM attune.audit_intents "
            "WHERE target_ref_hash = %s ORDER BY created_at DESC LIMIT 1",
            (hashlib.sha256(str(export_id).encode()).digest(),),
        )
        assert cursor.fetchone() == ("export.attempt.cleaned", {"records": 1})
    initialized_database.commit()


def test_protocol_retention_prunes_only_expired_records_and_audits_per_tenant(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    installation_b = UUID("20000000-0000-4000-8000-000000000090")
    old_event = UUID("20000000-0000-4000-8000-000000000091")
    recent_event = UUID("20000000-0000-4000-8000-000000000092")
    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO attune.installations
                (tenant_id, id, provider, kind, external_ref_hash)
            VALUES (%s, %s, 'google', 'workspace', %s)
            ON CONFLICT (tenant_id, id) DO NOTHING
            """,
            (
                TENANT_B,
                installation_b,
                hashlib.sha256(b"retention-installation-b").digest(),
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.provider_events
                (tenant_id, id, installation_id, provider, kind,
                 deduplication_key, signal, processed_at)
            VALUES
                (%s, %s, %s, 'google', 'retention-old', %s, '{}',
                 clock_timestamp() - interval '8 days'),
                (%s, %s, %s, 'google', 'retention-recent', %s, '{}',
                 clock_timestamp() - interval '6 days')
            """,
            (
                TENANT_B,
                old_event,
                installation_b,
                hashlib.sha256(b"retention-old").digest(),
                TENANT_B,
                recent_event,
                installation_b,
                hashlib.sha256(b"retention-recent").digest(),
            ),
        )
    initialized_database.commit()

    retention = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_retention"]
    )()
    try:
        with retention.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT id FROM attune.provider_events")
        retention.rollback()
        with retention.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.prune_expired_protocol_records(%s, %s)",
                (UUID("20000000-0000-4000-8000-000000000093"), 100),
            )
            counts = cursor.fetchone()
        retention.commit()
        assert counts[3] >= 1
        with retention.cursor() as cursor:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                cursor.execute(
                    "SELECT * FROM attune.prune_expired_protocol_records(%s, 0)",
                    (UUID("20000000-0000-4000-8000-000000000094"),),
                )
        retention.rollback()
    finally:
        retention.close()

    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM attune.provider_events WHERE tenant_id = %s "
            "AND id IN (%s, %s) ORDER BY id",
            (TENANT_B, old_event, recent_event),
        )
        assert cursor.fetchall() == [(recent_event,)]
        cursor.execute(
            "SELECT producer_kind, action, metadata FROM attune.audit_intents "
            "WHERE tenant_id = %s "
            "AND action = 'retention.provider_events.pruned'",
            (TENANT_B,),
        )
        audit = cursor.fetchone()
        assert audit[0:2] == (
            "retention",
            "retention.provider_events.pruned",
        )
        assert audit[2]["records"] >= 1
    initialized_database.commit()


def test_slack_install_delivery_conversation_and_lifecycle_are_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    from attune.hosted.slack_channel_broker import (
        PostgresSlackChannelBrokerRepository,
    )
    from attune.hosted.slack_conversation_executor import (
        PostgresSlackConversationWorkRepository,
    )

    context = TenantContext(CHANNEL_TENANT)
    setups = PostgresHostedChannelSetupRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_control_plane"])
    )
    broker = PostgresSlackChannelBrokerRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_channel_broker"])
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
    )
    key_resource = "projects/test/locations/test/keyRings/test/cryptoKeys/test"

    def envelope(seed: bytes) -> EncryptedCredential:
        return EncryptedCredential(
            ciphertext=seed * 32,
            nonce=b"n" * 12,
            wrapped_dek=b"w" * 32,
            key_resource=key_resource,
        )

    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions SET created_at = clock_timestamp() "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, CHANNEL_SESSION),
        )
    state_secret = b"slack-install-state"
    started = setups.begin(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="slack",
        mechanism="oauth",
        secret_hash=hashlib.sha256(state_secret).digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=9),
    )
    assert started.state == "pending" and started.mechanism == "oauth"

    claim_hash = hashlib.sha256(b"slack-install-claim").digest()
    claim = broker.claim(
        state_hash=hashlib.sha256(state_secret).digest(),
        claim_hash=claim_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert claim.tenant_id == CHANNEL_TENANT
    assert claim.owner_principal_id == CHANNEL_PRINCIPAL
    assert writer.write(claim.pre_audit_intent_id) is not None
    destination_id = broker.resolve_destination_id(
        state_hash=hashlib.sha256(state_secret).digest(),
        claim_hash=claim_hash,
        candidate_id=UUID("30000000-0000-4000-8000-000000000201"),
    )
    installation_hash = hashlib.sha256(b"slack-team").digest()
    actor_hash = hashlib.sha256(b"slack-owner").digest()
    destination_hash = hashlib.sha256(b"slack-owner-dm").digest()
    with pytest.raises(psycopg.errors.NoDataFound):
        broker.consume(
            state_hash=hashlib.sha256(state_secret).digest(),
            claim_hash=claim_hash,
            owner_tenant_id=CHANNEL_TENANT,
            owner_principal_id=UUID("30000000-0000-4000-8000-000000000299"),
            installation_ref_hash=installation_hash,
            actor_ref_hash=actor_hash,
            destination_ref_hash=destination_hash,
            destination_id=destination_id,
            encrypted_route=envelope(b"r"),
            encrypted_token=envelope(b"t"),
        )
    installed = broker.consume(
        state_hash=hashlib.sha256(state_secret).digest(),
        claim_hash=claim_hash,
        owner_tenant_id=CHANNEL_TENANT,
        owner_principal_id=CHANNEL_PRINCIPAL,
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        destination_id=destination_id,
        encrypted_route=envelope(b"r"),
        encrypted_token=envelope(b"t"),
    )
    assert installed.destination_status == "pending_test"
    assert writer.write(installed.outcome_audit_intent_id) is not None
    with pytest.raises(psycopg.errors.NoDataFound):
        broker.claim(
            state_hash=hashlib.sha256(state_secret).digest(),
            claim_hash=hashlib.sha256(b"slack-replay").digest(),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )

    delivery_claim = hashlib.sha256(b"slack-delivery-claim").digest()
    delivery = broker.claim_delivery(
        destination_id=installed.destination_id,
        claim_hash=delivery_claim,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert delivery.encrypted_route.ciphertext == b"r" * 32
    assert delivery.encrypted_token.ciphertext == b"t" * 32
    assert writer.write(delivery.pre_audit_intent_id) is not None
    completed = broker.complete_delivery(
        destination_id=installed.destination_id,
        claim_hash=delivery_claim,
        succeeded=True,
    )
    assert completed.destination_status == "active"
    assert writer.write(completed.outcome_audit_intent_id) is not None

    message_hash = hashlib.sha256(b"slack-owner-message").digest()
    accepted = broker.accept_message(
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        message_ref_hash=message_hash,
        text="What is on my calendar tomorrow?",
    )
    assert accepted.accepted_new is True
    assert writer.write(accepted.pre_audit_intent_id) is not None
    replayed = broker.accept_message(
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        message_ref_hash=message_hash,
        text="What is on my calendar tomorrow?",
    )
    assert replayed.accepted_new is False
    assert replayed.dispatch_intent_id == accepted.dispatch_intent_id

    # The fixed Slack acknowledgment resolves the same active, owner-DM
    # destination by reference hash, and is idempotent per provider message:
    # a retried Slack event must win the claim at most once.
    ack_claim = broker.claim_acknowledgment(
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        message_ref_hash=message_hash,
    )
    assert ack_claim.won is True
    assert ack_claim.destination_id == installed.destination_id
    assert ack_claim.encrypted_route.ciphertext == b"r" * 32
    assert ack_claim.encrypted_token.ciphertext == b"t" * 32
    assert writer.write(ack_claim.pre_audit_intent_id) is not None
    completed_ack = broker.complete_acknowledgment(
        message_ref_hash=message_hash, succeeded=True
    )
    assert writer.write(completed_ack.outcome_audit_intent_id) is not None
    replay_ack = broker.claim_acknowledgment(
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        message_ref_hash=message_hash,
    )
    assert replay_ack.won is False
    assert replay_ack.destination_id == installed.destination_id
    assert replay_ack.encrypted_route is None and replay_ack.encrypted_token is None

    direct_ack = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_channel_broker"]
    )()
    try:
        with direct_ack.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT * FROM attune.audit_intents")
        direct_ack.rollback()
    finally:
        direct_ack.close()

    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "SELECT id, (payload->>'conversation_id')::uuid FROM attune.jobs "
            "WHERE tenant_id = %s AND kind = 'channel.slack.converse'",
            (CHANNEL_TENANT,),
        )
        rows = cursor.fetchall()
        assert len(rows) == 1
        job_id, conversation_id = rows[0]
        cursor.execute(
            "UPDATE attune.jobs SET state = 'leased', attempts = 1, "
            "lease_expires_at = clock_timestamp() + interval '5 minutes' "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, job_id),
        )
        cursor.execute(
            """
            INSERT INTO attune.conversation_turns
                (tenant_id, conversation_id, sequence, actor_type, content,
                 provenance)
            VALUES (%s, %s, 2, 'assistant', 'Canonical Slack answer',
                    jsonb_build_object('schema_version', 1, 'job_id', %s::text))
            """,
            (CHANNEL_TENANT, conversation_id, job_id),
        )
    initialized_database.commit()

    worker_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_worker"]
    )
    canonical_job = PostgresJobRepository(worker_factory).get(context, job_id)
    work = PostgresSlackConversationWorkRepository(worker_factory).resolve(
        context, canonical_job
    )
    assert work.destination_id == installed.destination_id

    reply_claim = hashlib.sha256(b"slack-reply-claim").digest()
    reply = broker.claim_conversation_delivery(
        destination_id=installed.destination_id,
        job_id=job_id,
        claim_hash=reply_claim,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert reply.reply_text == "Canonical Slack answer"
    assert reply.encrypted_token.ciphertext == b"t" * 32
    assert writer.write(reply.pre_audit_intent_id) is not None
    delivered = broker.complete_conversation_delivery(
        job_id=job_id,
        claim_hash=reply_claim,
        succeeded=True,
        provider_message_ref_hash=hashlib.sha256(b"slack-provider-ts").digest(),
    )
    assert delivered.delivery_state == "delivered"
    assert writer.write(delivered.outcome_audit_intent_id) is not None
    replay_reply = broker.claim_conversation_delivery(
        destination_id=installed.destination_id,
        job_id=job_id,
        claim_hash=hashlib.sha256(b"slack-reply-replay").digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert replay_reply.already_delivered and replay_reply.reply_text is None

    direct = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_channel_broker"]
    )()
    try:
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT * FROM attune.hosted_channel_credentials")
        direct.rollback()
    finally:
        direct.close()

    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions SET created_at = clock_timestamp() "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, CHANNEL_SESSION),
        )
    assert setups.disconnect(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="slack",
    ) is True
    assert setups.disconnect(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="slack",
    ) is False
    with pytest.raises(psycopg.errors.NoDataFound):
        broker.accept_message(
            installation_ref_hash=installation_hash,
            actor_ref_hash=actor_hash,
            destination_ref_hash=destination_hash,
            message_ref_hash=hashlib.sha256(b"slack-after-disconnect").digest(),
            text="This must not be accepted",
        )
    # The revoked destination is no longer active, so the acknowledgment
    # claim must resolve nothing for it either -- only an active destination
    # is ever eligible.
    with pytest.raises(psycopg.errors.NoDataFound):
        broker.claim_acknowledgment(
            installation_ref_hash=installation_hash,
            actor_ref_hash=actor_hash,
            destination_ref_hash=destination_hash,
            message_ref_hash=hashlib.sha256(b"slack-ack-after-disconnect").digest(),
        )
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "SELECT count(*) FROM attune.hosted_channel_credentials "
            "WHERE tenant_id = %s AND destination_id = %s",
            (CHANNEL_TENANT, installed.destination_id),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT count(*) FROM attune.hosted_channel_routes "
            "WHERE tenant_id = %s AND destination_id = %s",
            (CHANNEL_TENANT, installed.destination_id),
        )
        assert cursor.fetchone() == (0,)

    reinstall_secret = b"slack-reinstall-state"
    setups.begin(
        context,
        principal_id=CHANNEL_PRINCIPAL,
        session_id=CHANNEL_SESSION,
        provider="slack",
        mechanism="oauth",
        secret_hash=hashlib.sha256(reinstall_secret).digest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=9),
    )
    reinstall_claim = hashlib.sha256(b"slack-reinstall-claim").digest()
    claim = broker.claim(
        state_hash=hashlib.sha256(reinstall_secret).digest(),
        claim_hash=reinstall_claim,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert writer.write(claim.pre_audit_intent_id) is not None
    resolved = broker.resolve_destination_id(
        state_hash=hashlib.sha256(reinstall_secret).digest(),
        claim_hash=reinstall_claim,
        candidate_id=UUID("30000000-0000-4000-8000-000000000202"),
    )
    assert resolved == installed.destination_id
    # A real Slack reinstall reconnects the same workspace via the same DM,
    # so the installation/actor/destination reference hashes are identical to
    # the first install. That is what previously collided with the unique
    # tenant/provider/reference constraint on attune.installations.
    reinstalled = broker.consume(
        state_hash=hashlib.sha256(reinstall_secret).digest(),
        claim_hash=reinstall_claim,
        owner_tenant_id=CHANNEL_TENANT,
        owner_principal_id=CHANNEL_PRINCIPAL,
        installation_ref_hash=installation_hash,
        actor_ref_hash=actor_hash,
        destination_ref_hash=destination_hash,
        destination_id=resolved,
        encrypted_route=envelope(b"R"),
        encrypted_token=envelope(b"T"),
    )
    assert reinstalled.destination_id == installed.destination_id
    assert reinstalled.destination_status == "pending_test"
    assert writer.write(reinstalled.outcome_audit_intent_id) is not None


def test_web_conversation_accept_is_session_scoped_idempotent_and_function_only(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    context = TenantContext(CHANNEL_TENANT)
    control_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    writer = PostgresAuditWriterRepository(
        _role_connection_factory(database_url, ROLE_BINDINGS["attune_audit_writer"])
    )

    # The Slack journey above already gave CHANNEL_TENANT an active policy
    # and an active Google connector for CHANNEL_PRINCIPAL; refresh only the
    # owner session so it is unexpired and unrevoked (web acceptance does not
    # require recent authentication, unlike disconnect or export requests).
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions SET revoked_at = NULL, "
            "expires_at = clock_timestamp() + interval '8 hours' "
            "WHERE tenant_id = %s AND id = %s",
            (CHANNEL_TENANT, CHANNEL_SESSION),
        )

    def accept(text: str):
        connection = control_factory()
        try:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.accept_web_owner_message(%s, %s, %s)",
                    (CHANNEL_PRINCIPAL, CHANNEL_SESSION, text),
                )
                return cursor.fetchone()
        finally:
            connection.close()

    first = accept("What is on my calendar today?")
    dispatch_id, audit_id, conversation_id, sequence, accepted_new = first
    assert sequence == 1
    assert accepted_new is True
    assert writer.write(audit_id) is not None

    second = accept("And tomorrow?")
    assert second[2] == conversation_id
    assert second[3] == 2
    assert second[4] is True
    assert second[0] != dispatch_id

    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT sequence, actor_type, content FROM attune.conversation_turns "
            "WHERE tenant_id = %s AND conversation_id = %s ORDER BY sequence",
            (CHANNEL_TENANT, conversation_id),
        )
        assert cursor.fetchall() == [
            (1, "user", "What is on my calendar today?"),
            (2, "user", "And tomorrow?"),
        ]
        cursor.execute(
            "SELECT count(*) FROM attune.jobs "
            "WHERE tenant_id = %s AND kind = 'channel.web.converse'",
            (CHANNEL_TENANT,),
        )
        assert cursor.fetchone() == (2,)
    initialized_database.commit()

    # Gate: an unknown or revoked session is refused, with no 10-minute
    # recency requirement -- the session refreshed above is well past any
    # "recent" window and still accepted above.
    connection = control_factory()
    try:
        with pytest.raises(psycopg.errors.NoDataFound):
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.accept_web_owner_message(%s, %s, %s)",
                    (
                        CHANNEL_PRINCIPAL,
                        UUID("30000000-0000-4000-8000-000000000299"),
                        "hi",
                    ),
                )
    finally:
        connection.close()

    # Gate: without an active policy, acceptance is refused.
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.policies SET active = false WHERE tenant_id = %s",
            (CHANNEL_TENANT,),
        )
    connection = control_factory()
    try:
        with pytest.raises(psycopg.errors.NoDataFound):
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.accept_web_owner_message(%s, %s, %s)",
                    (CHANNEL_PRINCIPAL, CHANNEL_SESSION, "hi"),
                )
    finally:
        connection.close()
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.policies SET active = true WHERE tenant_id = %s",
            (CHANNEL_TENANT,),
        )

    # Gate: without an active Google connector, acceptance is refused.
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.connectors SET status = 'revoked' "
            "WHERE tenant_id = %s AND principal_id = %s AND provider = 'google'",
            (CHANNEL_TENANT, CHANNEL_PRINCIPAL),
        )
    connection = control_factory()
    try:
        with pytest.raises(psycopg.errors.NoDataFound):
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.accept_web_owner_message(%s, %s, %s)",
                    (CHANNEL_PRINCIPAL, CHANNEL_SESSION, "hi"),
                )
    finally:
        connection.close()
    with tenant_transaction(initialized_database, context) as cursor:
        cursor.execute(
            "UPDATE attune.connectors SET status = 'active' "
            "WHERE tenant_id = %s AND principal_id = %s AND provider = 'google'",
            (CHANNEL_TENANT, CHANNEL_PRINCIPAL),
        )

    # Privilege: only the control-plane role may execute the function.
    direct = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_channel_broker"]
    )()
    try:
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute(
                "SELECT * FROM attune.accept_web_owner_message(%s, %s, %s)",
                (CHANNEL_PRINCIPAL, CHANNEL_SESSION, "hi"),
            )
        direct.rollback()
    finally:
        direct.close()

    # Privilege: direct writes into provider_events and installations remain
    # denied even to the control plane's own ordinary role -- only the
    # validated function may produce them.
    direct = control_factory()
    try:
        with direct.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('attune.tenant_id', %s, false)",
                (str(CHANNEL_TENANT),),
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "INSERT INTO attune.provider_events "
                    "(tenant_id, installation_id, provider, kind, "
                    "deduplication_key, signal) VALUES "
                    "(%s, %s, 'web', 'web.message', %s, '{}'::jsonb)",
                    (
                        CHANNEL_TENANT,
                        UUID("30000000-0000-4000-8000-000000000301"),
                        hashlib.sha256(b"direct-provider-event").digest(),
                    ),
                )
        direct.rollback()
        with direct.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "INSERT INTO attune.installations "
                    "(tenant_id, provider, kind, external_ref_hash) VALUES "
                    "(%s, 'web', 'channel', %s)",
                    (
                        CHANNEL_TENANT,
                        hashlib.sha256(b"direct-installation").digest(),
                    ),
                )
        direct.rollback()
    finally:
        direct.close()


# ---------------------------------------------------------------------------
# Hosted intelligence persistence (docs/future-state.md Phase 5 item 1;
# docs/gap-analysis.md G8/G18): PostgresImportanceProfile/PostgresAttentionStore.
# ---------------------------------------------------------------------------

INTELLIGENCE_TENANT_A = UUID("40000000-0000-4000-8000-000000000001")
INTELLIGENCE_TENANT_B = UUID("40000000-0000-4000-8000-000000000002")
INTELLIGENCE_PRINCIPAL_A = UUID("40000000-0000-4000-8000-000000000011")
INTELLIGENCE_PRINCIPAL_B = UUID("40000000-0000-4000-8000-000000000012")


def _attention_item(**overrides):
    fields = dict(
        source="slack",
        channel_ref="C-general",
        channel_name="general",
        sender_ref="U-alice",
        sender_display="Alice",
        summary="please review the proposal",
        ts=datetime.now(timezone.utc),
        priority=Priority.ROUTINE,
        mentions_principal=False,
        thread_ref=None,
    )
    fields.update(overrides)
    return AttentionItem(**fields)


def _run_importance_tier_rule_matrix(profile_factory):
    """The same tier-rule scenarios as tests/test_importance.py (LOW
    demotion, HIGH promotion, pin override, decay, unknown sender), run here
    against any ``ImportanceProfile``-shaped object built by
    ``profile_factory()``. This is the shared conformance proof that
    ``PostgresImportanceProfile`` applies ``orchestrator.importance
    .assess_from_signals`` -- the exact same rule engine
    ``JsonImportanceProfile`` uses -- not a reimplementation."""
    now = datetime.now(timezone.utc)

    low_profile = profile_factory()
    for i in range(LOW_RUN_THRESHOLD):
        low_profile.record_signal(
            "matrix-newsletter@example.com", ActionSignal.IGNORED,
            ts=now - timedelta(days=LOW_RUN_THRESHOLD - i),
        )
    assessment = low_profile.assess("matrix-newsletter@example.com", now=now)
    assert assessment.tier == ImportanceTier.LOW
    assert assessment.pinned is False

    high_profile = profile_factory()
    for i in range(HIGH_MIN_SIGNALS):
        high_profile.record_signal(
            "matrix-vip@example.com", ActionSignal.APPROVED,
            ts=now - timedelta(days=HIGH_MIN_SIGNALS - i),
        )
    assert high_profile.assess("matrix-vip@example.com", now=now).tier == (
        ImportanceTier.HIGH
    )

    pin_profile = profile_factory()
    for i in range(LOW_RUN_THRESHOLD):
        pin_profile.record_signal(
            "matrix-pinned@example.com", ActionSignal.IGNORED,
            ts=now - timedelta(days=LOW_RUN_THRESHOLD - i),
        )
    pin_profile.pin("matrix-pinned@example.com", ImportanceTier.HIGH)
    pinned_assessment = pin_profile.assess("matrix-pinned@example.com", now=now)
    assert pinned_assessment.tier == ImportanceTier.HIGH
    assert pinned_assessment.pinned is True
    assert pin_profile.unpin("matrix-pinned@example.com") is True
    assert pin_profile.assess("matrix-pinned@example.com", now=now).tier == (
        ImportanceTier.LOW
    )
    assert pin_profile.unpin("matrix-pinned@example.com") is False

    decay_profile = profile_factory()
    for i in range(LOW_RUN_THRESHOLD):
        decay_profile.record_signal(
            "matrix-decayed@example.com", ActionSignal.IGNORED,
            ts=now - timedelta(days=LOW_RUN_THRESHOLD - i),
        )
    long_later = now + timedelta(days=DECAY_DAYS + LOW_RUN_THRESHOLD + 1)
    decayed_assessment = decay_profile.assess(
        "matrix-decayed@example.com", now=long_later
    )
    assert decayed_assessment.tier == ImportanceTier.NORMAL
    assert decayed_assessment.reason == "no recorded signals"

    unknown_profile = profile_factory()
    assert unknown_profile.assess("matrix-unknown@example.com", now=now) == (
        TierAssessment(ImportanceTier.NORMAL, "no recorded signals", False)
    )


def test_json_importance_profile_matches_the_shared_tier_rule_matrix(tmp_path):
    """Offline sanity check that the shared helper above is itself correct,
    proven against the existing local backend before trusting it to certify
    the new Postgres one."""
    from attune.orchestrator.importance import JsonImportanceProfile

    counter = {"n": 0}

    def profile_factory():
        counter["n"] += 1
        return JsonImportanceProfile(str(tmp_path / f"importance-{counter['n']}.json"))

    _run_importance_tier_rule_matrix(profile_factory)


def test_intelligence_tenants_are_provisioned(initialized_database):
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attune.tenants (id, slug, region) "
            "VALUES (%s, %s, %s), (%s, %s, %s)",
            (
                INTELLIGENCE_TENANT_A, "intelligence-tenant-a", "test",
                INTELLIGENCE_TENANT_B, "intelligence-tenant-b", "test",
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.principals (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, 'test'), (%s, %s, %s, 'test')
            """,
            (
                INTELLIGENCE_TENANT_A, INTELLIGENCE_PRINCIPAL_A,
                hashlib.sha256(b"intelligence-a").digest(),
                INTELLIGENCE_TENANT_B, INTELLIGENCE_PRINCIPAL_B,
                hashlib.sha256(b"intelligence-b").digest(),
            ),
        )
    initialized_database.commit()


def test_postgres_importance_profile_matches_the_local_tier_rule_matrix(
    initialized_database, database_url
):
    hasher = IntelligenceReferenceHasher(_TEST_HMAC_KEY)
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    context = TenantContext(INTELLIGENCE_TENANT_A)

    def profile_factory():
        return PostgresImportanceProfile(
            factory, context, INTELLIGENCE_PRINCIPAL_A, reference_hasher=hasher
        )

    _run_importance_tier_rule_matrix(profile_factory)


def test_postgres_attention_store_recent_ordering_since_and_limit(
    initialized_database, database_url
):
    hasher = IntelligenceReferenceHasher(_TEST_HMAC_KEY)
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    store = PostgresAttentionStore(
        factory, TenantContext(INTELLIGENCE_TENANT_A), INTELLIGENCE_PRINCIPAL_A,
        reference_hasher=hasher,
    )
    now = datetime.now(timezone.utc)
    store.add(_attention_item(sender_ref="ordering-alice", ts=now, summary="alice message"))
    store.add(_attention_item(
        sender_ref="ordering-bob", ts=now + timedelta(minutes=1), summary="bob message",
    ))

    recent = store.recent()
    summaries = [item.summary for item in recent]
    assert summaries.index("bob message") < summaries.index("alice message")
    newest = recent[summaries.index("bob message")]
    assert newest.source == "slack"
    assert newest.channel_name == "general"
    # channel_ref/sender_ref come back as opaque hex digests, not the
    # original provider identifier (module docstring's documented, reviewed
    # divergence from the local JSON store) -- but they are STABLE: the same
    # provider reference always hashes to the same value.
    assert newest.sender_ref == hasher.hash("sender", "ordering-bob").hex()

    since_recent = store.recent(since=now + timedelta(seconds=30))
    assert [item.summary for item in since_recent] == ["bob message"]

    limited = store.recent(limit=1)
    assert len(limited) == 1
    assert limited[0].summary == "bob message"


def test_postgres_attention_store_retention_window_prunes_old_items(
    initialized_database, database_url
):
    hasher = IntelligenceReferenceHasher(_TEST_HMAC_KEY)
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])
    store = PostgresAttentionStore(
        factory, TenantContext(INTELLIGENCE_TENANT_B), INTELLIGENCE_PRINCIPAL_B,
        reference_hasher=hasher,
    )
    now = datetime.now(timezone.utc)
    old_ts = now - timedelta(days=RETENTION_DAYS + 1)
    store.add(_attention_item(sender_ref="stale", ts=old_ts, summary="stale message"))
    store.add(_attention_item(sender_ref="fresh", ts=now, summary="fresh message"))

    summaries = [item.summary for item in store.recent()]
    assert "fresh message" in summaries
    assert "stale message" not in summaries


def test_importance_and_attention_are_isolated_per_tenant_under_rls(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    hasher = IntelligenceReferenceHasher(_TEST_HMAC_KEY)
    factory = _role_connection_factory(database_url, ROLE_BINDINGS["attune_worker"])

    profile_a = PostgresImportanceProfile(
        factory, TenantContext(INTELLIGENCE_TENANT_A), INTELLIGENCE_PRINCIPAL_A,
        reference_hasher=hasher,
    )
    profile_b = PostgresImportanceProfile(
        factory, TenantContext(INTELLIGENCE_TENANT_B), INTELLIGENCE_PRINCIPAL_B,
        reference_hasher=hasher,
    )
    profile_a.record_signal("isolation-vip@example.com", ActionSignal.APPROVED)
    # profile_a's tenant/principal is shared with the tier-rule-matrix test
    # above, so senders() may carry other hashes too -- only the isolation
    # boundary (profile_b sees NONE of tenant A's senders) is asserted here.
    assert hasher.hash("sender", "isolation-vip@example.com").hex() in (
        profile_a.senders()
    )
    assert profile_b.senders() == []
    assert profile_b.assess("isolation-vip@example.com").tier == ImportanceTier.NORMAL

    attention_a = PostgresAttentionStore(
        factory, TenantContext(INTELLIGENCE_TENANT_A), INTELLIGENCE_PRINCIPAL_A,
        reference_hasher=hasher,
    )
    attention_b = PostgresAttentionStore(
        factory, TenantContext(INTELLIGENCE_TENANT_B), INTELLIGENCE_PRINCIPAL_B,
        reference_hasher=hasher,
    )
    attention_a.add(_attention_item(sender_ref="isolation-alice", summary="tenant a only"))
    assert "tenant a only" in [item.summary for item in attention_a.recent()]
    assert "tenant a only" not in [item.summary for item in attention_b.recent()]

    direct = factory()
    try:
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT id FROM attune.importance_signals")
        direct.rollback()
        with direct.cursor() as cursor, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            cursor.execute("SELECT id FROM attune.attention_items")
        direct.rollback()
    finally:
        direct.close()


def _seed_deletion_owner(connection, tenant_id, principal_id, session_id):
    """Insert a tenant/principal/recent-session triple for deletion tests."""

    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attune.tenants (id, slug, region) VALUES (%s, %s, 'test')",
            (tenant_id, "tn-" + tenant_id.hex),
        )
        cursor.execute(
            """
            INSERT INTO attune.principals (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, 'test')
            """,
            (tenant_id, principal_id, hashlib.sha256(tenant_id.bytes).digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.identity_sessions
                (tenant_id, id, principal_id, token_hash, csrf_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp() + interval '8 hours')
            """,
            (
                tenant_id,
                session_id,
                principal_id,
                hashlib.sha256(session_id.bytes + b"token").digest(),
                hashlib.sha256(session_id.bytes + b"csrf").digest(),
            ),
        )
    connection.commit()


def test_tenant_deletion_request_cancel_and_rls_isolation(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    tenant_a, principal_a, session_a = uuid4(), uuid4(), uuid4()
    tenant_b, principal_b, session_b = uuid4(), uuid4(), uuid4()
    _reset_role(initialized_database)
    _seed_deletion_owner(initialized_database, tenant_a, principal_a, session_a)
    _seed_deletion_owner(initialized_database, tenant_b, principal_b, session_b)

    control_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    requests = PostgresTenantDeletionRequests(control_factory)
    context_a = TenantContext(tenant_a)
    context_b = TenantContext(tenant_b)

    assert requests.read(context_a, principal_id=principal_a) is None

    first = requests.request(context_a, principal_id=principal_a, session_id=session_a)
    assert first.created is True
    assert first.status == "pending"
    repeat = requests.request(context_a, principal_id=principal_a, session_id=session_a)
    assert repeat.created is False
    assert repeat.id == first.id

    # RLS: tenant B's own context (an ordinary, non-superuser role connection
    # -- the admin fixture connection bypasses RLS entirely) sees none of
    # tenant A's request row.
    isolation_probe = control_factory()
    try:
        with tenant_transaction(isolation_probe, context_b) as cursor:
            cursor.execute("SELECT id FROM attune.deletion_requests")
            assert cursor.fetchall() == []
        b_request = requests.request(
            context_b, principal_id=principal_b, session_id=session_b
        )
        with tenant_transaction(isolation_probe, context_b) as cursor:
            cursor.execute("SELECT id FROM attune.deletion_requests")
            assert cursor.fetchall() == [(b_request.id,)]
    finally:
        isolation_probe.close()

    cancelled = requests.cancel(context_a, principal_id=principal_a, session_id=session_a)
    assert cancelled.cancelled is True
    assert cancelled.status == "cancelled"
    already = requests.cancel(context_a, principal_id=principal_a, session_id=session_a)
    assert already.cancelled is False
    assert already.status == "cancelled"

    # Only the SECURITY DEFINER functions may mutate the table; the ordinary
    # control-plane role has SELECT only.
    control = control_factory()
    try:
        with control.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('attune.tenant_id', %s, true)", (str(tenant_a),)
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "UPDATE attune.deletion_requests SET status = 'completed'"
                )
        control.rollback()
    finally:
        control.close()

    # A stale (non-recent) session is refused even for an otherwise-valid
    # cancel/request call.
    with tenant_transaction(initialized_database, context_b) as cursor:
        cursor.execute(
            "UPDATE attune.identity_sessions "
            "SET created_at = clock_timestamp() - interval '11 minutes' "
            "WHERE tenant_id = %s AND id = %s",
            (tenant_b, session_b),
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        requests.cancel(context_b, principal_id=principal_b, session_id=session_b)


def test_claim_tenant_deletion_is_one_use_and_resumable(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    tenant_c, principal_c, session_c = uuid4(), uuid4(), uuid4()
    _reset_role(initialized_database)
    _seed_deletion_owner(initialized_database, tenant_c, principal_c, session_c)

    control_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    requests = PostgresTenantDeletionRequests(control_factory)
    context_c = TenantContext(tenant_c)
    requested = requests.request(
        context_c, principal_id=principal_c, session_id=session_c
    )

    with initialized_database.cursor() as cursor:
        cursor.execute(
            "UPDATE attune.deletion_requests SET grace_expires_at = clock_timestamp() "
            "WHERE tenant_id = %s AND id = %s",
            (tenant_c, requested.id),
        )
    initialized_database.commit()

    deletion = _role_connection_factory(database_url, ROLE_BINDINGS["attune_deletion"])()
    try:
        run_1 = uuid4()
        with deletion.cursor() as cursor:
            cursor.execute("SELECT * FROM attune.claim_tenant_deletion(%s)", (run_1,))
            claimed = cursor.fetchone()
        deletion.commit()
        assert claimed[0] == tenant_c
        assert claimed[3] == run_1
        assert claimed[4] is False

        # A second, fresh run id does not steal or duplicate the claim: the
        # already-claimed row resumes with its ORIGINAL run id.
        run_2 = uuid4()
        with deletion.cursor() as cursor:
            cursor.execute("SELECT * FROM attune.claim_tenant_deletion(%s)", (run_2,))
            resumed = cursor.fetchone()
        deletion.commit()
        assert resumed[0] == tenant_c
        assert resumed[3] == run_1
        assert resumed[4] is True

        # Erasing with the wrong (unclaimed) run id is refused.
        with deletion.cursor() as cursor:
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                cursor.execute(
                    "SELECT attune.erase_tenant_deletion_relation(%s, %s, %s, %s, %s)",
                    (run_2, uuid4(), tenant_c, "memories", 500),
                )
        deletion.rollback()

        with deletion.cursor() as cursor:
            cursor.execute(
                "SELECT attune.erase_tenant_deletion_relation(%s, %s, %s, %s, %s)",
                (run_1, uuid4(), tenant_c, "memories", 500),
            )
            assert cursor.fetchone() == (0,)
        deletion.commit()

        with deletion.cursor() as cursor:
            cursor.execute(
                "SELECT attune.complete_tenant_deletion(%s, %s, %s)",
                (run_1, uuid4(), tenant_c),
            )
            assert cursor.fetchone() == ("completed",)
        deletion.commit()

        with deletion.cursor() as cursor:
            with pytest.raises(psycopg.errors.NoDataFound):
                cursor.execute(
                    "SELECT attune.complete_tenant_deletion(%s, %s, %s)",
                    (run_1, uuid4(), tenant_c),
                )
        deletion.rollback()
    finally:
        deletion.close()


def test_tenant_deletion_end_to_end_erases_one_tenant_and_isolates_the_other(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    tenant_a, principal_a, session_a = uuid4(), uuid4(), uuid4()
    tenant_b, principal_b, session_b = uuid4(), uuid4(), uuid4()
    _reset_role(initialized_database)
    _seed_deletion_owner(initialized_database, tenant_a, principal_a, session_a)
    _seed_deletion_owner(initialized_database, tenant_b, principal_b, session_b)

    memory_a, memory_b = uuid4(), uuid4()
    with initialized_database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO attune.memories
                (tenant_id, id, principal_id, content, provenance,
                 source_class, confidence)
            VALUES (%s, %s, %s, 'tenant a secret', '{}', 'user_taught', 1),
                   (%s, %s, %s, 'tenant b secret', '{}', 'user_taught', 1)
            """,
            (tenant_a, memory_a, principal_a, tenant_b, memory_b, principal_b),
        )
        cursor.execute(
            """
            INSERT INTO attune.memory_embeddings
                (tenant_id, memory_id, model, dimensions, embedding)
            VALUES (%s, %s, 'test', 3, '[1,0,0]'), (%s, %s, 'test', 3, '[1,0,0]')
            """,
            (tenant_a, memory_a, tenant_b, memory_b),
        )
    initialized_database.commit()

    control_factory = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_control_plane"]
    )
    requests = PostgresTenantDeletionRequests(control_factory)
    requested = requests.request(
        TenantContext(tenant_a), principal_id=principal_a, session_id=session_a
    )
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "UPDATE attune.deletion_requests SET grace_expires_at = clock_timestamp() "
            "WHERE tenant_id = %s AND id = %s",
            (tenant_a, requested.id),
        )
    initialized_database.commit()

    deletion = _role_connection_factory(database_url, ROLE_BINDINGS["attune_deletion"])()
    try:
        run_id = uuid4()
        with deletion.cursor() as cursor:
            cursor.execute("SELECT * FROM attune.claim_tenant_deletion(%s)", (run_id,))
            claimed = cursor.fetchone()
        deletion.commit()
        assert claimed[0] == tenant_a

        # Mirror tenant_deletion_executor.run_tenant_deletion_once's own
        # foreign-key deferral loop: the registry does not hand-order
        # relations, so a relation whose dependents have not been erased yet
        # is retried after a later pass clears them.
        pending = list(erasable_relations_in_order())
        for _pass in range(len(pending) + 1):
            if not pending:
                break
            next_pending = []
            for relation in pending:
                try:
                    with deletion.cursor() as cursor:
                        cursor.execute(
                            "SELECT attune.erase_tenant_deletion_relation("
                            "%s, %s, %s, %s, %s)",
                            (run_id, uuid4(), tenant_a, relation, 500),
                        )
                        cursor.fetchone()
                    deletion.commit()
                except psycopg.errors.ForeignKeyViolation:
                    deletion.rollback()
                    next_pending.append(relation)
            pending = next_pending
        assert pending == []

        with deletion.cursor() as cursor:
            cursor.execute(
                "SELECT attune.complete_tenant_deletion(%s, %s, %s)",
                (run_id, uuid4(), tenant_a),
            )
            assert cursor.fetchone() == ("completed",)
        deletion.commit()
    finally:
        deletion.close()

    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM attune.tenants WHERE id IN (%s, %s) ORDER BY id",
            (tenant_a, tenant_b) if tenant_a < tenant_b else (tenant_b, tenant_a),
        )
        statuses = dict(
            zip(
                sorted([tenant_a, tenant_b]),
                (row[0] for row in cursor.fetchall()),
            )
        )
        assert statuses[tenant_a] == "deleted"
        assert statuses[tenant_b] == "active"

        cursor.execute(
            "SELECT status FROM attune.principals WHERE tenant_id = %s", (tenant_a,)
        )
        assert cursor.fetchone() == ("deleted",)

        cursor.execute(
            "SELECT count(*) FROM attune.memories WHERE tenant_id = %s", (tenant_a,)
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT content FROM attune.memories WHERE tenant_id = %s", (tenant_b,)
        )
        assert cursor.fetchone() == ("tenant b secret",)

        # The content-free deletion evidence survives the tenant's own
        # content erasure -- audit_intents rows are never a target of the
        # walk (SECURITY_AUDIT/DEIDENTIFY is not an erase rule) -- proof the
        # ceremony happened, not just that content is gone.
        cursor.execute(
            "SELECT count(*) FROM attune.audit_intents WHERE tenant_id = %s "
            "AND action = 'hosted.deletion.relation.erased'",
            (tenant_a,),
        )
        assert cursor.fetchone()[0] >= len(erasable_relations_in_order())
        cursor.execute(
            "SELECT status FROM attune.deletion_requests WHERE tenant_id = %s "
            "AND id = %s",
            (tenant_a, requested.id),
        )
        assert cursor.fetchone() == ("completed",)
    initialized_database.commit()


def test_content_retention_prunes_only_out_of_window_conversations(
    initialized_database, database_url
):
    psycopg = pytest.importorskip("psycopg")
    tenant_id, principal_id, installation_id = uuid4(), uuid4(), uuid4()
    stale_conversation, fresh_conversation = uuid4(), uuid4()
    _reset_role(initialized_database)
    with initialized_database.cursor() as cursor:
        cursor.execute(
            "INSERT INTO attune.tenants (id, slug, region) VALUES (%s, %s, 'test')",
            (tenant_id, "tn-" + tenant_id.hex),
        )
        cursor.execute(
            """
            INSERT INTO attune.principals (tenant_id, id, subject_hash, issuer)
            VALUES (%s, %s, %s, 'test')
            """,
            (tenant_id, principal_id, hashlib.sha256(tenant_id.bytes).digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.installations
                (tenant_id, id, provider, kind, external_ref_hash)
            VALUES (%s, %s, 'google', 'workspace', %s)
            """,
            (tenant_id, installation_id, hashlib.sha256(tenant_id.bytes).digest()),
        )
        cursor.execute(
            """
            INSERT INTO attune.conversations
                (tenant_id, id, principal_id, installation_id, surface,
                 external_ref_hash, created_at)
            VALUES
                (%s, %s, %s, %s, 'web', %s,
                 clock_timestamp() - interval '40 days'),
                (%s, %s, %s, %s, 'web', %s,
                 clock_timestamp() - interval '40 days')
            """,
            (
                tenant_id, stale_conversation, principal_id, installation_id,
                hashlib.sha256(b"stale-conversation").digest(),
                tenant_id, fresh_conversation, principal_id, installation_id,
                hashlib.sha256(b"fresh-conversation").digest(),
            ),
        )
        cursor.execute(
            """
            INSERT INTO attune.conversation_turns
                (tenant_id, conversation_id, sequence, actor_type, content,
                 created_at)
            VALUES
                (%s, %s, 1, 'user', 'stale turn',
                 clock_timestamp() - interval '40 days'),
                (%s, %s, 1, 'user', 'fresh turn',
                 clock_timestamp() - interval '1 day')
            """,
            (tenant_id, stale_conversation, tenant_id, fresh_conversation),
        )
    initialized_database.commit()

    content_retention = _role_connection_factory(
        database_url, ROLE_BINDINGS["attune_content_retention"]
    )()
    try:
        with content_retention.cursor() as cursor:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("SELECT sequence FROM attune.conversation_turns")
        content_retention.rollback()
        with content_retention.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM attune.prune_expired_customer_content(%s, %s)",
                (uuid4(), 500),
            )
            counts = cursor.fetchone()
        content_retention.commit()
        assert counts[0] >= 1
        assert counts[1] >= 1
    finally:
        content_retention.close()

    with initialized_database.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM attune.conversations WHERE tenant_id = %s "
            "AND id IN (%s, %s) ORDER BY id",
            (tenant_id, stale_conversation, fresh_conversation),
        )
        assert cursor.fetchall() == [(fresh_conversation,)]
        cursor.execute(
            "SELECT content FROM attune.conversation_turns WHERE tenant_id = %s",
            (tenant_id,),
        )
        assert cursor.fetchall() == [("fresh turn",)]
        cursor.execute(
            "SELECT producer_kind, action, metadata FROM attune.audit_intents "
            "WHERE tenant_id = %s "
            "AND action = 'content_retention.conversation_turns.pruned'",
            (tenant_id,),
        )
        audit = cursor.fetchone()
        assert audit[0:2] == (
            "content_retention",
            "content_retention.conversation_turns.pruned",
        )
        assert audit[2]["records"] >= 1
    initialized_database.commit()
