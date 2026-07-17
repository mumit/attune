-- One-time, recent-authenticated customer export download authority. The
-- download executor is a distinct read/decrypt identity and never receives
-- object create, list, or delete permission.
DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'attune_export_download'
    ) THEN
        CREATE ROLE attune_export_download
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_export_download_coordinator'
    ) THEN
        CREATE ROLE attune_export_download_coordinator
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

CREATE TABLE attune.export_download_grants (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    export_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    secret_hash bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    expires_at timestamptz NOT NULL,
    lease_run_id uuid,
    lease_expires_at timestamptz,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, secret_hash),
    FOREIGN KEY (tenant_id, export_id) REFERENCES attune.export_jobs(tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES attune.principals(tenant_id, id),
    CHECK (expires_at <= created_at + interval '2 minutes'),
    CHECK (
        (lease_run_id IS NULL AND lease_expires_at IS NULL)
        OR (consumed_at IS NULL AND lease_run_id IS NOT NULL
            AND lease_expires_at IS NOT NULL)
    )
);
ALTER TABLE attune.export_download_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.export_download_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.export_download_grants
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());

CREATE FUNCTION attune.issue_customer_export_download(
    p_principal_id uuid, p_session_id uuid, p_export_id uuid,
    p_secret_hash bytea
)
RETURNS TABLE (grant_id uuid, grant_expires_at timestamptz)
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
        RAISE EXCEPTION 'download issuer is unauthorized' USING ERRCODE = '42501';
    END IF;
    v_tenant_id := attune.current_tenant_id();
    IF p_principal_id IS NULL OR p_session_id IS NULL OR p_export_id IS NULL
       OR p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32 THEN
        RAISE EXCEPTION 'invalid download authorization' USING ERRCODE = '22023';
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
    ) OR NOT EXISTS (
        SELECT 1 FROM attune.export_jobs AS job
         WHERE job.tenant_id = v_tenant_id AND job.id = p_export_id
           AND job.requested_by = p_principal_id AND job.state = 'ready'
           AND job.expires_at > clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'recent owner and ready export are required'
            USING ERRCODE = '42501';
    END IF;

    UPDATE attune.export_download_grants AS grant_row
       SET expires_at = LEAST(grant_row.expires_at, clock_timestamp())
     WHERE grant_row.tenant_id = v_tenant_id
       AND grant_row.export_id = p_export_id
       AND grant_row.principal_id = p_principal_id
       AND grant_row.consumed_at IS NULL;
    RETURN QUERY
    INSERT INTO attune.export_download_grants (
        tenant_id, export_id, principal_id, secret_hash, expires_at
    ) VALUES (
        v_tenant_id, p_export_id, p_principal_id, p_secret_hash,
        clock_timestamp() + interval '90 seconds'
    ) RETURNING id, expires_at;

    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, actor_ref_hash,
        action, outcome, target_type, target_ref_hash, metadata
    ) VALUES (
        v_tenant_id, 'export',
        attune_ext.digest(pg_catalog.convert_to(
            'export-download-authorized-v1:' || p_export_id::text || ':' ||
            p_secret_hash::text, 'UTF8'), 'sha256'),
        'principal', attune_ext.digest(pg_catalog.convert_to(
            p_principal_id::text, 'UTF8'), 'sha256'),
        'export.download_authorized', 'observed', 'export_job',
        attune_ext.digest(pg_catalog.convert_to(p_export_id::text, 'UTF8'), 'sha256'),
        pg_catalog.jsonb_build_object('ttl_seconds', 90)
    );
END
$function$;

CREATE FUNCTION attune.claim_customer_export_download(
    p_grant_id uuid, p_secret_hash bytea, p_run_id uuid
)
RETURNS TABLE (
    tenant_id uuid, export_id uuid, scope_name text, object_id uuid,
    object_generation bigint, wrapped_dek bytea, nonce bytea,
    key_resource text, archive_sha256 bytea, ciphertext_sha256 bytea,
    archive_bytes bigint, ciphertext_bytes bigint, encryption_format integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT pg_catalog.pg_has_role(
        session_user, 'attune_export_download', 'MEMBER'
    ) THEN
        RAISE EXCEPTION 'download executor is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_grant_id IS NULL OR p_run_id IS NULL OR p_secret_hash IS NULL
       OR octet_length(p_secret_hash) <> 32 THEN
        RAISE EXCEPTION 'invalid download claim' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH claimed AS (
        UPDATE attune.export_download_grants AS grant_row
           SET lease_run_id = p_run_id,
               lease_expires_at = clock_timestamp() + interval '2 minutes'
          FROM attune.export_jobs AS job
         WHERE grant_row.id = p_grant_id
           AND grant_row.secret_hash = p_secret_hash
           AND grant_row.consumed_at IS NULL
           AND grant_row.expires_at > clock_timestamp()
           AND (grant_row.lease_expires_at IS NULL
                OR grant_row.lease_expires_at <= clock_timestamp())
           AND job.tenant_id = grant_row.tenant_id
           AND job.id = grant_row.export_id
           AND job.requested_by = grant_row.principal_id
           AND job.state = 'ready' AND job.expires_at > clock_timestamp()
        RETURNING job.tenant_id, job.id, job.scope ->> 'name', job.object_ref,
                  job.object_generation, job.wrapped_dek, job.nonce,
                  job.key_resource, job.archive_sha256, job.ciphertext_sha256,
                  job.archive_bytes, job.ciphertext_bytes, job.encryption_format
    ) SELECT * FROM claimed;
END
$function$;

CREATE FUNCTION attune.finish_customer_export_download(
    p_grant_id uuid, p_export_id uuid, p_run_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_tenant_id uuid;
BEGIN
    IF NOT pg_catalog.pg_has_role(
        session_user, 'attune_export_download', 'MEMBER'
    ) THEN
        RAISE EXCEPTION 'download executor is unauthorized' USING ERRCODE = '42501';
    END IF;
    UPDATE attune.export_jobs AS job
       SET state = 'consumed', updated_at = clock_timestamp()
      FROM attune.export_download_grants AS grant_row
     WHERE grant_row.id = p_grant_id AND grant_row.export_id = p_export_id
       AND grant_row.tenant_id = job.tenant_id AND grant_row.export_id = job.id
       AND grant_row.lease_run_id = p_run_id
       AND grant_row.lease_expires_at > clock_timestamp()
       AND grant_row.consumed_at IS NULL AND job.state = 'ready'
    RETURNING job.tenant_id INTO v_tenant_id;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE attune.export_download_grants AS grant_row
       SET consumed_at = clock_timestamp(), lease_run_id = NULL,
           lease_expires_at = NULL
     WHERE grant_row.tenant_id = v_tenant_id AND grant_row.id = p_grant_id
       AND grant_row.lease_run_id = p_run_id;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type,
        action, outcome, target_type, target_ref_hash, metadata
    ) VALUES (
        v_tenant_id, 'export',
        attune_ext.digest(pg_catalog.convert_to(
            'export-downloaded-v1:' || p_export_id::text, 'UTF8'), 'sha256'),
        'system', 'export.downloaded', 'observed', 'export_job',
        attune_ext.digest(pg_catalog.convert_to(p_export_id::text, 'UTF8'), 'sha256'),
        pg_catalog.jsonb_build_object('one_time', true)
    ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    RETURN true;
END
$function$;

CREATE FUNCTION attune.release_customer_export_download(
    p_grant_id uuid, p_run_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT pg_catalog.pg_has_role(
        session_user, 'attune_export_download', 'MEMBER'
    ) THEN
        RAISE EXCEPTION 'download executor is unauthorized' USING ERRCODE = '42501';
    END IF;
    UPDATE attune.export_download_grants AS grant_row
       SET lease_run_id = NULL, lease_expires_at = NULL
     WHERE grant_row.id = p_grant_id AND grant_row.lease_run_id = p_run_id
       AND grant_row.consumed_at IS NULL;
    RETURN FOUND;
END
$function$;

-- Consumed objects are cleaned immediately by the same delete-only executor;
-- expired ready objects retain the existing expired terminal transition.
ALTER TABLE attune.export_jobs DROP CONSTRAINT export_jobs_expiry_cleanup_lease_check;
ALTER TABLE attune.export_jobs ADD CONSTRAINT export_jobs_expiry_cleanup_lease_check CHECK (
    (expiry_cleanup_run_id IS NULL AND expiry_cleanup_expires_at IS NULL)
    OR (state IN ('ready', 'consumed') AND expiry_cleanup_run_id IS NOT NULL
        AND expiry_cleanup_expires_at IS NOT NULL)
);

CREATE OR REPLACE FUNCTION attune.claim_customer_export_expirations(
    p_run_id uuid, p_batch_size integer
)
RETURNS TABLE (
    tenant_id uuid, export_id uuid, object_id uuid, object_generation bigint
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_export_cleanup', 'MEMBER') THEN
        RAISE EXCEPTION 'export cleanup caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_run_id IS NULL OR p_batch_size IS NULL OR p_batch_size NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'invalid export expiry claim' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH candidates AS MATERIALIZED (
        SELECT job.tenant_id, job.id
          FROM attune.export_jobs AS job
         WHERE ((job.state = 'ready' AND job.expires_at <= clock_timestamp())
                OR job.state = 'consumed')
           AND (job.expiry_cleanup_expires_at IS NULL
                OR job.expiry_cleanup_expires_at <= clock_timestamp())
         ORDER BY job.updated_at, job.id LIMIT p_batch_size
         FOR UPDATE OF job SKIP LOCKED
    ), claimed AS (
        UPDATE attune.export_jobs AS job
           SET expiry_cleanup_run_id = p_run_id,
               expiry_cleanup_expires_at = clock_timestamp() + interval '5 minutes',
               updated_at = clock_timestamp()
          FROM candidates
         WHERE job.tenant_id = candidates.tenant_id AND job.id = candidates.id
        RETURNING job.tenant_id, job.id, job.object_ref, job.object_generation
    ) SELECT claimed.tenant_id, claimed.id, claimed.object_ref,
             claimed.object_generation FROM claimed
      ORDER BY claimed.tenant_id, claimed.id;
END
$function$;

CREATE OR REPLACE FUNCTION attune.complete_customer_export_expiration(
    p_export_id uuid, p_object_id uuid, p_object_generation bigint,
    p_cleanup_run_id uuid
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_job record;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_export_cleanup', 'MEMBER') THEN
        RAISE EXCEPTION 'export cleanup caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_export_id IS NULL OR p_object_id IS NULL
       OR p_object_generation IS NULL OR p_object_generation <= 0
       OR p_cleanup_run_id IS NULL THEN
        RAISE EXCEPTION 'invalid export cleanup completion' USING ERRCODE = '22023';
    END IF;
    UPDATE attune.export_jobs AS job
       SET state = CASE WHEN job.state = 'ready' THEN 'expired' ELSE 'consumed' END,
           object_ref = NULL, object_generation = NULL, wrapped_dek = NULL,
           nonce = NULL, key_resource = NULL, archive_sha256 = NULL,
           ciphertext_sha256 = NULL, archive_bytes = NULL,
           ciphertext_bytes = NULL, encryption_format = NULL, ready_at = NULL,
           expiry_cleanup_run_id = NULL, expiry_cleanup_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE job.id = p_export_id
       AND ((job.state = 'ready' AND job.expires_at <= clock_timestamp())
            OR job.state = 'consumed')
       AND job.object_ref = p_object_id
       AND job.object_generation = p_object_generation
       AND job.expiry_cleanup_run_id = p_cleanup_run_id
       AND job.expiry_cleanup_expires_at > clock_timestamp()
    RETURNING job.tenant_id, job.id, job.state INTO v_job;
    IF NOT FOUND THEN
        IF EXISTS (
            SELECT 1 FROM attune.export_jobs AS job WHERE job.id = p_export_id
              AND job.state IN ('expired', 'consumed')
              AND job.object_ref IS NULL AND job.wrapped_dek IS NULL
        ) THEN RETURN false; END IF;
        RAISE EXCEPTION 'active export cleanup claim is required' USING ERRCODE = '42501';
    END IF;
    UPDATE attune.export_object_attempts AS attempt
       SET cleanup_pending = false, cleaned_at = clock_timestamp(),
           cleanup_lease_run_id = NULL, cleanup_lease_expires_at = NULL
     WHERE attempt.tenant_id = v_job.tenant_id
       AND attempt.export_id = p_export_id AND attempt.object_ref = p_object_id
       AND attempt.cleanup_pending;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type,
        action, outcome, target_type, target_ref_hash, metadata
    ) VALUES (
        v_job.tenant_id, 'export', attune_ext.digest(pg_catalog.convert_to(
            'export-cleaned-v1:' || p_export_id::text, 'UTF8'), 'sha256'),
        'system', CASE WHEN v_job.state = 'expired' THEN 'export.expired'
                       ELSE 'export.consumed_cleaned' END,
        'observed', 'export_job', attune_ext.digest(pg_catalog.convert_to(
            p_export_id::text, 'UTF8'), 'sha256'),
        pg_catalog.jsonb_build_object('records', 1)
    ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    RETURN true;
END
$function$;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_export_download_coordinator TO %I', current_user);
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_export_download_coordinator;
GRANT USAGE ON SCHEMA attune_ext TO attune_export_download_coordinator;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id()
TO attune_export_download_coordinator;
GRANT SELECT, UPDATE ON attune.export_jobs TO attune_export_download_coordinator;
GRANT SELECT, INSERT, UPDATE ON attune.export_download_grants TO attune_export_download_coordinator;
GRANT SELECT ON attune.identity_sessions, attune.principals TO attune_export_download_coordinator;
GRANT SELECT, INSERT ON attune.audit_intents TO attune_export_download_coordinator;
ALTER FUNCTION attune.issue_customer_export_download(uuid,uuid,uuid,bytea) OWNER TO attune_export_download_coordinator;
ALTER FUNCTION attune.claim_customer_export_download(uuid,bytea,uuid) OWNER TO attune_export_download_coordinator;
ALTER FUNCTION attune.finish_customer_export_download(uuid,uuid,uuid) OWNER TO attune_export_download_coordinator;
ALTER FUNCTION attune.release_customer_export_download(uuid,uuid) OWNER TO attune_export_download_coordinator;
REVOKE CREATE ON SCHEMA attune FROM attune_export_download_coordinator;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_export_download_coordinator FROM %I', current_user);
END
$revoke_owner$;

REVOKE ALL ON TABLE attune.export_download_grants FROM PUBLIC, attune_control_plane, attune_export_download;
REVOKE ALL ON FUNCTION
    attune.issue_customer_export_download(uuid,uuid,uuid,bytea),
    attune.claim_customer_export_download(uuid,bytea,uuid),
    attune.finish_customer_export_download(uuid,uuid,uuid),
    attune.release_customer_export_download(uuid,uuid)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION attune.issue_customer_export_download(uuid,uuid,uuid,bytea) TO attune_control_plane;
GRANT EXECUTE ON FUNCTION
    attune.claim_customer_export_download(uuid,bytea,uuid),
    attune.finish_customer_export_download(uuid,uuid,uuid),
    attune.release_customer_export_download(uuid,uuid)
TO attune_export_download;
GRANT USAGE ON SCHEMA attune TO attune_export_download;
