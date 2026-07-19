"""Production composition root for the first deterministic hosted worker."""

from __future__ import annotations

import os

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .brief_delivery import HostedBriefExecutor, PostgresHostedBriefRepository
from .channel_broker import decode_channel_reference_key
from .intelligence import (
    IntelligenceReferenceHasher,
    PostgresAttentionStore,
    PostgresImportanceProfile,
    PostgresImportanceSignalCapture,
)
from .capability_admission import (
    CapabilityAdmissionProducer,
    PostgresCapabilityAdmissionRepository,
)
from .capability_gateway import PostgresCapabilityAuthorityRepository, TypedCapabilityGateway
from .cloud_sql import iam_connection
from .dispatch import PostgresDispatchProducerRepository
from .dispatch_broker_client import DispatchBrokerClient
from .gmail_draft_capability import build_draft_capability_registry
from .google_gmail_draft_create_executor import GoogleGmailDraftCreateExecutor
from .google_gmail_profile_executor import GoogleGmailProfileExecutor
from .google_workspace_verification_executor import (
    GoogleWorkspaceVerificationExecutor,
)
from .google_chat_conversation_executor import (
    GoogleChatConversationExecutor,
    PostgresGoogleChatConversationWorkRepository,
)
from .channel_broker_client import ChannelBrokerClient
from .slack_conversation_executor import (
    PostgresSlackConversationWorkRepository,
    SlackConversationExecutor,
)
from .web_conversation_executor import (
    PostgresWebConversationWorkRepository,
    WebConversationExecutor,
)
from .model_gateway_client import ModelGatewayClient
from .repositories import (
    PostgresApprovalRepository,
    PostgresJobRepository,
    PostgresMemoryRepository,
)
from .reconciliation import PostgresJobReconciliationRepository
from .secret_broker_client import SecretBrokerClient
from .vault import PostgresCredentialIntentRepository
from .worker_audit import WorkerAudit, WorkerMemoryAudit
from .worker_dispatch import WorkerDispatcher
from .worker_routes import registered_routes
from .worker_service import create_app


def _secret(secret_resource: str) -> bytes:
    from google.cloud import secretmanager

    response = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": f"{secret_resource}/versions/latest"}
    )
    return response.payload.data


def _intelligence_reference_hasher() -> IntelligenceReferenceHasher:
    """Reuses the channel broker's own base64 HMAC-key decoder (a plain
    utility, not Google-Chat-specific) for the separate, domain-separated
    intelligence reference key (docs/future-state.md Phase 5 item 1's
    stage-1 note: "no HMAC key is provisioned outside tests" -- signal
    capture and the hosted brief job are this key's first production
    consumers, Phase 5 stage 4)."""
    key = decode_channel_reference_key(
        _secret(os.environ["ATTUNE_INTELLIGENCE_HMAC_SECRET"])
    )
    return IntelligenceReferenceHasher(key)


def create_production_app():
    audit = WorkerAudit(
        PostgresAuditProducerRepository(
            iam_connection,
            producer_kind="worker",
        ),
        AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"]),
    )
    google_gmail_profile = None
    enabled = os.environ.get("ATTUNE_ENABLE_GOOGLE_GMAIL_PROFILE", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_GOOGLE_GMAIL_PROFILE must be true or false")
    if enabled == "true":
        google_gmail_profile = GoogleGmailProfileExecutor(
            PostgresCredentialIntentRepository(
                iam_connection,
                producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
        )
    google_workspace_verification = None
    workspace_enabled = os.environ.get(
        "ATTUNE_ENABLE_GOOGLE_WORKSPACE_VERIFICATION", "false"
    )
    if workspace_enabled not in {"true", "false"}:
        raise ValueError(
            "ATTUNE_ENABLE_GOOGLE_WORKSPACE_VERIFICATION must be true or false"
        )
    if workspace_enabled == "true":
        google_workspace_verification = GoogleWorkspaceVerificationExecutor(
            PostgresCredentialIntentRepository(
                iam_connection,
                producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
        )
    google_gmail_draft_create = None
    capability_gateway = None
    capability_admissions = None
    importance_signals = None
    draft_capability_enabled = os.environ.get(
        "ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY", "false"
    )
    if draft_capability_enabled not in {"true", "false"}:
        raise ValueError(
            "ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY must be true or false"
        )
    if draft_capability_enabled == "true":
        # Dormant even when this gate is on: no tenant holds an R2 autonomy
        # grant for google.gmail.draft.create, and no OAuth flow ever
        # requests gmail.compose, so TypedCapabilityGateway.authorize()
        # always fails closed in production (docs/capability-gateway.md).
        google_gmail_draft_create = GoogleGmailDraftCreateExecutor(
            PostgresCredentialIntentRepository(
                iam_connection, producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
        )
        capability_gateway = TypedCapabilityGateway(
            registry=build_draft_capability_registry(),
            authority=PostgresCapabilityAuthorityRepository(iam_connection),
        )
        capability_admissions = CapabilityAdmissionProducer(
            PostgresCapabilityAdmissionRepository(iam_connection),
            PostgresApprovalRepository(iam_connection),
            PostgresDispatchProducerRepository(
                iam_connection, producer_kind="worker",
            ),
            DispatchBrokerClient(
                os.environ["ATTUNE_DISPATCH_BROKER_URL"],
                os.environ["ATTUNE_DISPATCH_BROKER_AUDIENCE"],
            ),
        )
        # Signal capture closes the loop (Phase 5 stage 4, G12): an
        # approve/reject decision on this capability now feeds stage 1's
        # importance profile, keyed on the hashed thread reference.
        importance_signals = PostgresImportanceSignalCapture(
            iam_connection, _intelligence_reference_hasher(),
        )

    hosted_memory_enabled = os.environ.get("ATTUNE_ENABLE_HOSTED_MEMORY", "false")
    if hosted_memory_enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_HOSTED_MEMORY must be true or false")
    memory = None
    memory_audit = None
    if hosted_memory_enabled == "true":
        memory = PostgresMemoryRepository(iam_connection)
        memory_audit = WorkerMemoryAudit(
            PostgresAuditProducerRepository(iam_connection, producer_kind="worker"),
            AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"]),
        )

    google_chat_conversation = None
    conversation_enabled = os.environ.get(
        "ATTUNE_ENABLE_GOOGLE_CHAT_CONVERSATION", "false"
    )
    if conversation_enabled not in {"true", "false"}:
        raise ValueError(
            "ATTUNE_ENABLE_GOOGLE_CHAT_CONVERSATION must be true or false"
        )
    if conversation_enabled == "true":
        google_chat_conversation = GoogleChatConversationExecutor(
            PostgresGoogleChatConversationWorkRepository(iam_connection),
            PostgresCredentialIntentRepository(
                iam_connection, producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
            ModelGatewayClient(
                os.environ["ATTUNE_MODEL_GATEWAY_URL"],
                os.environ["ATTUNE_MODEL_GATEWAY_AUDIENCE"],
            ),
            ChannelBrokerClient(
                os.environ["ATTUNE_CHANNEL_BROKER_URL"],
                os.environ["ATTUNE_CHANNEL_BROKER_AUDIENCE"],
            ),
            timezone_name=os.environ.get("ATTUNE_HOSTED_TIMEZONE", "UTC"),
            memory=memory,
            memory_audit=memory_audit,
        )
    slack_conversation = None
    slack_conversation_enabled = os.environ.get(
        "ATTUNE_ENABLE_SLACK_CONVERSATION", "false"
    )
    if slack_conversation_enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_SLACK_CONVERSATION must be true or false")
    if slack_conversation_enabled == "true":
        slack_conversation = SlackConversationExecutor(
            PostgresSlackConversationWorkRepository(iam_connection),
            PostgresCredentialIntentRepository(
                iam_connection, producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
            ModelGatewayClient(
                os.environ["ATTUNE_MODEL_GATEWAY_URL"],
                os.environ["ATTUNE_MODEL_GATEWAY_AUDIENCE"],
            ),
            ChannelBrokerClient(
                os.environ["ATTUNE_CHANNEL_BROKER_URL"],
                os.environ["ATTUNE_CHANNEL_BROKER_AUDIENCE"],
            ),
            timezone_name=os.environ.get("ATTUNE_HOSTED_TIMEZONE", "UTC"),
            memory=memory,
            memory_audit=memory_audit,
        )
    web_conversation = None
    web_conversation_enabled = os.environ.get(
        "ATTUNE_ENABLE_WEB_CONVERSATION", "false"
    )
    if web_conversation_enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_WEB_CONVERSATION must be true or false")
    if web_conversation_enabled == "true":
        web_conversation = WebConversationExecutor(
            PostgresWebConversationWorkRepository(iam_connection),
            PostgresCredentialIntentRepository(
                iam_connection, producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
            ModelGatewayClient(
                os.environ["ATTUNE_MODEL_GATEWAY_URL"],
                os.environ["ATTUNE_MODEL_GATEWAY_AUDIENCE"],
            ),
            timezone_name=os.environ.get("ATTUNE_HOSTED_TIMEZONE", "UTC"),
            memory=memory,
            memory_audit=memory_audit,
            capability_gateway=capability_gateway,
            capability_admissions=capability_admissions,
            importance_signals=importance_signals,
        )

    hosted_brief = None
    hosted_brief_enabled = os.environ.get("ATTUNE_ENABLE_HOSTED_BRIEF", "false")
    if hosted_brief_enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_HOSTED_BRIEF must be true or false")
    if hosted_brief_enabled == "true":
        brief_hasher = _intelligence_reference_hasher()
        hosted_brief = HostedBriefExecutor(
            PostgresHostedBriefRepository(iam_connection),
            PostgresCredentialIntentRepository(
                iam_connection, producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
            ChannelBrokerClient(
                os.environ["ATTUNE_CHANNEL_BROKER_URL"],
                os.environ["ATTUNE_CHANNEL_BROKER_AUDIENCE"],
            ),
            lambda context, principal_id: PostgresImportanceProfile(
                iam_connection, context, principal_id, reference_hasher=brief_hasher,
            ),
            lambda context, principal_id: PostgresAttentionStore(
                iam_connection, context, principal_id, reference_hasher=brief_hasher,
            ),
            timezone_name=os.environ.get("ATTUNE_HOSTED_TIMEZONE", "UTC"),
            audit=WorkerMemoryAudit(
                PostgresAuditProducerRepository(iam_connection, producer_kind="worker"),
                AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"]),
            ),
        )

    dispatcher = WorkerDispatcher(
        jobs=PostgresJobRepository(iam_connection),
        audit=audit,
        reconciliations=PostgresJobReconciliationRepository(iam_connection),
        routes=registered_routes(
            google_gmail_profile=google_gmail_profile,
            google_workspace_verification=google_workspace_verification,
            google_gmail_draft_create=google_gmail_draft_create,
            google_chat_conversation=google_chat_conversation,
            slack_conversation=slack_conversation,
            web_conversation=web_conversation,
            hosted_brief=hosted_brief,
        ),
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_service_account=os.environ[
            "ATTUNE_TASK_DISPATCH_SERVICE_ACCOUNT"
        ],
    )
    return create_app(dispatcher)


app = create_production_app()
