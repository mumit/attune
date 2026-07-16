from attune.hosted.google_chat_ingress import (
    decode_owner_dm_link,
    decode_owner_dm_link_diagnostic,
    decode_owner_dm_message_diagnostic,
)

CODE = "A" * 43


def event():
    return {
        "type": "MESSAGE",
        "user": {"name": "users/123456", "type": "HUMAN"},
        "space": {
            "name": "spaces/AAAA-test",
            "spaceType": "DIRECT_MESSAGE",
        },
        "message": {
            "text": f"<users/123456-app> /link {CODE}",
            "argumentText": f"/link {CODE}",
            "sender": {"name": "users/123456", "type": "HUMAN"},
            "space": {"name": "spaces/AAAA-test"},
        },
    }


def test_decode_accepts_only_matching_human_owner_dm_and_redacts_repr():
    decoded = decode_owner_dm_link(event())
    assert decoded is not None
    assert decoded.link_code == CODE
    assert "A" * 10 not in repr(decoded)
    assert "users/123456" not in repr(decoded)


def test_decode_rejects_group_actor_and_space_substitution():
    cases = []
    group = event()
    group["space"]["spaceType"] = "SPACE"
    cases.append(group)
    actor_swap = event()
    actor_swap["message"]["sender"]["name"] = "users/attacker"
    cases.append(actor_swap)
    space_swap = event()
    space_swap["message"]["space"]["name"] = "spaces/attacker"
    cases.append(space_swap)
    nested_space_conflict = event()
    nested_space_conflict["message"]["space"]["spaceType"] = "SPACE"
    cases.append(nested_space_conflict)
    bot = event()
    bot["user"]["type"] = "BOT"
    cases.append(bot)
    for value in cases:
        assert decode_owner_dm_link(value) is None


def test_decode_supports_legacy_direct_message_type_field():
    value = event()
    value["space"]["type"] = value["space"].pop("spaceType")
    assert decode_owner_dm_link(value) is not None


def test_decode_requires_exact_link_command_without_hidden_or_extra_text():
    for text in (
        f"link {CODE}",
        f"/link  {CODE}",
        f"/link {CODE} extra",
        f"/link {CODE}\n",
    ):
        value = event()
        value["message"]["argumentText"] = text
        assert decode_owner_dm_link(value) is None


def test_decode_prefers_provider_canonical_argument_text_and_supports_exact_fallback():
    value = event()
    value["message"]["text"] = f"hidden prefix /link {CODE} hidden suffix"
    assert decode_owner_dm_link(value) is not None

    value = event()
    del value["message"]["argumentText"]
    value["message"]["text"] = f"/link {CODE}"
    assert decode_owner_dm_link(value) is not None

    value = event()
    value["message"]["argumentText"] = None
    assert decode_owner_dm_link(value) is None

    value = event()
    value["message"]["argumentText"] = ""
    value["message"]["text"] = f"/link {CODE}"
    assert decode_owner_dm_link(value) is not None


def test_decode_accepts_only_one_provider_separator_before_canonical_argument():
    value = event()
    value["message"]["argumentText"] = f" /link {CODE}"
    assert decode_owner_dm_link(value) is not None

    for text in (f"  /link {CODE}", f"\t/link {CODE}", f" /link {CODE} "):
        value = event()
        value["message"]["argumentText"] = text
        assert decode_owner_dm_link(value) is None

    value = event()
    value["message"]["argumentText"] = ""
    value["message"]["text"] = f" /link {CODE}"
    assert decode_owner_dm_link(value) is None


def test_decode_diagnostics_are_bounded_and_content_free():
    value = event()
    value["message"]["argumentText"] = "not a link command"
    decoded, reason = decode_owner_dm_link_diagnostic(value)
    assert decoded is None
    assert reason == "command_body"
    assert "not a link command" not in reason


def test_decode_owner_dm_message_preserves_untrusted_text_and_redacts_repr():
    value = event()
    value["message"]["argumentText"] = "what is on my calendar?"
    decoded, reason = decode_owner_dm_message_diagnostic(value)
    assert reason == "accepted"
    assert decoded is not None
    assert decoded.text == "what is on my calendar?"
    assert decoded.actor_ref == "users/123456"
    assert "calendar" not in repr(decoded)
    assert "users/123456" not in repr(decoded)
