from types import SimpleNamespace

import pytest

from attune.hosted.model_gateway import (
    MAX_EMBED_CHARS,
    MAX_EMBED_DIMENSIONS,
    MAX_MESSAGE_CHARS,
    MAX_RESPONSE_CHARS,
    HostedModelGateway,
    TokenUsage,
    validate_embed_input,
    validate_messages,
)


class Completions:
    def __init__(self, text="answer", usage=None):
        self.text = text
        self.usage = usage
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))],
            usage=self.usage,
        )


class Embeddings:
    def __init__(self, vector=(0.1, 0.2, 0.3), usage=None):
        self.vector = vector
        self.usage = usage
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=list(self.vector))], usage=self.usage,
        )


def gateway(text="answer", vector=(0.1, 0.2, 0.3), *, profiles=None, usage=None):
    completions = Completions(text, usage)
    embeddings = Embeddings(vector, usage)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        embeddings=embeddings,
    )
    return HostedModelGateway(
        client,
        models={
            "classify": "small-model",
            "converse": "large-model",
            "embed": "embed-model",
        },
        profiles=profiles,
    ), completions, embeddings


def messages(content="hello"):
    return [
        {"role": "system", "content": "fixed boundary"},
        {"role": "user", "content": content},
    ]


def test_gateway_selects_only_fixed_task_route_and_budget():
    instance, completions, _ = gateway()
    assert instance.complete(task="classify", messages=messages()).text == "answer"
    assert completions.calls == [
        {
            "model": "small-model",
            "messages": messages(),
            "max_tokens": 256,
        }
    ]

    instance.complete(task="converse", messages=messages())
    assert completions.calls[-1]["model"] == "large-model"
    assert completions.calls[-1]["max_tokens"] == 1_200


def test_gateway_embed_task_is_bounded_and_uses_fixed_model():
    instance, _, embeddings = gateway(vector=(0.5, -0.25, 0.75))
    result = instance.embed(text="hello world")
    assert result.vector == (0.5, -0.25, 0.75)
    assert embeddings.calls == [{"model": "embed-model", "input": "hello world"}]

    with pytest.raises(ValueError):
        validate_embed_input("")
    with pytest.raises(ValueError):
        validate_embed_input("x" * (MAX_EMBED_CHARS + 1))
    with pytest.raises(ValueError):
        validate_embed_input(123)


def test_gateway_embed_rejects_invalid_provider_response():
    instance, _, _ = gateway(vector=())
    with pytest.raises(RuntimeError, match="contract"):
        instance.embed(text="hello")

    instance, _, _ = gateway(vector=tuple(0.1 for _ in range(MAX_EMBED_DIMENSIONS + 1)))
    with pytest.raises(RuntimeError, match="contract"):
        instance.embed(text="hello")

    instance, _, _ = gateway(vector=(float("nan"), 0.1))
    with pytest.raises(RuntimeError, match="contract"):
        instance.embed(text="hello")


def test_complete_still_rejects_embed_task_unchanged():
    instance, _, _ = gateway()
    with pytest.raises(ValueError):
        instance.complete(task="embed", messages=messages())


@pytest.mark.parametrize(
    "task,value",
    [
        ("other", messages()),
        ([], messages()),
        ("converse", []),
        ("converse", [{"role": "user", "content": "no boundary"}]),
        ("converse", [{"role": [], "content": "bad"}]),
        ("converse", [{"role": "system", "content": ""}]),
        ("converse", [{"role": "system", "content": "ok", "model": "x"}]),
        ("converse", messages("x" * (MAX_MESSAGE_CHARS + 1))),
    ],
)
def test_gateway_rejects_caller_authority_and_invalid_budgets(task, value):
    with pytest.raises(ValueError):
        validate_messages(task=task, messages=value)


def test_gateway_rejects_invalid_configuration_and_provider_response():
    instance, _, _ = gateway("")
    with pytest.raises(RuntimeError, match="contract"):
        instance.complete(task="converse", messages=messages())

    instance, _, _ = gateway("x" * (MAX_RESPONSE_CHARS + 1))
    with pytest.raises(RuntimeError, match="contract"):
        instance.complete(task="converse", messages=messages())

    with pytest.raises(ValueError, match="routes"):
        HostedModelGateway(SimpleNamespace(), models={"converse": "model"})
    with pytest.raises(ValueError, match="route"):
        HostedModelGateway(
            SimpleNamespace(),
            models={
                "classify": "model?caller=authority",
                "converse": "model",
                "embed": "model",
            },
        )
    with pytest.raises(ValueError, match="routes"):
        HostedModelGateway(
            SimpleNamespace(),
            models={"classify": "model", "converse": "model"},
        )


# -- Per-tenant model profiles (docs/future-state.md Phase 6 "hosted
# operations") ---------------------------------------------------------------


def test_gate_off_profiles_none_resolves_identically_regardless_of_profile_arg():
    """Pin: with ``profiles=None`` (the gate-off construction), passing
    ``profile=None`` or ``profile="standard"`` selects the exact same fixed
    model route -- byte-identical to the pre-profile behavior."""
    instance, completions, _ = gateway()
    instance.complete(task="classify", messages=messages())
    instance.complete(task="classify", messages=messages(), profile="standard")
    assert completions.calls[0]["model"] == completions.calls[1]["model"] == "small-model"


def test_gate_off_unknown_profile_fails_closed_not_a_silent_default():
    instance, completions, _ = gateway()
    with pytest.raises(ValueError, match="unknown"):
        instance.complete(task="classify", messages=messages(), profile="premium")
    assert completions.calls == []


def test_gate_on_profile_map_resolves_the_matching_task_route():
    profiles = {
        "standard": {
            "classify": "small-model", "converse": "large-model", "embed": "embed-model",
        },
        "premium": {
            "classify": "small-premium", "converse": "large-premium", "embed": "embed-premium",
        },
    }
    instance, completions, embeddings = gateway(profiles=profiles)
    instance.complete(task="classify", messages=messages(), profile="premium")
    assert completions.calls[-1]["model"] == "small-premium"
    instance.complete(task="classify", messages=messages())
    assert completions.calls[-1]["model"] == "small-model"
    instance.embed(text="hi", profile="premium")
    assert embeddings.calls[-1]["model"] == "embed-premium"


def test_gate_on_unknown_profile_still_fails_closed():
    profiles = {
        "standard": {
            "classify": "small-model", "converse": "large-model", "embed": "embed-model",
        },
        "premium": {
            "classify": "small-premium", "converse": "large-premium", "embed": "embed-premium",
        },
    }
    instance, completions, _ = gateway(profiles=profiles)
    with pytest.raises(ValueError, match="unknown"):
        instance.complete(task="classify", messages=messages(), profile="enterprise")
    assert completions.calls == []


def test_gate_on_rejects_a_malformed_profile_string():
    profiles = {
        "standard": {
            "classify": "small-model", "converse": "large-model", "embed": "embed-model",
        },
        "premium": {
            "classify": "small-premium", "converse": "large-premium", "embed": "embed-premium",
        },
    }
    instance, _, _ = gateway(profiles=profiles)
    with pytest.raises(ValueError):
        instance.complete(task="classify", messages=messages(), profile="Not Valid!")


def test_profiles_construction_requires_the_standard_profile_to_match_fixed_routes():
    with pytest.raises(ValueError, match="standard"):
        HostedModelGateway(
            SimpleNamespace(),
            models={"classify": "small-model", "converse": "large-model", "embed": "embed-model"},
            profiles={
                "standard": {
                    "classify": "different", "converse": "large-model", "embed": "embed-model",
                },
            },
        )
    with pytest.raises(ValueError, match="standard"):
        HostedModelGateway(
            SimpleNamespace(),
            models={"classify": "small-model", "converse": "large-model", "embed": "embed-model"},
            profiles={
                "premium": {
                    "classify": "a", "converse": "b", "embed": "c",
                },
            },
        )


def test_profiles_construction_rejects_an_out_of_vocabulary_profile_name():
    with pytest.raises(ValueError, match="name"):
        HostedModelGateway(
            SimpleNamespace(),
            models={"classify": "small-model", "converse": "large-model", "embed": "embed-model"},
            profiles={
                "standard": {
                    "classify": "small-model", "converse": "large-model", "embed": "embed-model",
                },
                "enterprise": {"classify": "a", "converse": "b", "embed": "c"},
            },
        )


# -- Content-free provider usage extraction (feeds Phase 6 metering) --------


def test_usage_is_extracted_when_the_provider_reports_it():
    instance, _, _ = gateway(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    result = instance.complete(task="converse", messages=messages())
    assert result.usage == TokenUsage(10, 5)


def test_embed_usage_defaults_output_tokens_to_zero():
    instance, _, _ = gateway(usage=SimpleNamespace(prompt_tokens=7))
    result = instance.embed(text="hello")
    assert result.usage == TokenUsage(7, 0)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(prompt_tokens="not-an-int"),
        SimpleNamespace(prompt_tokens=True, completion_tokens=1),
        SimpleNamespace(prompt_tokens=-1, completion_tokens=1),
    ],
)
def test_usage_extraction_never_raises_and_degrades_to_none(usage):
    """A malformed or missing usage field must never break the model call
    it rode in on -- the actual text/vector contract is unaffected."""
    instance, _, _ = gateway(usage=usage)
    result = instance.complete(task="converse", messages=messages())
    assert result.text == "answer"
    assert result.usage is None
