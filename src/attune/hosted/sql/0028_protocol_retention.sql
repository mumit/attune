-- First bounded retention executor. It prunes only expired protocol artifacts;
-- customer conversation and memory retention remain separate later workflows.
DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'attune_retention'
    ) THEN
        CREATE ROLE attune_retention
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_retention_executor'
    ) THEN
        CREATE ROLE attune_retention_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

ALTER TABLE attune.audit_intents
DROP CONSTRAINT audit_intents_producer_kind_check;
ALTER TABLE attune.audit_intents
ADD CONSTRAINT audit_intents_producer_kind_check CHECK (producer_kind IN (
    'control_plane', 'worker', 'secret_broker', 'dispatch_broker',
    'channel_broker', 'retention'
));

CREATE OR REPLACE FUNCTION attune.enforce_audit_intent_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    expected_producer text;
    memberships integer;
BEGIN
    IF NEW.producer_kind IN (
        'dispatch_broker', 'channel_broker', 'retention'
    ) THEN
        IF NOT pg_catalog.pg_has_role(
            session_user,
            CASE NEW.producer_kind
                WHEN 'dispatch_broker' THEN 'attune_dispatch_broker'
                WHEN 'channel_broker' THEN 'attune_channel_broker'
                ELSE 'attune_retention'
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

CREATE FUNCTION attune.prune_expired_protocol_records(
    p_run_id uuid, p_batch_size integer
)
RETURNS TABLE (
    oauth_transactions integer,
    channel_setup_transactions integer,
    identity_sessions integer,
    provider_events integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_row record;
    v_oauth integer := 0;
    v_channel integer := 0;
    v_sessions integer := 0;
    v_events integer := 0;
BEGIN
    IF NOT pg_catalog.pg_has_role(
        session_user, 'attune_retention', 'MEMBER'
    ) THEN
        RAISE EXCEPTION 'retention caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_run_id IS NULL OR p_batch_size IS NULL
       OR p_batch_size NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'invalid retention request' USING ERRCODE = '22023';
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(682947138, 1) THEN
        RAISE EXCEPTION 'retention executor is already running'
            USING ERRCODE = '55P03';
    END IF;

    FOR v_row IN
        WITH doomed AS MATERIALIZED (
            SELECT transaction.ctid
              FROM attune.oauth_transactions AS transaction
             WHERE transaction.expires_at
                   < clock_timestamp() - interval '24 hours'
             ORDER BY transaction.expires_at, transaction.id
             LIMIT p_batch_size
        ), deleted AS (
            DELETE FROM attune.oauth_transactions AS transaction
             USING doomed
             WHERE transaction.ctid = doomed.ctid
            RETURNING transaction.tenant_id
        )
        SELECT deleted.tenant_id, count(*)::integer AS records
          FROM deleted GROUP BY deleted.tenant_id
    LOOP
        v_oauth := v_oauth + v_row.records;
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, metadata
        ) VALUES (
            v_row.tenant_id, 'retention',
            attune_ext.digest(pg_catalog.convert_to(
                'retention-v1:' || p_run_id::text || ':oauth_transactions:'
                || v_row.tenant_id::text, 'UTF8'), 'sha256'),
            'system', 'retention.oauth_transactions.pruned', 'observed',
            'protocol_records',
            pg_catalog.jsonb_build_object('records', v_row.records)
        ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END LOOP;

    FOR v_row IN
        WITH doomed AS MATERIALIZED (
            SELECT setup.ctid
              FROM attune.hosted_channel_setup_transactions AS setup
             WHERE setup.expires_at < clock_timestamp() - interval '24 hours'
             ORDER BY setup.expires_at, setup.id
             LIMIT p_batch_size
        ), deleted AS (
            DELETE FROM attune.hosted_channel_setup_transactions AS setup
             USING doomed
             WHERE setup.ctid = doomed.ctid
            RETURNING setup.tenant_id
        )
        SELECT deleted.tenant_id, count(*)::integer AS records
          FROM deleted GROUP BY deleted.tenant_id
    LOOP
        v_channel := v_channel + v_row.records;
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, metadata
        ) VALUES (
            v_row.tenant_id, 'retention',
            attune_ext.digest(pg_catalog.convert_to(
                'retention-v1:' || p_run_id::text
                || ':hosted_channel_setup_transactions:'
                || v_row.tenant_id::text, 'UTF8'), 'sha256'),
            'system', 'retention.channel_setup.pruned', 'observed',
            'protocol_records',
            pg_catalog.jsonb_build_object('records', v_row.records)
        ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END LOOP;

    FOR v_row IN
        WITH doomed AS MATERIALIZED (
            SELECT session.ctid
              FROM attune.identity_sessions AS session
             WHERE COALESCE(session.revoked_at, session.expires_at)
                   < clock_timestamp() - interval '24 hours'
               AND NOT EXISTS (
                   SELECT 1
                     FROM attune.hosted_channel_setup_transactions AS setup
                    WHERE setup.tenant_id = session.tenant_id
                      AND setup.session_id = session.id
               )
             ORDER BY session.expires_at, session.id
             LIMIT p_batch_size
        ), deleted AS (
            DELETE FROM attune.identity_sessions AS session
             USING doomed
             WHERE session.ctid = doomed.ctid
            RETURNING session.tenant_id
        )
        SELECT deleted.tenant_id, count(*)::integer AS records
          FROM deleted GROUP BY deleted.tenant_id
    LOOP
        v_sessions := v_sessions + v_row.records;
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, metadata
        ) VALUES (
            v_row.tenant_id, 'retention',
            attune_ext.digest(pg_catalog.convert_to(
                'retention-v1:' || p_run_id::text || ':identity_sessions:'
                || v_row.tenant_id::text, 'UTF8'), 'sha256'),
            'system', 'retention.identity_sessions.pruned', 'observed',
            'protocol_records',
            pg_catalog.jsonb_build_object('records', v_row.records)
        ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END LOOP;

    FOR v_row IN
        WITH doomed AS MATERIALIZED (
            SELECT event.ctid
              FROM attune.provider_events AS event
             WHERE event.processed_at
                   < clock_timestamp() - interval '7 days'
             ORDER BY event.processed_at, event.id
             LIMIT p_batch_size
        ), deleted AS (
            DELETE FROM attune.provider_events AS event
             USING doomed
             WHERE event.ctid = doomed.ctid
            RETURNING event.tenant_id
        )
        SELECT deleted.tenant_id, count(*)::integer AS records
          FROM deleted GROUP BY deleted.tenant_id
    LOOP
        v_events := v_events + v_row.records;
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, metadata
        ) VALUES (
            v_row.tenant_id, 'retention',
            attune_ext.digest(pg_catalog.convert_to(
                'retention-v1:' || p_run_id::text || ':provider_events:'
                || v_row.tenant_id::text, 'UTF8'), 'sha256'),
            'system', 'retention.provider_events.pruned', 'observed',
            'protocol_records',
            pg_catalog.jsonb_build_object('records', v_row.records)
        ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END LOOP;

    RETURN QUERY SELECT v_oauth, v_channel, v_sessions, v_events;
END
$function$;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_retention_executor TO %I', current_user
    );
END
$grant_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_retention_executor;
GRANT USAGE ON SCHEMA attune_ext TO attune_retention_executor;
GRANT SELECT, DELETE ON attune.oauth_transactions,
    attune.hosted_channel_setup_transactions, attune.identity_sessions,
    attune.provider_events TO attune_retention_executor;
GRANT SELECT, INSERT ON attune.audit_intents TO attune_retention_executor;
ALTER FUNCTION attune.prune_expired_protocol_records(uuid, integer)
OWNER TO attune_retention_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_retention_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_retention_executor FROM %I', current_user
    );
END
$revoke_owner$;

REVOKE ALL ON FUNCTION
    attune.prune_expired_protocol_records(uuid, integer) FROM PUBLIC;
GRANT USAGE ON SCHEMA attune TO attune_retention;
GRANT EXECUTE ON FUNCTION
    attune.prune_expired_protocol_records(uuid, integer) TO attune_retention;
REVOKE CREATE ON SCHEMA attune FROM attune_retention_executor;
