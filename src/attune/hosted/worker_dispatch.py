"""Fail-closed dispatch core for authenticated hosted worker requests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol
from .repositories import HostedJob, PostgresJobRepository
from .task_envelope import TokenVerifier, verify_task_envelope
from .tenant import TenantContext

LOG = logging.getLogger(__name__)


class AuditSink(Protocol):
    """Content-free audit boundary implemented by the audit-writer service."""

    def record(
        self,
        context: TenantContext,
        *,
        action: str,
        outcome: str,
        job_id: str,
        caller_subject: str,
    ) -> None: ...


class ReconciliationSink(Protocol):
    def open(
        self,
        context: TenantContext,
        job: HostedJob,
        *,
        reason_code: str,
    ) -> object: ...


JobExecutor = Callable[[TenantContext, HostedJob], None]


@dataclass(frozen=True)
class TaskRoute:
    purpose: str
    capability: str
    execute: JobExecutor

    def __post_init__(self) -> None:
        if not self.purpose or len(self.purpose) > 80:
            raise ValueError("route purpose must contain between 1 and 80 characters")
        if not self.capability or len(self.capability) > 120:
            raise ValueError(
                "route capability must contain between 1 and 120 characters"
            )


@dataclass(frozen=True)
class DispatchResult:
    status_code: int


class WorkerDispatcher:
    """Verify, bind, claim, audit, and execute one registered task route.

    HTTP adapters must pass the raw body and Authorization header unchanged.
    Responses are intentionally content-free. An ambiguous executor or audit
    result moves the job to reconciliation instead of making it retryable.
    """

    def __init__(
        self,
        *,
        jobs: PostgresJobRepository,
        audit: AuditSink,
        reconciliations: ReconciliationSink,
        routes: Mapping[str, TaskRoute],
        expected_audience: str,
        expected_service_account: str,
        token_verifier: TokenVerifier | None = None,
    ):
        if not routes or any(key != route.purpose for key, route in routes.items()):
            raise ValueError("routes must be keyed by their exact purpose")
        self._jobs = jobs
        self._audit = audit
        self._reconciliations = reconciliations
        self._routes = dict(routes)
        self._audience = expected_audience
        self._service_account = expected_service_account
        self._token_verifier = token_verifier

    def dispatch(self, *, authorization: str, raw_body: bytes) -> DispatchResult:
        try:
            envelope = verify_task_envelope(
                authorization=authorization,
                raw_body=raw_body,
                expected_audience=self._audience,
                expected_service_account=self._service_account,
                allowed_purposes=self._routes,
                token_verifier=self._token_verifier,
            )
        except PermissionError:
            return DispatchResult(403)
        except ValueError:
            return DispatchResult(400)

        route = self._routes[envelope.purpose]
        try:
            job = self._jobs.claim(
                envelope.tenant,
                envelope.job_id,
                expected_kind=route.purpose,
                expected_capability=route.capability,
            )
        except Exception:
            return DispatchResult(503)
        if job is None:
            return DispatchResult(204)

        if not self._record(
            envelope.tenant,
            action="worker.job.claimed",
            outcome="allowed",
            job=job,
            caller_subject=envelope.caller_subject,
        ):
            self._reconcile(envelope.tenant, job, "pre_effect_audit")
            return DispatchResult(503)

        try:
            route.execute(envelope.tenant, job)
        except Exception:
            self._record(
                envelope.tenant,
                action="worker.job.execute",
                outcome="failed",
                job=job,
                caller_subject=envelope.caller_subject,
            )
            self._reconcile(envelope.tenant, job, "executor_ambiguous")
            return DispatchResult(500)

        if not self._record(
            envelope.tenant,
            action="worker.job.execute",
            outcome="allowed",
            job=job,
            caller_subject=envelope.caller_subject,
        ):
            self._reconcile(envelope.tenant, job, "post_effect_audit")
            return DispatchResult(503)
        try:
            if not self._jobs.finish(
                envelope.tenant, job.id, outcome="succeeded"
            ):
                self._reconcile(envelope.tenant, job, "job_finalize")
                return DispatchResult(503)
        except Exception:
            self._reconcile(envelope.tenant, job, "job_finalize")
            return DispatchResult(503)
        return DispatchResult(204)

    def _record(
        self,
        context: TenantContext,
        *,
        action: str,
        outcome: str,
        job: HostedJob,
        caller_subject: str,
    ) -> bool:
        try:
            self._audit.record(
                context,
                action=action,
                outcome=outcome,
                job_id=str(job.id),
                caller_subject=caller_subject,
            )
        except Exception:
            return False
        return True

    def _reconcile(
        self,
        context: TenantContext,
        job: HostedJob,
        reason_code: str,
    ) -> None:
        try:
            self._reconciliations.open(
                context,
                job,
                reason_code=reason_code,
            )
        except Exception as error:
            LOG.warning(
                "job reconciliation failed (%s)",
                type(error).__name__,
            )
