"""Reviewed lifecycle inventory for tenant-bearing hosted relations.

This module is deliberately declarative.  A new tenant table cannot quietly
appear outside the retention, export, and deletion design: the migrator checks
the live schema against this inventory before accepting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class DataClass(StrEnum):
    ACCOUNT = "account"
    CUSTOMER_CONTENT = "customer_content"
    CREDENTIAL = "credential"
    OPERATIONAL = "operational"
    SECURITY_AUDIT = "security_audit"
    DELETION_LEDGER = "deletion_ledger"


class DeletionRule(StrEnum):
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
    *_assets(
        "tenants principals installations connectors policies autonomy_grants "
        "hosted_onboarding_states hosted_channel_preferences "
        "hosted_channel_destinations",
        DataClass.ACCOUNT,
        DeletionRule.ERASE,
        customer_export=True,
    ),
    *_assets(
        "memories memory_embeddings conversations conversation_turns",
        DataClass.CUSTOMER_CONTENT,
        DeletionRule.ERASE,
        customer_export=True,
    ),
    *_assets(
        "connector_credentials",
        DataClass.CREDENTIAL,
        DeletionRule.CRYPTO_ERASE,
        customer_export=False,
    ),
    *_assets(
        "jobs approvals provider_events job_retries workflow_checkpoints "
        "usage_records dispatch_intents credential_intents job_reconciliations "
        "oauth_transactions identity_sessions hosted_channel_setup_transactions "
        "hosted_channel_routes hosted_channel_deliveries export_jobs",
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
    *_assets(
        "deletion_markers",
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
