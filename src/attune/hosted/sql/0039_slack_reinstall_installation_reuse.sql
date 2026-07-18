-- Reinstalling Slack after an owner disconnect reused the revoked
-- destination row (0038's consume_slack_install already handled that) but
-- always inserted a fresh attune.installations row first, unconditionally.
-- When the reinstall carries the same workspace/team reference as the
-- original install -- exactly what happens when the same Slack workspace is
-- reconnected -- that second insert collides with the revoked installation's
-- row on the (tenant_id, provider, external_ref_hash) unique constraint.
--
-- Mirror the Google Chat relink (0026's consume_google_chat_link_v2): in the
-- reuse branch, reactivate and repoint the existing installation row instead
-- of inserting a second one.
--
-- The prior migration assigned this SECURITY DEFINER function to the
-- memberless link executor. A non-superuser migrator must temporarily become
-- that owner to replace it; membership alone is not sufficient for
-- CREATE OR REPLACE FUNCTION.
DO $grant_link_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_channel_link_executor TO %I', current_user
    );
END
$grant_link_owner$;
GRANT CREATE ON SCHEMA attune TO attune_channel_link_executor;
SET LOCAL ROLE attune_channel_link_executor;

CREATE OR REPLACE FUNCTION attune.consume_slack_install(
    p_state_hash bytea, p_claim_hash bytea,
    p_owner_tenant_id uuid, p_owner_principal_id uuid,
    p_installation_ref_hash bytea, p_actor_ref_hash bytea,
    p_destination_ref_hash bytea, p_destination_id uuid,
    p_route_ciphertext bytea, p_route_nonce bytea, p_route_wrapped_dek bytea,
    p_route_key_resource text, p_route_format_version integer,
    p_token_ciphertext bytea, p_token_nonce bytea, p_token_wrapped_dek bytea,
    p_token_key_resource text, p_token_format_version integer
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
    IF p_state_hash IS NULL OR octet_length(p_state_hash) <> 32
       OR p_claim_hash IS NULL OR octet_length(p_claim_hash) <> 32
       OR p_owner_tenant_id IS NULL OR p_owner_principal_id IS NULL
       OR p_installation_ref_hash IS NULL
       OR octet_length(p_installation_ref_hash) <> 32
       OR p_actor_ref_hash IS NULL OR octet_length(p_actor_ref_hash) <> 32
       OR p_destination_ref_hash IS NULL
       OR octet_length(p_destination_ref_hash) <> 32
       OR p_destination_id IS NULL
       OR p_route_ciphertext IS NULL OR length(p_route_ciphertext) < 17
       OR p_route_nonce IS NULL OR octet_length(p_route_nonce) <> 12
       OR p_route_wrapped_dek IS NULL OR length(p_route_wrapped_dek) < 1
       OR p_route_key_resource IS NULL
       OR length(p_route_key_resource) NOT BETWEEN 20 AND 1024
       OR p_route_format_version <> 1
       OR p_token_ciphertext IS NULL OR length(p_token_ciphertext) < 17
       OR p_token_nonce IS NULL OR octet_length(p_token_nonce) <> 12
       OR p_token_wrapped_dek IS NULL OR length(p_token_wrapped_dek) < 1
       OR p_token_key_resource IS NULL
       OR length(p_token_key_resource) NOT BETWEEN 20 AND 1024
       OR p_token_format_version <> 1 THEN
        RAISE EXCEPTION 'invalid Slack install consumption' USING ERRCODE = '22023';
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
     WHERE setup.provider = 'slack' AND setup.mechanism = 'oauth'
       AND setup.secret_hash = p_state_hash AND setup.state = 'claimed'
       AND setup.claim_hash = p_claim_hash
       AND setup.claim_expires_at > clock_timestamp()
       AND setup.expires_at > clock_timestamp()
       AND setup.tenant_id = p_owner_tenant_id
       AND setup.owner_principal_id = p_owner_principal_id
       AND tenant.status = 'active' AND principal.status = 'active'
       AND setup.preference_revision = preference.revision
       AND 'slack' = ANY(
           preference.interaction_channels || preference.brief_channels
       )
     FOR UPDATE OF setup;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Slack install is unavailable' USING ERRCODE = 'P0002';
    END IF;
    SELECT destination.* INTO v_existing
      FROM attune.hosted_channel_destinations destination
     WHERE destination.tenant_id = v_setup.tenant_id
       AND destination.provider = 'slack'
     FOR UPDATE;
    IF FOUND AND (
        v_existing.owner_principal_id <> v_setup.owner_principal_id
        OR v_existing.status <> 'revoked'
    ) THEN
        RAISE EXCEPTION 'channel destination requires replacement ceremony'
            USING ERRCODE = '23514';
    END IF;

    IF v_existing.id IS NULL THEN
      INSERT INTO attune.installations (
        tenant_id, provider, kind, external_ref_hash, metadata
      ) VALUES (
        v_setup.tenant_id, 'slack', 'channel', p_installation_ref_hash,
        jsonb_build_object('surface', 'slack', 'schema_version', 1)
      ) RETURNING id INTO v_installation_id;
      INSERT INTO attune.hosted_channel_destinations (
        tenant_id, id, owner_principal_id, installation_id, provider,
        installation_ref_hash, actor_ref_hash, destination_ref_hash,
        ingress_verified_at, route_version
      ) VALUES (
        v_setup.tenant_id, p_destination_id, v_setup.owner_principal_id,
        v_installation_id, 'slack', p_installation_ref_hash,
        p_actor_ref_hash, p_destination_ref_hash, clock_timestamp(), 1
      );
    ELSE
      -- Reuse the revoked installation row: reactivating and repointing it
      -- avoids a second insert colliding with its own
      -- (tenant_id, provider, external_ref_hash) unique row when the
      -- reinstall carries the same workspace reference as before.
      v_installation_id := v_existing.installation_id;
      UPDATE attune.installations installation
         SET external_ref_hash = p_installation_ref_hash,
             status = 'active', updated_at = clock_timestamp()
       WHERE installation.tenant_id = v_setup.tenant_id
         AND installation.id = v_installation_id;
      p_destination_id := v_existing.id;
      DELETE FROM attune.hosted_channel_routes route
       WHERE route.tenant_id = v_setup.tenant_id
         AND route.destination_id = v_existing.id;
      DELETE FROM attune.hosted_channel_credentials credential
       WHERE credential.tenant_id = v_setup.tenant_id
         AND credential.destination_id = v_existing.id;
      UPDATE attune.hosted_channel_destinations destination
         SET status = 'pending_test', installation_id = v_installation_id,
             installation_ref_hash = p_installation_ref_hash,
             actor_ref_hash = p_actor_ref_hash,
             destination_ref_hash = p_destination_ref_hash,
             ingress_verified_at = clock_timestamp(),
             delivery_verified_at = NULL, route_version = 1,
             delivery_claim_hash = NULL, delivery_claim_expires_at = NULL,
             version = destination.version + 1, updated_at = clock_timestamp()
       WHERE destination.tenant_id = v_setup.tenant_id
         AND destination.id = v_existing.id;
    END IF;
    INSERT INTO attune.hosted_channel_routes (
        tenant_id, destination_id, ciphertext, nonce, wrapped_dek,
        key_resource, format_version
    ) VALUES (
        v_setup.tenant_id, p_destination_id, p_route_ciphertext, p_route_nonce,
        p_route_wrapped_dek, p_route_key_resource, p_route_format_version
    );
    INSERT INTO attune.hosted_channel_credentials (
        tenant_id, destination_id, purpose, ciphertext, nonce, wrapped_dek,
        key_resource, format_version
    ) VALUES (
        v_setup.tenant_id, p_destination_id, 'slack_bot_token',
        p_token_ciphertext, p_token_nonce, p_token_wrapped_dek,
        p_token_key_resource, p_token_format_version
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
        'provider', p_actor_ref_hash, 'hosted.channels.slack.install',
        'observed', 'owner_dm', p_destination_ref_hash,
        jsonb_build_object('schema_version', 1)
    ) RETURNING id INTO v_audit_id;
    RETURN QUERY SELECT v_setup.tenant_id, v_setup.owner_principal_id,
                        v_installation_id, p_destination_id,
                        'pending_test'::text, v_audit_id;
END
$function$;

RESET ROLE;

REVOKE ALL ON FUNCTION attune.consume_slack_install(
    bytea,bytea,uuid,uuid,bytea,bytea,bytea,uuid,bytea,bytea,bytea,text,integer,
    bytea,bytea,bytea,text,integer
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION attune.consume_slack_install(
    bytea,bytea,uuid,uuid,bytea,bytea,bytea,uuid,bytea,bytea,bytea,text,integer,
    bytea,bytea,bytea,text,integer
) TO attune_channel_broker;

REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor;
DO $revoke_link_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_channel_link_executor FROM %I', current_user
    );
END
$revoke_link_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
