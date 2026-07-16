ALTER TABLE attune.hosted_channel_setup_transactions
DROP CONSTRAINT hosted_channel_setup_transactions_state_check;
ALTER TABLE attune.hosted_channel_setup_transactions
ADD CONSTRAINT hosted_channel_setup_transactions_state_check
CHECK (state IN ('pending', 'claimed', 'consumed', 'expired', 'cancelled'));
ALTER TABLE attune.hosted_channel_setup_transactions
ADD COLUMN claim_hash bytea,
ADD COLUMN claim_expires_at timestamptz,
ADD CONSTRAINT hosted_channel_setup_claim_shape CHECK (
    (state = 'claimed') = (claim_hash IS NOT NULL)
    AND (state = 'claimed') = (claim_expires_at IS NOT NULL)
    AND (claim_hash IS NULL OR octet_length(claim_hash) = 32)
    AND (claim_expires_at IS NULL OR claim_expires_at <= expires_at)
);

ALTER TABLE attune.audit_intents
DROP CONSTRAINT audit_intents_producer_kind_check;
ALTER TABLE attune.audit_intents
ADD CONSTRAINT audit_intents_producer_kind_check CHECK (producer_kind IN (
    'control_plane', 'worker', 'secret_broker', 'dispatch_broker',
    'channel_broker'
));

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_channel_broker'
    ) THEN
        CREATE ROLE attune_channel_broker
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$roles$;
GRANT USAGE ON SCHEMA attune TO attune_channel_broker;

CREATE OR REPLACE FUNCTION attune.enforce_audit_intent_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    expected_producer text;
    memberships integer;
BEGIN
    IF NEW.producer_kind IN ('dispatch_broker', 'channel_broker') THEN
        IF NOT pg_catalog.pg_has_role(
            session_user,
            CASE NEW.producer_kind
                WHEN 'dispatch_broker' THEN 'attune_dispatch_broker'
                ELSE 'attune_channel_broker'
            END,
            'MEMBER'
        ) THEN
            RAISE EXCEPTION 'audit producer identity does not match intent'
                USING ERRCODE = '42501';
        END IF;
        RETURN NEW;
    END IF;
    memberships :=
        pg_catalog.pg_has_role(
            current_user, 'attune_control_plane', 'MEMBER'
        )::integer
        + pg_catalog.pg_has_role(
            current_user, 'attune_worker', 'MEMBER'
        )::integer
        + pg_catalog.pg_has_role(
            current_user, 'attune_secret_broker', 'MEMBER'
        )::integer;
    IF memberships <> 1 THEN
        RAISE EXCEPTION 'audit producer identity is ambiguous or unauthorized'
            USING ERRCODE = '42501';
    END IF;
    IF pg_catalog.pg_has_role(
        current_user, 'attune_control_plane', 'MEMBER'
    ) THEN
        expected_producer := 'control_plane';
    ELSIF pg_catalog.pg_has_role(
        current_user, 'attune_worker', 'MEMBER'
    ) THEN
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

CREATE FUNCTION attune.claim_google_chat_link(
    p_secret_hash bytea, p_claim_hash bytea, p_claim_expires_at timestamptz
)
RETURNS TABLE (
    transaction_id uuid, tenant_id uuid, owner_principal_id uuid,
    pre_audit_intent_id uuid
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_setup attune.hosted_channel_setup_transactions%ROWTYPE;
    v_audit_id uuid;
BEGIN
    IF p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32
       OR p_claim_hash IS NULL OR octet_length(p_claim_hash) <> 32
       OR p_claim_expires_at IS NULL
       OR p_claim_expires_at <= clock_timestamp()
       OR p_claim_expires_at > clock_timestamp() + interval '60 seconds' THEN
        RAISE EXCEPTION 'invalid channel link claim' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        encode(p_secret_hash, 'hex'), 0
    ));
    UPDATE attune.hosted_channel_setup_transactions setup
       SET state = CASE WHEN setup.expires_at <= clock_timestamp()
                        THEN 'expired' ELSE 'pending' END,
           claim_hash = NULL, claim_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE setup.provider = 'google_chat' AND setup.mechanism = 'link_code'
       AND setup.secret_hash = p_secret_hash AND setup.state = 'claimed'
       AND setup.claim_expires_at <= clock_timestamp();

    SELECT setup.* INTO v_setup
      FROM attune.hosted_channel_setup_transactions setup
      JOIN attune.tenants tenant ON tenant.id = setup.tenant_id
      JOIN attune.principals principal
        ON principal.tenant_id = setup.tenant_id
       AND principal.id = setup.owner_principal_id
      JOIN attune.hosted_onboarding_states onboarding
        ON onboarding.tenant_id = setup.tenant_id
       AND onboarding.owner_principal_id = setup.owner_principal_id
      JOIN attune.hosted_channel_preferences preference
        ON preference.tenant_id = setup.tenant_id
       AND preference.owner_principal_id = setup.owner_principal_id
     WHERE setup.provider = 'google_chat' AND setup.mechanism = 'link_code'
       AND setup.secret_hash = p_secret_hash AND setup.state = 'pending'
       AND setup.expires_at > clock_timestamp()
       AND tenant.status = 'active' AND principal.status = 'active'
       AND onboarding.channels_status IN ('authorized', 'applied')
       AND setup.preference_revision = preference.revision
       AND 'google_chat' = ANY(
           preference.interaction_channels || preference.brief_channels
       )
     FOR UPDATE OF setup;
    IF NOT FOUND OR p_claim_expires_at > v_setup.expires_at THEN
        RAISE EXCEPTION 'channel link is unavailable' USING ERRCODE = 'P0002';
    END IF;

    UPDATE attune.hosted_channel_setup_transactions AS claimed_setup
       SET state = 'claimed', claim_hash = p_claim_hash,
           claim_expires_at = p_claim_expires_at, updated_at = clock_timestamp()
     WHERE claimed_setup.tenant_id = v_setup.tenant_id
       AND claimed_setup.id = v_setup.id;
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action, outcome,
        target_type, target_ref_hash, metadata
    ) VALUES (
        v_setup.tenant_id, 'channel_broker',
        p_claim_hash,
        'provider', 'hosted.channels.google_chat.link', 'allowed',
        'channel_setup', p_secret_hash,
        jsonb_build_object('schema_version', 1)
    ) RETURNING id INTO v_audit_id;
    RETURN QUERY SELECT v_setup.id, v_setup.tenant_id,
                        v_setup.owner_principal_id, v_audit_id;
END
$function$;

CREATE FUNCTION attune.release_google_chat_link_claim(
    p_secret_hash bytea, p_claim_hash bytea
)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_changed boolean;
BEGIN
    IF p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32
       OR p_claim_hash IS NULL OR octet_length(p_claim_hash) <> 32 THEN
        RAISE EXCEPTION 'invalid channel link claim' USING ERRCODE = '22023';
    END IF;
    UPDATE attune.hosted_channel_setup_transactions setup
       SET state = CASE WHEN setup.expires_at <= clock_timestamp()
                        THEN 'expired' ELSE 'pending' END,
           claim_hash = NULL, claim_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE setup.provider = 'google_chat' AND setup.mechanism = 'link_code'
       AND setup.secret_hash = p_secret_hash AND setup.state = 'claimed'
       AND setup.claim_hash = p_claim_hash;
    v_changed := FOUND;
    RETURN v_changed;
END
$function$;

CREATE FUNCTION attune.consume_google_chat_link(
    p_secret_hash bytea, p_claim_hash bytea,
    p_installation_ref_hash bytea, p_actor_ref_hash bytea,
    p_destination_ref_hash bytea
)
RETURNS TABLE (
    tenant_id uuid, owner_principal_id uuid, installation_id uuid,
    destination_id uuid, destination_status text, outcome_audit_intent_id uuid
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_setup attune.hosted_channel_setup_transactions%ROWTYPE;
    v_installation_id uuid;
    v_destination_id uuid;
    v_audit_id uuid;
BEGIN
    IF p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32
       OR p_claim_hash IS NULL OR octet_length(p_claim_hash) <> 32
       OR p_installation_ref_hash IS NULL
       OR octet_length(p_installation_ref_hash) <> 32
       OR p_actor_ref_hash IS NULL OR octet_length(p_actor_ref_hash) <> 32
       OR p_destination_ref_hash IS NULL
       OR octet_length(p_destination_ref_hash) <> 32 THEN
        RAISE EXCEPTION 'invalid channel link consumption' USING ERRCODE = '22023';
    END IF;
    SELECT setup.* INTO v_setup
      FROM attune.hosted_channel_setup_transactions setup
      JOIN attune.tenants tenant ON tenant.id = setup.tenant_id
      JOIN attune.principals principal
        ON principal.tenant_id = setup.tenant_id
       AND principal.id = setup.owner_principal_id
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
       AND 'google_chat' = ANY(
           preference.interaction_channels || preference.brief_channels
       )
     FOR UPDATE OF setup;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'channel link is unavailable' USING ERRCODE = 'P0002';
    END IF;
    IF EXISTS (
        SELECT 1 FROM attune.hosted_channel_destinations destination
         WHERE destination.tenant_id = v_setup.tenant_id
           AND destination.provider = 'google_chat'
    ) THEN
        RAISE EXCEPTION 'channel destination requires replacement ceremony'
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO attune.installations (
        tenant_id, provider, kind, external_ref_hash, metadata
    ) VALUES (
        v_setup.tenant_id, 'google', 'channel', p_installation_ref_hash,
        jsonb_build_object('surface', 'google_chat', 'schema_version', 1)
    ) RETURNING id INTO v_installation_id;
    INSERT INTO attune.hosted_channel_destinations (
        tenant_id, owner_principal_id, installation_id, provider,
        installation_ref_hash, actor_ref_hash, destination_ref_hash,
        ingress_verified_at
    ) VALUES (
        v_setup.tenant_id, v_setup.owner_principal_id, v_installation_id,
        'google_chat', p_installation_ref_hash, p_actor_ref_hash,
        p_destination_ref_hash, clock_timestamp()
    ) RETURNING id INTO v_destination_id;
    UPDATE attune.hosted_channel_setup_transactions AS consumed_setup
       SET state = 'consumed', consumed_at = clock_timestamp(),
           claim_hash = NULL, claim_expires_at = NULL,
           updated_at = clock_timestamp()
     WHERE consumed_setup.tenant_id = v_setup.tenant_id
       AND consumed_setup.id = v_setup.id;
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
                        v_installation_id, v_destination_id,
                        'pending_test'::text, v_audit_id;
END
$function$;

REVOKE ALL ON FUNCTION attune.claim_google_chat_link(bytea,bytea,timestamptz),
    attune.release_google_chat_link_claim(bytea,bytea),
    attune.consume_google_chat_link(bytea,bytea,bytea,bytea,bytea)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION attune.claim_google_chat_link(bytea,bytea,timestamptz),
    attune.release_google_chat_link_claim(bytea,bytea),
    attune.consume_google_chat_link(bytea,bytea,bytea,bytea,bytea)
TO attune_channel_broker;

GRANT SELECT, INSERT ON attune.audit_intents TO attune_channel_link_executor;
GRANT SELECT, INSERT ON attune.installations TO attune_channel_link_executor;
GRANT SELECT, INSERT ON attune.hosted_channel_destinations
TO attune_channel_link_executor;
ALTER FUNCTION attune.claim_google_chat_link(bytea,bytea,timestamptz)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.release_google_chat_link_claim(bytea,bytea)
OWNER TO attune_channel_link_executor;
ALTER FUNCTION attune.consume_google_chat_link(bytea,bytea,bytea,bytea,bytea)
OWNER TO attune_channel_link_executor;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
