CREATE TABLE attune.hosted_channel_preferences (
    tenant_id uuid PRIMARY KEY,
    owner_principal_id uuid NOT NULL,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    interaction_channels text[] NOT NULL,
    brief_channels text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id) REFERENCES attune.tenants(id),
    FOREIGN KEY (tenant_id, owner_principal_id)
        REFERENCES attune.principals(tenant_id, id),
    CHECK (interaction_channels <@ ARRAY['google_chat','slack']::text[]),
    CHECK (brief_channels <@ ARRAY['google_chat','slack']::text[]),
    CHECK (cardinality(interaction_channels) <= 2),
    CHECK (cardinality(brief_channels) <= 2),
    CHECK (cardinality(interaction_channels) + cardinality(brief_channels) > 0)
);

ALTER TABLE attune.hosted_channel_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.hosted_channel_preferences FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.hosted_channel_preferences
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
REVOKE ALL ON attune.hosted_channel_preferences FROM PUBLIC;
GRANT SELECT ON attune.hosted_channel_preferences TO attune_control_plane;

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_channel_config_executor'
    ) THEN
        CREATE ROLE attune_channel_config_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

CREATE FUNCTION attune.configure_hosted_channels(
    p_principal_id uuid, p_session_id uuid,
    p_interaction_channels text[], p_brief_channels text[]
)
RETURNS TABLE (
    schema_version integer, preference_revision bigint,
    interaction_channels text[], brief_channels text[],
    onboarding_revision bigint, channels_status text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_interaction text[];
    v_briefs text[];
    v_preference attune.hosted_channel_preferences%ROWTYPE;
    v_onboarding attune.hosted_onboarding_states%ROWTYPE;
BEGIN
    IF p_principal_id IS NULL OR p_session_id IS NULL
       OR p_interaction_channels IS NULL OR p_brief_channels IS NULL THEN
        RAISE EXCEPTION 'channel configuration is incomplete' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(p_interaction_channels || p_brief_channels) channel
         WHERE channel NOT IN ('google_chat', 'slack')
    ) OR cardinality(p_interaction_channels) > 2
       OR cardinality(p_brief_channels) > 2
       OR cardinality(p_interaction_channels) + cardinality(p_brief_channels) = 0
       OR cardinality(p_interaction_channels) <> (
           SELECT count(DISTINCT channel) FROM unnest(p_interaction_channels) channel
       ) OR cardinality(p_brief_channels) <> (
           SELECT count(DISTINCT channel) FROM unnest(p_brief_channels) channel
       ) THEN
        RAISE EXCEPTION 'channel configuration is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT COALESCE(array_agg(channel ORDER BY channel), ARRAY[]::text[])
      INTO v_interaction FROM unnest(p_interaction_channels) channel;
    SELECT COALESCE(array_agg(channel ORDER BY channel), ARRAY[]::text[])
      INTO v_briefs FROM unnest(p_brief_channels) channel;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(v_tenant_id::text || ':hosted-channels', 0)
    );
    SELECT onboarding.* INTO v_onboarding
      FROM attune.hosted_onboarding_states onboarding
     WHERE onboarding.tenant_id = v_tenant_id
       AND onboarding.owner_principal_id = p_principal_id
     FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM attune.principals principal
        JOIN attune.tenants tenant ON tenant.id = principal.tenant_id
         WHERE principal.tenant_id = v_tenant_id
           AND principal.id = p_principal_id
           AND principal.status = 'active'
           AND tenant.status = 'active'
    ) OR NOT EXISTS (
        SELECT 1 FROM attune.identity_sessions session
         WHERE session.tenant_id = v_tenant_id
           AND session.id = p_session_id
           AND session.principal_id = p_principal_id
           AND session.revoked_at IS NULL
           AND session.expires_at > clock_timestamp()
           AND session.created_at >= clock_timestamp() - interval '10 minutes'
    ) THEN
        RAISE EXCEPTION 'hosted channel principal is unavailable'
            USING ERRCODE = '23514';
    END IF;

    SELECT preference.* INTO v_preference
      FROM attune.hosted_channel_preferences preference
     WHERE preference.tenant_id = v_tenant_id
     FOR UPDATE;
    IF FOUND AND (v_preference.owner_principal_id <> p_principal_id
                  OR v_onboarding.channels_status = 'externally_modified') THEN
        RAISE EXCEPTION 'hosted channel configuration requires repair'
            USING ERRCODE = '23514';
    END IF;
    IF FOUND AND v_onboarding.channels_status = 'validated'
       AND (v_preference.interaction_channels <> v_interaction
            OR v_preference.brief_channels <> v_briefs) THEN
        RAISE EXCEPTION 'validated channel configuration requires replacement ceremony'
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO attune.hosted_channel_preferences (
        tenant_id, owner_principal_id, interaction_channels, brief_channels
    ) VALUES (v_tenant_id, p_principal_id, v_interaction, v_briefs)
    ON CONFLICT (tenant_id) DO UPDATE
       SET interaction_channels = EXCLUDED.interaction_channels,
           brief_channels = EXCLUDED.brief_channels,
           revision = CASE WHEN
               attune.hosted_channel_preferences.interaction_channels = EXCLUDED.interaction_channels
               AND attune.hosted_channel_preferences.brief_channels = EXCLUDED.brief_channels
               THEN attune.hosted_channel_preferences.revision
               ELSE attune.hosted_channel_preferences.revision + 1 END,
           updated_at = CASE WHEN
               attune.hosted_channel_preferences.interaction_channels = EXCLUDED.interaction_channels
               AND attune.hosted_channel_preferences.brief_channels = EXCLUDED.brief_channels
               THEN attune.hosted_channel_preferences.updated_at
               ELSE clock_timestamp() END
    RETURNING * INTO v_preference;

    UPDATE attune.hosted_onboarding_states onboarding
       SET channels_status = CASE WHEN onboarding.channels_status = 'validated'
                                  THEN 'validated' ELSE 'authorized' END,
           revision = CASE WHEN onboarding.channels_status IN ('authorized','validated')
                           THEN onboarding.revision ELSE onboarding.revision + 1 END,
           updated_at = CASE WHEN onboarding.channels_status IN ('authorized','validated')
                             THEN onboarding.updated_at ELSE clock_timestamp() END
     WHERE onboarding.tenant_id = v_tenant_id
    RETURNING onboarding.* INTO v_onboarding;

    RETURN QUERY SELECT v_preference.schema_version, v_preference.revision,
                        v_preference.interaction_channels,
                        v_preference.brief_channels, v_onboarding.revision,
                        v_onboarding.channels_status;
END
$function$;

REVOKE ALL ON FUNCTION attune.configure_hosted_channels(uuid,uuid,text[],text[])
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION attune.configure_hosted_channels(uuid,uuid,text[],text[])
TO attune_control_plane;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_channel_config_executor TO %I', current_user);
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_channel_config_executor;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id()
TO attune_channel_config_executor;
GRANT SELECT ON attune.tenants, attune.principals
TO attune_channel_config_executor;
GRANT SELECT ON attune.identity_sessions TO attune_channel_config_executor;
GRANT SELECT, UPDATE ON attune.hosted_onboarding_states
TO attune_channel_config_executor;
GRANT SELECT, INSERT, UPDATE ON attune.hosted_channel_preferences
TO attune_channel_config_executor;
ALTER FUNCTION attune.configure_hosted_channels(uuid,uuid,text[],text[])
OWNER TO attune_channel_config_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_channel_config_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_channel_config_executor FROM %I', current_user);
END
$revoke_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
