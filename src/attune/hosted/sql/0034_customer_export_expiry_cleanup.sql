-- Exact-generation cleanup and cryptographic erasure for expired ready exports.
-- The delete-only executor must prove storage deletion (or absence) before the
-- database can leave ready or discard the wrapped data-encryption key.
ALTER TABLE attune.export_jobs
    ADD COLUMN expiry_cleanup_run_id uuid,
    ADD COLUMN expiry_cleanup_expires_at timestamptz,
    ADD CONSTRAINT export_jobs_expiry_cleanup_lease_check CHECK (
        (expiry_cleanup_run_id IS NULL AND expiry_cleanup_expires_at IS NULL)
        OR (state = 'ready' AND expiry_cleanup_run_id IS NOT NULL
            AND expiry_cleanup_expires_at IS NOT NULL)
    );

CREATE FUNCTION attune.claim_customer_export_expirations(
    p_run_id uuid, p_batch_size integer
)
RETURNS TABLE (
    tenant_id uuid, export_id uuid, object_id uuid, object_generation bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
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
         WHERE job.state = 'ready' AND job.expires_at <= clock_timestamp()
           AND (job.expiry_cleanup_expires_at IS NULL
                OR job.expiry_cleanup_expires_at <= clock_timestamp())
         ORDER BY job.expires_at, job.id
         LIMIT p_batch_size
         FOR UPDATE OF job SKIP LOCKED
    ), claimed AS (
        UPDATE attune.export_jobs AS job
           SET expiry_cleanup_run_id = p_run_id,
               expiry_cleanup_expires_at = clock_timestamp() + interval '5 minutes',
               updated_at = clock_timestamp()
          FROM candidates
         WHERE job.tenant_id = candidates.tenant_id AND job.id = candidates.id
        RETURNING job.tenant_id, job.id, job.object_ref, job.object_generation
    )
    SELECT claimed.tenant_id, claimed.id, claimed.object_ref,
           claimed.object_generation
      FROM claimed ORDER BY claimed.tenant_id, claimed.id;
END
$function$;

CREATE FUNCTION attune.complete_customer_export_expiration(
    p_export_id uuid, p_object_id uuid, p_object_generation bigint,
    p_cleanup_run_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
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
        RAISE EXCEPTION 'invalid export expiry completion' USING ERRCODE = '22023';
    END IF;

    UPDATE attune.export_jobs AS job
       SET state = 'expired', object_ref = NULL, object_generation = NULL,
           wrapped_dek = NULL, nonce = NULL, key_resource = NULL,
           archive_sha256 = NULL, ciphertext_sha256 = NULL,
           archive_bytes = NULL, ciphertext_bytes = NULL,
           encryption_format = NULL, ready_at = NULL,
           expiry_cleanup_run_id = NULL, expiry_cleanup_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE job.id = p_export_id AND job.state = 'ready'
       AND job.expires_at <= clock_timestamp()
       AND job.object_ref = p_object_id
       AND job.object_generation = p_object_generation
       AND job.expiry_cleanup_run_id = p_cleanup_run_id
       AND job.expiry_cleanup_expires_at > clock_timestamp()
    RETURNING job.tenant_id, job.id INTO v_job;

    IF NOT FOUND THEN
        IF EXISTS (
            SELECT 1 FROM attune.export_jobs AS job
             WHERE job.id = p_export_id AND job.state = 'expired'
               AND job.object_ref IS NULL AND job.wrapped_dek IS NULL
        ) THEN
            RETURN false;
        END IF;
        RAISE EXCEPTION 'active export expiry claim is required' USING ERRCODE = '42501';
    END IF;

    UPDATE attune.export_object_attempts AS attempt
       SET cleanup_pending = false, cleaned_at = clock_timestamp(),
           cleanup_lease_run_id = NULL, cleanup_lease_expires_at = NULL
     WHERE attempt.tenant_id = v_job.tenant_id
       AND attempt.export_id = p_export_id
       AND attempt.object_ref = p_object_id
       AND attempt.cleanup_pending;

    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type,
        action, outcome, target_type, target_ref_hash, metadata
    ) VALUES (
        v_job.tenant_id, 'export',
        attune_ext.digest(pg_catalog.convert_to(
            'export-expired-v1:' || p_export_id::text, 'UTF8'), 'sha256'),
        'system', 'export.expired', 'observed', 'export_job',
        attune_ext.digest(pg_catalog.convert_to(p_export_id::text, 'UTF8'), 'sha256'),
        pg_catalog.jsonb_build_object('records', 1)
    ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    RETURN true;
END
$function$;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_export_cleanup_coordinator TO %I', current_user);
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_export_cleanup_coordinator;
GRANT UPDATE ON attune.export_jobs TO attune_export_cleanup_coordinator;
ALTER FUNCTION attune.claim_customer_export_expirations(uuid, integer)
OWNER TO attune_export_cleanup_coordinator;
ALTER FUNCTION attune.complete_customer_export_expiration(uuid, uuid, bigint, uuid)
OWNER TO attune_export_cleanup_coordinator;
REVOKE CREATE ON SCHEMA attune FROM attune_export_cleanup_coordinator;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_export_cleanup_coordinator FROM %I', current_user);
END
$revoke_owner$;
REVOKE ALL ON FUNCTION
    attune.claim_customer_export_expirations(uuid, integer),
    attune.complete_customer_export_expiration(uuid, uuid, bigint, uuid)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.claim_customer_export_expirations(uuid, integer),
    attune.complete_customer_export_expiration(uuid, uuid, bigint, uuid)
TO attune_export_cleanup;
