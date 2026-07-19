from types import SimpleNamespace

import pytest

from attune.hosted.model_gateway import (
    MAX_EMBED_CHARS,
    MAX_EMBED_DIMENSIONS,
    MAX_MESSAGE_CHARS,
    MAX_RESPONSE_CHARS,
    HostedModelGateway,
    validate_embed_input,
    validate_messages,
)


class Completions:
    def __init__(self, text="answer"):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))]
        )


class Embeddings:
    def __init__(self, vector=(0.1, 0.2, 0.3)):
        self.vector = vector
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=list(self.vector))]
        )


def gateway(text="answer", vector=(0.1, 0.2, 0.3)):
    completions = Completions(text)
    embeddings = Embeddings(vector)
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
