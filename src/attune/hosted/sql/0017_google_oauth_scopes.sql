CREATE FUNCTION attune.store_google_oauth_credential(
    p_intent_id uuid,
    p_ciphertext bytea,
    p_nonce bytea,
    p_wrapped_dek bytea,
    p_key_resource text,
    p_format_version integer,
    p_granted_scopes text[]
)
RETURNS TABLE (credential_id uuid, credential_version integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    intent attune.credential_intents%ROWTYPE;
    stored record;
BEGIN
    IF p_granted_scopes <> ARRAY[
        'openid',
        'email',
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/calendar.readonly'
    ]::text[] THEN
        RAISE EXCEPTION 'Google OAuth scopes are not the reviewed capability'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO intent
      FROM attune.credential_intents
     WHERE id = p_intent_id
       AND producer_kind = 'control_plane'
       AND operation = 'install'
       AND capability = 'google.oauth.install'
       AND state = 'leased'
       AND expires_at > clock_timestamp()
     FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM attune.connectors AS connector
         WHERE connector.tenant_id = intent.tenant_id
           AND connector.id = intent.connector_id
           AND connector.provider = 'google'
           AND connector.status = 'pending'
    ) THEN
        RETURN;
    END IF;
    SELECT * INTO stored
      FROM attune.store_connector_credential(
          p_intent_id, p_ciphertext, p_nonce, p_wrapped_dek,
          p_key_resource, p_format_version
      );
    IF stored.credential_id IS NULL THEN
        RETURN;
    END IF;
    UPDATE attune.connectors
       SET granted_scopes = p_granted_scopes,
           updated_at = clock_timestamp()
     WHERE tenant_id = intent.tenant_id AND id = intent.connector_id;
    RETURN QUERY SELECT stored.credential_id, stored.credential_version;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.store_google_oauth_credential(
        uuid,bytea,bytea,bytea,text,integer,text[]
    )
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.store_google_oauth_credential(
        uuid,bytea,bytea,bytea,text,integer,text[]
    )
TO attune_secret_broker;
DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_vault_executor TO %I', current_user
    );
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_vault_executor;
ALTER FUNCTION
    attune.store_google_oauth_credential(
        uuid,bytea,bytea,bytea,text,integer,text[]
    )
OWNER TO attune_vault_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_vault_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_vault_executor FROM %I', current_user
    );
END
$revoke_owner$;
