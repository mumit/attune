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
from attune.hosted.capability_gateway import (
    CapabilityDefinition,
    CapabilityDenied,
    CapabilityRegistry,
    EmptyArguments,
    PostgresCapabilityAuthorityRepository,
    RiskTier,
    TypedCapabilityGateway,
)
from attune.hosted.identity import VerifiedIdentity
from attune.hosted.identity_session import (
    IdentitySessionSecrets,
    PostgresIdentitySessionRepository,
)
from attune.hosted.tenant import TenantContext, tenant_transaction
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
}


def test_packaged_migrations_are_ordered_and_checksum_pinned():
    migrations = load_migrations()
    assert [migration.name for migration in migrations] == sorted(
        migration.name for migration in migrations
    )
    assert migrations[0].name == "0001_tenant_boundary.sql"
    assert migrations[-1].name == "0023_google_chat_delivery_test.sql"
    assert all(
        migration.checksum == hashlib.sha256(migration.sql.encode()).hexdigest()
        for migration in migrations
    )
    channel_broker = migrations[-1].sql
    assert "GRANT attune_channel_link_executor TO %I" in channel_broker
    assert "GRANT CREATE ON SCHEMA attune TO attune_channel_link_executor" in channel_broker
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor" in channel_broker
    assert "REVOKE attune_channel_link_executor FROM %I" in channel_broker


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

    assert apply_migrations(admin) == 23
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
    assert (
        approvals.decide(
            TenantContext(TENANT_B),
            opaque_ref_hash=opaque_hash,
            approver_id=PRINCIPAL_A,
            decision="approved",
        )
        is None
    )
    decided = approvals.decide(
        TenantContext(TENANT_A),
        opaque_ref_hash=opaque_hash,
        approver_id=PRINCIPAL_A,
        decision="approved",
    )
    assert decided is not None and decided.id == approval.id
    assert (
        approvals.consume(
            TenantContext(TENANT_A),
            approval_id=approval.id,
            expected_action_hash=hashlib.sha256(b"changed-action").digest(),
            expected_source_version="history-123",
            expected_policy_version=1,
        )
        is None
    )
    consumed = approvals.consume(
        TenantContext(TENANT_A),
        approval_id=approval.id,
        expected_action_hash=action_hash,
        expected_source_version="history-123",
        expected_policy_version=1,
    )
    assert consumed is not None and consumed.status == "consumed"
    assert (
        approvals.consume(
            TenantContext(TENANT_A),
            approval_id=approval.id,
            expected_action_hash=action_hash,
            expected_source_version="history-123",
            expected_policy_version=1,
        )
        is None
    )


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

    export = lifecycle.request_export(
        TenantContext(TENANT_A),
        requested_by=PRINCIPAL_A,
        scope={"object": "account"},
    )
    assert (
        lifecycle.transition_export(
            TenantContext(TENANT_B),
            export.id,
            expected_state="requested",
            state="running",
        )
        is None
    )
    assert (
        lifecycle.transition_export(
            TenantContext(TENANT_A),
            export.id,
            expected_state="requested",
            state="running",
        )
        is not None
    )

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
