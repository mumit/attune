-- Hosted proactive brief delivery (docs/future-state.md Phase 5 item 4, G12).
--
-- A brief is not an interactive conversation turn: it is a system-initiated
-- deliverable with no preceding user message, so it gets its own small,
-- durable table rather than being shoehorned into `attune.conversations`
-- (whose `surface` CHECK and mandatory `installation_id` model an
-- interactive, provider-linked message thread). `hosted_brief_deliveries`
-- stores the bounded rendered brief text directly, keyed
-- (tenant_id, job_id, destination_id) -- deliberately NOT unique on
-- (tenant_id, job_id) alone like `hosted_channel_deliveries`, because one
-- `channel.brief.deliver` job fans out to every ACTIVE destination whose
-- stored preference includes briefs (hosted-channels.md), which can be more
-- than one destination for the same job.
--
-- The worker inserts its own tenant's pending row directly under ordinary
-- RLS (exactly how `PostgresGoogleChatConversationWorkRepository.append_assistant`
-- writes `conversation_turns` -- a worker is already trusted for its own
-- tenant's writes); the channel broker's claim/complete functions below are
-- the only path that can transition a row to `delivered`/`failed`, mirroring
-- `claim_google_chat_conversation_delivery`/`claim_slack_conversation_delivery`
-- exactly except sourcing text from this table (never from a live worker
-- parameter -- docs/hosted-conversation.md's "the worker cannot supply reply
-- text ... the broker reads the stored ... turn itself") and matching
-- `brief_channels` rather than `interaction_channels`.

CREATE TABLE attune.hosted_brief_deliveries (
    tenant_id uuid NOT NULL,
    job_id uuid NOT NULL,
    destination_id uuid NOT NULL,
    brief_text text NOT NULL CHECK (length(brief_text) BETWEEN 1 AND 8000),
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested', 'claimed', 'delivered', 'failed')),
    claim_hash bytea,
    claim_expires_at timestamptz,
    provider_message_ref_hash bytea,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    PRIMARY KEY (tenant_id, job_id, destination_id),
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

ALTER TABLE attune.hosted_brief_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.hosted_brief_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.hosted_brief_deliveries
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
REVOKE ALL ON attune.hosted_brief_deliveries FROM PUBLIC;

-- The worker writes its own tenant's pending row directly (ordinary RLS-scoped
-- INSERT, not a SECURITY DEFINER function) before ever asking the channel
-- broker to claim/deliver it.
GRANT SELECT, INSERT ON attune.hosted_brief_deliveries TO attune_worker;

-- ---------------------------------------------------------------------------
-- Google Chat brief delivery -- mirrors claim_google_chat_conversation_delivery
-- (0025) except: job.kind = 'channel.brief.deliver', capability =
-- 'assistant.brief.deliver', brief_channels (not interaction_channels), text
-- sourced from hosted_brief_deliveries (not conversation_turns), and the
-- claim/complete pair is keyed on (job_id, destination_id) rather than
-- job_id alone, since one job now legitimately fans out to N destinations.
-- ---------------------------------------------------------------------------

CREATE FUNCTION attune.claim_google_chat_brief_delivery(
    p_destination_id uuid, p_job_id uuid, p_claim_hash bytea,
    p_claim_expires_at timestamptz
)
RETURNS TABLE (
    tenant_id uuid, ciphertext bytea, nonce bytea, wrapped_dek bytea,
    key_resource text, format_version integer, brief_text text,
    pre_audit_intent_id uuid, already_delivered boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid;
    v_route attune.hosted_channel_routes%ROWTYPE;
    v_text text;
    v_delivery attune.hosted_brief_deliveries%ROWTYPE;
    v_audit_id uuid;
    v_audit_key bytea;
    v_matches integer := 0;
    v_found boolean := false;
    candidate record;
BEGIN
    IF p_destination_id IS NULL OR p_job_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_claim_expires_at IS NULL
       OR p_claim_expires_at <= clock_timestamp()
       OR p_claim_expires_at > clock_timestamp() + interval '60 seconds' THEN
        RAISE EXCEPTION 'invalid brief delivery claim' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_job_id::text || ':' || p_destination_id::text, 0)
    );
    SELECT delivery.* INTO v_delivery
      FROM attune.hosted_brief_deliveries delivery
     WHERE delivery.job_id = p_job_id AND delivery.destination_id = p_destination_id
     FOR UPDATE;
    v_found := FOUND;
    IF v_found AND v_delivery.state = 'delivered' THEN
        RETURN QUERY SELECT v_delivery.tenant_id, NULL::bytea, NULL::bytea,
            NULL::bytea, NULL::text, NULL::integer, NULL::text, NULL::uuid, true;
        RETURN;
    END IF;
    IF v_found AND v_delivery.state = 'claimed'
       AND v_delivery.claim_expires_at > clock_timestamp() THEN
        RAISE EXCEPTION 'brief delivery is already claimed' USING ERRCODE = '55P03';
    END IF;
    IF NOT v_found THEN
        RAISE EXCEPTION 'canonical brief delivery is unavailable' USING ERRCODE = 'P0002';
    END IF;

    FOR candidate IN
        SELECT job.tenant_id, route.*, delivery.brief_text
          FROM attune.jobs job
          JOIN attune.hosted_brief_deliveries delivery
            ON delivery.tenant_id = job.tenant_id
           AND delivery.job_id = job.id
           AND delivery.destination_id = p_destination_id
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
         WHERE job.id = p_job_id AND job.kind = 'channel.brief.deliver'
           AND job.capability = 'assistant.brief.deliver'
           AND job.state = 'leased'
           AND destination.provider = 'google_chat'
           AND destination.visibility = 'owner_dm'
           AND destination.status = 'active'
           AND destination.delivery_verified_at IS NOT NULL
           AND destination.route_version = 1
           AND tenant.status = 'active' AND principal.status = 'active'
           AND 'google_chat' = ANY(preference.brief_channels)
           AND EXISTS (
               SELECT 1 FROM attune.policies policy
                WHERE policy.tenant_id = job.tenant_id AND policy.active
           )
         LIMIT 2
    LOOP
        v_matches := v_matches + 1;
        v_tenant_id := candidate.tenant_id;
        v_route.ciphertext := candidate.ciphertext;
        v_route.nonce := candidate.nonce;
        v_route.wrapped_dek := candidate.wrapped_dek;
        v_route.key_resource := candidate.key_resource;
        v_route.format_version := candidate.format_version;
        v_text := candidate.brief_text;
    END LOOP;
    IF v_matches <> 1 OR length(v_text) NOT BETWEEN 1 AND 8000 THEN
        RAISE EXCEPTION 'canonical brief delivery is unavailable' USING ERRCODE = 'P0002';
    END IF;

    UPDATE attune.hosted_brief_deliveries delivery
       SET state = 'claimed', claim_hash = p_claim_hash,
           claim_expires_at = p_claim_expires_at,
           provider_message_ref_hash = NULL, delivered_at = NULL,
           updated_at = clock_timestamp()
     WHERE delivery.tenant_id = v_tenant_id AND delivery.job_id = p_job_id
       AND delivery.destination_id = p_destination_id;

    v_audit_key := p_claim_hash;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action, outcome,
        target_type, target_ref_hash, metadata
    ) VALUES (
        v_tenant_id, 'channel_broker', v_audit_key, 'assistant',
        'hosted.channels.google_chat.brief', 'allowed', 'owner_dm',
        (SELECT destination.destination_ref_hash
           FROM attune.hosted_channel_destinations destination
          WHERE destination.tenant_id = v_tenant_id
            AND destination.id = p_destination_id),
        jsonb_build_object('schema_version', 1, 'content_profile', 'brief_v1')
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
        v_text, v_audit_id, false;
END
$function$;

CREATE FUNCTION attune.complete_google_chat_brief_delivery(
    p_job_id uuid, p_destination_id uuid, p_claim_hash bytea, p_succeeded boolean,
    p_provider_message_ref_hash bytea
)
RETURNS TABLE (delivery_state text, outcome_audit_intent_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_delivery attune.hosted_brief_deliveries%ROWTYPE;
    v_destination_hash bytea;
    v_audit_key bytea;
    v_audit_id uuid;
BEGIN
    IF p_job_id IS NULL OR p_destination_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_succeeded IS NULL
       OR (p_succeeded AND (p_provider_message_ref_hash IS NULL
           OR octet_length(p_provider_message_ref_hash) <> 32))
       OR (NOT p_succeeded AND p_provider_message_ref_hash IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid brief delivery completion' USING ERRCODE = '22023';
    END IF;
    SELECT delivery.* INTO v_delivery
      FROM attune.hosted_brief_deliveries delivery
     WHERE delivery.job_id = p_job_id AND delivery.destination_id = p_destination_id
       AND delivery.state = 'claimed' AND delivery.claim_hash = p_claim_hash
       AND delivery.claim_expires_at > clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'brief delivery claim is unavailable' USING ERRCODE = 'P0002';
    END IF;
    UPDATE attune.hosted_brief_deliveries delivery
       SET state = CASE WHEN p_succeeded THEN 'delivered' ELSE 'failed' END,
           claim_hash = NULL, claim_expires_at = NULL,
           provider_message_ref_hash = p_provider_message_ref_hash,
           delivered_at = CASE WHEN p_succeeded THEN clock_timestamp() ELSE NULL END,
           updated_at = clock_timestamp()
     WHERE delivery.tenant_id = v_delivery.tenant_id
       AND delivery.job_id = v_delivery.job_id
       AND delivery.destination_id = v_delivery.destination_id;
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
        'hosted.channels.google_chat.brief',
        CASE WHEN p_succeeded THEN 'observed' ELSE 'failed' END,
        'owner_dm', v_destination_hash,
        jsonb_build_object('schema_version', 1, 'content_profile', 'brief_v1')
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

-- ---------------------------------------------------------------------------
-- Slack brief delivery -- mirrors claim_slack_conversation_delivery (0038)
-- with the same substitutions as the Google Chat pair above.
-- ---------------------------------------------------------------------------

CREATE FUNCTION attune.claim_slack_brief_delivery(
    p_destination_id uuid, p_job_id uuid, p_claim_hash bytea,
    p_claim_expires_at timestamptz
)
RETURNS TABLE (
    tenant_id uuid,
    route_ciphertext bytea, route_nonce bytea, route_wrapped_dek bytea,
    route_key_resource text, route_format_version integer,
    token_ciphertext bytea, token_nonce bytea, token_wrapped_dek bytea,
    token_key_resource text, token_format_version integer,
    brief_text text, pre_audit_intent_id uuid, already_delivered boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid;
    v_route attune.hosted_channel_routes%ROWTYPE;
    v_credential attune.hosted_channel_credentials%ROWTYPE;
    v_text text;
    v_delivery attune.hosted_brief_deliveries%ROWTYPE;
    v_audit_id uuid;
    v_audit_key bytea;
    v_matches integer := 0;
    v_found boolean := false;
    candidate record;
BEGIN
    IF p_destination_id IS NULL OR p_job_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_claim_expires_at IS NULL
       OR p_claim_expires_at <= clock_timestamp()
       OR p_claim_expires_at > clock_timestamp() + interval '60 seconds' THEN
        RAISE EXCEPTION 'invalid brief delivery claim' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_job_id::text || ':' || p_destination_id::text, 0)
    );
    SELECT delivery.* INTO v_delivery
      FROM attune.hosted_brief_deliveries delivery
     WHERE delivery.job_id = p_job_id AND delivery.destination_id = p_destination_id
     FOR UPDATE;
    v_found := FOUND;
    IF v_found AND v_delivery.state = 'delivered' THEN
        RETURN QUERY SELECT v_delivery.tenant_id,
            NULL::bytea, NULL::bytea, NULL::bytea, NULL::text, NULL::integer,
            NULL::bytea, NULL::bytea, NULL::bytea, NULL::text, NULL::integer,
            NULL::text, NULL::uuid, true;
        RETURN;
    END IF;
    IF v_found AND v_delivery.state = 'claimed'
       AND v_delivery.claim_expires_at > clock_timestamp() THEN
        RAISE EXCEPTION 'brief delivery is already claimed' USING ERRCODE = '55P03';
    END IF;
    IF NOT v_found THEN
        RAISE EXCEPTION 'canonical brief delivery is unavailable' USING ERRCODE = 'P0002';
    END IF;

    FOR candidate IN
        SELECT job.tenant_id, route.*, credential.ciphertext AS credential_ciphertext,
               credential.nonce AS credential_nonce,
               credential.wrapped_dek AS credential_wrapped_dek,
               credential.key_resource AS credential_key_resource,
               credential.format_version AS credential_format_version,
               delivery.brief_text
          FROM attune.jobs job
          JOIN attune.hosted_brief_deliveries delivery
            ON delivery.tenant_id = job.tenant_id
           AND delivery.job_id = job.id
           AND delivery.destination_id = p_destination_id
          JOIN attune.hosted_channel_destinations destination
            ON destination.tenant_id = job.tenant_id
           AND destination.id = p_destination_id
          JOIN attune.hosted_channel_routes route
            ON route.tenant_id = destination.tenant_id
           AND route.destination_id = destination.id
          JOIN attune.hosted_channel_credentials credential
            ON credential.tenant_id = destination.tenant_id
           AND credential.destination_id = destination.id
           AND credential.purpose = 'slack_bot_token'
          JOIN attune.tenants tenant ON tenant.id = job.tenant_id
          JOIN attune.principals principal
            ON principal.tenant_id = job.tenant_id
           AND principal.id = destination.owner_principal_id
          JOIN attune.hosted_channel_preferences preference
            ON preference.tenant_id = job.tenant_id
           AND preference.owner_principal_id = destination.owner_principal_id
         WHERE job.id = p_job_id AND job.kind = 'channel.brief.deliver'
           AND job.capability = 'assistant.brief.deliver'
           AND job.state = 'leased'
           AND destination.provider = 'slack'
           AND destination.visibility = 'owner_dm'
           AND destination.status = 'active'
           AND destination.delivery_verified_at IS NOT NULL
           AND destination.route_version = 1
           AND tenant.status = 'active' AND principal.status = 'active'
           AND 'slack' = ANY(preference.brief_channels)
           AND EXISTS (
               SELECT 1 FROM attune.policies policy
                WHERE policy.tenant_id = job.tenant_id AND policy.active
           )
         LIMIT 2
    LOOP
        v_matches := v_matches + 1;
        v_tenant_id := candidate.tenant_id;
        v_route.ciphertext := candidate.ciphertext;
        v_route.nonce := candidate.nonce;
        v_route.wrapped_dek := candidate.wrapped_dek;
        v_route.key_resource := candidate.key_resource;
        v_route.format_version := candidate.format_version;
        v_credential.ciphertext := candidate.credential_ciphertext;
        v_credential.nonce := candidate.credential_nonce;
        v_credential.wrapped_dek := candidate.credential_wrapped_dek;
        v_credential.key_resource := candidate.credential_key_resource;
        v_credential.format_version := candidate.credential_format_version;
        v_text := candidate.brief_text;
    END LOOP;
    IF v_matches <> 1 OR length(v_text) NOT BETWEEN 1 AND 8000 THEN
        RAISE EXCEPTION 'canonical brief delivery is unavailable' USING ERRCODE = 'P0002';
    END IF;

    UPDATE attune.hosted_brief_deliveries delivery
       SET state = 'claimed', claim_hash = p_claim_hash,
           claim_expires_at = p_claim_expires_at,
           provider_message_ref_hash = NULL, delivered_at = NULL,
           updated_at = clock_timestamp()
     WHERE delivery.tenant_id = v_tenant_id AND delivery.job_id = p_job_id
       AND delivery.destination_id = p_destination_id;

    v_audit_key := p_claim_hash;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action, outcome,
        target_type, target_ref_hash, metadata
    ) VALUES (
        v_tenant_id, 'channel_broker', v_audit_key, 'assistant',
        'hosted.channels.slack.brief', 'allowed', 'owner_dm',
        (SELECT destination.destination_ref_hash
           FROM attune.hosted_channel_destinations destination
          WHERE destination.tenant_id = v_tenant_id
            AND destination.id = p_destination_id),
        jsonb_build_object('schema_version', 1, 'content_profile', 'brief_v1')
    ) ON CONFLICT ON CONSTRAINT audit_intents_tenant_id_idempotency_key_key
      DO NOTHING
      RETURNING id INTO v_audit_id;
    IF v_audit_id IS NULL THEN
        SELECT intent.id INTO STRICT v_audit_id FROM attune.audit_intents intent
         WHERE intent.tenant_id = v_tenant_id
           AND intent.idempotency_key = v_audit_key;
    END IF;
    RETURN QUERY SELECT v_tenant_id,
        v_route.ciphertext, v_route.nonce, v_route.wrapped_dek,
        v_route.key_resource, v_route.format_version,
        v_credential.ciphertext, v_credential.nonce, v_credential.wrapped_dek,
        v_credential.key_resource, v_credential.format_version,
        v_text, v_audit_id, false;
END
$function$;

CREATE FUNCTION attune.complete_slack_brief_delivery(
    p_job_id uuid, p_destination_id uuid, p_claim_hash bytea, p_succeeded boolean,
    p_provider_message_ref_hash bytea
)
RETURNS TABLE (delivery_state text, outcome_audit_intent_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_delivery attune.hosted_brief_deliveries%ROWTYPE;
    v_destination_hash bytea;
    v_audit_key bytea;
    v_audit_id uuid;
BEGIN
    IF p_job_id IS NULL OR p_destination_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_succeeded IS NULL
       OR (p_succeeded AND (p_provider_message_ref_hash IS NULL
           OR octet_length(p_provider_message_ref_hash) <> 32))
       OR (NOT p_succeeded AND p_provider_message_ref_hash IS NOT NULL) THEN
        RAISE EXCEPTION 'invalid brief delivery completion' USING ERRCODE = '22023';
    END IF;
    SELECT delivery.* INTO v_delivery
      FROM attune.hosted_brief_deliveries delivery
     WHERE delivery.job_id = p_job_id AND delivery.destination_id = p_destination_id
       AND delivery.state = 'claimed' AND delivery.claim_hash = p_claim_hash
       AND delivery.claim_expires_at > clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'brief delivery claim is unavailable' USING ERRCODE = 'P0002';
    END IF;
    UPDATE attune.hosted_brief_deliveries delivery
       SET state = CASE WHEN p_succeeded THEN 'delivered' ELSE 'failed' END,
           claim_hash = NULL, claim_expires_at = NULL,
           provider_message_ref_hash = p_provider_message_ref_hash,
           delivered_at = CASE WHEN p_succeeded THEN clock_timestamp() ELSE NULL END,
           updated_at = clock_timestamp()
     WHERE delivery.tenant_id = v_delivery.tenant_id
       AND delivery.job_id = v_delivery.job_id
       AND delivery.destination_id = v_delivery.destination_id;
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
        'hosted.channels.slack.brief',
        CASE WHEN p_succeeded THEN 'observed' ELSE 'failed' END,
        'owner_dm', v_destination_hash,
        jsonb_build_object('schema_version', 1, 'content_profile', 'brief_v1')
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
    attune.claim_google_chat_brief_delivery(uuid,uuid,bytea,timestamptz),
    attune.complete_google_chat_brief_delivery(uuid,uuid,bytea,boolean,bytea),
    attune.claim_slack_brief_delivery(uuid,uuid,bytea,timestamptz),
    attune.complete_slack_brief_delivery(uuid,uuid,bytea,boolean,bytea)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.claim_google_chat_brief_delivery(uuid,uuid,bytea,timestamptz),
    attune.complete_google_chat_brief_delivery(uuid,uuid,bytea,boolean,bytea),
    attune.claim_slack_brief_delivery(uuid,uuid,bytea,timestamptz),
    attune.complete_slack_brief_delivery(uuid,uuid,bytea,boolean,bytea)
TO attune_channel_broker;

GRANT SELECT, INSERT, UPDATE ON attune.hosted_brief_deliveries
TO attune_channel_link_executor;
DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_channel_link_executor TO %I', current_user);
END
$grant_owner$;
GRANT CREATE ON SCHEMA attune TO attune_channel_link_executor;
ALTER FUNCTION attune.claim_google_chat_brief_delivery(uuid,uuid,bytea,timestamptz)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.complete_google_chat_brief_delivery(uuid,uuid,bytea,boolean,bytea)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.claim_slack_brief_delivery(uuid,uuid,bytea,timestamptz)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.complete_slack_brief_delivery(uuid,uuid,bytea,boolean,bytea)
OWNER TO attune_channel_link_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_channel_link_executor FROM %I', current_user);
END
$revoke_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
