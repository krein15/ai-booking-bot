"""Заглушки вместо настоящей модели.

- ScriptedLLM — для тестов: отдаёт заранее заданные ответы.
- OfflineDemoLLM — для локальной проверки сайта и Google-таблицы без ключа
  GigaChat (LLM_PROVIDER=fake). Понимает пару ключевых слов, не более.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from app.llm.base import FunctionCall, FunctionSpec, LLMResult, Message

Step = LLMResult | Callable[[list[Message], list[FunctionSpec]], LLMResult]


class ScriptedLLM:
    name = "scripted"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[list[Message]] = []
        self.allow_calls: list[bool] = []
        self.function_counts: list[int] = []

    async def chat(
        self, messages: list[Message], functions: list[FunctionSpec], allow_calls: bool = True
    ) -> LLMResult:
        self.calls.append(list(messages))
        self.allow_calls.append(allow_calls)
        self.function_counts.append(len(functions))
        if not self.steps:
            raise AssertionError("ScriptedLLM: ответы закончились")
        step = self.steps.pop(0)
        return step(messages, functions) if callable(step) else step

    async def aclose(self) -> None:
        return None


_PHONE = re.compile(r"(?:\+7|8)?[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
PREFIX = "[режим без модели] "


class OfflineDemoLLM:
    name = "offline-demo"

    async def chat(
        self, messages: list[Message], functions: list[FunctionSpec], allow_calls: bool = True
    ) -> LLMResult:
        last = messages[-1]
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
        specs = {f.name: f for f in functions}

        if last.role == "function" and last.name == "get_free_slots":
            data = json.loads(last.content)
            day = (data.get("days") or [data])[0]
            slots = day.get("free_slots") or []
            phone = _PHONE.search(user_text)
            if phone and slots and "create_booking" in specs:
                return LLMResult("", FunctionCall("create_booking", self._booking_args(specs, day, slots[0], phone.group(0))))
            if not slots:
                return LLMResult(PREFIX + "Свободного времени не нашлось.")
            return LLMResult(
                PREFIX + f"Свободно {day.get('date_text')}: {', '.join(slots)}.\n"
                "Чтобы проверить запись в таблицу, напишите «запиши» и номер телефона."
            )

        if last.role == "function" and last.name == "create_booking":
            data = json.loads(last.content)
            if data.get("ok"):
                return LLMResult(
                    PREFIX + f"Готово, запись №{data['booking_number']}: {data['service']}, "
                    f"{data['date_text']} в {data['time']}."
                )
            return LLMResult(PREFIX + f"Не получилось: {data.get('error')}")

        text = user_text.lower()
        if any(word in text for word in ("свобод", "время", "запиш", "записа", "когда")) and "get_free_slots" in specs:
            service_id = specs["get_free_slots"].parameters["properties"]["service_id"]["enum"][0]
            return LLMResult("", FunctionCall("get_free_slots", {"service_id": service_id}))
        return LLMResult(
            PREFIX + "Это заглушка для проверки виджета и таблицы без GigaChat. "
            "Напишите «когда свободно?» или «запиши меня, телефон 8 900 123-45-67»."
        )

    @staticmethod
    def _booking_args(specs: dict[str, FunctionSpec], day: dict[str, Any], slot: str, phone: str) -> dict[str, Any]:
        params = specs["create_booking"].parameters
        args: dict[str, Any] = {
            "service_id": params["properties"]["service_id"]["enum"][0],
            "date": day["date"],
            "time": slot,
            "client_name": "Тест Тестов",
            "phone": phone,
            "summary": "Проверка записи без модели",
        }
        if "parts_option" in params["properties"]:
            args["parts_option"] = "master"
        for field_name in params.get("required", []):
            args.setdefault(field_name, "Тест")
        return args

    async def aclose(self) -> None:
        return None
