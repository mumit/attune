CREATE TABLE attune.hosted_channel_routes (
    tenant_id uuid NOT NULL,
    destination_id uuid NOT NULL,
    ciphertext bytea NOT NULL,
    nonce bytea NOT NULL CHECK (octet_length(nonce) = 12),
    wrapped_dek bytea NOT NULL,
    key_resource text NOT NULL CHECK (length(key_resource) BETWEEN 20 AND 1024),
    format_version integer NOT NULL DEFAULT 1 CHECK (format_version = 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, destination_id),
    FOREIGN KEY (tenant_id, destination_id)
        REFERENCES attune.hosted_channel_destinations(tenant_id, id)
        ON DELETE CASCADE
);

ALTER TABLE attune.hosted_channel_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.hosted_channel_routes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.hosted_channel_routes
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
REVOKE ALL ON attune.hosted_channel_routes FROM PUBLIC;

ALTER TABLE attune.hosted_channel_destinations
ADD COLUMN route_version integer CHECK (route_version = 1),
ADD COLUMN delivery_claim_hash bytea,
ADD COLUMN delivery_claim_expires_at timestamptz,
ADD CONSTRAINT hosted_channel_delivery_claim_shape CHECK (
    (delivery_claim_hash IS NULL) = (delivery_claim_expires_at IS NULL)
    AND (delivery_claim_hash IS NULL OR octet_length(delivery_claim_hash) = 32)
    AND (delivery_claim_hash IS NULL OR status = 'pending_test')
),
ADD CONSTRAINT hosted_channel_active_route CHECK (
    status <> 'active' OR route_version = 1
);

CREATE FUNCTION attune.begin_hosted_channel_setup_v2(
    p_principal_id uuid, p_session_id uuid, p_provider text,
    p_mechanism text, p_secret_hash bytea, p_expires_at timestamptz
)
RETURNS TABLE (
    transaction_id uuid, preference_revision bigint, provider text,
    mechanism text, state text, expires_at timestamptz
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_preference attune.hosted_channel_preferences%ROWTYPE;
    v_transaction attune.hosted_channel_setup_transactions%ROWTYPE;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM attune.hosted_channel_destinations destination
         WHERE destination.tenant_id = v_tenant_id
           AND destination.owner_principal_id = p_principal_id
           AND destination.provider = 'google_chat'
           AND destination.status = 'pending_test'
           AND destination.route_version IS NULL
    ) THEN
        RETURN QUERY SELECT * FROM attune.begin_hosted_channel_setup(
            p_principal_id, p_session_id, p_provider, p_mechanism,
            p_secret_hash, p_expires_at
        );
        RETURN;
    END IF;
    IF p_provider <> 'google_chat' OR p_mechanism <> 'link_code'
       OR p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32
       OR p_expires_at < clock_timestamp() + interval '2 minutes'
       OR p_expires_at > clock_timestamp() + interval '10 minutes' THEN
        RAISE EXCEPTION 'channel adoption request is invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        v_tenant_id::text || ':' || p_principal_id::text || ':google_chat:channel-setup', 0
    ));
    IF NOT EXISTS (
        SELECT 1 FROM attune.identity_sessions session
         JOIN attune.principals principal
           ON principal.tenant_id = session.tenant_id
          AND principal.id = session.principal_id
         JOIN attune.tenants tenant ON tenant.id = session.tenant_id
         WHERE session.tenant_id = v_tenant_id AND session.id = p_session_id
           AND session.principal_id = p_principal_id
           AND session.revoked_at IS NULL AND session.expires_at > clock_timestamp()
           AND session.created_at >= clock_timestamp() - interval '10 minutes'
           AND principal.status = 'active' AND tenant.status = 'active'
    ) THEN
        RAISE EXCEPTION 'channel adoption principal is unavailable'
            USING ERRCODE = '23514';
    END IF;
    SELECT preference.* INTO v_preference
      FROM attune.hosted_channel_preferences preference
      JOIN attune.hosted_onboarding_states onboarding
        ON onboarding.tenant_id = preference.tenant_id
       AND onboarding.owner_principal_id = preference.owner_principal_id
     WHERE preference.tenant_id = v_tenant_id
       AND preference.owner_principal_id = p_principal_id
       AND onboarding.channels_status IN ('authorized', 'applied')
       AND 'google_chat' = ANY(
           preference.interaction_channels || preference.brief_channels
       );
    IF NOT FOUND THEN
        RAISE EXCEPTION 'selected channel adoption is unavailable'
            USING ERRCODE = '23514';
    END IF;
    UPDATE attune.hosted_channel_setup_transactions transaction
       SET state = CASE WHEN transaction.expires_at <= clock_timestamp()
                        THEN 'expired' ELSE 'cancelled' END,
           updated_at = clock_timestamp()
     WHERE transaction.tenant_id = v_tenant_id
       AND transaction.owner_principal_id = p_principal_id
       AND transaction.provider = 'google_chat'
       AND transaction.state = 'pending';
    INSERT INTO attune.hosted_channel_setup_transactions (
        tenant_id, owner_principal_id, session_id, preference_revision,
        provider, mechanism, secret_hash, expires_at
    ) VALUES (
        v_tenant_id, p_principal_id, p_session_id, v_preference.revision,
        'google_chat', 'link_code', p_secret_hash, p_expires_at
    ) RETURNING * INTO v_transaction;
    RETURN QUERY SELECT v_transaction.id, v_transaction.preference_revision,
                        v_transaction.provider, v_transaction.mechanism,
                        v_transaction.state, v_transaction.expires_at;
END
$function$;

CREATE FUNCTION attune.resolve_google_chat_link_destination(
    p_secret_hash bytea, p_claim_hash bytea, p_candidate_id uuid
)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_destination_id uuid;
BEGIN
    IF p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32
       OR p_claim_hash IS NULL OR octet_length(p_claim_hash) <> 32
       OR p_candidate_id IS NULL THEN
        RAISE EXCEPTION 'invalid channel destination resolution'
            USING ERRCODE = '22023';
    END IF;
    SELECT destination.id INTO v_destination_id
      FROM attune.hosted_channel_setup_transactions setup
      LEFT JOIN attune.hosted_channel_destinations destination
        ON destination.tenant_id = setup.tenant_id
       AND destination.owner_principal_id = setup.owner_principal_id
       AND destination.provider = 'google_chat'
       AND destination.status = 'pending_test'
       AND destination.route_version IS NULL
     WHERE setup.provider = 'google_chat' AND setup.mechanism = 'link_code'
       AND setup.secret_hash = p_secret_hash AND setup.state = 'claimed'
       AND setup.claim_hash = p_claim_hash
       AND setup.claim_expires_at > clock_timestamp()
       AND setup.expires_at > clock_timestamp();
    IF NOT FOUND THEN
        RAISE EXCEPTION 'channel link is unavailable' USING ERRCODE = 'P0002';
    END IF;
    RETURN COALESCE(v_destination_id, p_candidate_id);
END
$function$;

CREATE FUNCTION attune.consume_google_chat_link_v2(
    p_secret_hash bytea, p_claim_hash bytea,
    p_installation_ref_hash bytea, p_actor_ref_hash bytea,
    p_destination_ref_hash bytea, p_destination_id uuid,
    p_ciphertext bytea, p_nonce bytea, p_wrapped_dek bytea,
    p_key_resource text, p_format_version integer
)
RETURNS TABLE (
    tenant_id uuid, owner_principal_id uuid, installation_id uuid,
    destination_id uuid, destination_status text, outcome_audit_intent_id uuid
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_setup attune.hosted_channel_setup_transactions%ROWTYPE;
    v_installation_id uuid;
    v_audit_id uuid;
    v_existing attune.hosted_channel_destinations%ROWTYPE;
BEGIN
    IF p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32
       OR p_claim_hash IS NULL OR octet_length(p_claim_hash) <> 32
       OR p_installation_ref_hash IS NULL OR octet_length(p_installation_ref_hash) <> 32
       OR p_actor_ref_hash IS NULL OR octet_length(p_actor_ref_hash) <> 32
       OR p_destination_ref_hash IS NULL OR octet_length(p_destination_ref_hash) <> 32
       OR p_destination_id IS NULL OR p_ciphertext IS NULL OR length(p_ciphertext) < 17
       OR p_nonce IS NULL OR octet_length(p_nonce) <> 12
       OR p_wrapped_dek IS NULL OR length(p_wrapped_dek) < 1
       OR p_key_resource IS NULL OR length(p_key_resource) NOT BETWEEN 20 AND 1024
       OR p_format_version <> 1 THEN
        RAISE EXCEPTION 'invalid channel link consumption' USING ERRCODE = '22023';
    END IF;
    SELECT setup.* INTO v_setup
      FROM attune.hosted_channel_setup_transactions setup
      JOIN attune.tenants tenant ON tenant.id = setup.tenant_id
      JOIN attune.principals principal
        ON principal.tenant_id = setup.tenant_id AND principal.id = setup.owner_principal_id
      JOIN attune.hosted_channel_preferences preference
        ON preference.tenant_id = setup.tenant_id
       AND preference.owner_principal_id = setup.owner_principal_id
     WHERE setup.provider = 'google_chat' AND setup.mechanism = 'link_code'
       AND setup.secret_hash = p_secret_hash AND setup.state = 'claimed'
       AND setup.claim_hash = p_claim_hash
       AND setup.claim_expires_at > clock_timestamp()
       AND setup.expires_at > clock_timestamp()
       AND tenant.status = 'active' AND principal.status = 'active'
       AND setup.preference_revision = preference.revision
       AND 'google_chat' = ANY(preference.interaction_channels || preference.brief_channels)
     FOR UPDATE OF setup;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'channel link is unavailable' USING ERRCODE = 'P0002';
    END IF;
    SELECT destination.* INTO v_existing
      FROM attune.hosted_channel_destinations destination
     WHERE destination.tenant_id = v_setup.tenant_id
       AND destination.provider = 'google_chat'
     FOR UPDATE;
    IF FOUND AND (
        v_existing.owner_principal_id <> v_setup.owner_principal_id
        OR v_existing.status <> 'pending_test'
        OR v_existing.route_version IS NOT NULL
        OR v_existing.installation_ref_hash <> p_installation_ref_hash
        OR v_existing.actor_ref_hash <> p_actor_ref_hash
        OR v_existing.destination_ref_hash <> p_destination_ref_hash
    ) THEN
        RAISE EXCEPTION 'channel destination requires replacement ceremony'
            USING ERRCODE = '23514';
    END IF;

    IF NOT FOUND THEN
      INSERT INTO attune.installations (
        tenant_id, provider, kind, external_ref_hash, metadata
      ) VALUES (
        v_setup.tenant_id, 'google', 'channel', p_installation_ref_hash,
        jsonb_build_object('surface', 'google_chat', 'schema_version', 1)
      ) RETURNING id INTO v_installation_id;
      INSERT INTO attune.hosted_channel_destinations (
        tenant_id, id, owner_principal_id, installation_id, provider,
        installation_ref_hash, actor_ref_hash, destination_ref_hash,
        ingress_verified_at, route_version
      ) VALUES (
        v_setup.tenant_id, p_destination_id, v_setup.owner_principal_id,
        v_installation_id, 'google_chat', p_installation_ref_hash,
        p_actor_ref_hash, p_destination_ref_hash, clock_timestamp(), 1
      );
    ELSE
      v_installation_id := v_existing.installation_id;
      p_destination_id := v_existing.id;
      UPDATE attune.hosted_channel_destinations destination
         SET route_version = 1, updated_at = clock_timestamp()
       WHERE destination.tenant_id = v_setup.tenant_id
         AND destination.id = v_existing.id;
    END IF;
    INSERT INTO attune.hosted_channel_routes (
        tenant_id, destination_id, ciphertext, nonce, wrapped_dek,
        key_resource, format_version
    ) VALUES (
        v_setup.tenant_id, p_destination_id, p_ciphertext, p_nonce,
        p_wrapped_dek, p_key_resource, p_format_version
    );
    UPDATE attune.hosted_channel_setup_transactions AS consumed_setup
       SET state = 'consumed', consumed_at = clock_timestamp(),
           claim_hash = NULL, claim_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE consumed_setup.tenant_id = v_setup.tenant_id
       AND consumed_setup.id = v_setup.id;
    UPDATE attune.hosted_onboarding_states onboarding
       SET channels_status = 'applied', revision = revision + 1,
           updated_at = clock_timestamp()
     WHERE onboarding.tenant_id = v_setup.tenant_id
       AND onboarding.owner_principal_id = v_setup.owner_principal_id
       AND onboarding.channels_status = 'authorized';
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, actor_ref_hash,
        action, outcome, target_type, target_ref_hash, metadata
    ) VALUES (
        v_setup.tenant_id, 'channel_broker',
        set_byte(p_claim_hash, 0, get_byte(p_claim_hash, 0) # 1),
        'provider', p_actor_ref_hash, 'hosted.channels.google_chat.link',
        'observed', 'owner_dm', p_destination_ref_hash,
        jsonb_build_object('schema_version', 1)
    ) RETURNING id INTO v_audit_id;
    RETURN QUERY SELECT v_setup.tenant_id, v_setup.owner_principal_id,
                        v_installation_id, p_destination_id,
                        'pending_test'::text, v_audit_id;
END
$function$;

CREATE FUNCTION attune.claim_google_chat_delivery_test(
    p_destination_id uuid, p_claim_hash bytea, p_claim_expires_at timestamptz
)
RETURNS TABLE (
    tenant_id uuid, owner_principal_id uuid, ciphertext bytea, nonce bytea,
    wrapped_dek bytea, key_resource text, format_version integer,
    pre_audit_intent_id uuid
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_destination attune.hosted_channel_destinations%ROWTYPE;
    v_route attune.hosted_channel_routes%ROWTYPE;
    v_audit_id uuid;
BEGIN
    IF p_destination_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_claim_expires_at IS NULL
       OR p_claim_expires_at <= clock_timestamp()
       OR p_claim_expires_at > clock_timestamp() + interval '60 seconds' THEN
        RAISE EXCEPTION 'invalid channel delivery claim' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_destination_id::text, 0));
    UPDATE attune.hosted_channel_destinations destination
       SET delivery_claim_hash = NULL, delivery_claim_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE destination.id = p_destination_id
       AND destination.status = 'pending_test'
       AND destination.delivery_claim_expires_at <= clock_timestamp();
    SELECT destination.* INTO v_destination
      FROM attune.hosted_channel_destinations destination
      JOIN attune.tenants tenant ON tenant.id = destination.tenant_id
      JOIN attune.principals principal
        ON principal.tenant_id = destination.tenant_id
       AND principal.id = destination.owner_principal_id
      JOIN attune.hosted_channel_preferences preference
        ON preference.tenant_id = destination.tenant_id
       AND preference.owner_principal_id = destination.owner_principal_id
     WHERE destination.id = p_destination_id
       AND destination.provider = 'google_chat'
       AND destination.visibility = 'owner_dm'
       AND destination.status = 'pending_test'
       AND destination.delivery_claim_hash IS NULL
       AND tenant.status = 'active' AND principal.status = 'active'
       AND 'google_chat' = ANY(preference.interaction_channels || preference.brief_channels)
     FOR UPDATE OF destination;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'channel delivery test is unavailable' USING ERRCODE = 'P0002';
    END IF;
    SELECT route.* INTO STRICT v_route
      FROM attune.hosted_channel_routes route
     WHERE route.tenant_id = v_destination.tenant_id
       AND route.destination_id = v_destination.id;
    UPDATE attune.hosted_channel_destinations destination
       SET delivery_claim_hash = p_claim_hash,
           delivery_claim_expires_at = p_claim_expires_at,
           updated_at = clock_timestamp()
     WHERE destination.tenant_id = v_destination.tenant_id
       AND destination.id = v_destination.id;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action, outcome,
        target_type, target_ref_hash, metadata
    ) VALUES (
        v_destination.tenant_id, 'channel_broker', p_claim_hash,
        'principal', 'hosted.channels.google_chat.delivery_test', 'allowed',
        'owner_dm', v_destination.destination_ref_hash,
        jsonb_build_object('schema_version', 1, 'content_profile', 'fixed_connection_test_v1')
    ) RETURNING id INTO v_audit_id;
    RETURN QUERY SELECT v_destination.tenant_id, v_destination.owner_principal_id,
                        v_route.ciphertext, v_route.nonce, v_route.wrapped_dek,
                        v_route.key_resource, v_route.format_version, v_audit_id;
END
$function$;

CREATE FUNCTION attune.complete_google_chat_delivery_test(
    p_destination_id uuid, p_claim_hash bytea, p_succeeded boolean
)
RETURNS TABLE (destination_status text, outcome_audit_intent_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_destination attune.hosted_channel_destinations%ROWTYPE;
    v_audit_id uuid;
    v_all_active boolean;
BEGIN
    IF p_destination_id IS NULL OR p_claim_hash IS NULL
       OR octet_length(p_claim_hash) <> 32 OR p_succeeded IS NULL THEN
        RAISE EXCEPTION 'invalid channel delivery completion' USING ERRCODE = '22023';
    END IF;
    SELECT destination.* INTO v_destination
      FROM attune.hosted_channel_destinations destination
     WHERE destination.id = p_destination_id
       AND destination.status = 'pending_test'
       AND destination.delivery_claim_hash = p_claim_hash
       AND destination.delivery_claim_expires_at > clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'channel delivery test is unavailable' USING ERRCODE = 'P0002';
    END IF;
    UPDATE attune.hosted_channel_destinations destination
       SET status = CASE WHEN p_succeeded THEN 'active' ELSE 'pending_test' END,
           delivery_verified_at = CASE WHEN p_succeeded THEN clock_timestamp() ELSE NULL END,
           delivery_claim_hash = NULL, delivery_claim_expires_at = NULL,
           version = CASE WHEN p_succeeded THEN destination.version + 1 ELSE destination.version END,
           updated_at = clock_timestamp()
     WHERE destination.tenant_id = v_destination.tenant_id
       AND destination.id = v_destination.id;
    IF p_succeeded THEN
        SELECT NOT EXISTS (
            SELECT selected.provider
              FROM (
                    SELECT DISTINCT unnest(
                        preference.interaction_channels || preference.brief_channels
                    ) AS provider
                      FROM attune.hosted_channel_preferences preference
                     WHERE preference.tenant_id = v_destination.tenant_id
                       AND preference.owner_principal_id = v_destination.owner_principal_id
              ) selected
             WHERE NOT EXISTS (
                    SELECT 1 FROM attune.hosted_channel_destinations destination
                     WHERE destination.tenant_id = v_destination.tenant_id
                       AND destination.owner_principal_id = v_destination.owner_principal_id
                       AND destination.provider = selected.provider
                       AND destination.status = 'active'
             )
        ) INTO v_all_active;
        IF v_all_active THEN
            UPDATE attune.hosted_onboarding_states onboarding
               SET channels_status = 'validated', revision = revision + 1,
                   updated_at = clock_timestamp()
             WHERE onboarding.tenant_id = v_destination.tenant_id
               AND onboarding.owner_principal_id = v_destination.owner_principal_id
               AND onboarding.channels_status IN ('authorized', 'applied');
        END IF;
    END IF;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action, outcome,
        target_type, target_ref_hash, metadata
    ) VALUES (
        v_destination.tenant_id, 'channel_broker',
        set_byte(p_claim_hash, 0, get_byte(p_claim_hash, 0) # 1),
        'principal', 'hosted.channels.google_chat.delivery_test',
        CASE WHEN p_succeeded THEN 'observed' ELSE 'failed' END,
        'owner_dm', v_destination.destination_ref_hash,
        jsonb_build_object('schema_version', 1, 'content_profile', 'fixed_connection_test_v1')
    ) RETURNING id INTO v_audit_id;
    RETURN QUERY SELECT CASE WHEN p_succeeded THEN 'active' ELSE 'pending_test' END,
                        v_audit_id;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.begin_hosted_channel_setup_v2(uuid,uuid,text,text,bytea,timestamptz),
    attune.resolve_google_chat_link_destination(bytea,bytea,uuid),
    attune.consume_google_chat_link_v2(bytea,bytea,bytea,bytea,bytea,uuid,bytea,bytea,bytea,text,integer),
    attune.claim_google_chat_delivery_test(uuid,bytea,timestamptz),
    attune.complete_google_chat_delivery_test(uuid,bytea,boolean)
FROM PUBLIC, attune_channel_broker;
GRANT EXECUTE ON FUNCTION
    attune.begin_hosted_channel_setup_v2(uuid,uuid,text,text,bytea,timestamptz)
TO attune_control_plane;
GRANT EXECUTE ON FUNCTION
    attune.consume_google_chat_link_v2(bytea,bytea,bytea,bytea,bytea,uuid,bytea,bytea,bytea,text,integer),
    attune.resolve_google_chat_link_destination(bytea,bytea,uuid),
    attune.claim_google_chat_delivery_test(uuid,bytea,timestamptz),
    attune.complete_google_chat_delivery_test(uuid,bytea,boolean)
TO attune_channel_broker;

GRANT SELECT, INSERT ON attune.hosted_channel_routes TO attune_channel_link_executor;
GRANT UPDATE ON attune.hosted_channel_destinations TO attune_channel_link_executor;
GRANT SELECT, UPDATE ON attune.hosted_onboarding_states TO attune_channel_link_executor;
DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_channel_link_executor TO %I', current_user);
END
$grant_owner$;
SET LOCAL ROLE attune_channel_link_executor;
REVOKE ALL ON FUNCTION
    attune.consume_google_chat_link(bytea,bytea,bytea,bytea,bytea)
FROM attune_channel_broker;
RESET ROLE;
GRANT CREATE ON SCHEMA attune TO attune_channel_link_executor;
ALTER FUNCTION attune.consume_google_chat_link_v2(bytea,bytea,bytea,bytea,bytea,uuid,bytea,bytea,bytea,text,integer)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.resolve_google_chat_link_destination(bytea,bytea,uuid)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.begin_hosted_channel_setup_v2(uuid,uuid,text,text,bytea,timestamptz)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.claim_google_chat_delivery_test(uuid,bytea,timestamptz)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.complete_google_chat_delivery_test(uuid,bytea,boolean)
OWNER TO attune_channel_link_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_channel_link_executor FROM %I', current_user);
END
$revoke_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
