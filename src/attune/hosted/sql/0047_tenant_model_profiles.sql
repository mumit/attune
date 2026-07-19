-- Per-tenant model configuration and usage metering (docs/future-state.md
-- Phase 6 "hosted operations": "per-tenant model configuration and usage
-- metering"; hosted review gaps #1/#2 -- no billing/usage metering existed,
-- and the model gateway was one fixed config for every tenant). See the
-- dated docs/decisions.md entry for the full design narrative this
-- migration implements.
--
-- Two independent, still-dormant slices:
--   1. attune.tenant_model_preferences: a bounded owner preference, one row
--      per tenant, naming an OPERATOR-DEFINED model profile from a fixed
--      vocabulary ('standard', 'premium') -- never a raw endpoint, model
--      string, or API key ("Named profiles, never raw endpoints" in
--      decisions.md). Extending the vocabulary is a reviewed migration, not
--      data. Mutation is a SECURITY DEFINER function, owned by a new
--      memberless attune_model_profile_executor, mirroring
--      configure_hosted_channels (0020) almost exactly -- except the bar is
--      ordinary session + CSRF, not the ten-minute recent-authentication
--      window: a bounded preference, not an authority change, the same
--      posture "Web conversation acceptance uses ordinary proofs, not
--      recency" already documents in this file. The mandatory allowed/
--      observed audit lives at the Python service layer
--      (HostedModelProfileService), exactly like HostedChannelService --
--      this function contains no audit_intents write of its own.
--   2. attune.model_usage_daily: a content-free per (tenant, task, profile,
--      UTC day) aggregate counter -- request count, input/output token
--      counts as the provider reports them, a bounded failure count --
--      never prompt/response text, never a per-message row (aggregate
--      upsert only). The worker is already trusted for ordinary INSERT/
--      UPDATE writes into its own tenant's rows elsewhere in this schema
--      (e.g. hosted_brief_deliveries, 0044), but a bare UPDATE grant here
--      would let a compromised or buggy worker overwrite these OPERATIONAL
--      counters to any absolute value -- including rewriting history to
--      hide overage once this data feeds real billing. The accumulate
--      function is therefore SECURITY DEFINER, owned by a new memberless
--      attune_usage_meter_executor, and is the ONLY mutation path: it
--      exposes nothing but an atomic "add one request, add these bounded
--      token counts, add this bounded failure count" operation, so the
--      worker can never SET an absolute counter value directly.

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_model_profile_executor'
    ) THEN
        CREATE ROLE attune_model_profile_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_usage_meter_executor'
    ) THEN
        CREATE ROLE attune_usage_meter_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

-- ---------------------------------------------------------------------------
-- Slice 1: attune.tenant_model_preferences
-- ---------------------------------------------------------------------------

CREATE TABLE attune.tenant_model_preferences (
    tenant_id uuid PRIMARY KEY,
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    profile text NOT NULL DEFAULT 'standard'
        CHECK (profile IN ('standard', 'premium')),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id) REFERENCES attune.tenants(id)
);

ALTER TABLE attune.tenant_model_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.tenant_model_preferences FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.tenant_model_preferences
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
REVOKE ALL ON attune.tenant_model_preferences FROM PUBLIC;

-- Ordinary control-plane reads (GET /v1/model-profile). The worker also
-- reads its own tenant's row directly to resolve which profile to pass to
-- the model gateway (ATTUNE_ENABLE_TENANT_MODEL_PROFILES) -- an ordinary
-- SELECT, the same trust the worker already has for
-- hosted_channel_preferences (0025). Every mutation is the SECURITY
-- DEFINER function below; neither role may INSERT or UPDATE directly.
GRANT SELECT ON attune.tenant_model_preferences TO attune_control_plane;
GRANT SELECT ON attune.tenant_model_preferences TO attune_worker;

CREATE FUNCTION attune.set_tenant_model_profile(
    p_principal_id uuid, p_session_id uuid, p_profile text
)
RETURNS TABLE (profile text, revision bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_profile text;
    v_revision bigint;
BEGIN
    IF p_principal_id IS NULL OR p_session_id IS NULL OR p_profile IS NULL
       OR p_profile NOT IN ('standard', 'premium') THEN
        RAISE EXCEPTION 'model profile request is invalid' USING ERRCODE = '22023';
    END IF;
    -- Independent, ordinary-bar session recheck (no ten-minute recency
    -- clause -- a bounded preference, not an authority change).
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
           AND principal.status = 'active'
           AND tenant.status = 'active'
    ) THEN
        RAISE EXCEPTION 'model profile principal is unavailable'
            USING ERRCODE = '23514';
    END IF;

    INSERT INTO attune.tenant_model_preferences (tenant_id, profile)
    VALUES (v_tenant_id, p_profile)
    ON CONFLICT (tenant_id) DO UPDATE
       SET profile = EXCLUDED.profile,
           revision = CASE
               WHEN attune.tenant_model_preferences.profile = EXCLUDED.profile
               THEN attune.tenant_model_preferences.revision
               ELSE attune.tenant_model_preferences.revision + 1 END,
           updated_at = CASE
               WHEN attune.tenant_model_preferences.profile = EXCLUDED.profile
               THEN attune.tenant_model_preferences.updated_at
               ELSE clock_timestamp() END
    RETURNING attune.tenant_model_preferences.profile,
              attune.tenant_model_preferences.revision
      INTO v_profile, v_revision;

    RETURN QUERY SELECT v_profile, v_revision;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.set_tenant_model_profile(uuid, uuid, text)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.set_tenant_model_profile(uuid, uuid, text)
TO attune_control_plane;

DO $grant_model_profile_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_model_profile_executor TO %I', current_user
    );
END
$grant_model_profile_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_model_profile_executor;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id()
TO attune_model_profile_executor;
GRANT SELECT ON attune.tenants, attune.principals, attune.identity_sessions
TO attune_model_profile_executor;
GRANT SELECT, INSERT, UPDATE ON attune.tenant_model_preferences
TO attune_model_profile_executor;
ALTER FUNCTION attune.set_tenant_model_profile(uuid, uuid, text)
OWNER TO attune_model_profile_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_model_profile_executor;
DO $revoke_model_profile_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_model_profile_executor FROM %I', current_user
    );
END
$revoke_model_profile_owner$;

-- ---------------------------------------------------------------------------
-- Slice 2: attune.model_usage_daily
-- ---------------------------------------------------------------------------

CREATE TABLE attune.model_usage_daily (
    tenant_id uuid NOT NULL,
    usage_date date NOT NULL,
    task text NOT NULL CHECK (task IN ('classify', 'converse', 'embed')),
    profile text NOT NULL CHECK (profile IN ('standard', 'premium')),
    request_count bigint NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    input_tokens bigint NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    failure_count bigint NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT model_usage_daily_pkey PRIMARY KEY (tenant_id, usage_date, task, profile),
    FOREIGN KEY (tenant_id) REFERENCES attune.tenants(id),
    CHECK (failure_count <= request_count)
);

ALTER TABLE attune.model_usage_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.model_usage_daily FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.model_usage_daily
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());
REVOKE ALL ON attune.model_usage_daily FROM PUBLIC;

-- Ordinary control-plane reads (GET /v1/usage, the owner's own bounded
-- 30-day window). The worker never reads or writes this table directly --
-- every accumulation goes through the function below.
GRANT SELECT ON attune.model_usage_daily TO attune_control_plane;

CREATE FUNCTION attune.accumulate_model_usage(
    p_task text, p_profile text, p_success boolean,
    p_input_tokens integer, p_output_tokens integer
)
RETURNS TABLE (
    usage_date date, request_count bigint, input_tokens bigint,
    output_tokens bigint, failure_count bigint
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_date date := (clock_timestamp() AT TIME ZONE 'UTC')::date;
    v_requests bigint;
    v_input bigint;
    v_output bigint;
    v_failures bigint;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'attune_worker', 'MEMBER') THEN
        RAISE EXCEPTION 'model usage caller is unauthorized' USING ERRCODE = '42501';
    END IF;
    IF p_task IS NULL OR p_task NOT IN ('classify', 'converse', 'embed')
       OR p_profile IS NULL OR p_profile NOT IN ('standard', 'premium')
       OR p_success IS NULL
       OR p_input_tokens IS NULL OR p_input_tokens < 0 OR p_input_tokens > 2000000
       OR p_output_tokens IS NULL OR p_output_tokens < 0
       OR p_output_tokens > 2000000 THEN
        RAISE EXCEPTION 'invalid model usage accumulation request'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO attune.model_usage_daily (
        tenant_id, usage_date, task, profile, request_count, input_tokens,
        output_tokens, failure_count
    ) VALUES (
        v_tenant_id, v_date, p_task, p_profile, 1, p_input_tokens,
        p_output_tokens, CASE WHEN p_success THEN 0 ELSE 1 END
    )
    ON CONFLICT ON CONSTRAINT model_usage_daily_pkey DO UPDATE
       SET request_count = attune.model_usage_daily.request_count + 1,
           input_tokens =
               attune.model_usage_daily.input_tokens + EXCLUDED.input_tokens,
           output_tokens =
               attune.model_usage_daily.output_tokens + EXCLUDED.output_tokens,
           failure_count =
               attune.model_usage_daily.failure_count + EXCLUDED.failure_count,
           updated_at = clock_timestamp()
    RETURNING attune.model_usage_daily.usage_date,
              attune.model_usage_daily.request_count,
              attune.model_usage_daily.input_tokens,
              attune.model_usage_daily.output_tokens,
              attune.model_usage_daily.failure_count
      INTO v_date, v_requests, v_input, v_output, v_failures;

    RETURN QUERY SELECT v_date, v_requests, v_input, v_output, v_failures;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.accumulate_model_usage(text, text, boolean, integer, integer)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.accumulate_model_usage(text, text, boolean, integer, integer)
TO attune_worker;

DO $grant_usage_meter_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_usage_meter_executor TO %I', current_user
    );
END
$grant_usage_meter_owner$;
GRANT USAGE, CREATE ON SCHEMA attune TO attune_usage_meter_executor;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id()
TO attune_usage_meter_executor;
GRANT SELECT, INSERT, UPDATE ON attune.model_usage_daily
TO attune_usage_meter_executor;
ALTER FUNCTION attune.accumulate_model_usage(text, text, boolean, integer, integer)
OWNER TO attune_usage_meter_executor;
REVOKE CREATE ON SCHEMA attune FROM attune_usage_meter_executor;
DO $revoke_usage_meter_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_usage_meter_executor FROM %I', current_user
    );
END
$revoke_usage_meter_owner$;

-- ---------------------------------------------------------------------------
-- Extend the tenant-deletion erase walk's defense-in-depth allowlist
-- (attune.hosted.data_lifecycle.RELATIONAL_ASSETS classifies both new
-- relations DeletionRule.ERASE, so the walk must be able to reach them).
-- This CREATE OR REPLACE reproduces 0046's erase_tenant_deletion_relation
-- body verbatim except for the two added array entries -- the array is a
-- defense-in-depth identifier check for the dynamic SQL below, never the
-- deletion policy itself (the policy is the Python registry).
-- ---------------------------------------------------------------------------

DO $grant_deletion_owner_for_replace$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_deletion_executor TO %I', current_user
    );
END
$grant_deletion_owner_for_replace$;

CREATE OR REPLACE FUNCTION attune.erase_tenant_deletion_relation(
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
           'tenant_model_preferences',
           'memories', 'memory_embeddings', 'conversations',
           'conversation_turns', 'importance_signals', 'attention_items',
           'hosted_brief_deliveries', 'connector_credentials',
           'hosted_channel_credentials', 'jobs', 'approvals',
           'capability_admissions', 'provider_events', 'job_retries',
           'workflow_checkpoints', 'usage_records', 'dispatch_intents',
           'credential_intents', 'job_reconciliations', 'oauth_transactions',
           'identity_sessions', 'hosted_channel_setup_transactions',
           'hosted_channel_routes', 'hosted_channel_deliveries',
           'export_jobs', 'export_object_attempts', 'export_download_grants',
           'model_usage_daily'
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

DO $revoke_deletion_owner_for_replace$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_deletion_executor FROM %I', current_user
    );
END
$revoke_deletion_owner_for_replace$;

-- Both new relations are DeletionRule.ERASE in the Python registry, so the
-- deletion executor needs the same SELECT/DELETE it already holds on every
-- other ERASE-classified relation.
GRANT SELECT, DELETE ON attune.tenant_model_preferences, attune.model_usage_daily
TO attune_deletion_executor;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune
REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune
REVOKE ALL ON FUNCTIONS FROM PUBLIC;
