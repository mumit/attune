"""Fail-closed core for the exclusive hosted Cloud Tasks producer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID

from .dispatch import LeasedDispatch, PostgresDispatchBrokerRepository


class TaskAlreadyExists(Exception):
    """The deterministic Cloud Task was created by an earlier attempt."""


class TaskCreator(Protocol):
    def create(self, route: "BrokerRoute", dispatch: LeasedDispatch, body: bytes) -> None:
        ...


class DispatchAudit(Protocol):
    def record(
        self,
        dispatch_intent_id: UUID,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class BrokerRoute:
    purpose: str
    queue: str
    target_url: str
    audience: str

    def __post_init__(self) -> None:
        if not self.purpose or len(self.purpose) > 80:
            raise ValueError("route purpose must contain between 1 and 80 characters")
        if not self.queue.startswith("projects/") or "/queues/" not in self.queue:
            raise ValueError("route queue must be a full Cloud Tasks queue name")
        if not self.target_url.startswith("https://"):
            raise ValueError("route target must be HTTPS")
        if self.audience != self.target_url:
            raise ValueError("route audience must exactly equal its target URL")


@dataclass(frozen=True)
class BrokerResult:
    status_code: int


class DispatchBroker:
    """Lease canonical state, require audit, and create one fixed-route task."""

    def __init__(
        self,
        *,
        intents: PostgresDispatchBrokerRepository,
        tasks: TaskCreator,
        audit: DispatchAudit,
        routes: Mapping[str, BrokerRoute],
    ):
        if not routes or any(key != route.purpose for key, route in routes.items()):
            raise ValueError("routes must be keyed by exact purpose")
        self._intents = intents
        self._tasks = tasks
        self._audit = audit
        self._routes = dict(routes)

    def dispatch(self, intent_id: UUID, *, producer_kind: str) -> BrokerResult:
        try:
            leased = self._intents.lease(
                intent_id, producer_kind=producer_kind, lease_seconds=30
            )
        except Exception:
            return BrokerResult(503)
        if leased is None:
            return BrokerResult(404)
        if leased.state == "dispatched":
            return BrokerResult(
                204 if self._audit.record(intent_id, outcome="observed") else 503
            )

        route = self._routes.get(leased.purpose)
        if route is None:
            self._fail(leased, producer_kind, "route_not_registered")
            return BrokerResult(403)
        if not self._audit.record(intent_id, outcome="allowed"):
            return BrokerResult(503)

        body = _task_body(leased)
        try:
            self._tasks.create(route, leased, body)
        except TaskAlreadyExists:
            pass
        except Exception:
            return BrokerResult(503)
        try:
            finalized = self._intents.finalize(
                intent_id,
                producer_kind=producer_kind,
                outcome="dispatched",
            )
        except Exception:
            return BrokerResult(503)
        if not finalized:
            return BrokerResult(503)
        return BrokerResult(
            204 if self._audit.record(intent_id, outcome="observed") else 503
        )

    def _fail(
        self, leased: LeasedDispatch, producer_kind: str, error_code: str
    ) -> None:
        try:
            if self._intents.finalize(
                leased.id, producer_kind=producer_kind, outcome="failed"
            ):
                self._audit.record(
                    leased.id, outcome="failed", error_code=error_code
                )
        except Exception:
            pass


def _task_body(dispatch: LeasedDispatch) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "tenant_id": str(dispatch.tenant.tenant_id),
            "job_id": str(dispatch.job_id),
            "delivery_id": str(dispatch.delivery_id),
            "purpose": dispatch.purpose,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
