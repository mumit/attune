-- Dormant customer-export request/claim authority. This migration deliberately
-- provides no ready/publish transition; storage and download arrive later.
DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'attune_export'
    ) THEN
        CREATE ROLE attune_export
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_export_coordinator'
    ) THEN
        CREATE ROLE attune_export_coordinator
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

-- FORCE RLS also applies to the table owner. Temporarily assume the new,
-- memberless BYPASS owner solely for the legacy-row preflight; any refusal
-- rolls the grant and the entire migration back atomically.
DO $legacy_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_export_coordinator TO %I', current_user
    );
END
$legacy_owner$;
GRANT USAGE ON SCHEMA attune TO attune_export_coordinator;
GRANT SELECT ON attune.export_jobs TO attune_export_coordinator;
SET LOCAL ROLE attune_export_coordinator;
DO $legacy$
BEGIN
    IF EXISTS (SELECT 1 FROM attune.export_jobs) THEN
        RAISE EXCEPTION
            'legacy export jobs require explicit reviewed adoption before migration 0029'
            USING ERRCODE = '55000';
    END IF;
END
$legacy$;
RESET ROLE;

REVOKE INSERT, UPDATE ON attune.export_jobs
FROM attune_control_plane, attune_worker;

ALTER TABLE attune.export_jobs
    ADD COLUMN requested_session_id uuid NOT NULL,
    ADD COLUMN request_idempotency_key bytea NOT NULL
        CHECK (octet_length(request_idempotency_key) = 32),
    ADD COLUMN lease_run_id uuid,
    ADD COLUMN lease_expires_at timestamptz;

ALTER TABLE attune.export_jobs
    DROP CONSTRAINT export_jobs_state_check,
    DROP CONSTRAINT export_jobs_scope_check,
    ADD CONSTRAINT export_jobs_state_check CHECK (
        state IN ('requested', 'running', 'ready', 'consumed', 'expired',
                  'failed', 'cancelled')
    ),
    ADD CONSTRAINT export_jobs_scope_check CHECK (
        scope = pg_catalog.jsonb_build_object(
            'schema_version', 1, 'name', scope ->> 'name'
        )
        AND scope ->> 'name' IN (
            'account', 'conversations', 'memories', 'activity'
        )
    ),
    ADD CONSTRAINT export_jobs_lease_check CHECK (
        (state = 'running' AND lease_run_id IS NOT NULL
         AND lease_expires_at IS NOT NULL)
        OR (state <> 'running' AND lease_run_id IS NULL
            AND lease_expires_at IS NULL)
    ),
    ADD CONSTRAINT export_jobs_request_key_unique
        UNIQUE (tenant_id, request_idempotency_key);

CREATE UNIQUE INDEX export_jobs_one_active_scope
ON attune.export_jobs (tenant_id, requested_by, ((scope ->> 'name')))
WHERE state IN ('requested', 'running', 'ready');

CREATE FUNCTION attune.request_customer_export(
    p_principal_id uuid, p_session_id uuid, p_scope text,
    p_idempotency_key bytea
)
RETURNS TABLE (
    export_id uuid, scope_name text, export_state text, created_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_tenant_id uuid;
BEGIN
    IF NOT pg_catalog.pg_has_role(
        session_user, 'attune_control_plane', 'MEMBER'
    ) THEN
        RAISE EXCEPTION 'export requester is unauthorized' USING ERRCODE = '42501';
    END IF;
    v_tenant_id := attune.current_tenant_id();
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
         WHERE identity_session.tenant_id = v_tenant_id
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

    INSERT INTO attune.export_jobs (
        tenant_id, requested_by, requested_session_id,
        request_idempotency_key, scope
    ) VALUES (
        v_tenant_id, p_principal_id, p_session_id, p_idempotency_key,
        pg_catalog.jsonb_build_object('schema_version', 1, 'name', p_scope)
    ) ON CONFLICT (tenant_id, request_idempotency_key) DO NOTHING;

    IF NOT EXISTS (
        SELECT 1 FROM attune.export_jobs AS job
         WHERE job.tenant_id = v_tenant_id
           AND job.request_idempotency_key = p_idempotency_key
           AND job.requested_by = p_principal_id
           AND job.requested_session_id = p_session_id
           AND job.scope ->> 'name' = p_scope
    ) THEN
        RAISE EXCEPTION 'export idempotency collision' USING ERRCODE = '23505';
    END IF;

    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, actor_ref_hash,
        action, outcome, target_type, target_ref_hash, metadata
    )
    SELECT job.tenant_id, 'export',
           attune_ext.digest(pg_catalog.convert_to(
               'export-request-v1:' || job.id::text, 'UTF8'), 'sha256'),
           'principal', attune_ext.digest(pg_catalog.convert_to(
               p_principal_id::text, 'UTF8'), 'sha256'),
           'export.requested', 'observed', 'export_job',
           attune_ext.digest(pg_catalog.convert_to(
               job.id::text, 'UTF8'), 'sha256'),
           pg_catalog.jsonb_build_object('scope', p_scope)
      FROM attune.export_jobs AS job
     WHERE job.tenant_id = v_tenant_id
       AND job.request_idempotency_key = p_idempotency_key
    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

    RETURN QUERY
    SELECT job.id, job.scope ->> 'name', job.state, job.created_at
      FROM attune.export_jobs AS job
     WHERE job.tenant_id = v_tenant_id
       AND job.request_idempotency_key = p_idempotency_key;
END
$function$;

CREATE FUNCTION attune.claim_customer_export(
    p_export_id uuid, p_run_id uuid
)
RETURNS TABLE (
    tenant_id uuid, export_id uuid, requested_by uuid, scope_name text,
    lease_expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_job record;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_export', 'MEMBER') THEN
        RAISE EXCEPTION 'export executor is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_export_id IS NULL OR p_run_id IS NULL THEN
        RAISE EXCEPTION 'invalid export claim' USING ERRCODE = '22023';
    END IF;

    UPDATE attune.export_jobs AS job
       SET state = 'running', lease_run_id = p_run_id,
           lease_expires_at = clock_timestamp() + interval '5 minutes',
           updated_at = clock_timestamp()
     WHERE job.id = p_export_id AND job.state = 'requested'
    RETURNING job.tenant_id AS tenant_id, job.id AS id,
              job.requested_by AS requested_by,
              job.scope ->> 'name' AS scope_name,
              job.lease_expires_at AS lease_expires_at
         INTO v_job;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type,
        action, outcome, target_type, target_ref_hash, metadata
    ) VALUES (
        v_job.tenant_id, 'export',
        attune_ext.digest(pg_catalog.convert_to(
            'export-claim-v1:' || p_run_id::text, 'UTF8'), 'sha256'),
        'system', 'export.claimed', 'observed', 'export_job',
        attune_ext.digest(pg_catalog.convert_to(
            v_job.id::text, 'UTF8'), 'sha256'),
        pg_catalog.jsonb_build_object('scope', v_job.scope_name)
    );

    RETURN QUERY SELECT v_job.tenant_id, v_job.id, v_job.requested_by,
                        v_job.scope_name, v_job.lease_expires_at;
END
$function$;

ALTER TABLE attune.audit_intents
DROP CONSTRAINT audit_intents_producer_kind_check;
ALTER TABLE attune.audit_intents
ADD CONSTRAINT audit_intents_producer_kind_check CHECK (producer_kind IN (
    'control_plane', 'worker', 'secret_broker', 'dispatch_broker',
    'channel_broker', 'retention', 'export'
));

CREATE OR REPLACE FUNCTION attune.enforce_audit_intent_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    expected_producer text;
    memberships integer;
BEGIN
    IF NEW.producer_kind IN (
        'dispatch_broker', 'channel_broker', 'retention', 'export'
    ) THEN
        IF NEW.producer_kind = 'export' THEN
            IF NOT (
                pg_catalog.pg_has_role(session_user, 'attune_control_plane', 'MEMBER')
                OR pg_catalog.pg_has_role(session_user, 'attune_export', 'MEMBER')
            ) THEN
                RAISE EXCEPTION 'audit producer identity does not match intent'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF NOT pg_catalog.pg_has_role(
            session_user,
            CASE NEW.producer_kind
                WHEN 'dispatch_broker' THEN 'attune_dispatch_broker'
                WHEN 'channel_broker' THEN 'attune_channel_broker'
                ELSE 'attune_retention'
            END,
            'MEMBER'
        ) THEN
            RAISE EXCEPTION 'audit producer identity does not match intent'
                USING ERRCODE = '42501';
        END IF;
        RETURN NEW;
    END IF;
    memberships :=
        pg_catalog.pg_has_role(current_user, 'attune_control_plane', 'MEMBER')::integer
        + pg_catalog.pg_has_role(current_user, 'attune_worker', 'MEMBER')::integer
        + pg_catalog.pg_has_role(current_user, 'attune_secret_broker', 'MEMBER')::integer;
    IF memberships <> 1 THEN
        RAISE EXCEPTION 'audit producer identity is ambiguous or unauthorized'
            USING ERRCODE = '42501';
    END IF;
    IF pg_catalog.pg_has_role(current_user, 'attune_control_plane', 'MEMBER') THEN
        expected_producer := 'control_plane';
    ELSIF pg_catalog.pg_has_role(current_user, 'attune_worker', 'MEMBER') THEN
        expected_producer := 'worker';
    ELSE
        expected_producer := 'secret_broker';
    END IF;
    IF NEW.producer_kind <> expected_producer THEN
        RAISE EXCEPTION 'audit producer identity does not match intent'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
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
GRANT USAGE ON SCHEMA attune_ext TO attune_export_coordinator;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id()
TO attune_export_coordinator;
GRANT SELECT, INSERT, UPDATE ON attune.export_jobs TO attune_export_coordinator;
GRANT SELECT ON attune.identity_sessions, attune.principals
TO attune_export_coordinator;
GRANT SELECT, INSERT ON attune.audit_intents TO attune_export_coordinator;
ALTER FUNCTION attune.request_customer_export(uuid, uuid, text, bytea)
OWNER TO attune_export_coordinator;
ALTER FUNCTION attune.claim_customer_export(uuid, uuid)
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
    attune.request_customer_export(uuid, uuid, text, bytea),
    attune.claim_customer_export(uuid, uuid)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.request_customer_export(uuid, uuid, text, bytea)
TO attune_control_plane;
GRANT EXECUTE ON FUNCTION attune.claim_customer_export(uuid, uuid)
TO attune_export;
GRANT USAGE ON SCHEMA attune TO attune_export;
REVOKE CREATE ON SCHEMA attune FROM attune_export_coordinator;
