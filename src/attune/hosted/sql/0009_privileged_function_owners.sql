-- Forced RLS also applies to SECURITY DEFINER functions unless their owner has
-- BYPASSRLS. Keep that power out of every login/runtime role by assigning the
-- narrow cross-tenant functions to distinct, memberless NOLOGIN owner roles.
DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_dispatch_executor'
    ) THEN
        CREATE ROLE attune_dispatch_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_audit_executor'
    ) THEN
        CREATE ROLE attune_audit_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_vault_executor'
    ) THEN
        CREATE ROLE attune_vault_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

-- ALTER FUNCTION OWNER requires temporary membership in the target role and
-- CREATE on the containing schema. Revoke both before this transaction commits.
DO $grant_owners$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_dispatch_executor, attune_audit_executor, '
        'attune_vault_executor TO %I',
        current_user
    );
END
$grant_owners$;

GRANT USAGE, CREATE ON SCHEMA attune TO
    attune_dispatch_executor, attune_audit_executor, attune_vault_executor;
GRANT USAGE ON SCHEMA attune_ext TO attune_dispatch_executor;
GRANT SELECT, UPDATE ON attune.dispatch_intents TO attune_dispatch_executor;
GRANT SELECT ON attune.jobs TO attune_dispatch_executor;
GRANT SELECT, INSERT, UPDATE ON attune.audit_intents
TO attune_dispatch_executor;

ALTER FUNCTION attune.lease_dispatch_intent(uuid, text, integer)
OWNER TO attune_dispatch_executor;
ALTER FUNCTION attune.finalize_dispatch_intent(uuid, text, text)
OWNER TO attune_dispatch_executor;
ALTER FUNCTION attune.request_dispatch_audit(uuid, text, text)
OWNER TO attune_dispatch_executor;

GRANT USAGE ON SCHEMA attune TO attune_audit_executor;
GRANT SELECT, UPDATE ON attune.audit_intents TO attune_audit_executor;
GRANT EXECUTE ON FUNCTION
    attune.append_audit_event(uuid, text, bytea, text, text, text, bytea, jsonb)
TO attune_audit_executor;
ALTER FUNCTION attune.write_audit_intent(uuid)
OWNER TO attune_audit_executor;

GRANT USAGE ON SCHEMA attune TO attune_vault_executor;
GRANT SELECT, UPDATE ON attune.credential_intents TO attune_vault_executor;
GRANT SELECT, INSERT, UPDATE ON attune.connector_credentials
TO attune_vault_executor;
GRANT SELECT, UPDATE ON attune.connectors TO attune_vault_executor;

ALTER FUNCTION attune.lease_credential_intent(uuid, text, integer)
OWNER TO attune_vault_executor;
ALTER FUNCTION attune.finalize_credential_intent(uuid, text, text)
OWNER TO attune_vault_executor;
ALTER FUNCTION
    attune.store_connector_credential(uuid, bytea, bytea, bytea, text, integer)
OWNER TO attune_vault_executor;
ALTER FUNCTION attune.revoke_connector_credential(uuid)
OWNER TO attune_vault_executor;

REVOKE CREATE ON SCHEMA attune FROM
    attune_dispatch_executor, attune_audit_executor, attune_vault_executor;
DO $revoke_owners$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_dispatch_executor, attune_audit_executor, '
        'attune_vault_executor FROM %I',
        current_user
    );
END
$revoke_owners$;
