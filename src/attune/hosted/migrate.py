"""Checksum-pinned PostgreSQL migration runner for the hosted data boundary."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import re
import sys
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Iterable

from .data_lifecycle import validate_relational_lifecycle_inventory

_MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")
_ROLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_LOGIN_NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,255}$")
_LOCK_ID = 5_746_885_417_301_991_188

RUNTIME_ROLES = (
    "attune_control_plane",
    "attune_channel_broker",
    "attune_dispatch_broker",
    "attune_worker",
    "attune_secret_broker",
    "attune_audit_writer",
    "attune_oauth_exchange",
    "attune_identity_provisioner",
    "attune_retention",
    "attune_export",
    "attune_export_cleanup",
)

FUNCTION_OWNER_ROLES = (
    "attune_dispatch_executor",
    "attune_audit_executor",
    "attune_vault_executor",
    "attune_oauth_executor",
    "attune_identity_executor",
    "attune_identity_provisioning_executor",
    "attune_policy_executor",
    "attune_channel_config_executor",
    "attune_channel_link_executor",
    "attune_channel_message_executor",
    "attune_channel_lifecycle_executor",
    "attune_retention_executor",
    "attune_export_coordinator",
    "attune_export_cleanup_coordinator",
)

FUNCTION_OWNER_TABLE_PRIVILEGES = frozenset(
    {
        ("attune_dispatch_executor", "attune.dispatch_intents", "SELECT"),
        ("attune_dispatch_executor", "attune.dispatch_intents", "UPDATE"),
        ("attune_dispatch_executor", "attune.jobs", "SELECT"),
        ("attune_dispatch_executor", "attune.audit_intents", "SELECT"),
        ("attune_dispatch_executor", "attune.audit_intents", "INSERT"),
        ("attune_dispatch_executor", "attune.audit_intents", "UPDATE"),
        ("attune_audit_executor", "attune.audit_intents", "SELECT"),
        ("attune_audit_executor", "attune.audit_intents", "UPDATE"),
        ("attune_vault_executor", "attune.credential_intents", "SELECT"),
        ("attune_vault_executor", "attune.credential_intents", "UPDATE"),
        ("attune_vault_executor", "attune.connector_credentials", "SELECT"),
        ("attune_vault_executor", "attune.connector_credentials", "INSERT"),
        ("attune_vault_executor", "attune.connector_credentials", "UPDATE"),
        ("attune_vault_executor", "attune.connectors", "SELECT"),
        ("attune_vault_executor", "attune.connectors", "UPDATE"),
        ("attune_oauth_executor", "attune.oauth_transactions", "SELECT"),
        ("attune_oauth_executor", "attune.oauth_transactions", "UPDATE"),
        ("attune_oauth_executor", "attune.connectors", "SELECT"),
        ("attune_oauth_executor", "attune.credential_intents", "SELECT"),
        ("attune_identity_executor", "attune.tenants", "SELECT"),
        ("attune_identity_executor", "attune.principals", "SELECT"),
        ("attune_identity_executor", "attune.identity_sessions", "SELECT"),
        ("attune_identity_executor", "attune.identity_sessions", "INSERT"),
        ("attune_identity_executor", "attune.identity_sessions", "UPDATE"),
        ("attune_identity_provisioning_executor", "attune.tenants", "SELECT"),
        ("attune_identity_provisioning_executor", "attune.tenants", "INSERT"),
        ("attune_identity_provisioning_executor", "attune.principals", "SELECT"),
        ("attune_identity_provisioning_executor", "attune.principals", "INSERT"),
        ("attune_policy_executor", "attune.tenants", "SELECT"),
        ("attune_policy_executor", "attune.principals", "SELECT"),
        ("attune_policy_executor", "attune.identity_sessions", "SELECT"),
        ("attune_policy_executor", "attune.policies", "SELECT"),
        ("attune_policy_executor", "attune.policies", "INSERT"),
        ("attune_policy_executor", "attune.autonomy_grants", "SELECT"),
        ("attune_policy_executor", "attune.autonomy_grants", "INSERT"),
        (
            "attune_policy_executor",
            "attune.hosted_onboarding_states",
            "SELECT",
        ),
        (
            "attune_policy_executor",
            "attune.hosted_onboarding_states",
            "UPDATE",
        ),
        ("attune_channel_config_executor", "attune.tenants", "SELECT"),
        ("attune_channel_config_executor", "attune.principals", "SELECT"),
        ("attune_channel_config_executor", "attune.identity_sessions", "SELECT"),
        (
            "attune_channel_config_executor",
            "attune.hosted_onboarding_states",
            "SELECT",
        ),
        (
            "attune_channel_config_executor",
            "attune.hosted_onboarding_states",
            "UPDATE",
        ),
        (
            "attune_channel_config_executor",
            "attune.hosted_channel_preferences",
            "SELECT",
        ),
        (
            "attune_channel_config_executor",
            "attune.hosted_channel_preferences",
            "INSERT",
        ),
        (
            "attune_channel_config_executor",
            "attune.hosted_channel_preferences",
            "UPDATE",
        ),
        ("attune_channel_link_executor", "attune.tenants", "SELECT"),
        ("attune_channel_link_executor", "attune.principals", "SELECT"),
        (
            "attune_channel_link_executor",
            "attune.identity_sessions",
            "SELECT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_onboarding_states",
            "SELECT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_preferences",
            "SELECT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_destinations",
            "SELECT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_destinations",
            "INSERT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_destinations",
            "UPDATE",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_routes",
            "SELECT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_routes",
            "INSERT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_routes",
            "DELETE",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_onboarding_states",
            "UPDATE",
        ),
        ("attune_channel_link_executor", "attune.installations", "SELECT"),
        ("attune_channel_link_executor", "attune.installations", "INSERT"),
        ("attune_channel_link_executor", "attune.installations", "UPDATE"),
        ("attune_channel_link_executor", "attune.audit_intents", "SELECT"),
        ("attune_channel_link_executor", "attune.audit_intents", "INSERT"),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_setup_transactions",
            "SELECT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_setup_transactions",
            "INSERT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_setup_transactions",
            "UPDATE",
        ),
        ("attune_channel_link_executor", "attune.jobs", "SELECT"),
        ("attune_channel_link_executor", "attune.conversation_turns", "SELECT"),
        ("attune_channel_link_executor", "attune.connectors", "SELECT"),
        ("attune_channel_link_executor", "attune.policies", "SELECT"),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_deliveries",
            "SELECT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_deliveries",
            "INSERT",
        ),
        (
            "attune_channel_link_executor",
            "attune.hosted_channel_deliveries",
            "UPDATE",
        ),
        ("attune_channel_message_executor", "attune.tenants", "SELECT"),
        ("attune_channel_message_executor", "attune.principals", "SELECT"),
        ("attune_channel_message_executor", "attune.installations", "SELECT"),
        ("attune_channel_message_executor", "attune.connectors", "SELECT"),
        ("attune_channel_message_executor", "attune.policies", "SELECT"),
        ("attune_channel_message_executor", "attune.hosted_channel_preferences", "SELECT"),
        ("attune_channel_message_executor", "attune.hosted_channel_destinations", "SELECT"),
        ("attune_channel_message_executor", "attune.hosted_channel_routes", "SELECT"),
        ("attune_channel_message_executor", "attune.provider_events", "SELECT"),
        ("attune_channel_message_executor", "attune.provider_events", "INSERT"),
        ("attune_channel_message_executor", "attune.provider_events", "UPDATE"),
        ("attune_channel_message_executor", "attune.conversations", "SELECT"),
        ("attune_channel_message_executor", "attune.conversations", "INSERT"),
        ("attune_channel_message_executor", "attune.conversations", "UPDATE"),
        ("attune_channel_message_executor", "attune.conversation_turns", "SELECT"),
        ("attune_channel_message_executor", "attune.conversation_turns", "INSERT"),
        ("attune_channel_message_executor", "attune.conversation_turns", "UPDATE"),
        ("attune_channel_message_executor", "attune.jobs", "SELECT"),
        ("attune_channel_message_executor", "attune.jobs", "INSERT"),
        ("attune_channel_message_executor", "attune.jobs", "UPDATE"),
        ("attune_channel_message_executor", "attune.dispatch_intents", "SELECT"),
        ("attune_channel_message_executor", "attune.dispatch_intents", "INSERT"),
        ("attune_channel_message_executor", "attune.dispatch_intents", "UPDATE"),
        ("attune_channel_message_executor", "attune.audit_intents", "SELECT"),
        ("attune_channel_message_executor", "attune.audit_intents", "INSERT"),
        ("attune_channel_message_executor", "attune.audit_intents", "UPDATE"),
        ("attune_channel_lifecycle_executor", "attune.tenants", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.principals", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.identity_sessions", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.hosted_channel_destinations", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.hosted_channel_destinations", "UPDATE"),
        ("attune_channel_lifecycle_executor", "attune.installations", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.installations", "UPDATE"),
        ("attune_channel_lifecycle_executor", "attune.hosted_channel_setup_transactions", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.hosted_channel_setup_transactions", "UPDATE"),
        ("attune_channel_lifecycle_executor", "attune.hosted_onboarding_states", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.hosted_onboarding_states", "UPDATE"),
        ("attune_channel_lifecycle_executor", "attune.hosted_channel_routes", "SELECT"),
        ("attune_channel_lifecycle_executor", "attune.hosted_channel_routes", "DELETE"),
        ("attune_retention_executor", "attune.oauth_transactions", "SELECT"),
        ("attune_retention_executor", "attune.oauth_transactions", "DELETE"),
        ("attune_retention_executor", "attune.hosted_channel_setup_transactions", "SELECT"),
        ("attune_retention_executor", "attune.hosted_channel_setup_transactions", "DELETE"),
        ("attune_retention_executor", "attune.identity_sessions", "SELECT"),
        ("attune_retention_executor", "attune.identity_sessions", "DELETE"),
        ("attune_retention_executor", "attune.provider_events", "SELECT"),
        ("attune_retention_executor", "attune.provider_events", "DELETE"),
        ("attune_retention_executor", "attune.audit_intents", "SELECT"),
        ("attune_retention_executor", "attune.audit_intents", "INSERT"),
        ("attune_export_coordinator", "attune.export_jobs", "SELECT"),
        ("attune_export_coordinator", "attune.export_jobs", "INSERT"),
        ("attune_export_coordinator", "attune.export_jobs", "UPDATE"),
        ("attune_export_coordinator", "attune.export_object_attempts", "SELECT"),
        ("attune_export_coordinator", "attune.export_object_attempts", "INSERT"),
        ("attune_export_coordinator", "attune.export_object_attempts", "UPDATE"),
        ("attune_export_coordinator", "attune.identity_sessions", "SELECT"),
        ("attune_export_coordinator", "attune.principals", "SELECT"),
        ("attune_export_coordinator", "attune.audit_intents", "SELECT"),
        ("attune_export_coordinator", "attune.audit_intents", "INSERT"),
        ("attune_export_coordinator", "attune.tenants", "SELECT"),
        ("attune_export_coordinator", "attune.installations", "SELECT"),
        ("attune_export_coordinator", "attune.connectors", "SELECT"),
        ("attune_export_coordinator", "attune.policies", "SELECT"),
        ("attune_export_coordinator", "attune.autonomy_grants", "SELECT"),
        ("attune_export_coordinator", "attune.hosted_onboarding_states", "SELECT"),
        ("attune_export_coordinator", "attune.hosted_channel_preferences", "SELECT"),
        ("attune_export_coordinator", "attune.hosted_channel_destinations", "SELECT"),
        ("attune_export_coordinator", "attune.conversations", "SELECT"),
        ("attune_export_coordinator", "attune.conversation_turns", "SELECT"),
        ("attune_export_coordinator", "attune.memories", "SELECT"),
        ("attune_export_coordinator", "attune.audit_events", "SELECT"),
        ("attune_export_coordinator", "attune.usage_records", "SELECT"),
        ("attune_export_cleanup_coordinator", "attune.export_object_attempts", "SELECT"),
        ("attune_export_cleanup_coordinator", "attune.export_object_attempts", "UPDATE"),
        ("attune_export_cleanup_coordinator", "attune.export_jobs", "SELECT"),
        ("attune_export_cleanup_coordinator", "attune.export_jobs", "UPDATE"),
        ("attune_export_cleanup_coordinator", "attune.audit_intents", "SELECT"),
        ("attune_export_cleanup_coordinator", "attune.audit_intents", "INSERT"),
    }
)


def _dispatch_function_invariants_hold(row: Any) -> bool:
    """Normalize DB-API row containers across psycopg and pg8000."""
    return tuple(row) == (True, True, True, True)


TENANT_TABLES = (
    "tenants",
    "principals",
    "installations",
    "connectors",
    "policies",
    "jobs",
    "approvals",
    "memories",
    "memory_embeddings",
    "audit_heads",
    "audit_events",
    "provider_events",
    "job_retries",
    "workflow_checkpoints",
    "conversations",
    "conversation_turns",
    "autonomy_grants",
    "usage_records",
    "export_jobs",
    "export_object_attempts",
    "deletion_markers",
    "dispatch_intents",
    "audit_intents",
    "connector_credentials",
    "credential_intents",
    "job_reconciliations",
    "oauth_transactions",
    "identity_sessions",
    "hosted_onboarding_states",
    "hosted_channel_preferences",
    "hosted_channel_setup_transactions",
    "hosted_channel_destinations",
    "hosted_channel_routes",
    "hosted_channel_deliveries",
)

validate_relational_lifecycle_inventory(TENANT_TABLES)


@dataclass(frozen=True)
class Migration:
    name: str
    sql: str
    checksum: str


def load_migrations() -> tuple[Migration, ...]:
    root = importlib.resources.files("attune.hosted.sql")
    migrations: list[Migration] = []
    for resource in sorted(root.iterdir(), key=lambda item: item.name):
        if not resource.is_file() or not _MIGRATION_NAME.fullmatch(resource.name):
            continue
        raw = resource.read_bytes()
        migrations.append(
            Migration(
                name=resource.name,
                sql=raw.decode("utf-8"),
                checksum=hashlib.sha256(raw).hexdigest(),
            )
        )
    if not migrations:
        raise RuntimeError("no hosted database migrations were packaged")
    return tuple(migrations)


def apply_migrations(
    connection: Any, migrations: Iterable[Migration] | None = None
) -> int:
    """Apply pending migrations under a session advisory lock.

    A changed checksum is a hard failure. DDL and its bookkeeping row commit in
    the same transaction; a partially applied migration is rolled back.
    """

    pending = tuple(migrations or load_migrations())
    applied = 0
    with closing(connection.cursor()) as cursor:
        cursor.execute("SET search_path = pg_catalog")
        cursor.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS attune_meta")
            cursor.execute("REVOKE ALL ON SCHEMA attune_meta FROM PUBLIC")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS attune_meta.schema_migrations (
                    name text PRIMARY KEY,
                    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )
                """
            )
        connection.commit()

        for migration in pending:
            if not _MIGRATION_NAME.fullmatch(migration.name):
                raise ValueError(f"invalid migration name: {migration.name!r}")
            try:
                with closing(connection.cursor()) as cursor:
                    cursor.execute(
                        "SELECT checksum FROM attune_meta.schema_migrations "
                        "WHERE name = %s",
                        (migration.name,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        if row[0] != migration.checksum:
                            raise RuntimeError(
                                f"migration checksum mismatch for {migration.name}"
                            )
                        connection.rollback()
                        continue
                    cursor.execute(migration.sql)
                    cursor.execute(
                        """
                        INSERT INTO attune_meta.schema_migrations (name, checksum)
                        VALUES (%s, %s)
                        """,
                        (migration.name, migration.checksum),
                    )
                connection.commit()
                applied += 1
            except BaseException:
                connection.rollback()
                raise
    finally:
        try:
            with closing(connection.cursor()) as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return applied


def bind_runtime_roles(connection: Any, bindings: dict[str, str]) -> None:
    """Grant fixed NOLOGIN roles to pre-created Cloud SQL IAM users."""

    if set(bindings) != set(RUNTIME_ROLES):
        raise ValueError("runtime role bindings must name every fixed Attune role")
    if len(set(bindings.values())) != len(bindings):
        raise ValueError("every runtime role requires a distinct IAM login")
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute("SELECT current_user")
            migrator = cursor.fetchone()[0]
            cursor.execute(
                f"ALTER ROLE {_quote_identifier(migrator)} SET search_path = pg_catalog"
            )
            for role in RUNTIME_ROLES:
                login = bindings[role]
                if not _ROLE_NAME.fullmatch(role) or not _LOGIN_NAME.fullmatch(login):
                    raise ValueError("unsafe PostgreSQL role or IAM login identifier")
                quoted_role = _quote_identifier(role)
                quoted_login = _quote_identifier(login)
                cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (login,)
                )
                if cursor.fetchone() is None:
                    raise RuntimeError(f"Cloud SQL IAM database user is missing: {login}")
                cursor.execute(
                    """
                    SELECT member.rolname
                      FROM pg_catalog.pg_auth_members AS membership
                      JOIN pg_catalog.pg_roles AS granted
                        ON granted.oid = membership.roleid
                      JOIN pg_catalog.pg_roles AS member
                        ON member.oid = membership.member
                     WHERE granted.rolname = %s
                    """,
                    (role,),
                )
                for existing_member in (row[0] for row in cursor.fetchall()):
                    if existing_member != login:
                        cursor.execute(
                            f"REVOKE {quoted_role} FROM "
                            f"{_quote_identifier(existing_member)}"
                        )
                cursor.execute(f"GRANT {quoted_role} TO {quoted_login}")
                cursor.execute(f"ALTER ROLE {quoted_login} SET search_path = pg_catalog")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def verify_database_boundary(connection: Any, bindings: dict[str, str]) -> None:
    """Fail unless the live database retains every storage security invariant."""

    with closing(connection.cursor()) as cursor:
        cursor.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
              FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'attune' AND c.relkind = 'r'
            """
        )
        rls = {name: (enabled, forced) for name, enabled, forced in cursor.fetchall()}
        if set(rls) != set(TENANT_TABLES) or not all(
            rls[name] == (True, True) for name in TENANT_TABLES
        ):
            raise RuntimeError(
                "hosted tenant table inventory must be exact and enable and force RLS"
            )

        placeholders = ", ".join("%s" for _ in RUNTIME_ROLES)
        cursor.execute(
            f"""
            SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
                   r.rolcanlogin, r.rolbypassrls
             FROM pg_catalog.pg_roles AS r
             WHERE r.rolname IN ({placeholders})
            """,
            RUNTIME_ROLES,
        )
        roles = {row[0]: row[1:] for row in cursor.fetchall()}
        if set(roles) != set(RUNTIME_ROLES) or any(
            any(flags) for flags in roles.values()
        ):
            raise RuntimeError(
                "runtime database roles must be unprivileged NOLOGIN roles"
            )

        placeholders = ", ".join("%s" for _ in FUNCTION_OWNER_ROLES)
        cursor.execute(
            f"""
            SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
                   r.rolcanlogin, r.rolinherit, r.rolbypassrls,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                        WHERE membership.roleid = r.oid
                           OR membership.member = r.oid
                   )
              FROM pg_catalog.pg_roles AS r
             WHERE r.rolname IN ({placeholders})
            """,
            FUNCTION_OWNER_ROLES,
        )
        owner_roles = {row[0]: tuple(row[1:]) for row in cursor.fetchall()}
        if set(owner_roles) != set(FUNCTION_OWNER_ROLES) or any(
            flags != (False, False, False, False, False, True, False)
            for flags in owner_roles.values()
        ):
            raise RuntimeError(
                "function owner roles must be memberless NOLOGIN BYPASSRLS roles"
            )

        cursor.execute(
            """
            SELECT role.rolname, namespace.nspname || '.' || class.relname,
                   acl.privilege_type
              FROM pg_catalog.pg_roles AS role
              JOIN pg_catalog.pg_class AS class ON class.relkind = 'r'
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = class.relnamespace
              CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
                  class.relacl,
                  pg_catalog.acldefault('r', class.relowner)
              )) AS acl
             WHERE role.rolname = ANY(%s)
               AND namespace.nspname = 'attune'
               AND acl.grantee = role.oid
            """,
            (list(FUNCTION_OWNER_ROLES),),
        )
        owner_table_privileges = {tuple(row) for row in cursor.fetchall()}
        if owner_table_privileges != FUNCTION_OWNER_TABLE_PRIVILEGES:
            raise RuntimeError("function owner table privileges do not match policy")

        cursor.execute(
            """
            SELECT role.rolname,
                   pg_catalog.has_schema_privilege(role.oid, 'attune', 'USAGE'),
                   pg_catalog.has_schema_privilege(role.oid, 'attune', 'CREATE'),
                   pg_catalog.has_schema_privilege(role.oid, 'attune_ext', 'USAGE'),
                   pg_catalog.has_schema_privilege(role.oid, 'attune_ext', 'CREATE')
              FROM pg_catalog.pg_roles AS role
             WHERE role.rolname = ANY(%s)
            """,
            (list(FUNCTION_OWNER_ROLES),),
        )
        owner_schema_privileges = {row[0]: tuple(row[1:]) for row in cursor.fetchall()}
        if owner_schema_privileges != {
            "attune_dispatch_executor": (True, False, True, False),
            "attune_audit_executor": (True, False, False, False),
            "attune_vault_executor": (True, False, False, False),
            "attune_oauth_executor": (True, False, False, False),
            "attune_identity_executor": (True, False, False, False),
            "attune_identity_provisioning_executor": (
                True,
                False,
                True,
                False,
            ),
            "attune_policy_executor": (True, False, True, False),
            "attune_channel_config_executor": (True, False, False, False),
            "attune_channel_link_executor": (True, False, False, False),
            "attune_channel_message_executor": (True, False, True, False),
            "attune_channel_lifecycle_executor": (True, False, False, False),
            "attune_retention_executor": (True, False, True, False),
            "attune_export_coordinator": (True, False, True, False),
            "attune_export_cleanup_coordinator": (True, False, True, False),
        }:
            raise RuntimeError("function owner schema privileges do not match policy")

        cursor.execute(
            """
            SELECT e.extname, n.nspname
              FROM pg_catalog.pg_extension AS e
              JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace
             WHERE e.extname IN ('pgcrypto', 'vector')
            """
        )
        if {tuple(row) for row in cursor.fetchall()} != {
            ("pgcrypto", "attune_ext"),
            ("vector", "attune_ext"),
        }:
            raise RuntimeError("pgcrypto and vector must be isolated in attune_ext")

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_class AS c
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
              CROSS JOIN LATERAL pg_catalog.aclexplode(
                  COALESCE(c.relacl, pg_catalog.acldefault('r', c.relowner))) AS acl
             WHERE n.nspname IN ('attune', 'attune_meta') AND acl.grantee = 0
            """
        )
        if cursor.fetchone()[0] != 0:
            raise RuntimeError("PUBLIC must have no hosted table privileges")

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_trigger AS t
              JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'attune' AND c.relname = 'audit_events'
               AND NOT t.tgisinternal AND t.tgenabled <> 'D'
               AND t.tgname IN (
                   'audit_events_no_update_delete', 'audit_events_no_truncate'
               )
            """
        )
        if cursor.fetchone()[0] != 2:
            raise RuntimeError("append-only audit triggers are missing or disabled")

        dispatch_functions = (
            "attune.lease_dispatch_intent(uuid,text,integer)",
            "attune.finalize_dispatch_intent(uuid,text,text)",
        )
        for signature in dispatch_functions:
            cursor.execute(
                """
                SELECT p.prosecdef,
                       COALESCE(
                           'search_path=pg_catalog' = ANY(p.proconfig), false
                       ),
                       pg_catalog.has_function_privilege(%s, %s, 'EXECUTE'),
                       NOT EXISTS (
                           SELECT 1
                             FROM pg_catalog.aclexplode(COALESCE(
                                 p.proacl,
                                 pg_catalog.acldefault('f', p.proowner)
                             )) AS acl
                            WHERE acl.grantee = 0
                              AND acl.privilege_type = 'EXECUTE'
                       ),
                       owner.rolname = 'attune_dispatch_executor'
                  FROM pg_catalog.pg_proc AS p
                  JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
                 WHERE p.oid = %s::pg_catalog.regprocedure
                """,
                (
                    "attune_dispatch_broker",
                    signature,
                    signature,
                ),
            )
            row = cursor.fetchone()
            if tuple(row) != (True, True, True, True, True):
                raise RuntimeError(
                    f"dispatch function invariant failed for {signature}: "
                    f"security_definer={row[0]}, search_path={row[1]}, "
                    f"broker_execute={row[2]}, no_public_execute={row[3]}, "
                    f"safe_owner={row[4]}"
                )
        cursor.execute(
            "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
            (
                "attune_dispatch_broker",
                "attune.dispatch_intents",
                "SELECT,INSERT,UPDATE,DELETE,TRUNCATE",
            ),
        )
        if cursor.fetchone()[0] is not False:
            raise RuntimeError("dispatch broker must not access intent rows directly")

        privileged_functions = (
            (
                "attune.write_audit_intent(uuid)",
                "attune_audit_writer",
                "attune_audit_executor",
            ),
            (
                "attune.request_dispatch_audit(uuid,text,text)",
                "attune_dispatch_broker",
                "attune_dispatch_executor",
            ),
            (
                "attune.lease_credential_intent(uuid,text,integer)",
                "attune_secret_broker",
                "attune_vault_executor",
            ),
            (
                "attune.finalize_credential_intent(uuid,text,text)",
                "attune_secret_broker",
                "attune_vault_executor",
            ),
            (
                "attune.store_connector_credential(uuid,bytea,bytea,bytea,text,integer)",
                "attune_secret_broker",
                "attune_vault_executor",
            ),
            (
                "attune.store_google_oauth_credential(uuid,bytea,bytea,bytea,text,integer,text[])",
                "attune_secret_broker",
                "attune_vault_executor",
            ),
            (
                "attune.revoke_connector_credential(uuid)",
                "attune_secret_broker",
                "attune_vault_executor",
            ),
            (
                "attune.lease_oauth_transaction(bytea,bytea,integer)",
                "attune_oauth_exchange",
                "attune_oauth_executor",
            ),
            (
                "attune.finalize_oauth_transaction(uuid,bytea,text)",
                "attune_oauth_exchange",
                "attune_oauth_executor",
            ),
            (
                "attune.open_identity_session(bytea,text,bytea,bytea,timestamp with time zone)",
                "attune_control_plane",
                "attune_identity_executor",
            ),
            (
                "attune.read_identity_session(bytea)",
                "attune_control_plane",
                "attune_identity_executor",
            ),
            (
                "attune.authorize_identity_session(bytea,bytea)",
                "attune_control_plane",
                "attune_identity_executor",
            ),
            (
                "attune.authorize_recent_identity_session(bytea,bytea)",
                "attune_control_plane",
                "attune_identity_executor",
            ),
            (
                "attune.revoke_identity_session(bytea,bytea)",
                "attune_control_plane",
                "attune_identity_executor",
            ),
            (
                "attune.provision_initial_identity(bytea,text,text,text)",
                "attune_identity_provisioner",
                "attune_identity_provisioning_executor",
            ),
            (
                "attune.activate_hosted_read_only_policy(uuid,uuid)",
                "attune_control_plane",
                "attune_policy_executor",
            ),
            (
                "attune.configure_hosted_channels(uuid,uuid,text[],text[])",
                "attune_control_plane",
                "attune_channel_config_executor",
            ),
            (
                "attune.begin_hosted_channel_setup(uuid,uuid,text,text,bytea,timestamp with time zone)",
                "attune_control_plane",
                "attune_channel_link_executor",
            ),
            (
                "attune.begin_hosted_channel_setup_v2(uuid,uuid,text,text,bytea,timestamp with time zone)",
                "attune_control_plane",
                "attune_channel_link_executor",
            ),
            (
                "attune.claim_google_chat_link(bytea,bytea,timestamp with time zone)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
            (
                "attune.release_google_chat_link_claim(bytea,bytea)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
            (
                "attune.consume_google_chat_link(bytea,bytea,bytea,bytea,bytea)",
                "attune_channel_link_executor",
                "attune_channel_link_executor",
            ),
            (
                "attune.consume_google_chat_link_v2(bytea,bytea,bytea,bytea,bytea,uuid,bytea,bytea,bytea,text,integer)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
            (
                "attune.resolve_google_chat_link_destination(bytea,bytea,uuid)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
            (
                "attune.claim_google_chat_delivery_test(uuid,bytea,timestamp with time zone)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
            (
                "attune.complete_google_chat_delivery_test(uuid,bytea,boolean)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
            (
                "attune.accept_google_chat_owner_message(bytea,bytea,bytea,bytea,text)",
                "attune_channel_broker",
                "attune_channel_message_executor",
            ),
            (
                "attune.claim_google_chat_conversation_delivery(uuid,uuid,bytea,timestamp with time zone)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
            (
                "attune.complete_google_chat_conversation_delivery(uuid,bytea,boolean,bytea)",
                "attune_channel_broker",
                "attune_channel_link_executor",
            ),
        )
        for signature, role, expected_owner in privileged_functions:
            cursor.execute(
                """
                SELECT p.prosecdef,
                       COALESCE(
                           'search_path=pg_catalog' = ANY(p.proconfig), false
                       ),
                       pg_catalog.has_function_privilege(%s, %s, 'EXECUTE'),
                       NOT EXISTS (
                           SELECT 1
                             FROM pg_catalog.aclexplode(COALESCE(
                                 p.proacl,
                                 pg_catalog.acldefault('f', p.proowner)
                             )) AS acl
                            WHERE acl.grantee = 0
                              AND acl.privilege_type = 'EXECUTE'
                       ),
                       owner.rolname = %s
                  FROM pg_catalog.pg_proc AS p
                  JOIN pg_catalog.pg_roles AS owner ON owner.oid = p.proowner
                 WHERE p.oid = %s::pg_catalog.regprocedure
                """,
                (role, signature, expected_owner, signature),
            )
            row = cursor.fetchone()
            if tuple(row) != (True, True, True, True, True):
                raise RuntimeError(
                    f"privileged function invariant failed for {signature}"
                )
        cursor.execute(
            """
            SELECT pg_catalog.has_function_privilege(
                       'attune_audit_writer',
                       'attune.append_audit_event(uuid,text,bytea,text,text,text,bytea,jsonb)',
                       'EXECUTE'
                   ),
                   pg_catalog.has_table_privilege(
                       'attune_audit_writer', 'attune.audit_intents',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
                   ),
                   pg_catalog.has_table_privilege(
                       'attune_dispatch_broker', 'attune.audit_intents',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
                   )
            """
        )
        if tuple(cursor.fetchone()) != (False, False, False):
            raise RuntimeError("audit writer or dispatch broker has ambient audit access")
        cursor.execute(
            """
            SELECT pg_catalog.has_table_privilege(
                       'attune_secret_broker', 'attune.connector_credentials',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
                   ),
                   pg_catalog.has_table_privilege(
                       'attune_secret_broker', 'attune.credential_intents',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
                   )
            """
        )
        if tuple(cursor.fetchone()) != (False, False):
            raise RuntimeError("secret broker has ambient connector-vault access")

        cursor.execute(
            """
            SELECT pg_catalog.has_table_privilege(
                       'attune_oauth_exchange', 'attune.oauth_transactions',
                       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
                   ),
                   pg_catalog.has_table_privilege(
                       'attune_control_plane', 'attune.oauth_transactions',
                       'SELECT'
                   ),
                   pg_catalog.has_table_privilege(
                       'attune_control_plane', 'attune.oauth_transactions',
                       'INSERT'
                   ),
                   pg_catalog.has_table_privilege(
                       'attune_control_plane', 'attune.oauth_transactions',
                       'UPDATE,DELETE,TRUNCATE'
                   )
            """
        )
        if tuple(cursor.fetchone()) != (False, True, True, False):
            raise RuntimeError("OAuth transaction privileges do not match policy")

        cursor.execute(
            """
            SELECT
                pg_catalog.has_table_privilege(
                    'attune_control_plane', 'attune.installations', 'SELECT'
                ),
                pg_catalog.has_table_privilege(
                    'attune_control_plane', 'attune.installations',
                    'INSERT,UPDATE,DELETE,TRUNCATE'
                ),
                pg_catalog.has_table_privilege(
                    'attune_control_plane',
                    'attune.hosted_channel_setup_transactions', 'SELECT'
                ),
                pg_catalog.has_table_privilege(
                    'attune_control_plane',
                    'attune.hosted_channel_setup_transactions',
                    'INSERT,UPDATE,DELETE,TRUNCATE'
                ),
                pg_catalog.has_table_privilege(
                    'attune_control_plane',
                    'attune.hosted_channel_destinations', 'SELECT'
                ),
                pg_catalog.has_table_privilege(
                    'attune_control_plane',
                    'attune.hosted_channel_destinations',
                    'INSERT,UPDATE,DELETE,TRUNCATE'
                )
            """
        )
        if tuple(cursor.fetchone()) != (True, False, True, False, True, False):
            raise RuntimeError("channel installation privileges do not match policy")

        cursor.execute(
            """
            SELECT
                pg_catalog.has_table_privilege(
                    'attune_worker', 'attune.job_reconciliations', 'SELECT'
                ),
                pg_catalog.has_table_privilege(
                    'attune_worker', 'attune.job_reconciliations', 'INSERT'
                ),
                pg_catalog.has_table_privilege(
                    'attune_worker', 'attune.job_reconciliations',
                    'UPDATE,DELETE,TRUNCATE'
                ),
                pg_catalog.has_table_privilege(
                    'attune_control_plane', 'attune.job_reconciliations', 'SELECT'
                ),
                pg_catalog.has_table_privilege(
                    'attune_control_plane', 'attune.job_reconciliations',
                    'INSERT,UPDATE,DELETE,TRUNCATE'
                )
            """
        )
        if tuple(cursor.fetchone()) != (True, True, False, True, False):
            raise RuntimeError("reconciliation intake privileges do not match policy")

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_trigger AS trigger
              JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = class.relnamespace
             WHERE namespace.nspname = 'attune'
               AND class.relname = 'job_reconciliations'
               AND trigger.tgname = 'job_reconciliation_insert_guard'
               AND NOT trigger.tgisinternal AND trigger.tgenabled <> 'D'
            """
        )
        if cursor.fetchone()[0] != 1:
            raise RuntimeError("reconciliation intake guard is missing or disabled")

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_trigger AS trigger
              JOIN pg_catalog.pg_class AS class ON class.oid = trigger.tgrelid
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = class.relnamespace
             WHERE namespace.nspname = 'attune'
               AND class.relname = 'oauth_transactions'
               AND trigger.tgname = 'oauth_transaction_insert_guard'
               AND NOT trigger.tgisinternal AND trigger.tgenabled <> 'D'
            """
        )
        if cursor.fetchone()[0] != 1:
            raise RuntimeError("OAuth transaction guard is missing or disabled")

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_constraint AS cst
              JOIN pg_catalog.pg_class AS class
                ON class.oid = cst.conrelid
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = class.relnamespace
             WHERE namespace.nspname = 'attune'
               AND class.relname = 'oauth_transactions'
               AND cst.conname =
                   'oauth_transactions_credential_intent_fk'
               AND cst.contype = 'f'
            """
        )
        if cursor.fetchone()[0] != 1:
            raise RuntimeError("OAuth install-intent foreign key is missing")

        for role, login in bindings.items():
            cursor.execute(
                """
                SELECT member.rolname
                  FROM pg_catalog.pg_auth_members AS membership
                  JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
                  JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
                 WHERE granted.rolname = %s
                """,
                (role,),
            )
            if [row[0] for row in cursor.fetchall()] != [login]:
                raise RuntimeError(
                    f"runtime role {role} must have exactly one IAM member"
                )

        for role in RUNTIME_ROLES:
            cursor.execute(
                "SELECT pg_catalog.has_table_privilege(%s, 'attune.audit_events', %s)",
                (role, "INSERT,UPDATE,DELETE,TRUNCATE"),
            )
            if cursor.fetchone()[0] is not False:
                raise RuntimeError(f"{role} has direct audit mutation privileges")
    connection.rollback()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _bindings_from_environment() -> dict[str, str]:
    raw = os.environ.get("ATTUNE_DB_ROLE_BINDINGS", "")
    if not raw:
        raise RuntimeError("ATTUNE_DB_ROLE_BINDINGS is required")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise ValueError("ATTUNE_DB_ROLE_BINDINGS must be a string-to-string object")
    return parsed


def _cloud_sql_connection() -> tuple[Any, Any]:
    from google.cloud.sql.connector import Connector, IPTypes, RefreshStrategy

    instance = os.environ["ATTUNE_CLOUD_SQL_INSTANCE"]
    user = os.environ["ATTUNE_DB_USER"]
    database = os.environ.get("ATTUNE_DB_NAME", "attune")
    connector = Connector(refresh_strategy=RefreshStrategy.LAZY)
    connection = connector.connect(
        instance,
        "pg8000",
        user=user,
        db=database,
        enable_iam_auth=True,
        ip_type=IPTypes.PRIVATE,
    )
    return connector, connection


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        raise ValueError("the hosted migrator accepts no runtime arguments")
    owner, connection = _cloud_sql_connection()
    try:
        with closing(connection):
            count = apply_migrations(connection)
            bindings = _bindings_from_environment()
            bind_runtime_roles(connection, bindings)
            verify_database_boundary(connection, bindings)
        print(
            f"hosted database boundary verified; {count} migration(s) applied; "
            f"{len(TENANT_TABLES)} tenant tables forced through RLS"
        )
    finally:
        if owner is not None:
            owner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
