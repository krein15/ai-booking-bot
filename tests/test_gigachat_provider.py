from __future__ import annotations

import asyncio

import pytest
from gigachat.exceptions import AuthenticationError, RateLimitError, ResponseError
from gigachat.models import ChatCompletion

from app.llm.base import FunctionCall, FunctionSpec, LLMBusyError, LLMUnavailableError, Message
from app.llm.gigachat_provider import GigaChatProvider

URL = "https://api.giga.chat/v1/chat/completions"


def completion(model: str, *, content: str = "", function_call: dict | None = None) -> ChatCompletion:
    message = {"role": "assistant", "content": content}
    if function_call:
        message["function_call"] = function_call
        message["functions_state_id"] = "state-42"
    return ChatCompletion.model_validate(
        {
            "choices": [{"message": message, "index": 0, "finish_reason": "function_call" if function_call else "stop"}],
            "created": 1,
            "model": model,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "object": "chat.completion",
        }
    )


class FakeClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.chats = []
        self.closed = False

    async def achat(self, chat):
        self.chats.append(chat)
        return await self.behaviour(chat)

    async def aclose(self):
        self.closed = True


SPEC = FunctionSpec(
    name="get_free_slots",
    description="Свободное время",
    parameters={
        "type": "object",
        "properties": {"service_id": {"type": "string", "enum": ["oil"], "description": "id"}},
        "required": ["service_id"],
    },
    few_shot_examples=[{"request": "Масло завтра", "params": {"service_id": "oil"}}],
)


async def test_request_mapping_and_function_call_parsing():
    async def answer(chat):
        return completion(chat.model, function_call={"name": "get_free_slots", "arguments": {"service_id": "oil"}})

    client = FakeClient(answer)
    provider = GigaChatProvider(models=["GigaChat-2-Max"], client=client)
    messages = [
        Message("system", "sys"),
        Message("user", "Масло завтра"),
        Message("assistant", "", function_call=FunctionCall("get_free_slots", {"service_id": "oil"}), state_id="s-1"),
        Message("function", '{"free_slots": []}', name="get_free_slots"),
    ]
    result = await provider.chat(messages, [SPEC])

    assert result.function_call == FunctionCall("get_free_slots", {"service_id": "oil"})
    assert result.state_id == "state-42"
    assert result.total_tokens == 15

    payload = client.chats[0].model_dump(exclude_none=True, by_alias=True)
    assert payload["model"] == "GigaChat-2-Max"
    assert payload["function_call"] == "auto"
    assert [m["role"] for m in payload["messages"]] == ["system", "user", "assistant", "function"]
    assert payload["messages"][2]["function_call"] == {"name": "get_free_slots", "arguments": {"service_id": "oil"}}
    assert payload["messages"][2]["functions_state_id"] == "s-1"
    assert payload["messages"][3]["name"] == "get_free_slots"
    fn = payload["functions"][0]
    assert fn["parameters"]["type"] == "object"
    assert fn["parameters"]["properties"]["service_id"]["enum"] == ["oil"]
    assert fn["few_shot_examples"][0]["params"] == {"service_id": "oil"}


async def test_text_only_mode_sends_function_call_none():
    async def answer(chat):
        return completion(chat.model, content="ok")

    client = FakeClient(answer)
    provider = GigaChatProvider(models=["GigaChat-2-Max"], client=client)
    await provider.chat([Message("user", "hi")], [SPEC], allow_calls=False)
    payload = client.chats[0].model_dump(exclude_none=True, by_alias=True)
    assert payload["function_call"] == "none" and payload["functions"]


async def test_falls_back_when_model_runs_out_of_tokens():
    async def answer(chat):
        if chat.model == "GigaChat-2-Max":
            raise ResponseError(URL, 402, b'{"message":"Payment Required"}', None)
        return completion(chat.model, content="ok")

    client = FakeClient(answer)
    provider = GigaChatProvider(models=["GigaChat-2-Max", "GigaChat-2-Pro"], client=client)
    result = await provider.chat([Message("user", "hi")], [])
    assert result.content == "ok" and result.model == "GigaChat-2-Pro"
    assert provider.skipped == {"GigaChat-2-Max": 402}

    await provider.chat([Message("user", "hi again")], [])
    assert [c.model for c in client.chats] == ["GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2-Pro"]
    assert client.chats[0].functions is None and client.chats[0].function_call is None


async def test_all_models_exhausted():
    async def answer(chat):
        raise ResponseError(URL, 402, b"", None)

    provider = GigaChatProvider(models=["A", "B"], client=FakeClient(answer))
    with pytest.raises(LLMUnavailableError):
        await provider.chat([Message("user", "hi")], [])


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RateLimitError(URL, 429, b"", None), LLMBusyError),
        (AuthenticationError(URL, 401, b"", None), LLMUnavailableError),
        (ResponseError(URL, 500, b"", None), LLMUnavailableError),
    ],
)
async def test_errors_are_mapped(error, expected):
    async def answer(chat):
        raise error

    provider = GigaChatProvider(models=["GigaChat-2-Max"], client=FakeClient(answer))
    with pytest.raises(expected):
        await provider.chat([Message("user", "hi")], [])


async def test_single_stream_queue_times_out():
    release = asyncio.Event()

    async def answer(chat):
        await release.wait()
        return completion(chat.model, content="ok")

    provider = GigaChatProvider(models=["GigaChat-2-Max"], client=FakeClient(answer), queue_timeout=0.05)
    first = asyncio.create_task(provider.chat([Message("user", "1")], []))
    await asyncio.sleep(0)
    with pytest.raises(LLMBusyError):
        await provider.chat([Message("user", "2")], [])
    release.set()
    assert (await first).content == "ok"
    # после освобождения очередь снова принимает запросы
    assert (await provider.chat([Message("user", "3")], [])).content == "ok"
