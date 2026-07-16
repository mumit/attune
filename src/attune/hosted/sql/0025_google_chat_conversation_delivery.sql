GRANT SELECT ON attune.principals, attune.hosted_channel_destinations,
    attune.hosted_channel_preferences
TO attune_worker;

CREATE TABLE attune.hosted_channel_deliveries (
    tenant_id uuid NOT NULL,
    job_id uuid NOT NULL,
    destination_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested', 'claimed', 'delivered', 'failed')),
    claim_hash bytea,
    claim_expires_at timestamptz,
    provider_message_ref_hash bytea,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    PRIMARY KEY (tenant_id, job_id),
    UNIQUE (job_id),
    FOREIGN KEY (tenant_id, job_id) REFERENCES attune.jobs(tenant_id, id),
    FOREIGN KEY (tenant_id, destination_id)
        REFERENCES attune.hosted_channel_destinations(tenant_id, id),
    CHECK (
        (state = 'claimed') = (claim_hash IS NOT NULL)
        AND (state = 'claimed') = (claim_expires_at IS NOT NULL)
        AND (claim_hash IS NULL OR octet_length(claim_hash) = 32)
        AND (provider_message_ref_hash IS NULL
             OR octet_length(provider_message_ref_hash) = 32)
        AND (state = 'delivered') = (provider_message_ref_hash IS NOT NULL)
        AND (state = 'delivered') = (delivered_at IS NOT NULL)
    )
);

ALTER TABLE attune.hosted_channel_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.hosted_channel_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.hosted_channel_deliveries
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
REVOKE ALL ON attune.hosted_channel_deliveries FROM PUBLIC;

CREATE FUNCTION attune.claim_google_chat_conversation_delivery(
    p_destination_id uuid, p_job_id uuid, p_claim_hash bytea,
    p_claim_expires_at timestamptz
)
RETURNS TABLE (
    tenant_id uuid, ciphertext bytea, nonce bytea, wrapped_dek bytea,
    key_resource text, format_version integer, reply_text text,
    pre_audit_intent_id uuid, already_delivered boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid;
    v_route attune.hosted_channel_routes%ROWTYPE;
    v_reply text;
    v_delivery attune.hosted_channel_deliveries%ROWTYPE;
    v_audit_id uuid;
    v_audit_key bytea;
    v_matches integer := 0;
    v_delivery_exists boolean := false;
    candidate record;
BEGIN
    IF p_destination_id IS NULL OR p_job_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_claim_expires_at IS NULL
       OR p_claim_expires_at <= clock_timestamp()
       OR p_claim_expires_at > clock_timestamp() + interval '60 seconds' THEN
        RAISE EXCEPTION 'invalid conversation delivery claim'
            USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_job_id::text, 0));
    SELECT delivery.* INTO v_delivery
      FROM attune.hosted_channel_deliveries delivery
     WHERE delivery.job_id = p_job_id FOR UPDATE;
    v_delivery_exists := FOUND;
    IF FOUND AND v_delivery.destination_id <> p_destination_id THEN
        RAISE EXCEPTION 'conversation delivery destination changed'
            USING ERRCODE = '23514';
    END IF;
    IF FOUND AND v_delivery.state = 'delivered' THEN
        RETURN QUERY SELECT v_delivery.tenant_id, NULL::bytea, NULL::bytea,
            NULL::bytea, NULL::text, NULL::integer, NULL::text, NULL::uuid, true;
        RETURN;
    END IF;
    IF FOUND AND v_delivery.state = 'claimed'
       AND v_delivery.claim_expires_at > clock_timestamp() THEN
        RAISE EXCEPTION 'conversation delivery is already claimed'
            USING ERRCODE = '55P03';
    END IF;

    FOR candidate IN
        SELECT job.tenant_id, route.*, turn.content
          FROM attune.jobs job
          JOIN attune.hosted_channel_destinations destination
            ON destination.tenant_id = job.tenant_id
           AND destination.id = p_destination_id
          JOIN attune.hosted_channel_routes route
            ON route.tenant_id = destination.tenant_id
           AND route.destination_id = destination.id
          JOIN attune.tenants tenant ON tenant.id = job.tenant_id
          JOIN attune.principals principal
            ON principal.tenant_id = job.tenant_id
           AND principal.id = destination.owner_principal_id
          JOIN attune.hosted_channel_preferences preference
            ON preference.tenant_id = job.tenant_id
           AND preference.owner_principal_id = destination.owner_principal_id
          JOIN attune.conversation_turns turn
            ON turn.tenant_id = job.tenant_id
           AND turn.conversation_id = (job.payload->>'conversation_id')::uuid
           AND turn.actor_type = 'assistant'
           AND turn.provenance->>'job_id' = job.id::text
         WHERE job.id = p_job_id AND job.kind = 'channel.google_chat.converse'
           AND job.capability = 'assistant.conversation.read'
           AND job.state = 'leased'
           AND job.payload->>'destination_id' = p_destination_id::text
           AND destination.provider = 'google_chat'
           AND destination.visibility = 'owner_dm'
           AND destination.status = 'active'
           AND destination.delivery_verified_at IS NOT NULL
           AND destination.route_version = 1
           AND tenant.status = 'active' AND principal.status = 'active'
           AND 'google_chat' = ANY(preference.interaction_channels)
           AND EXISTS (
               SELECT 1 FROM attune.policies policy
                WHERE policy.tenant_id = job.tenant_id AND policy.active
           )
         LIMIT 2
    LOOP
        v_matches := v_matches + 1;
        v_tenant_id := candidate.tenant_id;
        v_route.tenant_id := candidate.tenant_id;
        v_route.destination_id := candidate.destination_id;
        v_route.ciphertext := candidate.ciphertext;
        v_route.nonce := candidate.nonce;
        v_route.wrapped_dek := candidate.wrapped_dek;
        v_route.key_resource := candidate.key_resource;
        v_route.format_version := candidate.format_version;
        v_reply := candidate.content;
    END LOOP;
    IF v_matches <> 1 OR length(v_reply) NOT BETWEEN 1 AND 8000 THEN
        RAISE EXCEPTION 'canonical conversation delivery is unavailable'
            USING ERRCODE = 'P0002';
    END IF;
    IF v_delivery_exists THEN
        UPDATE attune.hosted_channel_deliveries delivery
           SET state = 'claimed', claim_hash = p_claim_hash,
               claim_expires_at = p_claim_expires_at,
               provider_message_ref_hash = NULL, delivered_at = NULL,
               updated_at = clock_timestamp()
         WHERE delivery.tenant_id = v_tenant_id AND delivery.job_id = p_job_id;
    ELSE
        INSERT INTO attune.hosted_channel_deliveries (
            tenant_id, job_id, destination_id, state, claim_hash, claim_expires_at
        ) VALUES (
            v_tenant_id, p_job_id, p_destination_id, 'claimed', p_claim_hash,
            p_claim_expires_at
        );
    END IF;
    v_audit_key := p_claim_hash;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action, outcome,
        target_type, target_ref_hash, metadata
    ) VALUES (
        v_tenant_id, 'channel_broker', v_audit_key, 'assistant',
        'hosted.channels.google_chat.reply', 'allowed', 'owner_dm',
        (SELECT destination.destination_ref_hash
           FROM attune.hosted_channel_destinations destination
          WHERE destination.tenant_id = v_tenant_id
            AND destination.id = p_destination_id),
        jsonb_build_object('schema_version', 1, 'content_profile', 'conversation_reply_v1')
    ) ON CONFLICT ON CONSTRAINT audit_intents_tenant_id_idempotency_key_key
      DO NOTHING
      RETURNING id INTO v_audit_id;
    IF v_audit_id IS NULL THEN
        SELECT intent.id INTO STRICT v_audit_id FROM attune.audit_intents intent
         WHERE intent.tenant_id = v_tenant_id
           AND intent.idempotency_key = v_audit_key;
    END IF;
    RETURN QUERY SELECT v_tenant_id, v_route.ciphertext, v_route.nonce,
        v_route.wrapped_dek, v_route.key_resource, v_route.format_version,
        v_reply, v_audit_id, false;
END
$function$;

CREATE FUNCTION attune.complete_google_chat_conversation_delivery(
    p_job_id uuid, p_claim_hash bytea, p_succeeded boolean,
    p_provider_message_ref_hash bytea
)
RETURNS TABLE (delivery_state text, outcome_audit_intent_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_delivery attune.hosted_channel_deliveries%ROWTYPE;
    v_destination_hash bytea;
    v_audit_key bytea;
    v_audit_id uuid;
BEGIN
    IF p_job_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_succeeded IS NULL
       OR (p_succeeded AND (p_provider_message_ref_hash IS NULL
           OR octet_length(p_provider_message_ref_hash) <> 32))
       OR (NOT p_succeeded AND p_provider_message_ref_hash IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid conversation delivery completion'
            USING ERRCODE = '22023';
    END IF;
    SELECT delivery.* INTO v_delivery
      FROM attune.hosted_channel_deliveries delivery
     WHERE delivery.job_id = p_job_id AND delivery.state = 'claimed'
       AND delivery.claim_hash = p_claim_hash
       AND delivery.claim_expires_at > clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'conversation delivery claim is unavailable'
            USING ERRCODE = 'P0002';
    END IF;
    UPDATE attune.hosted_channel_deliveries delivery
       SET state = CASE WHEN p_succeeded THEN 'delivered' ELSE 'failed' END,
           claim_hash = NULL, claim_expires_at = NULL,
           provider_message_ref_hash = p_provider_message_ref_hash,
           delivered_at = CASE WHEN p_succeeded THEN clock_timestamp() ELSE NULL END,
           updated_at = clock_timestamp()
     WHERE delivery.tenant_id = v_delivery.tenant_id
       AND delivery.job_id = v_delivery.job_id;
    SELECT destination.destination_ref_hash INTO STRICT v_destination_hash
      FROM attune.hosted_channel_destinations destination
     WHERE destination.tenant_id = v_delivery.tenant_id
       AND destination.id = v_delivery.destination_id;
    v_audit_key := set_byte(
        p_claim_hash, 0,
        get_byte(p_claim_hash, 0) # CASE WHEN p_succeeded THEN 1 ELSE 2 END
    );
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action, outcome,
        target_type, target_ref_hash, metadata
    ) VALUES (
        v_delivery.tenant_id, 'channel_broker', v_audit_key, 'assistant',
        'hosted.channels.google_chat.reply',
        CASE WHEN p_succeeded THEN 'observed' ELSE 'failed' END,
        'owner_dm', v_destination_hash,
        jsonb_build_object('schema_version', 1, 'content_profile', 'conversation_reply_v1')
    ) ON CONFLICT ON CONSTRAINT audit_intents_tenant_id_idempotency_key_key
      DO NOTHING
      RETURNING id INTO v_audit_id;
    IF v_audit_id IS NULL THEN
        SELECT intent.id INTO STRICT v_audit_id FROM attune.audit_intents intent
         WHERE intent.tenant_id = v_delivery.tenant_id
           AND intent.idempotency_key = v_audit_key;
    END IF;
    RETURN QUERY SELECT CASE WHEN p_succeeded THEN 'delivered' ELSE 'failed' END,
                        v_audit_id;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.claim_google_chat_conversation_delivery(uuid,uuid,bytea,timestamptz),
    attune.complete_google_chat_conversation_delivery(uuid,bytea,boolean,bytea)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.claim_google_chat_conversation_delivery(uuid,uuid,bytea,timestamptz),
    attune.complete_google_chat_conversation_delivery(uuid,bytea,boolean,bytea)
TO attune_channel_broker;

GRANT SELECT ON attune.jobs, attune.conversation_turns, attune.connectors,
    attune.policies TO attune_channel_link_executor;
GRANT SELECT, INSERT, UPDATE ON attune.hosted_channel_deliveries
TO attune_channel_link_executor;
DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_channel_link_executor TO %I', current_user);
END
$grant_owner$;
GRANT CREATE ON SCHEMA attune TO attune_channel_link_executor;
ALTER FUNCTION attune.claim_google_chat_conversation_delivery(uuid,uuid,bytea,timestamptz)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.complete_google_chat_conversation_delivery(uuid,bytea,boolean,bytea)
OWNER TO attune_channel_link_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_channel_link_executor FROM %I', current_user);
END
$revoke_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
