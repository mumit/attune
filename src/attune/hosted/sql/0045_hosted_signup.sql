-- Hosted production signup (docs/hosted-signup.md; docs/future-state.md
-- Phase 6 "hosted onboarding"; docs/gap-analysis.md G19).
--
-- Unlike attune.provision_initial_identity (0016), this function accepts no
-- caller-supplied tenant slug at all -- it generates one from the tenant id
-- it creates -- so it is safe to grant EXECUTE to the control plane's own
-- runtime role without handing that role either a slug oracle or direct
-- table authority. It is owned by the *same* memberless
-- attune_identity_provisioning_executor role 0016 already created: that
-- role's existing SELECT/INSERT on attune.tenants/attune.principals and
-- USAGE on attune/attune_ext are exactly what this function needs, so no
-- new role, table grant, or schema grant is introduced. It shares 0016's
-- fixed advisory lock constant because both functions write into the same
-- (issuer, subject_hash) uniqueness space and must serialize against each
-- other, not just against themselves.
CREATE FUNCTION attune.provision_hosted_signup_tenant(
    p_subject_hash bytea, p_issuer text, p_region text
)
RETURNS TABLE (tenant_id uuid, principal_id uuid, created boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $function$
DECLARE
    v_tenant_id uuid;
    v_slug text;
    v_principal_id uuid;
    v_principal_tenant_id uuid;
    v_principal_status text;
    v_principal_count integer;
    v_tenant_status text;
BEGIN
    IF p_subject_hash IS NULL OR octet_length(p_subject_hash) <> 32
       OR p_issuer IS NULL
       OR p_issuer !~ '^https://securetoken[.]google[.]com/[a-z][a-z0-9-]{4,29}$'
       OR p_region IS NULL
       OR p_region !~ '^[a-z][a-z0-9-]{1,62}$' THEN
        RAISE EXCEPTION 'invalid hosted signup provisioning request'
            USING ERRCODE = '22023';
    END IF;

    -- Serialize against both this ceremony and the private operator
    -- ceremony (attune.provision_initial_identity): both create tenant and
    -- principal rows keyed by the same (issuer, subject_hash) uniqueness,
    -- so a signup call must not race the operator job for the same
    -- subject into creating two tenants.
    PERFORM pg_catalog.pg_advisory_xact_lock(214748301);

    SELECT count(*), min(principal.tenant_id::text)::uuid,
           min(principal.id::text)::uuid, min(principal.status)
      INTO v_principal_count, v_principal_tenant_id,
           v_principal_id, v_principal_status
      FROM attune.principals AS principal
     WHERE principal.issuer = p_issuer
       AND principal.subject_hash = p_subject_hash;

    IF v_principal_count = 0 THEN
        v_tenant_id := attune_ext.gen_random_uuid();
        v_slug := 'tn-' || replace(v_tenant_id::text, '-', '');

        INSERT INTO attune.tenants (id, slug, region)
        VALUES (v_tenant_id, v_slug, p_region);

        INSERT INTO attune.principals (tenant_id, subject_hash, issuer)
        VALUES (v_tenant_id, p_subject_hash, p_issuer)
        RETURNING principals.id INTO v_principal_id;

        RETURN QUERY SELECT v_tenant_id, v_principal_id, true;
        RETURN;
    END IF;

    SELECT tenant.status INTO v_tenant_status
      FROM attune.tenants AS tenant
     WHERE tenant.id = v_principal_tenant_id;

    IF v_principal_count = 1
       AND v_principal_status = 'active'
       AND v_tenant_status = 'active' THEN
        RETURN QUERY SELECT v_principal_tenant_id, v_principal_id, false;
        RETURN;
    END IF;

    -- Zero and multiple mappings both fail closed (mirroring the login and
    -- operator paths): a disabled principal, a suspended tenant, or an
    -- ambiguous multi-tenant mapping all raise the same generic conflict,
    -- never a distinguishable error the caller could use to learn anything
    -- about another tenant's state.
    RAISE EXCEPTION 'hosted signup provisioning conflicts with existing state'
        USING ERRCODE = '23505';
END
$function$;

REVOKE ALL ON FUNCTION
    attune.provision_hosted_signup_tenant(bytea,text,text)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    attune.provision_hosted_signup_tenant(bytea,text,text)
TO attune_control_plane;

-- A non-superuser migrator must temporarily become the memberless owner to
-- transfer ownership of a newly created function to it; membership alone is
-- not sufficient for ALTER FUNCTION ... OWNER TO.
DO $grant_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'GRANT attune_identity_provisioning_executor TO %I', current_user
    );
END
$grant_owner$;
GRANT CREATE ON SCHEMA attune TO attune_identity_provisioning_executor;
ALTER FUNCTION attune.provision_hosted_signup_tenant(bytea,text,text)
OWNER TO attune_identity_provisioning_executor;
REVOKE CREATE ON SCHEMA attune
FROM attune_identity_provisioning_executor;
DO $revoke_owner$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE attune_identity_provisioning_executor FROM %I', current_user
    );
END
$revoke_owner$;

ALTER DEFAULT PRIVILEGES IN SCHEMA attune
REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA attune
REVOKE ALL ON FUNCTIONS FROM PUBLIC;
