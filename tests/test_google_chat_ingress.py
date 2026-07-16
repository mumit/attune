from attune.hosted.google_chat_ingress import decode_owner_dm_link

CODE = "A" * 43


def event():
    return {
        "type": "MESSAGE",
        "user": {"name": "users/123456", "type": "HUMAN"},
        "space": {"name": "spaces/AAAA-test", "type": "DIRECT_MESSAGE"},
        "message": {
            "text": f"/link {CODE}",
            "sender": {"name": "users/123456", "type": "HUMAN"},
            "space": {"name": "spaces/AAAA-test", "type": "DIRECT_MESSAGE"},
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
    group["space"]["type"] = "SPACE"
    cases.append(group)
    actor_swap = event()
    actor_swap["message"]["sender"]["name"] = "users/attacker"
    cases.append(actor_swap)
    space_swap = event()
    space_swap["message"]["space"]["name"] = "spaces/attacker"
    cases.append(space_swap)
    bot = event()
    bot["user"]["type"] = "BOT"
    cases.append(bot)
    for value in cases:
        assert decode_owner_dm_link(value) is None


def test_decode_requires_exact_link_command_without_hidden_or_extra_text():
    for text in (
        f"link {CODE}",
        f"/link  {CODE}",
        f"/link {CODE} extra",
        f" /link {CODE}",
        f"/link {CODE}\n",
    ):
        value = event()
        value["message"]["text"] = text
        assert decode_owner_dm_link(value) is None
