DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_policy_executor'
    ) THEN
        CREATE ROLE attune_policy_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

CREATE FUNCTION attune.authorize_recent_identity_session(
    p_token_hash bytea, p_csrf_hash bytea
)
RETURNS TABLE (session_id uuid, tenant_id uuid, principal_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $function$
    UPDATE attune.identity_sessions AS session
       SET last_seen_at = clock_timestamp()
      FROM attune.principals AS principal, attune.tenants AS tenant
     WHERE p_token_hash IS NOT NULL AND octet_length(p_token_hash) = 32
       AND p_csrf_hash IS NOT NULL AND octet_length(p_csrf_hash) = 32
       AND session.token_hash = p_token_hash
       AND session.csrf_hash = p_csrf_hash
       AND session.revoked_at IS NULL
       AND session.expires_at > clock_timestamp()
       AND session.created_at >= clock_timestamp() - interval '10 minutes'
       AND principal.tenant_id = session.tenant_id
       AND principal.id = session.principal_id
       AND principal.status = 'active'
       AND tenant.id = session.tenant_id
       AND tenant.status = 'active'
    RETURNING session.id, session.tenant_id, session.principal_id
$function$;

CREATE FUNCTION attune.activate_hosted_read_only_policy(
    p_principal_id uuid, p_session_id uuid
)
RETURNS TABLE (policy_version bigint, onboarding_revision bigint, policy_status text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_document jsonb := jsonb_build_object(
        'schema_version', 1,
        'profile', 'private_alpha_read_only',
        'maximum_risk', 0,
        'capabilities', jsonb_build_array(
            'google.workspace.connection.verify'
        )
    );
    v_onboarding attune.hosted_onboarding_states%ROWTYPE;
    v_policy attune.policies%ROWTYPE;
    v_version bigint;
    v_revision bigint;
    v_total_grants bigint;
    v_exact_grants bigint;
BEGIN
    IF p_principal_id IS NULL OR p_session_id IS NULL THEN
        RAISE EXCEPTION 'policy principal and session are required'
            USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(v_tenant_id::text || ':hosted-policy', 0)
    );
    SELECT onboarding.* INTO v_onboarding
      FROM attune.hosted_onboarding_states AS onboarding
     WHERE onboarding.tenant_id = v_tenant_id
       AND onboarding.owner_principal_id = p_principal_id
     FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM attune.principals AS principal
         WHERE principal.tenant_id = v_tenant_id
           AND principal.id = p_principal_id
           AND principal.status = 'active'
    ) OR NOT EXISTS (
        SELECT 1 FROM attune.identity_sessions AS session
         WHERE session.tenant_id = v_tenant_id
           AND session.id = p_session_id
           AND session.principal_id = p_principal_id
           AND session.revoked_at IS NULL
           AND session.expires_at > clock_timestamp()
           AND session.created_at >= clock_timestamp() - interval '10 minutes'
    ) THEN
        RAISE EXCEPTION 'hosted policy principal is unavailable'
            USING ERRCODE = '23514';
    END IF;

    SELECT policy.* INTO v_policy
      FROM attune.policies AS policy
     WHERE policy.tenant_id = v_tenant_id AND policy.active;
    IF FOUND THEN
        SELECT count(*), count(*) FILTER (
            WHERE autonomy.principal_id = p_principal_id
              AND autonomy.capability = 'google.workspace.connection.verify'
              AND autonomy.domain = 'private_workspace'
              AND autonomy.maximum_risk = 0
              AND autonomy.granted_by = p_principal_id
        )
          INTO v_total_grants, v_exact_grants
          FROM attune.autonomy_grants AS autonomy
         WHERE autonomy.tenant_id = v_tenant_id
           AND autonomy.policy_version = v_policy.version
           AND autonomy.revoked_at IS NULL;
        IF v_policy.document <> v_document
           OR v_policy.created_by <> p_principal_id
           OR v_total_grants <> 1 OR v_exact_grants <> 1 THEN
            UPDATE attune.hosted_onboarding_states AS onboarding
               SET policy_status = 'externally_modified',
                   revision = onboarding.revision + 1,
                   updated_at = clock_timestamp()
             WHERE onboarding.tenant_id = v_tenant_id
               AND onboarding.policy_status <> 'externally_modified'
            RETURNING onboarding.revision INTO v_revision;
            IF v_revision IS NULL THEN
                v_revision := v_onboarding.revision;
            END IF;
            RETURN QUERY SELECT v_policy.version, v_revision,
                                'externally_modified'::text;
            RETURN;
        END IF;
        v_version := v_policy.version;
    ELSE
        SELECT COALESCE(max(policy.version), 0) + 1 INTO v_version
          FROM attune.policies AS policy
         WHERE policy.tenant_id = v_tenant_id;
        INSERT INTO attune.policies
            (tenant_id, version, document, active, created_by)
        VALUES (v_tenant_id, v_version, v_document, true, p_principal_id);
        INSERT INTO attune.autonomy_grants
            (tenant_id, principal_id, capability, domain, maximum_risk,
             policy_version, granted_by)
        VALUES (
            v_tenant_id, p_principal_id,
            'google.workspace.connection.verify', 'private_workspace',
            0, v_version, p_principal_id
        );
    END IF;

    UPDATE attune.hosted_onboarding_states AS onboarding
       SET policy_status = 'validated',
           revision = CASE
               WHEN onboarding.policy_status = 'validated'
               THEN onboarding.revision ELSE onboarding.revision + 1
           END,
           updated_at = CASE
               WHEN onboarding.policy_status = 'validated'
               THEN onboarding.updated_at ELSE clock_timestamp()
           END
     WHERE onboarding.tenant_id = v_tenant_id
    RETURNING onboarding.revision INTO v_revision;
    RETURN QUERY SELECT v_version, v_revision, 'validated'::text;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.authorize_recent_identity_session(bytea,bytea),
    attune.activate_hosted_read_only_policy(uuid,uuid)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.authorize_recent_identity_session(bytea,bytea),
    attune.activate_hosted_read_only_policy(uuid,uuid)
TO attune_control_plane;

DO $grant_identity_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_identity_executor TO %I', current_user);
END
$grant_identity_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_identity_executor;
ALTER FUNCTION attune.authorize_recent_identity_session(bytea,bytea)
OWNER TO attune_identity_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_identity_executor;
DO $revoke_identity_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_identity_executor FROM %I', current_user);
END
$revoke_identity_owner$;

DO $grant_policy_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_policy_executor TO %I', current_user);
END
$grant_policy_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_policy_executor;
GRANT USAGE ON SCHEMA attune_ext TO attune_policy_executor;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id(),
    attune_ext.gen_random_uuid() TO attune_policy_executor;
GRANT SELECT ON attune.tenants, attune.principals TO attune_policy_executor;
GRANT SELECT ON attune.identity_sessions TO attune_policy_executor;
GRANT SELECT, INSERT ON attune.policies, attune.autonomy_grants
TO attune_policy_executor;
GRANT SELECT, UPDATE ON attune.hosted_onboarding_states
TO attune_policy_executor;
ALTER FUNCTION attune.activate_hosted_read_only_policy(uuid,uuid)
OWNER TO attune_policy_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_policy_executor;
DO $revoke_policy_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_policy_executor FROM %I', current_user);
END
$revoke_policy_owner$;

REVOKE INSERT, UPDATE ON attune.policies, attune.autonomy_grants
FROM attune_control_plane;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune
REVOKE ALL ON FUNCTIONS FROM PUBLIC;
