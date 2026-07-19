-- Wires the dormant typed capability gateway (docs/capability-gateway.md) to
-- the dispatch spine and adds the first hosted write capability,
-- google.gmail.draft.create v1, registered at product risk tier R2 (the
-- security architecture's own risk-tier table lists a Gmail draft as an R2
-- example; the implementation conforms to that normative table rather than
-- the other way around). Everything here stays dormant: no tenant has an R2
-- autonomy grant, no OAuth flow ever requests gmail.compose, and the worker
-- gate ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY defaults off.
--
-- Two schema changes:
--
-- 1. attune.capability_admissions (new): a truly immutable, append-only
--    record of one gateway admission -- what capability/arguments/connector/
--    policy version the gateway resolved. It carries no lifecycle status of
--    its own (unlike a job); it is content, not a state machine. Mirrors
--    attune.audit_events' own no-update/delete/truncate posture exactly
--    (attune.reject_audit_mutation(), already defined in 0001, is reused
--    verbatim -- it does not reference a table name).
--
-- 2. attune.approvals (existing, unused since 0001): this stage makes it a
--    real security transition for the first time, so its mutation
--    discipline is tightened to match every other privileged-state table in
--    this schema (0009's pattern): job_id becomes nullable and a new
--    admission_id column lets an approval bind to an admission instead of
--    an already-created job (this ceremony approves *before* the job
--    exists -- see docs/capability-gateway.md); a new surface column
--    satisfies SEC-500's "originating surface" binding (fixed to 'web' for
--    this slice, the only approval surface built); and -- per the reviewed
--    decision -- attune_worker and attune_control_plane both LOSE direct
--    UPDATE on attune.approvals. The one-use decide/consume transition is
--    now reachable only through the new attune.claim_capability_approval
--    SECURITY DEFINER function, owned by a new memberless, NOLOGIN,
--    BYPASSRLS role (attune_capability_executor) exactly like
--    attune_dispatch_executor/attune_audit_executor/attune_vault_executor in
--    0009. Unlike those three, this one's BYPASSRLS is not load-bearing for
--    cross-tenant lookup (the caller already runs inside a tenant
--    transaction) -- it exists so the state transition on a real security
--    boundary is reachable through exactly one reviewed, atomic, actor-bound
--    path, matching the established convention rather than inventing a
--    weaker one. docs/decisions.md records this distinction explicitly.
--
-- The claim function is deliberately generic over both approval shapes
-- (job_id-bound or admission_id-bound) so any future ceremony that reuses
-- attune.approvals inherits the same one-use, actor-bound, idempotent-replay
-- claim path instead of a bespoke UPDATE. For the admission_id shape it also
-- re-checks that the resolved connector and policy version are still live
-- immediately before honoring the decision (SEC-503, partial: connector and
-- policy liveness only -- live provider-resource freshness, e.g. Gmail
-- thread history, is NOT re-verified here and remains a documented
-- remaining gate in docs/capability-gateway.md).
--
-- Job and dispatch-intent creation after approval is NOT done by this
-- function. It intentionally stops at returning the frozen admission
-- content so the caller can create the job and dispatch intent through the
-- existing, unmodified dispatch producer (attune.hosted.dispatch
-- .PostgresDispatchProducerRepository, producer_kind='worker') -- already
-- grants attune_worker everything it needs on attune.jobs (0001) and
-- attune.dispatch_intents (0003), so no trigger or grant changes are needed
-- there at all.

CREATE TABLE attune.capability_admissions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    principal_id uuid NOT NULL,
    connector_id uuid NOT NULL,
    capability text NOT NULL CHECK (length(capability) BETWEEN 1 AND 120),
    contract_version integer NOT NULL CHECK (contract_version = 1),
    risk smallint NOT NULL CHECK (risk BETWEEN 0 AND 4),
    policy_version bigint NOT NULL CHECK (policy_version > 0),
    arguments jsonb NOT NULL CHECK (
        jsonb_typeof(arguments) = 'object' AND pg_column_size(arguments) <= 16384
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES attune.principals(tenant_id, id),
    FOREIGN KEY (tenant_id, connector_id) REFERENCES attune.connectors(tenant_id, id)
);

ALTER TABLE attune.capability_admissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.capability_admissions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.capability_admissions
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());

CREATE TRIGGER capability_admissions_no_update_delete
BEFORE UPDATE OR DELETE ON attune.capability_admissions
FOR EACH ROW EXECUTE FUNCTION attune.reject_audit_mutation();
CREATE TRIGGER capability_admissions_no_truncate
BEFORE TRUNCATE ON attune.capability_admissions
FOR EACH STATEMENT EXECUTE FUNCTION attune.reject_audit_mutation();

REVOKE ALL ON attune.capability_admissions FROM PUBLIC;
GRANT SELECT, INSERT ON attune.capability_admissions TO attune_worker;

-- attune.approvals: widen the shape, then tighten the mutation path.
ALTER TABLE attune.approvals ALTER COLUMN job_id DROP NOT NULL;
ALTER TABLE attune.approvals ADD COLUMN admission_id uuid;
ALTER TABLE attune.approvals ADD COLUMN surface text NOT NULL DEFAULT 'web'
    CHECK (surface IN ('web'));
ALTER TABLE attune.approvals ALTER COLUMN surface DROP DEFAULT;
ALTER TABLE attune.approvals
    ADD CONSTRAINT approvals_job_xor_admission
    CHECK ((job_id IS NOT NULL) <> (admission_id IS NOT NULL));
ALTER TABLE attune.approvals
    ADD CONSTRAINT approvals_admission_id_fkey
    FOREIGN KEY (tenant_id, admission_id)
    REFERENCES attune.capability_admissions(tenant_id, id);

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = 'attune_capability_executor'
    ) THEN
        CREATE ROLE attune_capability_executor
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
    END IF;
END
$roles$;

DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_capability_executor TO %I', current_user
    );
END
$grant_owner$;

GRANT USAGE, CREATE ON SCHEMA attune TO attune_capability_executor;
GRANT EXECUTE ON FUNCTION attune.current_tenant_id() TO attune_capability_executor;
GRANT SELECT, UPDATE ON attune.approvals TO attune_capability_executor;
GRANT SELECT ON attune.capability_admissions TO attune_capability_executor;
GRANT SELECT ON attune.connectors, attune.policies TO attune_capability_executor;

CREATE FUNCTION attune.claim_capability_approval(
    p_approval_id uuid,
    p_principal_id uuid,
    p_decision text
)
RETURNS TABLE (
    approval_id uuid,
    admission_id uuid,
    job_id uuid,
    capability text,
    arguments jsonb,
    connector_id uuid,
    policy_version bigint,
    final_status text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid := attune.current_tenant_id();
    v_status text;
    v_admission_id uuid;
    v_job_id uuid;
    v_expires_at timestamptz;
    v_connector_id uuid;
    v_policy_version bigint;
    v_capability text;
    v_target_status text;
    v_fresh boolean;
BEGIN
    IF p_approval_id IS NULL OR p_principal_id IS NULL THEN
        RAISE EXCEPTION 'approval claim requires an approval and principal'
            USING ERRCODE = '22023';
    END IF;
    IF p_decision NOT IN ('approved', 'rejected') THEN
        RAISE EXCEPTION 'invalid approval decision' USING ERRCODE = '22023';
    END IF;

    SELECT approval.status, approval.admission_id, approval.job_id,
           approval.expires_at, approval.connector_id, approval.policy_version,
           approval.capability
      INTO v_status, v_admission_id, v_job_id, v_expires_at, v_connector_id,
           v_policy_version, v_capability
      FROM attune.approvals AS approval
     WHERE approval.tenant_id = v_tenant_id
       AND approval.id = p_approval_id
       AND approval.approver_id = p_principal_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    -- Idempotent replay: an already-decided approval returns its recorded
    -- outcome rather than erroring or re-mutating (SEC-501).
    IF v_status <> 'pending' THEN
        RETURN QUERY SELECT p_approval_id, v_admission_id, v_job_id,
            v_capability,
            CASE WHEN v_status = 'consumed' AND v_admission_id IS NOT NULL
                 THEN (SELECT admission.arguments
                         FROM attune.capability_admissions admission
                        WHERE admission.tenant_id = v_tenant_id
                          AND admission.id = v_admission_id)
                 ELSE NULL END,
            v_connector_id, v_policy_version, v_status;
        RETURN;
    END IF;

    IF v_expires_at <= clock_timestamp() THEN
        UPDATE attune.approvals
           SET status = 'expired', decided_at = clock_timestamp()
         WHERE tenant_id = v_tenant_id AND id = p_approval_id AND status = 'pending';
        RETURN QUERY SELECT p_approval_id, v_admission_id, v_job_id,
            v_capability, NULL::jsonb, v_connector_id, v_policy_version, 'expired';
        RETURN;
    END IF;

    -- Reauthorize immediately before honoring the decision (SEC-503,
    -- partial -- see the migration header comment for what is not
    -- reverified here).
    v_fresh := true;
    IF v_admission_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
              FROM attune.capability_admissions admission
              JOIN attune.connectors connector
                ON connector.tenant_id = admission.tenant_id
               AND connector.id = admission.connector_id
               AND connector.status = 'active'
              JOIN attune.policies policy
                ON policy.tenant_id = admission.tenant_id
               AND policy.active
               AND policy.version = admission.policy_version
             WHERE admission.tenant_id = v_tenant_id
               AND admission.id = v_admission_id
        ) INTO v_fresh;
    END IF;
    IF NOT v_fresh THEN
        UPDATE attune.approvals
           SET status = 'expired', decided_at = clock_timestamp()
         WHERE tenant_id = v_tenant_id AND id = p_approval_id AND status = 'pending';
        RETURN QUERY SELECT p_approval_id, v_admission_id, v_job_id,
            v_capability, NULL::jsonb, v_connector_id, v_policy_version, 'expired';
        RETURN;
    END IF;

    v_target_status := CASE WHEN p_decision = 'approved' THEN 'consumed'
                             ELSE 'rejected' END;
    UPDATE attune.approvals
       SET status = v_target_status,
           decided_at = clock_timestamp(),
           consumed_at = CASE WHEN v_target_status = 'consumed'
                               THEN clock_timestamp() ELSE NULL END
     WHERE tenant_id = v_tenant_id AND id = p_approval_id AND status = 'pending';

    RETURN QUERY SELECT p_approval_id, v_admission_id, v_job_id,
        v_capability,
        CASE WHEN v_target_status = 'consumed' AND v_admission_id IS NOT NULL
             THEN (SELECT admission.arguments
                     FROM attune.capability_admissions admission
                    WHERE admission.tenant_id = v_tenant_id
                      AND admission.id = v_admission_id)
             ELSE NULL END,
        v_connector_id, v_policy_version, v_target_status;
END
$function$;

REVOKE ALL ON FUNCTION
    attune.claim_capability_approval(uuid, uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.claim_capability_approval(uuid, uuid, text) TO attune_worker;
ALTER FUNCTION attune.claim_capability_approval(uuid, uuid, text)
OWNER TO attune_capability_executor;

-- The one-use decide/consume transition is now reachable only through the
-- function above -- neither runtime role may UPDATE the table directly.
REVOKE UPDATE ON attune.approvals FROM attune_worker, attune_control_plane;

REVOKE CREATE ON SCHEMA attune FROM attune_capability_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_capability_executor FROM %I', current_user
    );
END
$revoke_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune REVOKE ALL ON FUNCTIONS FROM PUBLIC;
