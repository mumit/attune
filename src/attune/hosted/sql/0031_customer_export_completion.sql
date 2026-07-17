-- Claim-bound encrypted-object completion. No writer, download, or cleanup
-- runtime is introduced by this migration.
ALTER TABLE attune.export_jobs
    ADD COLUMN object_generation bigint,
    ADD COLUMN wrapped_dek bytea,
    ADD COLUMN nonce bytea,
    ADD COLUMN key_resource text,
    ADD COLUMN archive_sha256 bytea,
    ADD COLUMN ciphertext_sha256 bytea,
    ADD COLUMN archive_bytes bigint,
    ADD COLUMN ciphertext_bytes bigint,
    ADD COLUMN encryption_format integer,
    ADD COLUMN ready_at timestamptz,
    DROP CONSTRAINT export_jobs_check,
    ADD CONSTRAINT export_jobs_ready_check CHECK (
        state <> 'ready' OR (
            object_ref IS NOT NULL
            AND object_generation > 0
            AND octet_length(wrapped_dek) BETWEEN 1 AND 65536
            AND octet_length(nonce) = 12
            AND length(key_resource) BETWEEN 1 AND 1024
            AND octet_length(archive_sha256) = 32
            AND octet_length(ciphertext_sha256) = 32
            AND archive_bytes BETWEEN 0 AND 52428800
            AND ciphertext_bytes = archive_bytes + 16
            AND encryption_format = 1
            AND ready_at IS NOT NULL
            AND expires_at > ready_at
            AND expires_at <= ready_at + interval '24 hours'
        )
    );

CREATE FUNCTION attune.complete_customer_export(
    p_export_id uuid, p_run_id uuid, p_object_id uuid,
    p_object_generation bigint, p_wrapped_dek bytea, p_nonce bytea,
    p_key_resource text, p_archive_sha256 bytea,
    p_ciphertext_sha256 bytea, p_archive_bytes bigint,
    p_ciphertext_bytes bigint, p_encryption_format integer
)
RETURNS TABLE (export_id uuid, export_state text, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_job record;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_export', 'MEMBER') THEN
        RAISE EXCEPTION 'export completer is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_export_id IS NULL OR p_run_id IS NULL OR p_object_id IS NULL
       OR p_object_generation IS NULL OR p_object_generation <= 0
       OR p_wrapped_dek IS NULL OR octet_length(p_wrapped_dek) NOT BETWEEN 1 AND 65536
       OR p_nonce IS NULL OR octet_length(p_nonce) <> 12
       OR p_key_resource IS NULL OR length(p_key_resource) NOT BETWEEN 1 AND 1024
       OR p_archive_sha256 IS NULL OR octet_length(p_archive_sha256) <> 32
       OR p_ciphertext_sha256 IS NULL OR octet_length(p_ciphertext_sha256) <> 32
       OR p_archive_bytes IS NULL OR p_archive_bytes NOT BETWEEN 0 AND 52428800
       OR p_ciphertext_bytes IS NULL OR p_ciphertext_bytes <> p_archive_bytes + 16
       OR p_encryption_format <> 1 THEN
        RAISE EXCEPTION 'invalid encrypted export metadata' USING ERRCODE = '22023';
    END IF;

    UPDATE attune.export_jobs AS job
       SET state = 'ready', object_ref = p_object_id,
           object_generation = p_object_generation,
           wrapped_dek = p_wrapped_dek, nonce = p_nonce,
           key_resource = p_key_resource,
           archive_sha256 = p_archive_sha256,
           ciphertext_sha256 = p_ciphertext_sha256,
           archive_bytes = p_archive_bytes,
           ciphertext_bytes = p_ciphertext_bytes,
           encryption_format = p_encryption_format,
           ready_at = clock_timestamp(),
           expires_at = clock_timestamp() + interval '24 hours',
           lease_run_id = NULL, lease_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE job.id = p_export_id AND job.state = 'running'
       AND job.lease_run_id = p_run_id
       AND job.lease_expires_at > clock_timestamp()
    RETURNING job.id, job.state, job.expires_at INTO v_job;

    IF NOT FOUND THEN
        SELECT job.id, job.state, job.expires_at INTO v_job
          FROM attune.export_jobs AS job
         WHERE job.id = p_export_id AND job.state = 'ready'
           AND job.object_ref = p_object_id
           AND job.object_generation = p_object_generation
           AND job.wrapped_dek = p_wrapped_dek AND job.nonce = p_nonce
           AND job.key_resource = p_key_resource
           AND job.archive_sha256 = p_archive_sha256
           AND job.ciphertext_sha256 = p_ciphertext_sha256
           AND job.archive_bytes = p_archive_bytes
           AND job.ciphertext_bytes = p_ciphertext_bytes
           AND job.encryption_format = p_encryption_format;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'active export claim is required' USING ERRCODE = '42501';
        END IF;
    ELSE
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, target_ref_hash, metadata
        ) SELECT job.tenant_id, 'export',
            attune_ext.digest(pg_catalog.convert_to(
                'export-ready-v1:' || p_export_id::text, 'UTF8'), 'sha256'),
            'system', 'export.ready', 'observed', 'export_job',
            attune_ext.digest(pg_catalog.convert_to(
                p_export_id::text, 'UTF8'), 'sha256'),
            pg_catalog.jsonb_build_object(
                'scope', job.scope ->> 'name',
                'archive_bytes', p_archive_bytes,
                'ciphertext_bytes', p_ciphertext_bytes)
          FROM attune.export_jobs AS job WHERE job.id = p_export_id
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END IF;
    RETURN QUERY SELECT v_job.id, v_job.state, v_job.expires_at;
END
$function$;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_export_coordinator TO %I', current_user);
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_export_coordinator;
ALTER FUNCTION attune.complete_customer_export(
    uuid, uuid, uuid, bigint, bytea, bytea, text,
    bytea, bytea, bigint, bigint, integer
) OWNER TO attune_export_coordinator;
REVOKE CREATE ON SCHEMA attune FROM attune_export_coordinator;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_export_coordinator FROM %I', current_user);
END
$revoke_owner$;
REVOKE ALL ON FUNCTION attune.complete_customer_export(
    uuid, uuid, uuid, bigint, bytea, bytea, text,
    bytea, bytea, bigint, bigint, integer
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION attune.complete_customer_export(
    uuid, uuid, uuid, bigint, bytea, bytea, text,
    bytea, bytea, bigint, bigint, integer
) TO attune_export;
