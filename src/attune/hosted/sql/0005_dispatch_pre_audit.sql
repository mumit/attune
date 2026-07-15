CREATE OR REPLACE FUNCTION attune.request_dispatch_audit(
    p_dispatch_intent_id uuid,
    p_outcome text,
    p_error_code text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_tenant_id uuid;
    v_state text;
    v_audit_intent_id uuid;
    v_idempotency_key bytea;
BEGIN
    IF p_outcome NOT IN ('allowed', 'observed', 'failed') THEN
        RAISE EXCEPTION 'invalid dispatch audit outcome' USING ERRCODE = '22023';
    END IF;
    IF p_error_code IS NOT NULL
       AND length(p_error_code) NOT BETWEEN 1 AND 80 THEN
        RAISE EXCEPTION 'invalid dispatch audit error code'
            USING ERRCODE = '22023';
    END IF;
    IF p_outcome <> 'failed' AND p_error_code IS NOT NULL THEN
        RAISE EXCEPTION 'error code is valid only for failed dispatch'
            USING ERRCODE = '22023';
    END IF;

    SELECT intent.tenant_id, intent.state
      INTO v_tenant_id, v_state
      FROM attune.dispatch_intents AS intent
     WHERE intent.id = p_dispatch_intent_id;
    IF v_tenant_id IS NULL
       OR (p_outcome = 'allowed' AND v_state <> 'leased')
       OR (p_outcome = 'observed' AND v_state <> 'dispatched')
       OR (p_outcome = 'failed' AND v_state NOT IN ('failed', 'cancelled')) THEN
        RETURN NULL;
    END IF;

    v_idempotency_key := attune_ext.digest(
        pg_catalog.convert_to(
            'dispatch-audit-v1:' || p_dispatch_intent_id::text || ':' || p_outcome,
            'UTF8'
        ),
        'sha256'
    );
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action,
        outcome, target_type, target_ref_hash, metadata
    ) VALUES (
        v_tenant_id, 'dispatch_broker', v_idempotency_key, 'workload',
        'task.dispatch', p_outcome, 'dispatch_intent',
        attune_ext.digest(
            pg_catalog.convert_to(p_dispatch_intent_id::text, 'UTF8'), 'sha256'
        ),
        CASE
            WHEN p_error_code IS NULL THEN '{}'::jsonb
            ELSE pg_catalog.jsonb_build_object('error_code', p_error_code)
        END
    )
    ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
       SET idempotency_key = EXCLUDED.idempotency_key
    RETURNING id INTO v_audit_intent_id;
    RETURN v_audit_intent_id;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.request_dispatch_audit(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.request_dispatch_audit(uuid, text, text)
TO attune_dispatch_broker;
