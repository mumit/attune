"""Settings tests — the ADC_DATA_DIR path derivation (roadmap prompt 08)."""

from __future__ import annotations

import pytest

from aidedecamp.config import Settings


def test_data_dir_derives_all_state_paths():
    s = Settings.from_env({"ADC_DATA_DIR": "/var/lib/adc"})
    assert s.data_dir == "/var/lib/adc"
    assert s.audit_log_path == "/var/lib/adc/audit.log.jsonl"
    assert s.checkpointer_db_path == "/var/lib/adc/aidedecamp.db"
    assert s.gmail_watch_state_path == "/var/lib/adc/gmail_watch_state.json"
    assert s.chat_subscription_state_path == "/var/lib/adc/chat_subscription_state.json"
    assert s.calendar_watch_state_path == "/var/lib/adc/calendar_watch_state.json"
    assert s.calendar_sync_state_path == "/var/lib/adc/calendar_sync_state.json"
    assert s.pending_state_path == "/var/lib/adc/pending_approvals.json"
    assert s.conversation_state_path == "/var/lib/adc/conversation_state.json"
    assert s.retry_queue_db_path == "/var/lib/adc/source_retries.db"


def test_explicit_path_overrides_data_dir():
    s = Settings.from_env({
        "ADC_DATA_DIR": "/var/lib/adc",
        "ADC_DB_PATH": "/fast-disk/checkpoints.db",
        "ADC_AUDIT_LOG_PATH": "/logs/audit.jsonl",
    })
    assert s.checkpointer_db_path == "/fast-disk/checkpoints.db"
    assert s.audit_log_path == "/logs/audit.jsonl"
    # everything else still derives
    assert s.pending_state_path == "/var/lib/adc/pending_approvals.json"


def test_no_data_dir_keeps_cwd_defaults():
    s = Settings.from_env({})
    assert s.data_dir is None
    assert s.audit_log_path == "./audit.log.jsonl"
    assert s.checkpointer_db_path == "./aidedecamp.db"
    assert s.pending_state_path == "./pending_approvals.json"


def test_owner_private_slack_dm_needs_no_visibility_ack():
    Settings.from_env({"ADC_SLACK_CHANNEL": "D0123"}).validate_proactive_destinations()


def test_shared_or_unverifiable_destinations_require_ack():
    for env in (
        {"ADC_SLACK_CHANNEL": "C0123"},
        {"ADC_CHAT_SPACE": "spaces/AAAA"},
    ):
        s = Settings.from_env(env)
        try:
            s.validate_proactive_destinations()
        except ValueError as exc:
            assert "ADC_ACK_DESTINATION_VISIBILITY=1" in str(exc)
        else:
            raise AssertionError("destination should require visibility acknowledgment")


def test_visibility_ack_allows_explicit_shared_destination():
    s = Settings.from_env({
        "ADC_SLACK_CHANNEL": "C0123",
        "ADC_CHAT_SPACE": "spaces/AAAA",
        "ADC_ACK_DESTINATION_VISIBILITY": "1",
    })
    s.validate_proactive_destinations()


@pytest.mark.parametrize(
    "env, message",
    [
        ({"ADC_SLACK_CHANNEL": "#aide"}, "conversation ID"),
        ({"ADC_CHAT_SPACE": "AAAA"}, "spaces/AAAA"),
    ],
)
def test_destination_ids_reject_display_names(env, message):
    with pytest.raises(ValueError, match=message):
        Settings.from_env(env).validate_proactive_destinations()
