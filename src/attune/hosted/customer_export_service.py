"""Recent-owner orchestration for fixed-scope customer export requests."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from .customer_export import (
    CustomerExportStatus,
    ExportScope,
    PostgresCustomerExportRequests,
)
from .dispatch import PostgresDispatchProducerRepository
from .tenant import TenantContext
from .export_download import (
    IssuedExportDownload,
    PostgresExportDownloadAuthorizations,
)

CAPABILITY = "customer.export.generate"


class Broker(Protocol):
    def dispatch(self, intent_id: UUID) -> bool: ...


@dataclass(frozen=True)
class StartedCustomerExport:
    export: CustomerExportStatus
    accepted: bool


class CustomerExportService:
    """Create, dispatch, and observe one canonical owner export."""

    def __init__(
        self,
        requests: PostgresCustomerExportRequests,
        dispatches: PostgresDispatchProducerRepository,
        broker: Broker,
        download_authorizations: PostgresExportDownloadAuthorizations,
    ):
        self._requests = requests
        self._dispatches = dispatches
        self._broker = broker
        self._download_authorizations = download_authorizations

    def request(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        scope: ExportScope,
    ) -> StartedCustomerExport:
        nonce = secrets.token_bytes(32)
        request_key = hashlib.sha256(
            b"attune-customer-export-request-v1:"
            + context.tenant_id.bytes
            + principal_id.bytes
            + session_id.bytes
            + scope.encode("ascii")
            + nonce
        ).digest()
        started = self._requests.request_or_existing(
            context,
            principal_id=principal_id,
            session_id=session_id,
            scope=scope,
            idempotency_key=request_key,
        )
        if started.state == "requested":
            dispatch_key = hashlib.sha256(
                b"attune-customer-export-dispatch-v1:" + started.id.bytes
            ).digest()
            try:
                dispatch = self._dispatches.enqueue(
                    context,
                    kind=CAPABILITY,
                    capability=CAPABILITY,
                    payload={"export_id": str(started.id)},
                    idempotency_key=dispatch_key,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            except RuntimeError as error:
                if str(error) != "existing dispatch job is no longer queued":
                    raise
            else:
                if not self._broker.dispatch(dispatch.intent.id):
                    raise RuntimeError("customer export dispatch was refused")
        current = self._find(context, principal_id, started.id)
        if current is None:
            raise RuntimeError("customer export disappeared after request")
        return StartedCustomerExport(current, started.was_created)

    def list(
        self, context: TenantContext, *, principal_id: UUID
    ) -> tuple[CustomerExportStatus, ...]:
        return self._requests.list(context, principal_id=principal_id)

    def authorize_download(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        export_id: UUID,
    ) -> IssuedExportDownload:
        return self._download_authorizations.issue(
            context,
            principal_id=principal_id,
            session_id=session_id,
            export_id=export_id,
        )

    def _find(
        self, context: TenantContext, principal_id: UUID, export_id: UUID
    ) -> CustomerExportStatus | None:
        return next(
            (
                item
                for item in self._requests.list(context, principal_id=principal_id)
                if item.id == export_id
            ),
            None,
        )
