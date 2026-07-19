-- Tenant-scoped persistence for the two Phase 1/2 local intelligence stores
-- (docs/future-state.md Phase 5 item 1; docs/gap-analysis.md G8/G18):
-- orchestrator/importance.py's per-sender importance profile and
-- orchestrator/attention.py's bounded attended-source signal log. This
-- migration adds ONLY the tables and privileges; no executor is wired to
-- them yet (see attune.hosted.intelligence for the dormant Postgres-backed
-- implementations and docs/decisions.md for the reviewed design choices).
--
-- Reference hashing: sender_ref/channel_ref/thread_ref are externally
-- supplied, often low-entropy provider identifiers (Gmail addresses, Slack
-- user/channel ids, Google Chat resource names) -- the same threat model
-- `channel_broker.ChannelReferenceHasher`/`slack_channel_broker
-- .SlackReferenceHasher` already assume for Google Chat/Slack link
-- references (a plain hash is invertible by an attacker with a candidate
-- list). This migration follows that reviewed posture rather than the
-- plain-sha256-of-a-random-UUID posture used for internal identifiers like
-- `attune.conversations.external_ref_hash`: every *_ref column here is a
-- 32-byte keyed HMAC digest computed by
-- `attune.hosted.intelligence.IntelligenceReferenceHasher` in application
-- code (never in SQL), so no plaintext sender/channel/thread identifier is
-- ever written to either table. Display text the product genuinely needs to
-- show a human (attention_items.channel_name/sender_display,
-- importance_signals has none) stays plain, bounded text -- exactly the
-- existing local split between an opaque `sender_ref` and a readable
-- `sender_display` in `orchestrator.attention.AttentionItem`.

CREATE TABLE attune.importance_signals (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    principal_id uuid NOT NULL,
    sender_ref_hash bytea NOT NULL CHECK (octet_length(sender_ref_hash) = 32),
    -- One row kind for an implicit signal, one for the principal's pin
    -- override -- the same two concepts orchestrator/importance.py's
    -- JsonImportanceProfile keeps in one JSON object per sender
    -- (`{"signals": [...], "pinned": tier}`), normalized into one table
    -- rather than a JSONB blob so the bounded-storage prune (below) and the
    -- decay-window read are plain indexed SQL, not application-side JSON
    -- surgery.
    kind text NOT NULL DEFAULT 'signal' CHECK (kind IN ('signal', 'pin')),
    signal text CHECK (signal IN ('approved', 'edited', 'ignored', 'rejected')),
    pinned_tier text CHECK (pinned_tier IN ('high', 'normal', 'low')),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES attune.principals(tenant_id, id),
    CHECK (
        (kind = 'signal' AND signal IS NOT NULL AND pinned_tier IS NULL)
        OR (kind = 'pin' AND pinned_tier IS NOT NULL AND signal IS NULL)
    )
);
-- Backs both the decay-window read (assess: most-recent-first over one
-- sender) and the bounded-storage prune (MAX_SIGNALS, application logic in
-- attune.hosted.intelligence.PostgresImportanceProfile.record_signal --
-- mirrors JsonImportanceProfile's own `signals[-MAX_SIGNALS:]` truncation,
-- not a SQL trigger, since it is the same repository method doing the
-- INSERT that already knows the bound).
CREATE INDEX importance_signals_lookup
    ON attune.importance_signals (tenant_id, principal_id, sender_ref_hash, recorded_at DESC);
-- Exactly one pin per (tenant, principal, sender): the arbiter for
-- PostgresImportanceProfile.pin()'s upsert.
CREATE UNIQUE INDEX importance_signals_one_pin_per_sender
    ON attune.importance_signals (tenant_id, principal_id, sender_ref_hash)
    WHERE kind = 'pin';

CREATE TABLE attune.attention_items (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL DEFAULT attune_ext.gen_random_uuid(),
    principal_id uuid NOT NULL,
    source text NOT NULL CHECK (source IN ('slack', 'google_chat')),
    channel_ref_hash bytea NOT NULL CHECK (octet_length(channel_ref_hash) = 32),
    channel_name text NOT NULL CHECK (length(channel_name) BETWEEN 1 AND 200),
    sender_ref_hash bytea NOT NULL CHECK (octet_length(sender_ref_hash) = 32),
    sender_display text NOT NULL CHECK (length(sender_display) BETWEEN 1 AND 200),
    -- Bounded text excerpt, mirrors AttentionItem.summary's own contract
    -- (never the full untrusted message body verbatim).
    summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 2000),
    ts timestamptz NOT NULL,
    priority text NOT NULL CHECK (priority IN ('urgent', 'routine', 'noise')),
    mentions_principal boolean NOT NULL DEFAULT false,
    thread_ref_hash bytea CHECK (thread_ref_hash IS NULL OR octet_length(thread_ref_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES attune.principals(tenant_id, id)
);
-- Backs recent(): newest-first per (tenant, principal), same as
-- JsonAttentionStore.recent()'s sort.
CREATE INDEX attention_items_recent
    ON attune.attention_items (tenant_id, principal_id, ts DESC);

DO $rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['importance_signals', 'attention_items']
    LOOP
        EXECUTE format('ALTER TABLE attune.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE attune.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON attune.%I USING (' ||
            'tenant_id = attune.current_tenant_id()) WITH CHECK (' ||
            'tenant_id = attune.current_tenant_id())',
            table_name);
    END LOOP;
END
$rls$;

REVOKE ALL ON attune.importance_signals, attune.attention_items FROM PUBLIC;

-- Least privilege: this stage wires no executor and no control-plane-facing
-- inspect/correct surface (the local CLI's `attune importance`
-- show/pin/unpin equivalent) exists yet, so only attune_worker -- the role a
-- future triage/brief-assembly job would run as, exactly like
-- attune.jobs/attune.memories -- gets a grant here. DELETE is included
-- (unlike the soft-delete-only attune.memories grant) because the bounded-
-- storage prune above is a hard delete of excess/aged rows performed by the
-- same worker-run repository method that writes them; it is not an account-
-- deletion erasure path. A future control-plane grant (for a hosted
-- "why is this sender ranked X" surface) is separate, reviewed work.
GRANT SELECT, INSERT, UPDATE, DELETE ON attune.importance_signals, attune.attention_items
TO attune_worker;
