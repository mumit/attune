"""One-time customer export authorization and download repository boundaries."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .export_crypto import EncryptedExportArchive, ExportEnvelopeCipher
from .customer_export_writer import canonical_export_object_name
from .repositories import ConnectionFactory, _fixed_hash
from .tenant import TenantContext, tenant_transaction


@dataclass(frozen=True)
class IssuedExportDownload:
    id: UUID
    secret: str
    expires_at: datetime


@dataclass(frozen=True)
class ClaimedExportDownload:
    tenant_id: UUID
    export_id: UUID
    scope: str
    object_id: UUID
    object_generation: int
    wrapped_dek: bytes
    nonce: bytes
    key_resource: str
    archive_sha256: bytes
    ciphertext_sha256: bytes
    archive_bytes: int
    ciphertext_bytes: int
    encryption_format: int


class PostgresExportDownloadAuthorizations:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def issue(
        self,
        context: TenantContext,
        *,
        principal_id: UUID,
        session_id: UUID,
        export_id: UUID,
    ) -> IssuedExportDownload:
        if not all(
            isinstance(value, UUID)
            for value in (principal_id, session_id, export_id)
        ):
            raise TypeError("download authority identifiers must be UUIDs")
        secret = secrets.token_urlsafe(32)
        secret_hash = _download_secret_hash(secret)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.issue_customer_export_download(%s,%s,%s,%s)",
                    (principal_id, session_id, export_id, secret_hash),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("download authorization was not issued")
        return IssuedExportDownload(row[0], secret, row[1])


class PostgresExportDownloads:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def claim(
        self, grant_id: UUID, secret: str, *, run_id: UUID
    ) -> ClaimedExportDownload | None:
        if not isinstance(grant_id, UUID) or not isinstance(run_id, UUID):
            raise TypeError("download claim identifiers must be UUIDs")
        secret_hash = _download_secret_hash(secret)
        with closing(self._connect()) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.claim_customer_export_download(%s,%s,%s)",
                    (grant_id, secret_hash, run_id),
                )
                row = cursor.fetchone()
            connection.commit()
        return ClaimedExportDownload(*row) if row is not None else None

    def finish(self, grant_id: UUID, export_id: UUID, *, run_id: UUID) -> bool:
        return self._transition(
            "SELECT attune.finish_customer_export_download(%s,%s,%s)",
            (grant_id, export_id, run_id),
        )

    def release(self, grant_id: UUID, *, run_id: UUID) -> bool:
        return self._transition(
            "SELECT attune.release_customer_export_download(%s,%s)",
            (grant_id, run_id),
        )

    def _transition(self, query: str, parameters: tuple[UUID, ...]) -> bool:
        if not all(isinstance(value, UUID) for value in parameters):
            raise TypeError("download transition identifiers must be UUIDs")
        with closing(self._connect()) as connection:
            with closing(connection.cursor()) as cursor:
                cursor.execute(query, parameters)
                row = cursor.fetchone()
            connection.commit()
        if row is None or not isinstance(row[0], bool):
            raise RuntimeError("download transition returned an invalid result")
        return row[0]


class ExportDownloadObjectStore:
    """Read exactly one known ciphertext object generation without listing."""

    def __init__(self, bucket_name: str, *, client=None):
        if not isinstance(bucket_name, str) or not 3 <= len(bucket_name) <= 63:
            raise ValueError("invalid customer export bucket name")
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._bucket = client.bucket(bucket_name)

    def read(self, object_id: UUID, *, generation: int, expected_bytes: int) -> bytes:
        if not isinstance(object_id, UUID):
            raise TypeError("export object identifier must be a UUID")
        if not isinstance(generation, int) or generation <= 0:
            raise ValueError("export object generation must be positive")
        if not isinstance(expected_bytes, int) or not 16 <= expected_bytes <= 52_428_816:
            raise ValueError("invalid encrypted export size")
        content = self._bucket.blob(
            canonical_export_object_name(object_id), generation=generation
        ).download_as_bytes(
            if_generation_match=generation,
            checksum="crc32c",
            single_shot_download=True,
        )
        if not isinstance(content, bytes) or len(content) != expected_bytes:
            raise RuntimeError("downloaded export object size mismatch")
        return content


class CustomerExportDownloadService:
    """Claim, read, authenticate/decrypt, and consume one export grant."""

    def __init__(
        self,
        repository: PostgresExportDownloads,
        objects: ExportDownloadObjectStore,
        cipher: ExportEnvelopeCipher,
    ):
        self._repository = repository
        self._objects = objects
        self._cipher = cipher

    def download(self, grant_id: UUID, secret: str, *, run_id: UUID) -> bytes | None:
        claimed = self._repository.claim(grant_id, secret, run_id=run_id)
        if claimed is None:
            return None
        try:
            ciphertext = self._objects.read(
                claimed.object_id,
                generation=claimed.object_generation,
                expected_bytes=claimed.ciphertext_bytes,
            )
            archive = self._cipher.decrypt(
                EncryptedExportArchive(
                    ciphertext=ciphertext,
                    nonce=claimed.nonce,
                    wrapped_dek=claimed.wrapped_dek,
                    key_resource=claimed.key_resource,
                    plaintext_sha256=claimed.archive_sha256,
                    ciphertext_sha256=claimed.ciphertext_sha256,
                    plaintext_bytes=claimed.archive_bytes,
                    format_version=claimed.encryption_format,
                ),
                tenant_id=claimed.tenant_id,
                export_id=claimed.export_id,
                scope=claimed.scope,
                object_id=claimed.object_id,
            )
        except Exception:
            self._repository.release(grant_id, run_id=run_id)
            raise
        if not self._repository.finish(
            grant_id, claimed.export_id, run_id=run_id
        ):
            return None
        return archive


def _download_secret_hash(secret: str) -> bytes:
    if not isinstance(secret, str) or not 40 <= len(secret) <= 64:
        raise ValueError("invalid one-time download secret")
    try:
        secret.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("invalid one-time download secret") from error
    digest = hashlib.sha256(
        b"attune-customer-export-download-v1:" + secret.encode("ascii")
    ).digest()
    return _fixed_hash("download_secret_hash", digest)
