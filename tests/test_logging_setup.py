"""Tests for logging_setup.py's redaction filter (security finding F3, Low,
docs/current-state.md's 2026-07-18 review; SEC-304).

The module docstring is explicit that this filter is DEFENSE IN DEPTH paired
with the writing discipline, not a license to log secrets — these tests pin
the filter's actual coverage (six secret shapes, in both the rendered
message and %-style args), that clean lines pass through untouched, and
that none of the patterns can blow up on adversarially long input.
"""

from __future__ import annotations

import logging
import time

from attune.logging_setup import RedactionFilter, _redact, configure


def _record(msg: str, args: tuple = ()) -> logging.LogRecord:
    return logging.LogRecord(
        name="attune.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )


# ---------------------------------------------------------------------------
# Each secret shape, redacted in the message
# ---------------------------------------------------------------------------


def test_bearer_token_redacted_in_message():
    text = _redact("Authorization: Bearer abc123.DEF-456_ghi~789+/==")
    assert "abc123.DEF-456_ghi~789" not in text
    assert "[REDACTED:bearer_token]" in text


def test_google_access_token_redacted_in_message():
    text = _redact("cred={'token': 'ya29.a0AfH6SMBxSecretValueHere1234567890'}")
    assert "a0AfH6SMBxSecretValueHere1234567890" not in text
    assert "[REDACTED:google_access_token]" in text


def test_refresh_token_json_shape_redacted():
    text = _redact('payload: {"refresh_token": "1//0gSecretRefreshTokenValue"}')
    assert "1//0gSecretRefreshTokenValue" not in text
    assert "[REDACTED:refresh_token]" in text


def test_refresh_token_kwarg_shape_redacted():
    text = _redact("params: refresh_token=1//0gSecretRefreshTokenValue&other=1")
    assert "1//0gSecretRefreshTokenValue" not in text
    assert "[REDACTED:refresh_token]" in text
    assert "other=1" in text  # only the token param is scrubbed


def test_private_key_block_redacted():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDSecretKeyMaterial\n"
        "AnotherLineOfBase64SecretMaterialHere==\n"
        "-----END PRIVATE KEY-----"
    )
    text = _redact(f"loaded key:\n{pem}\ndone")
    assert "SecretKeyMaterial" not in text
    assert "[REDACTED:private_key]" in text
    assert "done" in text


def test_rsa_private_key_variant_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "SomeSecretBase64Material\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text = _redact(pem)
    assert "SomeSecretBase64Material" not in text
    assert "[REDACTED:private_key]" in text


# Slack-shaped fixtures are assembled at runtime so the source file never
# contains a literal that trips GitHub push-protection secret scanning —
# the redaction filter sees the same final string either way.
def test_slack_bot_token_redacted():
    fake = "xoxb-" + "1234567890" + "-" + "abcdefghijklmnop"
    text = _redact(f"SLACK_BOT_TOKEN={fake}")
    assert "abcdefghijklmnop" not in text
    assert "[REDACTED:slack_token]" in text


def test_slack_app_token_redacted():
    fake = "xapp-" + "1-A012345-6789012345-" + "abcdefabcdefabcdef"
    text = _redact(f"app token {fake}")
    assert "A012345" not in text
    assert "[REDACTED:slack_token]" in text


def test_slack_user_token_redacted():
    fake = "xoxp-" + "111111111111-222222222222-333333333333-" + "abcdefabcdef"
    text = _redact(fake)
    assert "[REDACTED:slack_token]" in text


def test_openai_style_api_key_redacted():
    text = _redact("ATTUNE_LLM_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in text
    assert "[REDACTED:api_key]" in text


# ---------------------------------------------------------------------------
# Each shape, redacted when it arrives via %-style args, not just the message
# ---------------------------------------------------------------------------


def test_bearer_token_redacted_in_percent_style_args():
    record = _record("outbound header: %s", ("Bearer abc123SecretToken456",))
    RedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "abc123SecretToken456" not in rendered
    assert "[REDACTED:bearer_token]" in rendered


def test_slack_token_redacted_in_percent_style_args():
    record = _record("configured token: %s", ("xoxb-9999999999-SecretSuffix",))
    RedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "SecretSuffix" not in rendered
    assert "[REDACTED:slack_token]" in rendered


def test_multiple_args_each_scrubbed_independently():
    record = _record(
        "%s / %s",
        ("Bearer FirstSecretToken1234", "sk-SecondSecretKey5678901234"),
    )
    RedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "FirstSecretToken1234" not in rendered
    assert "SecondSecretKey5678901234" not in rendered
    assert rendered.count("[REDACTED:") == 2


def test_non_string_args_left_alone():
    """Args like ints/exception objects must pass through filter() without
    raising — only string args are candidates for redaction."""
    record = _record("count=%d id=%s", (42, "thread-123"))
    RedactionFilter().filter(record)
    assert record.getMessage() == "count=42 id=thread-123"


def test_dict_style_args_are_scrubbed_too():
    record = logging.LogRecord(
        name="attune.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="token=%(tok)s", args=({"tok": "Bearer DictStyleSecretToken"},),
        exc_info=None,
    )
    RedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "DictStyleSecretToken" not in rendered
    assert "[REDACTED:bearer_token]" in rendered


# ---------------------------------------------------------------------------
# Clean lines pass through untouched
# ---------------------------------------------------------------------------


def test_clean_message_unmodified():
    clean = "processed thread gmail:t123:h456 -> ROUTINE"
    assert _redact(clean) == clean


def test_clean_message_with_args_unmodified():
    record = _record("processing %s in domain %s", ("thread-42", "mail"))
    RedactionFilter().filter(record)
    assert record.getMessage() == "processing thread-42 in domain mail"


def test_filter_always_returns_true_never_drops_records():
    """A record that fails to redact for some reason must still be logged
    (a raised exception or a dropped record would be worse than an
    unredacted line reaching the discipline-following caller)."""
    record = _record("perfectly ordinary log line")
    assert RedactionFilter().filter(record) is True


# ---------------------------------------------------------------------------
# Bounded regexes: no catastrophic backtracking on adversarial/huge input
# ---------------------------------------------------------------------------


def test_100kb_clean_line_processes_fast():
    huge = "x" * 100_000
    start = time.perf_counter()
    result = _redact(huge)
    elapsed = time.perf_counter() - start
    assert result == huge
    assert elapsed < 1.0


def test_100kb_line_with_secret_processes_fast():
    huge = ("benign " * 5000) + "Bearer abcdefghijklmnop123456" + (" more" * 5000)
    start = time.perf_counter()
    result = _redact(huge)
    elapsed = time.perf_counter() - start
    assert "[REDACTED:bearer_token]" in result
    assert elapsed < 1.0


def test_unterminated_private_key_marker_does_not_hang():
    """A BEGIN marker with no matching END and 100KB of filler must fail to
    match (nothing to redact) without pathological backtracking — the
    lazy, length-capped quantifier is what keeps this bounded."""
    adversarial = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 100_000)
    start = time.perf_counter()
    result = _redact(adversarial)
    elapsed = time.perf_counter() - start
    assert result == adversarial  # no END marker -> nothing matched
    assert elapsed < 1.0


def test_many_bearer_like_substrings_process_fast():
    """A line with many near-matches (the word 'Bearer' without a token
    after it, repeated) must not cause quadratic-or-worse blowup."""
    adversarial = "Bearer " * 20_000
    start = time.perf_counter()
    _redact(adversarial)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# configure() actually installs the filter on the root handler
# ---------------------------------------------------------------------------


def test_configure_installs_redaction_filter_on_handler():
    configure(level="INFO")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert any(isinstance(f, RedactionFilter) for f in root.handlers[0].filters)


def test_configure_wired_filter_redacts_real_log_output(capsys):
    configure(level="INFO")
    logging.getLogger("attune.test").info("token=%s", "Bearer RealSecretValue123")
    captured = capsys.readouterr()
    assert "RealSecretValue123" not in captured.err
    assert "[REDACTED:bearer_token]" in captured.err
