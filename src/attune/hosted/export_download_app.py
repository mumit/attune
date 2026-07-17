"""Production composition for the isolated customer export download gateway."""

from __future__ import annotations

import os

from .cloud_sql import iam_connection
from .export_crypto import ExportEnvelopeCipher
from .export_download import (
    CustomerExportDownloadService,
    ExportDownloadObjectStore,
    PostgresExportDownloads,
)
from .export_download_service import create_app
from .vault_crypto import GoogleKmsKeyWrapper


def create_production_app():
    cipher = ExportEnvelopeCipher(
        GoogleKmsKeyWrapper(os.environ["ATTUNE_EXPORT_KMS_KEY"])
    )
    return create_app(
        os.environ["ATTUNE_PUBLIC_HOST"],
        CustomerExportDownloadService(
            PostgresExportDownloads(iam_connection),
            ExportDownloadObjectStore(os.environ["ATTUNE_EXPORT_BUCKET"]),
            cipher,
        ),
    )


app = create_production_app()
