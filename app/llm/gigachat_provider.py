"""GigaChat через официальный SDK `gigachat`.

Особенности бесплатного тарифа, которые здесь учтены:
- генерация в один поток — запросы встают в очередь (семафор);
- у каждой модели свой пакет токенов — когда пакет кончается (HTTP 402),
  бот переходит к следующей модели из списка.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from gigachat import GigaChat
from gigachat.exceptions import AuthenticationError, GigaChatException, RateLimitError, ResponseError
from gigachat.models import Chat, ChatCompletion, Function, Messages
from gigachat.models import FunctionCall as GigaFunctionCall

from app.llm.base import (
    FunctionCall,
    FunctionSpec,
    LLMBusyError,
    LLMResult,
    LLMUnavailableError,
    Message,
)

log = logging.getLogger(__name__)

# 402 — у модели закончился пакет токенов, 404 — модель недоступна для ключа.
SKIP_MODEL_STATUSES = {402, 404}


def to_giga_message(message: Message) -> Messages:
    data: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.function_call is not None:
        data["function_call"] = GigaFunctionCall(
            name=message.function_call.name, arguments=dict(message.function_call.arguments)
        )
    if message.name:
        data["name"] = message.name
    if message.state_id:
        data["functions_state_id"] = message.state_id
    return Messages(**data)


def to_giga_function(spec: FunctionSpec) -> Function:
    data: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }
    if spec.few_shot_examples:
        data["few_shot_examples"] = spec.few_shot_examples
    return Function.model_validate(data)


def from_completion(completion: ChatCompletion) -> LLMResult:
    message = completion.choices[0].message
    call = None
    if message.function_call is not None:
        call = FunctionCall(name=message.function_call.name, arguments=dict(message.function_call.arguments or {}))
    return LLMResult(
        content=message.content or "",
        function_call=call,
        state_id=message.functions_state_id,
        model=completion.model,
        total_tokens=completion.usage.total_tokens if completion.usage else None,
    )


class GigaChatProvider:
    name = "gigachat"

    def __init__(
        self,
        *,
        models: list[str],
        credentials: str | None = None,
        scope: str = "GIGACHAT_API_PERS",
        ca_bundle_file: str | None = None,
        verify_ssl: bool = True,
        timeout: float = 60.0,
        max_concurrency: int = 1,
        queue_timeout: float = 30.0,
        temperature: float | None = None,
        client: Any = None,
    ) -> None:
        if not models:
            raise ValueError("Нужна хотя бы одна модель GigaChat")
        self.models = list(models)
        self.skipped: dict[str, int] = {}
        self._client = client or GigaChat(
            credentials=credentials,
            scope=scope,
            ca_bundle_file=ca_bundle_file,
            verify_ssl_certs=verify_ssl,
            timeout=timeout,
            max_retries=2,
            retry_backoff_factor=1.0,
        )
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._queue_timeout = queue_timeout
        self._temperature = temperature

    @property
    def active_models(self) -> list[str]:
        return [m for m in self.models if m not in self.skipped]

    async def chat(
        self, messages: list[Message], functions: list[FunctionSpec], allow_calls: bool = True
    ) -> LLMResult:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except TimeoutError as exc:
            raise LLMBusyError("Слишком много одновременных диалогов") from exc
        try:
            return await self._chat_with_fallback(messages, functions, allow_calls)
        finally:
            self._semaphore.release()

    async def _chat_with_fallback(
        self, messages: list[Message], functions: list[FunctionSpec], allow_calls: bool
    ) -> LLMResult:
        giga_messages = [to_giga_message(m) for m in messages]
        giga_functions = [to_giga_function(f) for f in functions] or None
        last_error: Exception | None = None

        for model in self.active_models:
            chat = Chat(
                model=model,
                messages=giga_messages,
                functions=giga_functions,
                function_call=("auto" if allow_calls else "none") if giga_functions else None,
                temperature=self._temperature,
            )
            try:
                completion = await self._client.achat(chat)
            except RateLimitError as exc:
                raise LLMBusyError("GigaChat просит подождать (429)") from exc
            except AuthenticationError as exc:
                raise LLMUnavailableError("GigaChat не принял ключ авторизации (401)") from exc
            except ResponseError as exc:
                if exc.status_code in SKIP_MODEL_STATUSES:
                    log.warning("Модель %s недоступна (HTTP %s), пробую следующую", model, exc.status_code)
                    self.skipped[model] = exc.status_code
                    last_error = exc
                    continue
                raise LLMUnavailableError(f"GigaChat ответил ошибкой {exc.status_code}: {exc}") from exc
            except (httpx.HTTPError, GigaChatException) as exc:
                raise LLMUnavailableError(f"GigaChat недоступен: {exc}") from exc
            return from_completion(completion)

        raise LLMUnavailableError("Ни одна модель GigaChat не доступна") from last_error

    async def aclose(self) -> None:
        await self._client.aclose()
