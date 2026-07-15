REVOKE INSERT, UPDATE ON attune.job_reconciliations
FROM attune_control_plane;
GRANT SELECT ON attune.job_reconciliations TO attune_control_plane;

CREATE FUNCTION attune.enforce_job_reconciliation_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT pg_catalog.pg_has_role(
        current_user, 'attune_worker', 'MEMBER'
    ) OR NEW.state <> 'open'
       OR NEW.result_code IS NOT NULL
       OR NEW.resolved_at IS NOT NULL THEN
        RAISE EXCEPTION 'invalid reconciliation intake identity or state'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM attune.jobs AS job
         WHERE job.tenant_id = NEW.tenant_id
           AND job.id = NEW.job_id
           AND job.state = 'reconcile'
    ) THEN
        RAISE EXCEPTION 'reconciliation requires canonical job state'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER job_reconciliation_insert_guard
BEFORE INSERT ON attune.job_reconciliations
FOR EACH ROW EXECUTE FUNCTION attune.enforce_job_reconciliation_insert();

REVOKE ALL ON FUNCTION attune.enforce_job_reconciliation_insert()
FROM PUBLIC;
