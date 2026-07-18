"""Settings tests — the ATTUNE_DATA_DIR path derivation (roadmap prompt 08)."""

from __future__ import annotations

import pytest

from attune.config import Settings


def test_data_dir_derives_all_state_paths():
    s = Settings.from_env({"ATTUNE_DATA_DIR": "/var/lib/adc"})
    assert s.data_dir == "/var/lib/adc"
    assert s.audit_log_path == "/var/lib/adc/audit.log.jsonl"
    assert s.checkpointer_db_path == "/var/lib/adc/attune.db"
    assert s.gmail_watch_state_path == "/var/lib/adc/gmail_watch_state.json"
    assert s.chat_subscription_state_path == "/var/lib/adc/chat_subscription_state.json"
    assert s.calendar_watch_state_path == "/var/lib/adc/calendar_watch_state.json"
    assert s.calendar_sync_state_path == "/var/lib/adc/calendar_sync_state.json"
    assert s.pending_state_path == "/var/lib/adc/pending_approvals.json"
    assert s.conversation_state_path == "/var/lib/adc/conversation_state.json"
    assert s.retry_queue_db_path == "/var/lib/adc/source_retries.db"


def test_explicit_path_overrides_data_dir():
    s = Settings.from_env({
        "ATTUNE_DATA_DIR": "/var/lib/adc",
        "ATTUNE_DB_PATH": "/fast-disk/checkpoints.db",
        "ATTUNE_AUDIT_LOG_PATH": "/logs/audit.jsonl",
    })
    assert s.checkpointer_db_path == "/fast-disk/checkpoints.db"
    assert s.audit_log_path == "/logs/audit.jsonl"
    # everything else still derives
    assert s.pending_state_path == "/var/lib/adc/pending_approvals.json"


def test_no_data_dir_keeps_cwd_defaults():
    s = Settings.from_env({})
    assert s.data_dir is None
    assert s.audit_log_path == "./audit.log.jsonl"
    assert s.checkpointer_db_path == "./attune.db"
    assert s.pending_state_path == "./pending_approvals.json"


def test_qdrant_server_defaults_are_typed_and_durable():
    # A copied .env may contain the key with no value; that must not select
    # Mem0's embedded SQLite backend.
    s = Settings.from_env({"ATTUNE_QDRANT_HOST": ""})
    assert s.qdrant_host == "127.0.0.1"
    assert s.qdrant_port == 6333


def test_qdrant_server_accepts_compose_service_override():
    s = Settings.from_env({
        "ATTUNE_QDRANT_HOST": "qdrant",
        "ATTUNE_QDRANT_PORT": "7333",
    })
    assert s.qdrant_host == "qdrant"
    assert s.qdrant_port == 7333


def test_owner_private_slack_dm_needs_no_visibility_ack():
    for destination in ("U0123", "D0123"):
        Settings.from_env({
            "ATTUNE_SLACK_CHANNEL": destination
        }).validate_proactive_destinations()


def test_shared_or_unverifiable_destinations_require_ack():
    for env in (
        {"ATTUNE_SLACK_CHANNEL": "C0123"},
        {"ATTUNE_CHAT_SPACE": "spaces/AAAA"},
    ):
        s = Settings.from_env(env)
        try:
            s.validate_proactive_destinations()
        except ValueError as exc:
            assert "ATTUNE_ACK_DESTINATION_VISIBILITY=1" in str(exc)
        else:
            raise AssertionError("destination should require visibility acknowledgment")


def test_visibility_ack_allows_explicit_shared_destination():
    s = Settings.from_env({
        "ATTUNE_SLACK_CHANNEL": "C0123",
        "ATTUNE_CHAT_SPACE": "spaces/AAAA",
        "ATTUNE_ACK_DESTINATION_VISIBILITY": "1",
    })
    s.validate_proactive_destinations()


@pytest.mark.parametrize(
    "env, message",
    [
        ({"ATTUNE_SLACK_CHANNEL": "#aide"}, "user/conversation ID"),
        ({"ATTUNE_CHAT_SPACE": "AAAA"}, "spaces/AAAA"),
    ],
)
def test_destination_ids_reject_display_names(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env).validate_proactive_destinations()


# ---------------------------------------------------------------------------
# Phase 2 stage 1 — attended sources (docs/future-state.md, G1/G3)
# ---------------------------------------------------------------------------


def test_source_channels_and_spaces_default_off():
    s = Settings.from_env({})
    assert s.slack_source_channels == frozenset()
    assert s.chat_source_spaces == frozenset()
    assert s.attention_path == "./attention.json"


def test_source_channels_and_spaces_parsed_from_csv():
    s = Settings.from_env({
        "ATTUNE_SLACK_SOURCE_CHANNELS": "C111,G222",
        "ATTUNE_CHAT_SOURCE_SPACES": "spaces/AAAA,spaces/BBBB",
    })
    assert s.slack_source_channels == frozenset({"C111", "G222"})
    assert s.chat_source_spaces == frozenset({"spaces/AAAA", "spaces/BBBB"})


def test_attention_path_derives_from_data_dir():
    s = Settings.from_env({"ATTUNE_DATA_DIR": "/var/lib/adc"})
    assert s.attention_path == "/var/lib/adc/attention.json"
    assert s.source_poll_state_path == "/var/lib/adc/source_poll_state.json"


# ---------------------------------------------------------------------------
# Phase 3 stage 1 — mail labeling opt-in (docs/future-state.md, G9)
# ---------------------------------------------------------------------------


def test_mail_labels_enabled_defaults_off():
    s = Settings.from_env({})
    assert s.mail_labels_enabled is False


def test_mail_labels_enabled_parses_true_values():
    for value in ("1", "true", "True", "yes", "on"):
        s = Settings.from_env({"ATTUNE_MAIL_LABELS_ENABLED": value})
        assert s.mail_labels_enabled is True, value


def test_mail_labels_enabled_false_for_other_values():
    for value in ("0", "false", "", "no"):
        s = Settings.from_env({"ATTUNE_MAIL_LABELS_ENABLED": value})
        assert s.mail_labels_enabled is False, value


def test_calendar_writes_enabled_defaults_off():
    s = Settings.from_env({})
    assert s.calendar_writes_enabled is False


def test_calendar_writes_enabled_parses_true_values():
    for value in ("1", "true", "True", "yes", "on"):
        s = Settings.from_env({"ATTUNE_CALENDAR_WRITES_ENABLED": value})
        assert s.calendar_writes_enabled is True, value


def test_calendar_writes_enabled_false_for_other_values():
    for value in ("0", "false", "", "no"):
        s = Settings.from_env({"ATTUNE_CALENDAR_WRITES_ENABLED": value})
        assert s.calendar_writes_enabled is False, value


def test_attention_path_explicit_override():
    s = Settings.from_env({
        "ATTUNE_DATA_DIR": "/var/lib/adc",
        "ATTUNE_ATTENTION_PATH": "/fast-disk/attention.json",
    })
    assert s.attention_path == "/fast-disk/attention.json"


# ---------------------------------------------------------------------------
# Phase 3 stage 3 — the "since yesterday" brief snapshot (docs/future-state.md, G11)
# ---------------------------------------------------------------------------


def test_brief_snapshot_path_defaults():
    s = Settings.from_env({})
    assert s.brief_snapshot_path == "./brief_snapshot.json"


def test_brief_snapshot_path_derives_from_data_dir():
    s = Settings.from_env({"ATTUNE_DATA_DIR": "/var/lib/adc"})
    assert s.brief_snapshot_path == "/var/lib/adc/brief_snapshot.json"


def test_brief_snapshot_path_explicit_override():
    s = Settings.from_env({
        "ATTUNE_DATA_DIR": "/var/lib/adc",
        "ATTUNE_BRIEF_SNAPSHOT_PATH": "/fast-disk/snap.json",
    })
    assert s.brief_snapshot_path == "/fast-disk/snap.json"


def test_validate_rejects_malformed_slack_source_channel():
    s = Settings.from_env({"ATTUNE_SLACK_SOURCE_CHANNELS": "not-a-channel-id"})
    with pytest.raises(ValueError, match="ATTUNE_SLACK_SOURCE_CHANNELS"):
        s.validate()


def test_validate_rejects_malformed_chat_source_space():
    s = Settings.from_env({"ATTUNE_CHAT_SOURCE_SPACES": "AAAA"})
    with pytest.raises(ValueError, match="ATTUNE_CHAT_SOURCE_SPACES"):
        s.validate()


def test_validate_accepts_well_formed_source_config():
    s = Settings.from_env({
        "ATTUNE_SLACK_SOURCE_CHANNELS": "C111,G222",
        "ATTUNE_CHAT_SOURCE_SPACES": "spaces/AAAA",
        "SLACK_BOT_TOKEN": "xoxb-...",
        "ATTUNE_CHAT_CREDENTIALS_FILE": "/path/chat-creds.json",
    })
    s.validate()  # must not raise
