"""Общий интерфейс языковой модели.

Бот работает с этими типами, а не с SDK конкретного провайдера,
поэтому подключить другую модель (например, DeepSeek) — значит
написать ещё один класс с методом chat().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "function"]


@dataclass
class FunctionCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    role: Role
    content: str = ""
    function_call: FunctionCall | None = None
    name: str | None = None  # имя функции для role="function"
    state_id: str | None = None  # служебный id вызова функции у провайдера


@dataclass
class FunctionSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema объекта аргументов
    few_shot_examples: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LLMResult:
    content: str
    function_call: FunctionCall | None = None
    state_id: str | None = None
    model: str = ""
    total_tokens: int | None = None


class LLMError(Exception):
    """Модель не ответила."""


class LLMBusyError(LLMError):
    """Модель занята или упёрлась в лимит — стоит повторить чуть позже."""


class LLMUnavailableError(LLMError):
    """Модель недоступна: сеть, ключ, закончились токены."""


class LLMProvider(Protocol):
    name: str

    async def chat(
        self, messages: list[Message], functions: list[FunctionSpec], allow_calls: bool = True
    ) -> LLMResult:
        """allow_calls=False — функции видны, но модель должна ответить текстом."""
        ...

    async def aclose(self) -> None: ...
