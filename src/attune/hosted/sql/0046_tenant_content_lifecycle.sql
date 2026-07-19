-- Customer-content retention and owner-initiated tenant deletion
-- (docs/future-state.md Phase 6 "hosted operations"; gap G19; hosted review
-- gap #4; docs/data-lifecycle.md's "Delivery sequence" items 2 and 4). See
-- the "Content retention and tenant deletion design" section added to
-- docs/data-lifecycle.md in the same change for the narrative contract this
-- migration implements.
--
-- Two independent, still-dormant slices:
--   1. A content-retention executor mirroring `prune_expired_protocol_records`
--      (0028) exactly in shape -- bounded batches, a singleton advisory lock,
--      per-tenant content-free audit -- but pruning conversation_turns/
--      conversations and hosted_brief_deliveries by the contract's 30-day
--      "conversation turns and derived summaries" window instead of protocol
--      artifacts. memories/memory_embeddings and importance_signals/
--      attention_items are deliberately untouched here: the contract keeps
--      taught memory "until the owner deletes it or the account", and
--      attention/importance already self-bound at write time (see the
--      existing "Reviewed storage inventory" section) -- adding a second
--      sweep for them would be a second, conflicting bound.
--   2. An owner-initiated, right-to-be-forgotten deletion ceremony: a durable
--      `deletion_requests` row with a 14-day grace period, a claim function
--      for one executor at a time, and a generic per-relation erase function
--      that is driven entirely by `attune.hosted.data_lifecycle
--      .RELATIONAL_ASSETS` from the Python orchestrator -- this migration's
--      allowlist is a defense-in-depth identifier check for the dynamic SQL
--      below, not the policy; the policy is the Python registry, and the
--      offline test suite pins that walking an unclassified/unlisted
--      relation fails closed rather than being silently skipped.

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'attune_content_retention'
    ) THEN
        CREATE ROLE attune_content_retention
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_content_retention_executor'
    ) THEN
        CREATE ROLE attune_content_retention_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'attune_deletion'
    ) THEN
        CREATE ROLE attune_deletion
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_deletion_request_executor'
    ) THEN
        CREATE ROLE attune_deletion_request_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_deletion_executor'
    ) THEN
        CREATE ROLE attune_deletion_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

-- ---------------------------------------------------------------------------
-- Extend the fixed audit producer-kind vocabulary (mirrors 0028's addition of
-- 'retention').
-- ---------------------------------------------------------------------------

ALTER TABLE attune.audit_intents
DROP CONSTRAINT audit_intents_producer_kind_check;
ALTER TABLE attune.audit_intents
ADD CONSTRAINT audit_intents_producer_kind_check CHECK (producer_kind IN (
    'control_plane', 'worker', 'secret_broker', 'dispatch_broker',
    'channel_broker', 'retention', 'export', 'channel_message',
    'content_retention', 'deletion'
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
        'dispatch_broker', 'channel_broker', 'retention', 'export',
        'channel_message', 'content_retention', 'deletion'
    ) THEN
        IF NEW.producer_kind = 'export' THEN
            IF NOT (
                pg_catalog.pg_has_role(session_user, 'attune_control_plane', 'MEMBER')
                OR pg_catalog.pg_has_role(session_user, 'attune_export', 'MEMBER')
                OR pg_catalog.pg_has_role(session_user, 'attune_export_cleanup', 'MEMBER')
            ) THEN
                RAISE EXCEPTION 'audit producer identity does not match intent'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF NEW.producer_kind = 'channel_message' THEN
            IF NOT pg_catalog.pg_has_role(
                session_user, 'attune_control_plane', 'MEMBER'
            ) THEN
                RAISE EXCEPTION 'audit producer identity does not match intent'
                    USING ERRCODE = '42501';
            END IF;
        ELSIF NOT pg_catalog.pg_has_role(
            session_user,
            CASE NEW.producer_kind
                WHEN 'dispatch_broker' THEN 'attune_dispatch_broker'
                WHEN 'channel_broker' THEN 'attune_channel_broker'
                WHEN 'retention' THEN 'attune_retention'
                WHEN 'content_retention' THEN 'attune_content_retention'
                WHEN 'deletion' THEN 'attune_deletion'
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

-- ---------------------------------------------------------------------------
-- Slice 1: bounded content-retention executor.
-- ---------------------------------------------------------------------------

CREATE FUNCTION attune.prune_expired_customer_content(
    p_run_id uuid, p_batch_size integer
)
RETURNS TABLE (
    conversation_turns integer, conversations integer,
    hosted_brief_deliveries integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_row record;
    v_turns integer := 0;
    v_conversations integer := 0;
    v_briefs integer := 0;
BEGIN
    IF NOT pg_catalog.pg_has_role(
        session_user, 'attune_content_retention', 'MEMBER'
    ) THEN
        RAISE EXCEPTION 'content retention caller is unauthorized'
            USING ERRCODE = '42501';
    END IF;
    IF p_run_id IS NULL OR p_batch_size IS NULL
       OR p_batch_size NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'invalid content retention request' USING ERRCODE = '22023';
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(682947139, 1) THEN
        RAISE EXCEPTION 'content retention executor is already running'
            USING ERRCODE = '55P03';
    END IF;

    -- "30 days after last activity" (docs/data-lifecycle.md) means a
    -- conversation whose most recent turn is still inside the window keeps
    -- every one of its older turns; only a conversation with zero turns
    -- newer than the window is stale.
    FOR v_row IN
        WITH stale_conversations AS MATERIALIZED (
            SELECT conversation.tenant_id, conversation.id
              FROM attune.conversations AS conversation
             WHERE NOT EXISTS (
                 SELECT 1 FROM attune.conversation_turns AS turn
                  WHERE turn.tenant_id = conversation.tenant_id
                    AND turn.conversation_id = conversation.id
                    AND turn.created_at > clock_timestamp() - interval '30 days'
             )
             LIMIT p_batch_size
        ), doomed AS MATERIALIZED (
            SELECT turn.ctid, turn.tenant_id
              FROM attune.conversation_turns AS turn
              JOIN stale_conversations AS stale
                ON stale.tenant_id = turn.tenant_id
               AND stale.id = turn.conversation_id
        ), deleted AS (
            DELETE FROM attune.conversation_turns AS turn
             USING doomed
             WHERE turn.ctid = doomed.ctid
            RETURNING turn.tenant_id
        )
        SELECT deleted.tenant_id, count(*)::integer AS records
          FROM deleted GROUP BY deleted.tenant_id
    LOOP
        v_turns := v_turns + v_row.records;
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, metadata
        ) VALUES (
            v_row.tenant_id, 'content_retention',
            attune_ext.digest(pg_catalog.convert_to(
                'content-retention-v1:' || p_run_id::text
                || ':conversation_turns:' || v_row.tenant_id::text, 'UTF8'),
                'sha256'),
            'system', 'content_retention.conversation_turns.pruned', 'observed',
            'conversation_turns',
            pg_catalog.jsonb_build_object('records', v_row.records)
        ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END LOOP;

    -- A conversation is only removed once it has no remaining turns at all
    -- (either it just lost its last stale turns above, or it already had
    -- none) and is itself outside the window.
    FOR v_row IN
        WITH doomed AS MATERIALIZED (
            SELECT conversation.ctid, conversation.tenant_id
              FROM attune.conversations AS conversation
             WHERE conversation.created_at < clock_timestamp() - interval '30 days'
               AND NOT EXISTS (
                   SELECT 1 FROM attune.conversation_turns AS turn
                    WHERE turn.tenant_id = conversation.tenant_id
                      AND turn.conversation_id = conversation.id
               )
             LIMIT p_batch_size
        ), deleted AS (
            DELETE FROM attune.conversations AS conversation
             USING doomed
             WHERE conversation.ctid = doomed.ctid
            RETURNING conversation.tenant_id
        )
        SELECT deleted.tenant_id, count(*)::integer AS records
          FROM deleted GROUP BY deleted.tenant_id
    LOOP
        v_conversations := v_conversations + v_row.records;
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, metadata
        ) VALUES (
            v_row.tenant_id, 'content_retention',
            attune_ext.digest(pg_catalog.convert_to(
                'content-retention-v1:' || p_run_id::text
                || ':conversations:' || v_row.tenant_id::text, 'UTF8'), 'sha256'),
            'system', 'content_retention.conversations.pruned', 'observed',
            'conversations',
            pg_catalog.jsonb_build_object('records', v_row.records)
        ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END LOOP;

    -- hosted_brief_deliveries stores the same class of rendered, owner-facing
    -- text as conversation_turns ("derived summaries" in the contract's
    -- table); age it off the same 30-day window by its own delivery attempt
    -- timestamp.
    FOR v_row IN
        WITH doomed AS MATERIALIZED (
            SELECT brief.ctid, brief.tenant_id
              FROM attune.hosted_brief_deliveries AS brief
             WHERE brief.created_at < clock_timestamp() - interval '30 days'
             LIMIT p_batch_size
        ), deleted AS (
            DELETE FROM attune.hosted_brief_deliveries AS brief
             USING doomed
             WHERE brief.ctid = doomed.ctid
            RETURNING brief.tenant_id
        )
        SELECT deleted.tenant_id, count(*)::integer AS records
          FROM deleted GROUP BY deleted.tenant_id
    LOOP
        v_briefs := v_briefs + v_row.records;
        INSERT INTO attune.audit_intents (
            tenant_id, producer_kind, idempotency_key, actor_type,
            action, outcome, target_type, metadata
        ) VALUES (
            v_row.tenant_id, 'content_retention',
            attune_ext.digest(pg_catalog.convert_to(
                'content-retention-v1:' || p_run_id::text
                || ':hosted_brief_deliveries:' || v_row.tenant_id::text,
                'UTF8'), 'sha256'),
            'system', 'content_retention.hosted_brief_deliveries.pruned',
            'observed', 'hosted_brief_deliveries',
            pg_catalog.jsonb_build_object('records', v_row.records)
        ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;
    END LOOP;

    RETURN QUERY SELECT v_turns, v_conversations, v_briefs;
END
$function$;

DO $grant_content_retention_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_content_retention_executor TO %I', current_user
    );
END
$grant_content_retention_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_content_retention_executor;
GRANT USAGE ON SCHEMA attune_ext TO attune_content_retention_executor;
GRANT SELECT, DELETE ON attune.conversations, attune.conversation_turns,
    attune.hosted_brief_deliveries TO attune_content_retention_executor;
GRANT SELECT, INSERT ON attune.audit_intents TO attune_content_retention_executor;
ALTER FUNCTION attune.prune_expired_customer_content(uuid, integer)
OWNER TO attune_content_retention_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_content_retention_executor;
DO $revoke_content_retention_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_content_retention_executor FROM %I', current_user
    );
END
$revoke_content_retention_owner$;

REVOKE ALL ON FUNCTION
    attune.prune_expired_customer_content(uuid, integer) FROM PUBLIC;
GRANT USAGE ON SCHEMA attune TO attune_content_retention;
GRANT EXECUTE ON FUNCTION
    attune.prune_expired_customer_content(uuid, integer)
TO attune_content_retention;

-- ---------------------------------------------------------------------------
-- Slice 2: owner-initiated tenant deletion.
-- ---------------------------------------------------------------------------

-- The deletion ledger itself: classified DataClass.DELETION_LEDGER /
-- DeletionRule.RETAIN_TOMBSTONE in attune.hosted.data_lifecycle, the same
-- triple as the existing `deletion_markers` table -- it must outlive the
-- tenant's own erase walk to prove the ceremony happened, so it is never a
-- target of that walk and carries no enforced foreign key into any table the
-- walk erases (requested_session_id and claim_run_id are opaque references,
-- not foreign keys, for exactly that reason). Its foreign keys into
-- `tenants`/`principals` remain safe because those two rows are never
-- physically removed by the walk either (see erase_tenant_deletion_relation
-- below) -- they reach a terminal `status` value the schema already reserved
-- for this since migration 0001.
CREATE TABLE attune.deletion_requests (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    requested_by uuid NOT NULL,
    requested_session_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'cancelled', 'claimed', 'completed', 'failed')),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    grace_expires_at timestamptz NOT NULL,
    cancelled_at timestamptz,
    cancelled_by uuid,
    claim_run_id uuid,
    claimed_at timestamptz,
    completed_at timestamptz,
    failure_code text CHECK (failure_code IS NULL OR failure_code IN (
        'pre_effect_audit', 'executor_ambiguous', 'post_effect_audit',
        'completion_unconfirmed'
    )),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id) REFERENCES attune.tenants(id),
    FOREIGN KEY (tenant_id, requested_by) REFERENCES attune.principals(tenant_id, id),
    CHECK (grace_expires_at > requested_at),
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
    ,
    CHECK ((status = 'claimed') = (claim_run_id IS NOT NULL AND claimed_at IS NOT NULL)
        OR status IN ('completed', 'failed'))
    ,
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
    ,
    CHECK (status <> 'failed' OR failure_code IS NOT NULL)
);

-- At most one active (still-pending or in-flight) request per tenant.
CREATE UNIQUE INDEX deletion_requests_one_active
ON attune.deletion_requests (tenant_id)
WHERE status IN ('pending', 'claimed');

CREATE INDEX deletion_requests_due
ON attune.deletion_requests (grace_expires_at)
WHERE status = 'pending';

ALTER TABLE attune.deletion_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.deletion_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.deletion_requests
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
REVOKE ALL ON attune.deletion_requests FROM PUBLIC;

-- Ordinary control-plane reads (status page) use plain RLS-scoped SELECT;
-- every mutation is a SECURITY DEFINER function below.
GRANT SELECT ON attune.deletion_requests TO attune_control_plane;

CREATE FUNCTION attune.request_tenant_deletion(
    p_principal_id uuid, p_session_id uuid
)
RETURNS TABLE (
    deletion_request_id uuid, request_status text, requested_at timestamptz,
    grace_expires_at timestamptz, created boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_id uuid;
    v_status text;
    v_requested_at timestamptz;
    v_grace_expires_at timestamptz;
BEGIN
    IF p_principal_id IS NULL OR p_session_id IS NULL THEN
        RAISE EXCEPTION 'deletion principal and session are required'
            USING ERRCODE = '22023';
    END IF;
    -- Recent-session recheck independent of the control plane's own recency
    -- gate (mirrors activate_hosted_read_only_policy, 0019).
    IF NOT EXISTS (
        SELECT 1
          FROM attune.identity_sessions AS session
          JOIN attune.principals AS principal
            ON principal.tenant_id = session.tenant_id
           AND principal.id = session.principal_id
          JOIN attune.tenants AS tenant ON tenant.id = session.tenant_id
         WHERE session.tenant_id = v_tenant_id
           AND session.id = p_session_id
           AND session.principal_id = p_principal_id
           AND session.revoked_at IS NULL
           AND session.expires_at > clock_timestamp()
           AND session.created_at >= clock_timestamp() - interval '10 minutes'
           AND principal.status = 'active'
           AND tenant.status = 'active'
    ) THEN
        RAISE EXCEPTION 'recent owner session is required' USING ERRCODE = '42501';
    END IF;

    BEGIN
        -- 14-day grace period: see the dated decisions.md entry for the
        -- rationale. Operator-adjustable only by a reviewed migration, not
        -- an environment variable.
        INSERT INTO attune.deletion_requests (
            tenant_id, requested_by, requested_session_id, grace_expires_at
        ) VALUES (
            v_tenant_id, p_principal_id, p_session_id,
            clock_timestamp() + interval '14 days'
        )
        RETURNING id, status, deletion_requests.requested_at,
                  deletion_requests.grace_expires_at
          INTO v_id, v_status, v_requested_at, v_grace_expires_at;
        RETURN QUERY SELECT v_id, v_status, v_requested_at, v_grace_expires_at, true;
        RETURN;
    EXCEPTION WHEN unique_violation THEN
        -- The partial unique index permits one active request per tenant; a
        -- double submission adopts that exact row instead of erroring.
        NULL;
    END;

    SELECT request.id, request.status, request.requested_at,
           request.grace_expires_at
      INTO v_id, v_status, v_requested_at, v_grace_expires_at
      FROM attune.deletion_requests AS request
     WHERE request.tenant_id = v_tenant_id
       AND request.status IN ('pending', 'claimed')
     ORDER BY request.requested_at DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active deletion request changed concurrently'
            USING ERRCODE = '40001';
    END IF;
    RETURN QUERY SELECT v_id, v_status, v_requested_at, v_grace_expires_at, false;
END
$function$;

CREATE FUNCTION attune.cancel_tenant_deletion_request(
    p_principal_id uuid, p_session_id uuid
)
RETURNS TABLE (cancelled boolean, request_status text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_status text;
BEGIN
    IF p_principal_id IS NULL OR p_session_id IS NULL THEN
        RAISE EXCEPTION 'deletion principal and session are required'
            USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM attune.identity_sessions AS session
          JOIN attune.principals AS principal
            ON principal.tenant_id = session.tenant_id
           AND principal.id = session.principal_id
          JOIN attune.tenants AS tenant ON tenant.id = session.tenant_id
         WHERE session.tenant_id = v_tenant_id
           AND session.id = p_session_id
           AND session.principal_id = p_principal_id
           AND session.revoked_at IS NULL
           AND session.expires_at > clock_timestamp()
           AND session.created_at >= clock_timestamp() - interval '10 minutes'
           AND principal.status = 'active'
           AND tenant.status = 'active'
    ) THEN
        RAISE EXCEPTION 'recent owner session is required' USING ERRCODE = '42501';
    END IF;

    -- Cancellable only while still 'pending': once claimed, grace has
    -- already elapsed by construction (claim_tenant_deletion only claims a
    -- request whose grace_expires_at is in the past) and the walk may
    -- already be under way, so a cancel at that point is refused rather than
    -- racing the executor.
    UPDATE attune.deletion_requests AS request
       SET status = 'cancelled', cancelled_at = clock_timestamp(),
           cancelled_by = p_principal_id, updated_at = clock_timestamp()
     WHERE request.tenant_id = v_tenant_id AND request.status = 'pending'
    RETURNING request.status INTO v_status;
    IF FOUND THEN
        RETURN QUERY SELECT true, v_status;
        RETURN;
    END IF;

    SELECT request.status INTO v_status
      FROM attune.deletion_requests AS request
     WHERE request.tenant_id = v_tenant_id
     ORDER BY request.requested_at DESC
     LIMIT 1;
    RETURN QUERY SELECT false, v_status;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.request_tenant_deletion(uuid, uuid),
    attune.cancel_tenant_deletion_request(uuid, uuid)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.request_tenant_deletion(uuid, uuid),
    attune.cancel_tenant_deletion_request(uuid, uuid)
TO attune_control_plane;

DO $grant_deletion_request_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_deletion_request_executor TO %I', current_user
    );
END
$grant_deletion_request_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_deletion_request_executor;
GRANT USAGE ON SCHEMA attune_ext TO attune_deletion_request_executor;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id(),
    attune_ext.gen_random_uuid() TO attune_deletion_request_executor;
GRANT SELECT ON attune.identity_sessions, attune.principals, attune.tenants
TO attune_deletion_request_executor;
GRANT SELECT, INSERT, UPDATE ON attune.deletion_requests
TO attune_deletion_request_executor;
ALTER FUNCTION attune.request_tenant_deletion(uuid, uuid)
OWNER TO attune_deletion_request_executor;
ALTER FUNCTION attune.cancel_tenant_deletion_request(uuid, uuid)
OWNER TO attune_deletion_request_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_deletion_request_executor;
DO $revoke_deletion_request_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_deletion_request_executor FROM %I', current_user
    );
END
$revoke_deletion_request_owner$;

-- ---------------------------------------------------------------------------
-- The executor: claim, per-relation erase, complete/fail.
-- ---------------------------------------------------------------------------

CREATE FUNCTION attune.claim_tenant_deletion(p_run_id uuid)
RETURNS TABLE (
    tenant_id uuid, deletion_request_id uuid, requested_by uuid,
    claim_run_id uuid, resumed boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_row attune.deletion_requests%ROWTYPE;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_deletion', 'MEMBER') THEN
        RAISE EXCEPTION 'deletion caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_run_id IS NULL THEN
        RAISE EXCEPTION 'invalid tenant deletion claim' USING ERRCODE = '22023';
    END IF;

    -- Resume path: an already-claimed request (a prior run crashed before
    -- completing) is picked back up with its ORIGINAL claim_run_id so that
    -- erase_tenant_deletion_relation's ownership check still matches and the
    -- walk can safely repeat every per-relation call from scratch.
    SELECT request.* INTO v_row
      FROM attune.deletion_requests AS request
     WHERE request.status = 'claimed'
     ORDER BY request.claimed_at
     LIMIT 1
       FOR UPDATE SKIP LOCKED;
    IF FOUND THEN
        RETURN QUERY SELECT v_row.tenant_id, v_row.id, v_row.requested_by,
                            v_row.claim_run_id, true;
        RETURN;
    END IF;

    SELECT request.* INTO v_row
      FROM attune.deletion_requests AS request
     WHERE request.status = 'pending'
       AND request.grace_expires_at <= clock_timestamp()
     ORDER BY request.requested_at
     LIMIT 1
       FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE attune.deletion_requests AS request
       SET status = 'claimed', claim_run_id = p_run_id,
           claimed_at = clock_timestamp(), updated_at = clock_timestamp()
     WHERE request.tenant_id = v_row.tenant_id AND request.id = v_row.id;
    UPDATE attune.tenants AS tenant
       SET status = 'deleting', updated_at = clock_timestamp()
     WHERE tenant.id = v_row.tenant_id AND tenant.status = 'active';

    RETURN QUERY SELECT v_row.tenant_id, v_row.id, v_row.requested_by,
                        p_run_id, false;
END
$function$;

CREATE FUNCTION attune.erase_tenant_deletion_relation(
    p_claim_run_id uuid, p_audit_nonce uuid, p_tenant_id uuid,
    p_relation text, p_batch_size integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_count integer;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_deletion', 'MEMBER') THEN
        RAISE EXCEPTION 'deletion caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_claim_run_id IS NULL OR p_audit_nonce IS NULL OR p_tenant_id IS NULL
       OR p_batch_size IS NULL OR p_batch_size NOT BETWEEN 1 AND 1000
       OR p_relation IS NULL
       -- Defense-in-depth identifier allowlist for the dynamic SQL below.
       -- This is NOT the deletion policy: the policy -- which relations get
       -- erased at all -- is attune.hosted.data_lifecycle.RELATIONAL_ASSETS,
       -- read by the Python orchestrator. This array exists only so a typo
       -- or an unreviewed relation name can never become a dynamic-SQL
       -- identifier; it is kept in sync with every ERASE/CRYPTO_ERASE
       -- classified table by test_tenant_deletion.py.
       OR NOT (p_relation = ANY (ARRAY[
           'tenants', 'principals', 'installations', 'connectors', 'policies',
           'autonomy_grants', 'hosted_onboarding_states',
           'hosted_channel_preferences', 'hosted_channel_destinations',
           'memories', 'memory_embeddings', 'conversations',
           'conversation_turns', 'importance_signals', 'attention_items',
           'hosted_brief_deliveries', 'connector_credentials',
           'hosted_channel_credentials', 'jobs', 'approvals',
           'capability_admissions', 'provider_events', 'job_retries',
           'workflow_checkpoints', 'usage_records', 'dispatch_intents',
           'credential_intents', 'job_reconciliations', 'oauth_transactions',
           'identity_sessions', 'hosted_channel_setup_transactions',
           'hosted_channel_routes', 'hosted_channel_deliveries',
           'export_jobs', 'export_object_attempts', 'export_download_grants'
       ])) THEN
        RAISE EXCEPTION 'invalid tenant deletion erase request'
            USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM attune.deletion_requests AS request
         WHERE request.tenant_id = p_tenant_id
           AND request.claim_run_id = p_claim_run_id
           AND request.status = 'claimed'
    ) THEN
        RAISE EXCEPTION 'tenant deletion is not claimed for this run'
            USING ERRCODE = '55000';
    END IF;

    -- `tenants`/`principals` reach the terminal `deleted` status the schema
    -- reserved for them since migration 0001 rather than being physically
    -- removed: every surviving deletion-ledger and audit row still
    -- references them, and neither column retains reversible content once
    -- terminal (subject_hash/issuer are already opaque, slug is a generated
    -- identifier).
    IF p_relation = 'tenants' THEN
        UPDATE attune.tenants AS tenant
           SET status = 'deleted', updated_at = clock_timestamp()
         WHERE tenant.id = p_tenant_id AND tenant.status <> 'deleted';
        GET DIAGNOSTICS v_count = ROW_COUNT;
    ELSIF p_relation = 'principals' THEN
        UPDATE attune.principals AS principal
           SET status = 'deleted', updated_at = clock_timestamp()
         WHERE principal.tenant_id = p_tenant_id AND principal.status <> 'deleted';
        GET DIAGNOSTICS v_count = ROW_COUNT;
    ELSE
        EXECUTE pg_catalog.format(
            'WITH doomed AS MATERIALIZED (
                 SELECT relation_row.ctid FROM attune.%I AS relation_row
                  WHERE relation_row.tenant_id = $1 LIMIT $2
             ), deleted AS (
                 DELETE FROM attune.%I AS relation_row USING doomed
                  WHERE relation_row.ctid = doomed.ctid RETURNING 1
             ) SELECT count(*)::integer FROM deleted',
            p_relation, p_relation
        ) INTO v_count USING p_tenant_id, p_batch_size;
    END IF;

    -- target_type is the fixed, reviewed relation name only -- never a row
    -- value -- keeping this audit content-free like every other retention
    -- and deletion intent in this migration.
    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action,
        outcome, target_type, metadata
    ) VALUES (
        p_tenant_id, 'deletion',
        attune_ext.digest(pg_catalog.convert_to(
            'deletion-erase-v1:' || p_audit_nonce::text, 'UTF8'), 'sha256'),
        'system', 'hosted.deletion.relation.erased', 'observed', p_relation,
        pg_catalog.jsonb_build_object('records', v_count)
    ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

    RETURN v_count;
END
$function$;

CREATE FUNCTION attune.complete_tenant_deletion(
    p_claim_run_id uuid, p_audit_nonce uuid, p_tenant_id uuid
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_status text;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_deletion', 'MEMBER') THEN
        RAISE EXCEPTION 'deletion caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_claim_run_id IS NULL OR p_audit_nonce IS NULL OR p_tenant_id IS NULL THEN
        RAISE EXCEPTION 'invalid tenant deletion completion' USING ERRCODE = '22023';
    END IF;

    UPDATE attune.deletion_requests AS request
       SET status = 'completed', completed_at = clock_timestamp(),
           updated_at = clock_timestamp()
     WHERE request.tenant_id = p_tenant_id
       AND request.claim_run_id = p_claim_run_id
       AND request.status = 'claimed'
    RETURNING request.status INTO v_status;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tenant deletion claim is unavailable' USING ERRCODE = 'P0002';
    END IF;

    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action,
        outcome, target_type, metadata
    ) VALUES (
        p_tenant_id, 'deletion',
        attune_ext.digest(pg_catalog.convert_to(
            'deletion-complete-v1:' || p_audit_nonce::text, 'UTF8'), 'sha256'),
        'system', 'hosted.deletion.completed', 'observed', 'tenant_account',
        pg_catalog.jsonb_build_object('schema_version', 1)
    ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

    RETURN v_status;
END
$function$;

CREATE FUNCTION attune.fail_tenant_deletion(
    p_claim_run_id uuid, p_audit_nonce uuid, p_tenant_id uuid,
    p_failure_code text
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_status text;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_deletion', 'MEMBER') THEN
        RAISE EXCEPTION 'deletion caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_claim_run_id IS NULL OR p_audit_nonce IS NULL OR p_tenant_id IS NULL
       OR p_failure_code IS NULL
       OR p_failure_code NOT IN (
           'pre_effect_audit', 'executor_ambiguous', 'post_effect_audit',
           'completion_unconfirmed'
       ) THEN
        RAISE EXCEPTION 'invalid tenant deletion failure' USING ERRCODE = '22023';
    END IF;

    -- The claim (and tenant.status = 'deleting') is left exactly as it was:
    -- a failed run is a stop signal, not an auto-retry, mirroring
    -- docs/reconciliation.md's posture for ambiguous effects. A future
    -- resumed run still finds this same row via the 'claimed' resume path
    -- above and may continue; only an explicit, separately reviewed operator
    -- workflow resolves a 'failed' row itself.
    UPDATE attune.deletion_requests AS request
       SET status = 'failed', failure_code = p_failure_code,
           updated_at = clock_timestamp()
     WHERE request.tenant_id = p_tenant_id
       AND request.claim_run_id = p_claim_run_id
       AND request.status = 'claimed'
    RETURNING request.status INTO v_status;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tenant deletion claim is unavailable' USING ERRCODE = 'P0002';
    END IF;

    INSERT INTO attune.audit_intents (
        tenant_id, producer_kind, idempotency_key, actor_type, action,
        outcome, target_type, metadata
    ) VALUES (
        p_tenant_id, 'deletion',
        attune_ext.digest(pg_catalog.convert_to(
            'deletion-fail-v1:' || p_audit_nonce::text, 'UTF8'), 'sha256'),
        'system', 'hosted.deletion.completed', 'failed', 'tenant_account',
        pg_catalog.jsonb_build_object(
            'schema_version', 1, 'failure_code', p_failure_code
        )
    ) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING;

    RETURN v_status;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.claim_tenant_deletion(uuid),
    attune.erase_tenant_deletion_relation(uuid, uuid, uuid, text, integer),
    attune.complete_tenant_deletion(uuid, uuid, uuid),
    attune.fail_tenant_deletion(uuid, uuid, uuid, text)
FROM PUBLIC;
GRANT USAGE ON SCHEMA attune TO attune_deletion;
GRANT EXECUTE ON FUNCTION
    attune.claim_tenant_deletion(uuid),
    attune.erase_tenant_deletion_relation(uuid, uuid, uuid, text, integer),
    attune.complete_tenant_deletion(uuid, uuid, uuid),
    attune.fail_tenant_deletion(uuid, uuid, uuid, text)
TO attune_deletion;

DO $grant_deletion_owner$
BEGIN
    EXECUTE pg_catalog.format('GRANT attune_deletion_executor TO %I', current_user);
END
$grant_deletion_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_deletion_executor;
GRANT USAGE ON SCHEMA attune_ext TO attune_deletion_executor;
GRANT SELECT, UPDATE ON attune.deletion_requests TO attune_deletion_executor;
GRANT SELECT, UPDATE ON attune.tenants, attune.principals TO attune_deletion_executor;
GRANT SELECT, DELETE ON
    attune.installations, attune.connectors, attune.policies,
    attune.autonomy_grants, attune.hosted_onboarding_states,
    attune.hosted_channel_preferences, attune.hosted_channel_destinations,
    attune.memories, attune.memory_embeddings, attune.conversations,
    attune.conversation_turns, attune.importance_signals,
    attune.attention_items, attune.hosted_brief_deliveries,
    attune.connector_credentials, attune.hosted_channel_credentials,
    attune.jobs, attune.approvals, attune.capability_admissions,
    attune.provider_events, attune.job_retries, attune.workflow_checkpoints,
    attune.usage_records, attune.dispatch_intents, attune.credential_intents,
    attune.job_reconciliations, attune.oauth_transactions,
    attune.identity_sessions, attune.hosted_channel_setup_transactions,
    attune.hosted_channel_routes, attune.hosted_channel_deliveries,
    attune.export_jobs, attune.export_object_attempts,
    attune.export_download_grants
TO attune_deletion_executor;
GRANT SELECT, INSERT ON attune.audit_intents TO attune_deletion_executor;
ALTER FUNCTION attune.claim_tenant_deletion(uuid)
OWNER TO attune_deletion_executor;
ALTER FUNCTION attune.erase_tenant_deletion_relation(uuid, uuid, uuid, text, integer)
OWNER TO attune_deletion_executor;
ALTER FUNCTION attune.complete_tenant_deletion(uuid, uuid, uuid)
OWNER TO attune_deletion_executor;
ALTER FUNCTION attune.fail_tenant_deletion(uuid, uuid, uuid, text)
OWNER TO attune_deletion_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_deletion_executor;
DO $revoke_deletion_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_deletion_executor FROM %I', current_user
    );
END
$revoke_deletion_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune
REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune
REVOKE ALL ON FUNCTIONS FROM PUBLIC;
