"""Documentation consistency tests."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_example_environment_variable_is_documented():
    example = (ROOT / ".env.example").read_text()
    reference = (ROOT / "docs" / "configuration.md").read_text()

    example_keys = set(
        re.findall(r"^(?:# )?([A-Z][A-Z0-9_]+)=", example, flags=re.MULTILINE)
    )
    documented_keys = set(
        re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", reference, flags=re.MULTILINE)
    )

    assert documented_keys == example_keys


def test_quickstart_uses_guided_local_setup():
    readme = (ROOT / "README.md").read_text()
    quickstart = readme.split("## Quick start", 1)[1].split("## Development", 1)[0]

    assert "attune init --target local" in quickstart
    assert "docker compose" not in quickstart
    assert "attune doctor" not in quickstart


def test_qdrant_compose_images_are_pinned_and_loopback_bound():
    compose = (ROOT / "deploy" / "compose.yml").read_text()
    local = (ROOT / "src" / "attune" / "resources" / "local-compose.yml").read_text()

    assert "qdrant/qdrant:latest" not in compose + local
    assert "qdrant/qdrant:v1.18.2" in compose
    assert "qdrant/qdrant:v1.18.2" in local
    assert '"127.0.0.1:6333:6333"' in compose
    assert '"127.0.0.1:6333:6333"' in local


def test_slack_owner_destination_reuses_allowlisted_user_id():
    guide = (ROOT / "docs" / "getting-started.md").read_text()

    assert "ATTUNE_SLACK_CHANNEL=U0123456789" in guide
    assert "conversations_open" not in guide


def test_gcp_foundation_preserves_hosted_security_boundaries():
    root = ROOT / "deploy" / "gcp" / "foundation"
    terraform = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))

    assert 'version = "7.34.0"' in terraform
    assert 'ipv4_enabled    = false' in terraform
    assert 'cloudsql.iam_authentication' in terraform
    assert 'edition                     = "ENTERPRISE"' in terraform
    assert '".gserviceaccount.com"' in terraform
    assert 'deletion_protection = true' in terraform
    assert 'public_access_prevention    = "enforced"' in terraform
    assert 'prevent_destroy = true' in terraform
    assert 'serviceAccount:gmail-api-push@system.gserviceaccount.com' in terraform
    assert 'roles/secretmanager.secretAccessor' in terraform
    assert 'workload["dispatch_broker"].email' in terraform
    assert 'toset(["control_plane", "ingress"])' not in terraform
    assert 'toset(["control_plane", "worker"])' not in terraform
    assert 'name            = "connector-credentials"' in terraform
    assert 'roles/cloudkms.cryptoKeyEncrypterDecrypter' in terraform
    assert 'roles/secretmanager.secretVersionAdder' not in terraform
    assert 'roles/secretmanager.admin' not in terraform
    assert 'roles/editor' not in terraform
    assert 'roles/owner' not in terraform
    assert "secret_manager_secret_version" not in terraform
    assert "google_cloud_run_v2_service" not in terraform


def test_gcp_foundation_documents_no_customer_data_gate():
    foundation = (ROOT / "deploy" / "gcp" / "foundation" / "README.md").read_text()
    architecture = (ROOT / "docs" / "hosted-gcp.md").read_text()
    normalized_foundation = " ".join(foundation.split())
    normalized_architecture = " ".join(architecture.split())

    assert "does not admit customer data" in normalized_foundation
    assert "gmail-api-push@system.gserviceaccount.com" in foundation
    assert "constraints/iam.allowedPolicyMemberDomains" in foundation
    assert "add-iam-policy-binding" in foundation
    assert "restore_domain_policy" in foundation
    assert "terraform plan -detailed-exitcode" in foundation
    assert "Repeat this procedure only for a new topic/project" in normalized_foundation
    assert "making the topic public" in normalized_foundation
    assert "No secret value may enter Terraform state" in normalized_architecture
    assert "Production is blocked" in normalized_architecture


def test_hosted_data_boundary_is_private_pinned_and_fail_closed():
    root = ROOT / "deploy" / "gcp" / "data"
    terraform = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))
    migration = "\n".join(
        path.read_text()
        for path in sorted((ROOT / "src" / "attune" / "hosted" / "sql").glob("*.sql"))
    )
    dockerfile = (ROOT / "deploy" / "migrator" / "Dockerfile").read_text()
    guide = (root / "README.md").read_text()
    normalized_guide = " ".join(guide.split())

    assert 'database_roles = ["cloudsqlsuperuser"]' in terraform
    assert 'roles/cloudsql.client' in terraform
    assert 'roles/cloudsql.instanceUser' in terraform
    assert 'roles/logging.logWriter' in terraform
    assert 'PRIVATE_RANGES_ONLY' in terraform
    assert 'max_retries     = 0' in terraform
    assert '@sha256:[0-9a-f]{64}$' in terraform
    assert "allUsers" not in terraform
    assert "secret_key_ref" not in terraform
    assert "google_cloud_run_v2_service" not in terraform

    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "verified tenant context is required" in migration
    assert "audit records are append-only" in migration
    assert "attune_ext.vector" in migration
    assert "credential_ref uuid" in migration
    assert "refresh_token" not in migration

    assert "@sha256:" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert (
        "The transaction tenant setting is a storage guard, not authentication"
        in guide
    )
    assert "Customer data remains prohibited" in normalized_guide
    assert "Provider content and executable arguments" in normalized_guide
    assert "does not sign arbitrary body fields" in normalized_guide
    assert "live worker service remains prohibited" in normalized_guide


def test_dispatch_broker_boundary_is_documented_and_fail_closed():
    architecture = (ROOT / "docs" / "security-architecture.md").read_text()
    broker = (ROOT / "docs" / "dispatch-broker.md").read_text()
    decisions = (ROOT / "docs" / "decisions.md").read_text()

    assert "SEC-207" in architecture
    assert "opaque intent ID only" in broker
    assert "only Cloud Tasks producer" in broker
    assert "Direct producer enqueue" in broker
    assert "A private broker exclusively owns hosted task dispatch" in decisions


def test_audit_writer_is_private_intent_only_and_least_privileged():
    root = ROOT / "deploy" / "gcp" / "runtime"
    terraform = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))
    migration = (
        ROOT / "src" / "attune" / "hosted" / "sql" / "0004_audit_intents.sql"
    ).read_text()
    architecture = (ROOT / "docs" / "audit-writer.md").read_text()

    assert 'INGRESS_TRAFFIC_INTERNAL_ONLY' in terraform
    assert 'roles/run.invoker' in terraform
    assert 'allUsers' not in terraform
    assert '@sha256:[0-9a-f]{64}$' in terraform
    assert 'secret_key_ref' not in terraform
    assert 'write_audit_intent(uuid)' in migration
    assert 'REVOKE EXECUTE ON FUNCTION' in migration
    assert 'FROM attune_audit_writer' in migration
    assert 'caller-supplied tenant' in architecture
    assert 'only the opaque audit-intent UUID' in architecture


def test_secret_broker_is_private_exact_identity_and_kms_bound():
    root = ROOT / "deploy" / "gcp" / "runtime"
    terraform = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))
    architecture = (ROOT / "docs" / "secret-broker.md").read_text()
    normalized_architecture = " ".join(architecture.split())
    dockerfile = (ROOT / "deploy" / "secret-broker" / "Dockerfile").read_text()

    assert 'custom_audiences' in terraform
    assert 'ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT' in terraform
    assert 'ATTUNE_CONNECTOR_KMS_KEY' in terraform
    assert 'secret_broker_invoker' in terraform
    assert 'allUsers' not in terraform
    assert 'USER 65532:65532' in dockerfile
    assert 'no caller-authoritative tenant field' in normalized_architecture
    assert 'content-free `allowed` audit intent' in normalized_architecture
