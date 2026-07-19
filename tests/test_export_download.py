"""Tests for one-time export download storage, crypto, and HTTP boundaries."""

from uuid import UUID

import pytest

pytest.importorskip("flask")

from attune.hosted.export_download import (
    ClaimedExportDownload,
    CustomerExportDownloadService,
    ExportDownloadObjectStore,
)
from attune.hosted.export_download_service import create_app

HOST = "dev.attune.mumit.org"
GRANT = UUID("10000000-0000-4000-8000-000000000001")
EXPORT = UUID("10000000-0000-4000-8000-000000000002")
TENANT = UUID("10000000-0000-4000-8000-000000000003")
OBJECT = UUID("10000000-0000-4000-8000-000000000004")
RUN = UUID("10000000-0000-4000-8000-000000000005")
SECRET = "a" * 43


class Blob:
    def __init__(self):
        self.calls = []

    def download_as_bytes(self, **kwargs):
        self.calls.append(kwargs)
        return b"c" * 16


class Bucket:
    def __init__(self):
        self.value = Blob()
        self.calls = []

    def blob(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return self.value


class Client:
    def __init__(self):
        self.value = Bucket()

    def bucket(self, name):
        assert name == "attune-customer-exports"
        return self.value


def test_download_object_store_binds_name_generation_size_and_crc():
    client = Client()
    store = ExportDownloadObjectStore("attune-customer-exports", client=client)
    assert store.read(OBJECT, generation=601, expected_bytes=16) == b"c" * 16
    assert client.value.calls == [
        (f"objects/{OBJECT}.bin", {"generation": 601})
    ]
    assert client.value.value.calls == [
        {
            "if_generation_match": 601,
            "checksum": "crc32c",
            "single_shot_download": True,
        }
    ]


class Repository:
    def __init__(self, claim):
        self.value = claim
        self.calls = []
        self.finish_value = True

    def claim(self, grant_id, secret, *, run_id):
        self.calls.append(("claim", grant_id, secret, run_id))
        return self.value

    def release(self, grant_id, *, run_id):
        self.calls.append(("release", grant_id, run_id))
        return True

    def finish(self, grant_id, export_id, *, run_id):
        self.calls.append(("finish", grant_id, export_id, run_id))
        return self.finish_value


def claim():
    return ClaimedExportDownload(
        TENANT, EXPORT, "account", OBJECT, 601, b"wrapped", b"n" * 12,
        "projects/p/locations/l/keyRings/r/cryptoKeys/k", b"p" * 32,
        b"c" * 32, 100, 116, 1,
    )


class Objects:
    def __init__(self, failure=None):
        self.failure = failure

    def read(self, *args, **kwargs):
        if self.failure:
            raise self.failure
        return b"ciphertext"


class Cipher:
    def decrypt(self, encrypted, **context):
        assert context == {
            "tenant_id": TENANT,
            "export_id": EXPORT,
            "scope": "account",
            "object_id": OBJECT,
        }
        return b"archive"


def test_download_finishes_only_after_authenticated_decryption():
    repository = Repository(claim())
    service = CustomerExportDownloadService(repository, Objects(), Cipher())
    assert service.download(GRANT, SECRET, run_id=RUN) == b"archive"
    assert [call[0] for call in repository.calls] == ["claim", "finish"]


def test_download_failure_releases_exact_lease_and_never_consumes():
    repository = Repository(claim())
    service = CustomerExportDownloadService(
        repository, Objects(RuntimeError("private storage failure")), Cipher()
    )
    with pytest.raises(RuntimeError, match="private storage"):
        service.download(GRANT, SECRET, run_id=RUN)
    assert [call[0] for call in repository.calls] == ["claim", "release"]


def test_http_boundary_requires_same_origin_and_never_places_secret_in_url():
    class Downloads:
        def __init__(self):
            self.calls = []

        def download(self, grant_id, secret, *, run_id):
            self.calls.append((grant_id, secret, run_id))
            return b"zip"

    downloads = Downloads()
    client = create_app(HOST, downloads).test_client()
    payload = {"grant_id": str(GRANT), "secret": SECRET}
    assert client.post(
        "/v1/export-download", json=payload, base_url=f"https://{HOST}"
    ).status_code == 401
    response = client.post(
        "/v1/export-download",
        json=payload,
        headers={"Origin": f"https://{HOST}", "Sec-Fetch-Site": "same-origin"},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 200 and response.data == b"zip"
    assert response.headers["Content-Disposition"].endswith(
        '"attune-account-export.zip"'
    )
    assert downloads.calls[0][:2] == (GRANT, SECRET)
    assert isinstance(downloads.calls[0][2], UUID)


def test_download_route_accepts_only_a_same_origin_post_with_a_json_body():
    # The one-time secret must never travel in a URL: pin that the download
    # route takes a POST body only, and that placing the grant/secret on the
    # query string or path instead of the JSON body is refused, not silently
    # accepted.
    class Downloads:
        def download(self, grant_id, secret, *, run_id):
            raise AssertionError("must not be reached")

    client = create_app(HOST, Downloads()).test_client()
    headers = {"Origin": f"https://{HOST}", "Sec-Fetch-Site": "same-origin"}
    get_response = client.get(
        "/v1/export-download", headers=headers, base_url=f"https://{HOST}"
    )
    assert get_response.status_code == 405
    query_response = client.post(
        f"/v1/export-download?grant_id={GRANT}&secret={SECRET}",
        headers=headers,
        base_url=f"https://{HOST}",
    )
    assert query_response.status_code == 401
    empty_body_response = client.post(
        "/v1/export-download",
        json={},
        headers=headers,
        base_url=f"https://{HOST}",
    )
    assert empty_body_response.status_code == 400
