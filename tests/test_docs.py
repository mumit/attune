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
    assert "ipv4_enabled    = false" in terraform
    assert '"dns.googleapis.com"' in terraform
    assert '"identitytoolkit.googleapis.com"' in terraform
    assert '"oauth2.googleapis.com"' in terraform
    assert '"www.googleapis.com"' in terraform
    assert '"gmail.googleapis.com"' in terraform
    required_services = terraform.split("services = toset([", 1)[1].split(
        "])", 1
    )[0]
    assert '"gmail.googleapis.com"' in required_services
    assert '"secretmanager.googleapis.com"' in terraform
    assert '"199.36.153.8"' in terraform
    assert "google_compute_router_nat" not in terraform
    assert 'dns_name    = "googleapis.com."' not in terraform
    assert 'log_id(\\"cloudaudit.googleapis.com/activity\\")' in terraform
    assert 'log_id(\\"cloudaudit.googleapis.com/data_access\\")' in terraform
    assert 'log_id(\\"cloudaudit.googleapis.com/policy\\")' in terraform
    assert 'log_id(\\"cloudaudit.googleapis.com/system_event\\")' in terraform
    assert "cloudsql.iam_authentication" in terraform
    assert 'edition                     = "ENTERPRISE"' in terraform
    assert '".gserviceaccount.com"' in terraform
    assert "deletion_protection = true" in terraform
    assert 'public_access_prevention    = "enforced"' in terraform
    assert "prevent_destroy = true" in terraform
    assert "serviceAccount:gmail-api-push@system.gserviceaccount.com" in terraform
    assert "roles/secretmanager.secretAccessor" in terraform
    assert 'workload["dispatch_broker"].email' in terraform
    assert 'uri_override_enforce_mode = "ALWAYS"' in terraform
    assert 'path = "/v1/tasks/dispatch"' in terraform
    assert 'toset(["control_plane", "ingress"])' not in terraform
    assert 'toset(["control_plane", "worker"])' not in terraform
    assert 'name            = "connector-credentials"' in terraform
    assert "roles/cloudkms.cryptoKeyEncrypterDecrypter" in terraform
    assert "roles/secretmanager.secretVersionAdder" not in terraform
    assert "roles/secretmanager.admin" not in terraform
    assert "roles/editor" not in terraform
    assert "roles/owner" not in terraform
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
    assert "roles/cloudsql.client" in terraform
    assert "roles/cloudsql.instanceUser" in terraform
    assert "roles/logging.logWriter" in terraform
    assert "PRIVATE_RANGES_ONLY" in terraform
    assert "max_retries     = 0" in terraform
    assert "@sha256:[0-9a-f]{64}$" in terraform
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
        "The transaction tenant setting is a storage guard, not authentication" in guide
    )
    assert "Customer data remains prohibited" in normalized_guide
    assert "Provider content and executable arguments" in normalized_guide
    assert "does not sign arbitrary body fields" in normalized_guide
    assert "Live provider-executor activation remains prohibited" in guide
    assert "memberless `NOLOGIN BYPASSRLS` roles" in normalized_guide


def test_dispatch_broker_boundary_is_documented_and_fail_closed():
    architecture = (ROOT / "docs" / "security-architecture.md").read_text()
    broker = (ROOT / "docs" / "dispatch-broker.md").read_text()
    normalized_broker = " ".join(broker.split())
    decisions = (ROOT / "docs" / "decisions.md").read_text()

    assert "SEC-207" in architecture
    assert "opaque intent ID only" in broker
    assert "only Cloud Tasks producer" in broker
    assert "Direct producer enqueue" in broker
    assert "exactly one canonical `intent_id`" in broker
    assert (
        "only with the registered `platform.smoke` route by default" in normalized_broker
    )
    assert (ROOT / "deploy" / "dispatch-broker" / "Dockerfile").exists()
    assert "A private broker exclusively owns hosted task dispatch" in decisions


def test_channel_service_images_include_the_web_runtime():
    for service in ("channel-broker", "google-chat-ingress"):
        dockerfile = (ROOT / "deploy" / service / "Dockerfile").read_text()
        assert '".[hosted-service]"' in dockerfile
        assert "USER 65532:65532" in dockerfile
        assert "ENV PORT=8080" in dockerfile


def test_audit_writer_is_private_intent_only_and_least_privileged():
    root = ROOT / "deploy" / "gcp" / "runtime"
    terraform = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))
    migration = (
        ROOT / "src" / "attune" / "hosted" / "sql" / "0004_audit_intents.sql"
    ).read_text()
    architecture = (ROOT / "docs" / "audit-writer.md").read_text()

    assert "INGRESS_TRAFFIC_INTERNAL_ONLY" in terraform
    assert "roles/run.invoker" in terraform
    assert "allUsers" not in terraform
    assert "@sha256:[0-9a-f]{64}$" in terraform
    assert "secret_key_ref" not in terraform
    assert "write_audit_intent(uuid)" in migration
    assert "REVOKE EXECUTE ON FUNCTION" in migration
    assert "FROM attune_audit_writer" in migration
    assert "caller-supplied tenant" in architecture
    assert "only the opaque audit-intent UUID" in architecture


def test_secret_broker_is_private_exact_identity_and_kms_bound():
    root = ROOT / "deploy" / "gcp" / "runtime"
    terraform = "\n".join(path.read_text() for path in sorted(root.glob("*.tf")))
    architecture = (ROOT / "docs" / "secret-broker.md").read_text()
    normalized_architecture = " ".join(architecture.split())
    dockerfile = (ROOT / "deploy" / "secret-broker" / "Dockerfile").read_text()

    assert "custom_audiences" in terraform
    assert "ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT" in terraform
    assert "ATTUNE_WORKER_SERVICE_ACCOUNT" in terraform
    assert "ATTUNE_CONNECTOR_KMS_KEY" in terraform
    assert "secret_broker_invoker" in terraform
    assert "secret_broker_use_anomaly" in terraform
    assert "alert_notification_channels" in terraform
    assert "allUsers" not in terraform
    assert "USER 65532:65532" in dockerfile
    assert "no caller-authoritative tenant field" in normalized_architecture
    assert "content-free `allowed` audit intent" in normalized_architecture
    assert "access tokens are not returned" in normalized_architecture


def test_worker_is_private_deterministic_and_queue_routed():
    runtime = ROOT / "deploy" / "gcp" / "runtime"
    terraform = "\n".join(path.read_text() for path in sorted(runtime.glob("*.tf")))
    dockerfile = (ROOT / "deploy" / "worker" / "Dockerfile").read_text()
    routes = (ROOT / "src" / "attune" / "hosted" / "worker_routes.py").read_text()

    assert 'resource "google_cloud_run_v2_service" "worker"' in terraform
    assert "worker_invoker" in terraform
    assert "workload_identities.task_dispatch" in terraform
    assert "custom_audiences" in terraform
    assert "USER 65532:65532" in dockerfile
    assert "platform.smoke" in routes
    assert "google_gmail_profile: JobExecutor | None = None" in routes
    assert "google_workspace_verification: JobExecutor | None = None" in routes
    assert "enable_google_gmail_profile" in terraform
    assert "enable_google_workspace_verification" in terraform
    assert "length(var.alert_notification_channels) > 0" in terraform
    assert "var.enable_dispatch_broker" in terraform


def test_control_plane_edge_is_locked_before_oauth_activation():
    edge = ROOT / "deploy" / "gcp" / "edge"
    terraform = "\n".join(path.read_text() for path in sorted(edge.glob("*.tf")))
    service = (
        ROOT / "src" / "attune" / "hosted" / "control_plane_service.py"
    ).read_text()
    dockerfile = (ROOT / "deploy" / "control-plane" / "Dockerfile").read_text()
    callback = (
        ROOT / "src" / "attune" / "hosted" / "oauth_callback_service.py"
    ).read_text()
    callback_dockerfile = (ROOT / "deploy" / "oauth-callback" / "Dockerfile").read_text()

    assert "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" in terraform
    assert "default_uri_disabled = true" in terraform
    assert "invoker_iam_disabled = true" in terraform
    assert 'member   = "allUsers"' not in terraform
    assert 'type        = "CLOUD_ARMOR"' in terraform
    assert 'action      = "deny(403)"' in terraform
    assert "request.path == '/healthz'" in terraform
    assert "strip_query            = true" in terraform
    assert 'min_tls_version = "TLS_1_2"' in terraform
    assert re.search(
        r'enable_google_workspace_oauth"\s*\{[^}]*default\s*=\s*false', terraform, re.S
    )
    assert "oauth_is_enabled            = var.enable_google_workspace_oauth" in terraform
    assert "google_oauth_provider_ready" in terraform
    assert '@app.get("/oauth/google/callback")' not in service
    assert "USER 65532:65532" in dockerfile
    assert "google_logging_project_exclusion" in terraform
    assert 'log_id("run.googleapis.com/requests")' in terraform
    assert "resource.labels.service_name" in terraform
    assert 'resource.type="http_load_balancer"' in terraform
    assert "resource.labels.backend_service_name" in terraform
    assert 'log_id("requests")' in terraform
    assert 'resource "google_compute_backend_service" "oauth_callback"' in terraform
    assert "enable = false" in terraform
    assert "request.method == 'GET'" in terraform
    assert "request.path == '/assets/attune-chat-avatar.png'" in terraform
    assert "request.query_string" in callback
    assert 'redirect("/", code=303)' in callback
    assert "OAUTH_BINDING_COOKIE" in callback
    assert "exchange.exchange" in callback
    assert '--access-logfile", "/dev/null"' in callback_dockerfile
    assert "USER 65532:65532" in callback_dockerfile
    assert "--require-hashes" in callback_dockerfile
    assert '".[hosted-service]"' not in callback_dockerfile

    chat_backend = terraform.split(
        'resource "google_compute_backend_service" "google_chat_ingress"', 1
    )[1].split('resource "google_compute_global_address"', 1)[0]
    assert "timeout_sec" not in chat_backend


def test_oauth_callback_identity_has_no_data_or_log_writer_authority():
    iam = (ROOT / "deploy" / "gcp" / "foundation" / "iam.tf").read_text()

    assert re.search(r'oauth_callback\s*=\s*"oauth-cb"', iam)
    assert 'if !contains(["oauth_callback", "oauth_exchange"], name)' in iam
    database_identity_set = iam.split(
        'resource "google_project_iam_member" "database_client"', 1
    )[1].split('resource "google_cloud_tasks_queue_iam_member"', 1)[0]
    assert '"oauth_callback"' not in database_identity_set
    assert re.search(r'oauth_exchange\s*=\s*"oauth-xchg"', iam)
    assert '"oauth_exchange"' in database_identity_set


def test_oauth_exchange_database_boundary_is_function_only():
    migration = (
        ROOT / "src" / "attune" / "hosted" / "sql" / "0013_oauth_transactions.sql"
    ).read_text()

    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "TO attune_oauth_exchange" in migration
    assert (
        "GRANT SELECT, INSERT ON attune.oauth_transactions TO attune_control_plane"
        in migration
    )
    assert (
        "GRANT SELECT, UPDATE ON attune.oauth_transactions TO attune_oauth_executor"
        in migration
    )
    assert "GRANT SELECT ON attune.connectors TO attune_oauth_executor" in migration
    assert (
        "GRANT SELECT ON attune.oauth_transactions TO attune_oauth_exchange"
        not in migration
    )
    assert "transaction.binding_hash = p_binding_hash" in migration


def test_oauth_exchange_runtime_preserves_broker_boundary():
    terraform = (ROOT / "deploy" / "gcp" / "runtime" / "main.tf").read_text()
    dispatch = terraform.split(
        'resource "google_cloud_run_v2_service" "dispatch_broker"', 1
    )[1].split('resource "google_cloud_run_v2_service" "secret_broker"', 1)[0]
    broker = terraform.split('resource "google_cloud_run_v2_service" "secret_broker"', 1)[
        1
    ].split('resource "google_cloud_run_v2_service" "oauth_exchange"', 1)[0]
    exchange = terraform.split(
        'resource "google_cloud_run_v2_service" "oauth_exchange"', 1
    )[1]

    assert "ATTUNE_GOOGLE_OAUTH_CLIENT_SECRET" not in dispatch
    assert "ATTUNE_OAUTH_EXCHANGE_SERVICE_ACCOUNT" not in dispatch
    assert "ATTUNE_GOOGLE_OAUTH_CLIENT_SECRET" in broker
    assert "ATTUNE_OAUTH_EXCHANGE_SERVICE_ACCOUNT" in broker
    assert 'ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"' in exchange
    assert "secret_broker_oauth_invoker" in terraform
    assert "oauth_exchange_invoker" in terraform


def test_identity_platform_is_secret_free_and_dormant_in_terraform():
    foundation = "\n".join(
        path.read_text()
        for path in sorted((ROOT / "deploy" / "gcp" / "foundation").glob("*.tf"))
    )
    edge = "\n".join(
        path.read_text()
        for path in sorted((ROOT / "deploy" / "gcp" / "edge").glob("*.tf"))
    )

    assert '"identitytoolkit.googleapis.com"' in foundation
    assert "google_identity_platform_default_supported_idp_config" not in foundation
    assert (
        "default     = false"
        in (ROOT / "deploy" / "gcp" / "edge" / "variables.tf").read_text()
    )
    assert 'name  = "ATTUNE_IDENTITY_ENABLED"' in edge
    assert "identity_provider_ready" in edge
    assert "request.path == '/v1/session/bootstrap'" in edge
    assert "google_oauth_client_secret" not in edge.lower()


def test_identity_session_database_boundary_is_function_only():
    migration = (
        ROOT / "src" / "attune" / "hosted" / "sql" / "0015_identity_sessions.sql"
    ).read_text()

    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "attune_identity_executor" in migration
    assert "LIMIT 2" in migration
    assert "count(*) FROM matches" in migration
    assert "TO attune_control_plane" in migration
    assert (
        "GRANT SELECT ON attune.identity_sessions TO attune_control_plane"
        not in migration
    )
    assert "REVOKE CREATE ON SCHEMA attune FROM attune_identity_executor" in migration


def test_initial_identity_provisioning_is_private_one_purpose_and_secret_aware():
    foundation = (ROOT / "deploy" / "gcp" / "foundation" / "iam.tf").read_text()
    data = (ROOT / "deploy" / "gcp" / "data" / "main.tf").read_text()
    migration = (
        ROOT
        / "src"
        / "attune"
        / "hosted"
        / "sql"
        / "0016_initial_identity_provisioning.sql"
    ).read_text()

    assert 'identity_provisioner = "id-prov"' in foundation
    assert 'if !contains(["channel-reference-hmac", "identity-bootstrap"], name)' in foundation
    assert 'workload["identity_provisioner"]' in foundation
    assert 'resource "google_cloud_run_v2_job" "identity_provision"' in data
    assert "google_cloud_run_v2_job_iam" not in data
    assert "ATTUNE_IDENTITY_BOOTSTRAP_SECRET" in data
    assert "ATTUNE_IDENTITY_SUBJECT_HASH" not in data
    assert "provision_initial_identity(bytea,text,text,text)" in migration
    assert "TO attune_identity_provisioner" in migration
    assert "GRANT SELECT, INSERT ON attune.tenants, attune.principals" in migration
    assert "GRANT SELECT ON attune.tenants" not in migration
    assert (
        "REVOKE CREATE ON SCHEMA attune\nFROM attune_identity_provisioning_executor"
        in migration
    )


def test_hosted_sign_in_prepares_binding_before_click_time_popup():
    source = (ROOT / "web" / "hosted-identity" / "src" / "sign-in.js").read_text()
    exchange = source[source.index("async function exchange") :]
    exchange = exchange[: exchange.index("function safeFailure")]
    popup = exchange.index("await signInWithPopup")
    assert "await " not in exchange[:popup]
    assert "fetch(" not in exchange[:popup]


def test_hosted_sign_in_diagnostics_disclose_only_normalized_error_metadata():
    source = (ROOT / "web" / "hosted-identity" / "src" / "sign-in.js").read_text()

    assert "error.message" not in source
    assert "error.customData" not in source
    assert "auth\\/[a-z0-9-]{1,64}" in source
    assert "DOMException|Error|FirebaseError|TypeError" in source
    main = source[source.index("async function main") :]
    assert main.index("prepareLoginBinding") < main.index("addEventListener")
