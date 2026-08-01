-- The decision ledger (build prompt 26, docs/plan-2026-h2.md P2): one
-- append-only-in-spirit row per proposal, written at propose time and
-- completed at decision time. See attune.hosted.ledger.PostgresDecisionLedger
-- for the dormant Postgres-backed implementation (no executor wired yet,
-- same posture as 0042_intelligence_persistence.sql) and docs/decisions.md
-- for the reviewed design choices.
--
-- Analytic state, not the trust root: the hash-chained audit log (see
-- attune.hosted.audit_service) remains authoritative for authority
-- decisions; this table is derived and may be rebuilt from it. UPDATE is
-- granted (not append-only at the SQL level) because `complete()` fills in
-- decision-time fields on the SAME row `propose()` created -- the audit log,
-- not this table, is the tamper-evident record.
--
-- Content-free by construction: no draft text, no diff text, ever. Only
-- derived numbers (edit_char_distance, edit_distance_normalized,
-- edit_semantic_similarity) and categorical labels (edit_sections_changed,
-- domain, action, decision, ...) are stored. proposal_id/thread_id and
-- context_attribution's memory ids are ALREADY internal, tenant-scoped
-- identifiers -- unlike 0042's externally-supplied sender/channel/thread
-- references, they are plain bounded text, not keyed HMAC hashes (see
-- attune.hosted.ledger's module docstring for the reasoning).

CREATE TABLE attune.decision_ledger (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    principal_id uuid NOT NULL,
    proposal_id text NOT NULL CHECK (length(proposal_id) BETWEEN 1 AND 320),
    thread_id text NOT NULL CHECK (length(thread_id) BETWEEN 1 AND 320),
    domain text NOT NULL CHECK (length(domain) BETWEEN 1 AND 40),
    action text NOT NULL CHECK (length(action) BETWEEN 1 AND 40),
    proposed_at timestamptz NOT NULL,

    autonomy_rung_granted integer,
    autonomy_rung_used integer,
    scope_matched boolean NOT NULL DEFAULT false,

    model_id text,
    prompt_version text,
    playbook_commit text,

    -- context_attribution (LedgerRow), normalized into three plain arrays
    -- rather than a JSONB blob -- each is a flat list of internal ids, no
    -- nested structure to justify JSONB's flexibility.
    memory_ids text[] NOT NULL DEFAULT '{}',
    playbook_bullet_ids text[] NOT NULL DEFAULT '{}',
    skill_ids text[] NOT NULL DEFAULT '{}',

    triage_priority text CHECK (triage_priority IS NULL OR triage_priority IN ('urgent', 'routine', 'noise')),
    base_priority text CHECK (base_priority IS NULL OR base_priority IN ('urgent', 'routine', 'noise')),
    sender_importance_tier text CHECK (sender_importance_tier IS NULL OR sender_importance_tier IN ('high', 'normal', 'low')),
    profile_reason text,

    -- The coverage denominator (see the local ledger module's docstring):
    -- recorded once per BATCH, shared across every row from that batch.
    eligible_item_count integer,
    batch_id text,

    decision text CHECK (decision IS NULL OR decision IN ('approved', 'edited', 'rejected')),
    decided_at timestamptz,
    actor_ref text,
    time_to_decision_seconds double precision,

    edit_char_distance integer,
    edit_distance_normalized double precision,
    edit_semantic_similarity double precision,
    edit_sections_changed text[] NOT NULL DEFAULT '{}',

    applied_ok boolean,
    apply_skip_reason text,
    undone boolean NOT NULL DEFAULT false,
    undone_at timestamptz,

    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, proposal_id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES attune.principals(tenant_id, id)
);

-- Backs attune metrics' window/domain/tier aggregation queries: newest-first
-- (and oldest-first, for the median calculation) over one tenant/principal.
CREATE INDEX decision_ledger_window
    ON attune.decision_ledger (tenant_id, principal_id, proposed_at);
CREATE INDEX decision_ledger_domain_action
    ON attune.decision_ledger (tenant_id, principal_id, domain, action);

ALTER TABLE attune.decision_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE attune.decision_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attune.decision_ledger USING (
    tenant_id = attune.current_tenant_id()
) WITH CHECK (
    tenant_id = attune.current_tenant_id()
);

REVOKE ALL ON attune.decision_ledger FROM PUBLIC;

-- Least privilege, same posture as 0042: no executor is wired yet, so only
-- attune_worker (the role a future draft-and-approve decision path would
-- run as) gets a grant here. No DELETE: a ledger row is completed via
-- UPDATE, never removed -- even `mark_undone` is an UPDATE.
GRANT SELECT, INSERT, UPDATE ON attune.decision_ledger TO attune_worker;
