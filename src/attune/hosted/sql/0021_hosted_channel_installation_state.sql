CREATE TABLE attune.hosted_channel_setup_transactions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    owner_principal_id uuid NOT NULL,
    session_id uuid NOT NULL,
    preference_revision bigint NOT NULL CHECK (preference_revision > 0),
    provider text NOT NULL CHECK (provider IN ('google_chat', 'slack')),
    mechanism text NOT NULL CHECK (mechanism IN ('link_code', 'oauth')),
    secret_hash bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'consumed', 'expired', 'cancelled')),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (provider, secret_hash),
    FOREIGN KEY (tenant_id, owner_principal_id)
        REFERENCES attune.principals(tenant_id, id),
    FOREIGN KEY (tenant_id, session_id)
        REFERENCES attune.identity_sessions(tenant_id, id),
    CHECK (
        (provider = 'google_chat' AND mechanism = 'link_code')
        OR (provider = 'slack' AND mechanism = 'oauth')
    ),
    CHECK (expires_at > created_at),
    CHECK (expires_at <= created_at + interval '10 minutes'),
    CHECK ((state = 'consumed') = (consumed_at IS NOT NULL)),
    CHECK (consumed_at IS NULL OR consumed_at >= created_at)
);
CREATE UNIQUE INDEX hosted_channel_one_pending_setup
ON attune.hosted_channel_setup_transactions (tenant_id, owner_principal_id, provider)
WHERE state = 'pending';

CREATE TABLE attune.hosted_channel_destinations (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    owner_principal_id uuid NOT NULL,
    installation_id uuid NOT NULL,
    provider text NOT NULL CHECK (provider IN ('google_chat', 'slack')),
    installation_ref_hash bytea NOT NULL
        CHECK (octet_length(installation_ref_hash) = 32),
    actor_ref_hash bytea NOT NULL CHECK (octet_length(actor_ref_hash) = 32),
    destination_ref_hash bytea NOT NULL
        CHECK (octet_length(destination_ref_hash) = 32),
    visibility text NOT NULL DEFAULT 'owner_dm' CHECK (visibility = 'owner_dm'),
    status text NOT NULL DEFAULT 'pending_test'
        CHECK (status IN ('pending_test', 'active', 'disabled', 'revoked')),
    ingress_verified_at timestamptz NOT NULL,
    delivery_verified_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (provider, destination_ref_hash),
    UNIQUE (tenant_id, provider),
    FOREIGN KEY (tenant_id, owner_principal_id)
        REFERENCES attune.principals(tenant_id, id),
    FOREIGN KEY (tenant_id, installation_id)
        REFERENCES attune.installations(tenant_id, id),
    CHECK ((status = 'active') = (delivery_verified_at IS NOT NULL)),
    CHECK (delivery_verified_at IS NULL
           OR delivery_verified_at >= ingress_verified_at)
);

ALTER TABLE attune.hosted_channel_setup_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.hosted_channel_setup_transactions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.hosted_channel_setup_transactions
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
ALTER TABLE attune.hosted_channel_destinations ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.hosted_channel_destinations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.hosted_channel_destinations
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());

REVOKE ALL ON attune.hosted_channel_setup_transactions,
    attune.hosted_channel_destinations FROM PUBLIC;
GRANT SELECT ON attune.hosted_channel_setup_transactions,
    attune.hosted_channel_destinations TO attune_control_plane;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON attune.installations
FROM attune_control_plane;

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_channel_link_executor'
    ) THEN
        CREATE ROLE attune_channel_link_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

CREATE FUNCTION attune.begin_hosted_channel_setup(
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
    IF p_principal_id IS NULL OR p_session_id IS NULL
       OR p_secret_hash IS NULL OR octet_length(p_secret_hash) <> 32
       OR p_expires_at IS NULL
       OR p_expires_at < clock_timestamp() + interval '2 minutes'
       OR p_expires_at > clock_timestamp() + interval '10 minutes'
       OR NOT (
           (p_provider = 'google_chat' AND p_mechanism = 'link_code')
           OR (p_provider = 'slack' AND p_mechanism = 'oauth')
       ) THEN
        RAISE EXCEPTION 'channel setup request is invalid' USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(v_tenant_id::text || ':hosted-channels', 0)
    );
    PERFORM pg_advisory_xact_lock(hashtextextended(
        v_tenant_id::text || ':' || p_principal_id::text || ':' || p_provider
        || ':channel-setup', 0
    ));
    IF NOT EXISTS (
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
        RAISE EXCEPTION 'channel setup principal is unavailable'
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
       AND p_provider = ANY(
           preference.interaction_channels || preference.brief_channels
       )
    ;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'selected channel setup is unavailable'
            USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM attune.hosted_channel_destinations destination
         WHERE destination.tenant_id = v_tenant_id
           AND destination.owner_principal_id = p_principal_id
           AND destination.provider = p_provider
           AND destination.status IN ('pending_test', 'active')
    ) THEN
        RAISE EXCEPTION 'channel destination requires replacement ceremony'
            USING ERRCODE = '23514';
    END IF;

    UPDATE attune.hosted_channel_setup_transactions transaction
       SET state = CASE WHEN transaction.expires_at <= clock_timestamp()
                        THEN 'expired' ELSE 'cancelled' END,
           updated_at = clock_timestamp()
     WHERE transaction.tenant_id = v_tenant_id
       AND transaction.owner_principal_id = p_principal_id
       AND transaction.provider = p_provider
       AND transaction.state = 'pending';

    INSERT INTO attune.hosted_channel_setup_transactions (
        tenant_id, owner_principal_id, session_id, preference_revision,
        provider, mechanism, secret_hash, expires_at
    ) VALUES (
        v_tenant_id, p_principal_id, p_session_id, v_preference.revision,
        p_provider, p_mechanism, p_secret_hash, p_expires_at
    ) RETURNING * INTO v_transaction;

    RETURN QUERY SELECT v_transaction.id, v_transaction.preference_revision,
                        v_transaction.provider, v_transaction.mechanism,
                        v_transaction.state, v_transaction.expires_at;
END
$function$;

REVOKE ALL ON FUNCTION attune.begin_hosted_channel_setup(
    uuid,uuid,text,text,bytea,timestamptz
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION attune.begin_hosted_channel_setup(
    uuid,uuid,text,text,bytea,timestamptz
) TO attune_control_plane;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_channel_link_executor TO %I', current_user);
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_channel_link_executor;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id()
TO attune_channel_link_executor;
GRANT SELECT ON attune.tenants, attune.principals,
    attune.identity_sessions, attune.hosted_onboarding_states,
    attune.hosted_channel_preferences, attune.hosted_channel_destinations
TO attune_channel_link_executor;
GRANT SELECT, INSERT, UPDATE ON attune.hosted_channel_setup_transactions
TO attune_channel_link_executor;
ALTER FUNCTION attune.begin_hosted_channel_setup(
    uuid,uuid,text,text,bytea,timestamptz
) OWNER TO attune_channel_link_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_channel_link_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format('REVOKE attune_channel_link_executor FROM %I', current_user);
END
$revoke_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
