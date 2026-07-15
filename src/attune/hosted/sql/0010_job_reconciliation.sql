CREATE TABLE attune.job_reconciliations (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    job_id uuid NOT NULL,
    reason_code text NOT NULL CHECK (reason_code IN (
        'pre_effect_audit', 'executor_ambiguous', 'post_effect_audit',
        'job_finalize'
    )),
    provider_request_ref_hash bytea CHECK (
        provider_request_ref_hash IS NULL
        OR octet_length(provider_request_ref_hash) = 32
    ),
    state text NOT NULL DEFAULT 'open' CHECK (state IN (
        'open', 'resolved_succeeded', 'resolved_failed', 'cancelled'
    )),
    result_code text CHECK (
        result_code IS NULL OR length(result_code) BETWEEN 1 AND 80
    ),
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, job_id),
    FOREIGN KEY (tenant_id, job_id) REFERENCES attune.jobs(tenant_id, id),
    CHECK ((state = 'open') = (resolved_at IS NULL)),
    CHECK ((state = 'open') = (result_code IS NULL))
);

CREATE INDEX job_reconciliations_opened
    ON attune.job_reconciliations (tenant_id, opened_at)
    WHERE state = 'open';

ALTER TABLE attune.job_reconciliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.job_reconciliations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.job_reconciliations
USING (tenant_id = attune.current_tenant_id())
WITH CHECK (tenant_id = attune.current_tenant_id());

REVOKE ALL ON attune.job_reconciliations FROM PUBLIC;
GRANT SELECT, INSERT ON attune.job_reconciliations TO attune_worker;
GRANT SELECT, INSERT, UPDATE ON attune.job_reconciliations
TO attune_control_plane;
