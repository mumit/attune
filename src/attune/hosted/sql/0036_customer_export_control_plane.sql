-- Principal-bound request/status authority for the customer export control plane.
-- Browser input never selects rows directly; the database derives tenant and
-- owner authority from the verified session transaction.

CREATE FUNCTION attune.request_or_read_customer_export(
    p_principal_id uuid, p_session_id uuid, p_scope text,
    p_idempotency_key bytea
)
RETURNS TABLE (
    export_id uuid, scope_name text, export_state text,
    created_at timestamptz, was_created boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    BEGIN
        RETURN QUERY
        SELECT requested.export_id, requested.scope_name,
               requested.export_state, requested.created_at, true
          FROM attune.request_customer_export(
              p_principal_id, p_session_id, p_scope, p_idempotency_key
          ) AS requested;
        RETURN;
    EXCEPTION WHEN unique_violation THEN
        -- The partial unique index permits one active request per owner/scope.
        -- A concurrent/double submission therefore adopts only that exact row.
        NULL;
    END;

    IF p_principal_id IS NULL OR p_session_id IS NULL
       OR p_scope NOT IN ('account', 'conversations', 'memories', 'activity')
       OR p_idempotency_key IS NULL
       OR octet_length(p_idempotency_key) <> 32 THEN
        RAISE EXCEPTION 'invalid export request' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM attune.identity_sessions AS identity_session
          JOIN attune.principals AS principal
            ON principal.tenant_id = identity_session.tenant_id
           AND principal.id = identity_session.principal_id
         WHERE identity_session.tenant_id = attune.current_tenant_id()
           AND identity_session.id = p_session_id
           AND identity_session.principal_id = p_principal_id
           AND identity_session.revoked_at IS NULL
           AND identity_session.expires_at > clock_timestamp()
           AND identity_session.created_at
               >= clock_timestamp() - interval '10 minutes'
           AND principal.status = 'active'
    ) THEN
        RAISE EXCEPTION 'recent owner session is required' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT job.id, job.scope ->> 'name', job.state, job.created_at, false
      FROM attune.export_jobs AS job
     WHERE job.tenant_id = attune.current_tenant_id()
       AND job.requested_by = p_principal_id
       AND job.scope ->> 'name' = p_scope
       AND job.state IN ('requested', 'running', 'ready')
     ORDER BY job.created_at DESC, job.id DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active export request changed concurrently'
            USING ERRCODE = '40001';
    END IF;
END
$function$;

CREATE FUNCTION attune.list_customer_exports(
    p_principal_id uuid, p_limit integer
)
RETURNS TABLE (
    export_id uuid, scope_name text, export_state text,
    created_at timestamptz, updated_at timestamptz,
    ready_at timestamptz, expires_at timestamptz,
    archive_bytes bigint, failure_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT pg_catalog.pg_has_role(
        session_user, 'attune_control_plane', 'MEMBER'
    ) THEN
        RAISE EXCEPTION 'export reader is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_principal_id IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 20
       OR NOT EXISTS (
           SELECT 1 FROM attune.principals AS principal
            WHERE principal.tenant_id = attune.current_tenant_id()
              AND principal.id = p_principal_id
              AND principal.status = 'active'
       ) THEN
        RAISE EXCEPTION 'active export owner is required' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT job.id, job.scope ->> 'name', job.state,
           job.created_at, job.updated_at, job.ready_at, job.expires_at,
           job.archive_bytes, job.failure_code
      FROM attune.export_jobs AS job
     WHERE job.tenant_id = attune.current_tenant_id()
       AND job.requested_by = p_principal_id
     ORDER BY job.created_at DESC, job.id DESC
     LIMIT p_limit;
END
$function$;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_export_coordinator TO %I', current_user
    );
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_export_coordinator;
ALTER FUNCTION attune.request_or_read_customer_export(uuid, uuid, text, bytea)
OWNER TO attune_export_coordinator;
ALTER FUNCTION attune.list_customer_exports(uuid, integer)
OWNER TO attune_export_coordinator;
REVOKE CREATE ON SCHEMA attune FROM attune_export_coordinator;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_export_coordinator FROM %I', current_user
    );
END
$revoke_owner$;

REVOKE ALL ON FUNCTION
    attune.request_or_read_customer_export(uuid, uuid, text, bytea),
    attune.list_customer_exports(uuid, integer)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.request_or_read_customer_export(uuid, uuid, text, bytea),
    attune.list_customer_exports(uuid, integer)
TO attune_control_plane;
