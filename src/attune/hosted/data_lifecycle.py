"""Reviewed lifecycle inventory for tenant-bearing hosted relations.

This module is deliberately declarative.  A new tenant table cannot quietly
appear outside the retention, export, and deletion design: the migrator checks
the live schema against this inventory before accepting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class _StringEnum(str, Enum):
    """Python 3.10-compatible equivalent of the 3.11 ``StrEnum`` behavior."""

    def __str__(self) -> str:
        return self.value


class DataClass(_StringEnum):
    ACCOUNT = "account"
    CUSTOMER_CONTENT = "customer_content"
    CREDENTIAL = "credential"
    OPERATIONAL = "operational"
    SECURITY_AUDIT = "security_audit"
    DELETION_LEDGER = "deletion_ledger"


class DeletionRule(_StringEnum):
    ERASE = "erase"
    CRYPTO_ERASE = "crypto_erase"
    DEIDENTIFY = "deidentify"
    RETAIN_TOMBSTONE = "retain_tombstone"


@dataclass(frozen=True)
class RelationalAsset:
    table: str
    data_class: DataClass
    deletion_rule: DeletionRule
    customer_export: bool


def _assets(
    tables: str,
    data_class: DataClass,
    deletion_rule: DeletionRule,
    *,
    customer_export: bool,
) -> tuple[RelationalAsset, ...]:
    return tuple(
        RelationalAsset(table, data_class, deletion_rule, customer_export)
        for table in tables.split()
    )


RELATIONAL_ASSETS = (
    # tenant_model_preferences (Phase 6 "hosted operations"; per-tenant model
    # configuration) is a bounded owner preference -- one row per tenant, a
    # named profile chosen from a fixed vocabulary -- the same shape as
    # `hosted_channel_preferences`, so it gets that table's exact
    # class/rule/export triple.
    *_assets(
        "tenants principals installations connectors policies autonomy_grants "
        "hosted_onboarding_states hosted_channel_preferences "
        "hosted_channel_destinations tenant_model_preferences",
        DataClass.ACCOUNT,
        DeletionRule.ERASE,
        customer_export=True,
    ),
    # importance_signals (docs/future-state.md Phase 5 item 1) is derived
    # behavioral state the principal can inspect/correct locally (`attune
    # importance show/pin`) -- the same "owner-inspectable, owner-correctable"
    # posture as `memories`, so it gets the same class/rule/export triple, not
    # a bespoke "derived" bucket. attention_items is recorded chat/Slack
    # signal content with its own retention window (RETENTION_DAYS,
    # attune.hosted.intelligence), matching conversation_turns.
    # hosted_brief_deliveries (docs/future-state.md Phase 5 item 4) stores the
    # bounded rendered brief text delivered to the owner directly -- unlike
    # hosted_channel_deliveries, which only tracks delivery state and reads
    # its content from conversation_turns -- so it gets the same class/rule/
    # export triple as conversation_turns/memories, not the OPERATIONAL
    # bucket the delivery-claim tables below otherwise share.
    *_assets(
        "memories memory_embeddings conversations conversation_turns "
        "importance_signals attention_items hosted_brief_deliveries",
        DataClass.CUSTOMER_CONTENT,
        DeletionRule.ERASE,
        customer_export=True,
    ),
    *_assets(
        "connector_credentials hosted_channel_credentials",
        DataClass.CREDENTIAL,
        DeletionRule.CRYPTO_ERASE,
        customer_export=False,
    ),
    # model_usage_daily (Phase 6 "hosted operations"; per-tenant model usage
    # metering) is a content-free per (tenant, task, profile, day) aggregate
    # counter feeding future billing -- never prompt/response text, never a
    # per-message row -- the same class/rule/export triple as the pre-existing
    # generic `usage_records` table it sits alongside.
    *_assets(
        "jobs approvals capability_admissions provider_events job_retries "
        "workflow_checkpoints "
        "usage_records dispatch_intents credential_intents job_reconciliations "
        "oauth_transactions identity_sessions hosted_channel_setup_transactions "
        "hosted_channel_routes hosted_channel_deliveries export_jobs "
        "export_object_attempts export_download_grants model_usage_daily",
        DataClass.OPERATIONAL,
        DeletionRule.ERASE,
        customer_export=False,
    ),
    *_assets(
        "audit_heads audit_events audit_intents",
        DataClass.SECURITY_AUDIT,
        DeletionRule.DEIDENTIFY,
        customer_export=True,
    ),
    # deletion_requests (docs/future-state.md Phase 6 "hosted operations";
    # G19; see docs/data-lifecycle.md's "Content retention and tenant
    # deletion design" section, dated 2026-07-19) is the durable
    # right-to-be-forgotten ceremony ledger: it must survive the tenant's own
    # deletion long enough to prove the ceremony happened, the same
    # class/rule/export triple as `deletion_markers`. It is never a target of
    # its own tenant's erase walk (see `erase_tenant_deletion_relation`).
    *_assets(
        "deletion_markers deletion_requests",
        DataClass.DELETION_LEDGER,
        DeletionRule.RETAIN_TOMBSTONE,
        customer_export=False,
    ),
)

RELATIONAL_ASSET_BY_TABLE = {asset.table: asset for asset in RELATIONAL_ASSETS}


def validate_relational_lifecycle_inventory(table_names: Iterable[str]) -> None:
    """Fail closed if the reviewed inventory and schema table set diverge."""

    names = tuple(table_names)
    if len(RELATIONAL_ASSET_BY_TABLE) != len(RELATIONAL_ASSETS):
        raise RuntimeError("hosted lifecycle inventory contains duplicate tables")
    expected = set(names)
    actual = set(RELATIONAL_ASSET_BY_TABLE)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise RuntimeError(
            "hosted lifecycle inventory does not match tenant tables "
            f"(missing={missing}, unexpected={unexpected})"
        )
