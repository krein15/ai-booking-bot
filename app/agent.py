"""Диалог с клиентом: история, вызовы функций, итоговый ответ."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time as monotonic_time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.business import Business
from app.llm.base import FunctionCall, LLMError, LLMProvider, LLMResult, Message
from app.prompts import build_system_prompt
from app.schedule import date_text, hhmm
from app.storage import Booking, Storage
from app.tools import BookingTools, ToolContext

log = logging.getLogger(__name__)

MAX_TOOL_STEPS = 5
MAX_TIMES_IN_REPLY = 6
HISTORY_LIMIT = 30
MAX_REPLY_CHARS = 1500
LOST_REPLY = "Извините, не получилось разобраться. Давайте уточним: на какую услугу и когда вы хотите записаться?"


@dataclass
class Session:
    id: str
    ip_hash: str
    created: float
    last_seen: float
    tool_ctx: ToolContext
    history: list[Message] = field(default_factory=list)
    user_messages: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """Диалоги в памяти процесса. Записи при этом хранятся в БД и не теряются."""

    def __init__(self, ttl_seconds: int, ip_bookings_limit: int, max_sessions: int = 5000) -> None:
        self.ttl = ttl_seconds
        self.ip_bookings_limit = ip_bookings_limit
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)

    def get_or_create(self, session_id: str, ip_hash: str, channel: str = "web") -> Session:
        now = monotonic_time.monotonic()
        self._cleanup(now)
        session = self._sessions.get(session_id)
        if session is None:
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                self._sessions.pop(oldest.id, None)
            session = Session(
                id=session_id,
                ip_hash=ip_hash,
                created=now,
                last_seen=now,
                tool_ctx=ToolContext(
                    session_id=session_id,
                    ip_hash=ip_hash,
                    ip_bookings_limit=self.ip_bookings_limit,
                    channel=channel,
                ),
            )
            self._sessions[session_id] = session
        session.last_seen = now
        return session

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _cleanup(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if now - s.last_seen > self.ttl and not s.lock.locked()]
        for sid in expired:
            del self._sessions[sid]


@dataclass
class AgentReply:
    text: str
    booking: Booking | None = None


def trim_history(history: list[Message], limit: int = HISTORY_LIMIT) -> list[Message]:
    """Последние сообщения, начиная с реплики клиента (без «оборванных» вызовов функций)."""
    tail = history[-limit:]
    while tail and tail[0].role != "user":
        tail = tail[1:]
    return tail


_MARKDOWN = re.compile(r"(\*\*|__|^#{1,6}\s*|`)", re.MULTILINE)


def clean_reply(text: str) -> str:
    text = _MARKDOWN.sub("", text or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:MAX_REPLY_CHARS]


_VERBS = "записал|оформил|забронировал|передал|подобрал|проверил|посмотрел|уточнил|зафиксировал|понял|поставил|внёс|нашёл"
_IRREGULAR = {"внёс": "внесла", "нашёл": "нашла"}


def _feminine(word: str) -> str:
    lower = word.lower()
    fem = _IRREGULAR.get(lower, lower + "а")
    return fem.capitalize() if word[0].isupper() else fem


def to_feminine(text: str) -> str:
    """Модели иногда говорят о себе в мужском роде, даже если ассистентка — Ева.

    Меняем только глаголы, которые явно про ассистента: «я записал», «записал вас»,
    «Готово, записал». Фразы вроде «мастер проверил» не трогаем.
    """
    flags = re.IGNORECASE
    # «Записал(а)», «рад(а)», «готов(-а)» — модель пишет оба рода сразу
    text = re.sub(r"\b(\w+)\(-?а\)", r"\1а", text)
    text = re.sub(rf"\b(я\s+)({_VERBS})\b", lambda m: m.group(1) + _feminine(m.group(2)), text, flags=flags)
    text = re.sub(
        rf"\b({_VERBS})(?=\s+(?:вас|вам|для\s+вас)\b)", lambda m: _feminine(m.group(1)), text, flags=flags
    )
    text = re.sub(
        rf"(^|[.!?,:;—]\s*)({_VERBS})\b", lambda m: m.group(1) + _feminine(m.group(2)), text, flags=flags | re.M
    )
    return re.sub(r"\b(рад)(?=\s+(?:помочь|вас|видеть)\b)", lambda m: m.group(1) + "а", text, flags=flags)


_TIME_IN_TEXT = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_PROMISE = re.compile(r"\b(посмотрю|проверю|подберу|гляну|минутку|секундочку)\b", re.IGNORECASE)
# Служебная разметка вызова функции, которую модель иногда печатает как текст
_TOOL_MARKUP = re.compile(r"tool_calls|<\s*/?\s*[^<>\s]*invoke\b|parameter\s+name=|\bfunction_call\b", re.IGNORECASE)


def looks_like_markup(text: str) -> bool:
    """Обрывок служебной разметки вместо ответа: теги вызова или ответ, начинающийся с «<»."""
    stripped = text.strip()
    return bool(_TOOL_MARKUP.search(stripped)) or stripped.startswith("<") or (0 < len(stripped) < 3)
_AVAILABILITY = re.compile(r"\b(свободн\w*|есть время|есть окн\w*|окошк\w*)\b", re.IGNORECASE)


def known_times(history: list[Message], business: Business) -> set[str]:
    """Время, которое бот имеет право назвать: из ответов функций и часы работы."""
    allowed = {hhmm(t) for hours in business.working_hours.values() if hours for t in hours}
    for message in history:
        if message.role != "function":
            continue
        try:
            data = json.loads(message.content)
        except ValueError:
            continue
        days = [data, *data.get("days", []), *data.get("alternatives", [])]
        for day in days:
            allowed.update(day.get("free_slots") or [])
        if data.get("ok") and data.get("time"):
            allowed.add(data["time"])
    return allowed


def review_answer(
    answer: str, called: list[str], history: list[Message], business: Business, text_only: bool = False
) -> str | None:
    """Замечание модели, если ответ нельзя отправлять клиенту как есть."""
    if text_only and _PROMISE.search(answer):
        # В этом ответе функции недоступны: «посмотрю время» повиснет без продолжения.
        return (
            "[Проверка перед отправкой] Не обещай посмотреть или проверить позже — клиент будет ждать впустую. "
            "Закончи ответ вопросом: уточни неисправность или спроси, на какой день и время подобрать запись. "
            "Напиши ответ заново."
        )
    occurrences = [f"{int(h):02d}:{m}" for h, m in _TIME_IN_TEXT.findall(answer)]
    mentioned = set(occurrences)
    working_hours = {hhmm(t) for hours in business.working_hours.values() if hours for t in hours}
    # считаем все упоминания: в разные дни время часто совпадает («08:00» в пн и во вт)
    if len([t for t in occurrences if t not in working_hours]) > MAX_TIMES_IN_REPLY:
        return (
            "[Проверка перед отправкой] В ответе слишком много вариантов времени — клиенту трудно выбрать. "
            "Оставь 2–4 варианта, ближайших к его пожеланию (или самых ранних), остальные — только если попросит. "
            "Напиши ответ заново."
        )
    unknown = sorted(mentioned - known_times(history, business))
    if unknown:
        return (
            f"[Проверка перед отправкой] В ответе есть время {', '.join(unknown)}, которого нет в free_slots "
            "из ответов функций. Не предлагай его клиенту. Если клиент просил это время — скажи, что оно занято. "
            "Если свободное время на нужный день ещё не запрашивалось — вызови get_free_slots. Напиши ответ заново."
        )
    if not called and _PROMISE.search(answer):
        return (
            "[Проверка перед отправкой] Ты пообещал посмотреть или проверить, но не вызвал функцию. "
            "Вызови нужную функцию сейчас и ответь клиенту уже с результатом."
        )
    slots_checked = any(m.role == "function" and m.name == "get_free_slots" for m in history)
    if not called and not slots_checked and _AVAILABILITY.search(answer):
        return (
            "[Проверка перед отправкой] Ты говоришь о свободном времени, но расписание ещё не проверено. "
            "Вызови get_free_slots и ответь клиенту по её результату."
        )
    return None


def booking_confirmation(booking: Booking) -> str:
    return (
        f"Готово, вы записаны: {booking.service_name}, {date_text(booking.visit_date)} в {hhmm(booking.start_time)}. "
        f"Номер записи — {booking.id}. Администратор перезвонит для подтверждения."
    )


class BookingAgent:
    def __init__(
        self,
        business: Business,
        llm: LLMProvider,
        tools: BookingTools,
        storage: Storage,
        now_fn: Callable[[], datetime],
    ) -> None:
        self.business = business
        self.llm = llm
        self.tools = tools
        self.storage = storage
        self.now_fn = now_fn

    @staticmethod
    def _answer_now(call: FunctionCall, names_before: list[str], repeated: bool) -> bool:
        """После этого вызова модель должна ответить клиенту текстом, а не звать функции дальше:
        повтор того же запроса или второй запрос расписания за ход — хватит перебирать, клиент ждёт.

        Запрещать функцию ответом-ошибкой нельзя: модель воспринимает это как сбой и
        повторяет вызов по кругу. А отнимать функции, когда модель ещё собирается их
        вызвать (например, сразу после справочника), опасно: она печатает обрывки
        служебной разметки или обещает «посмотрю» и замолкает.
        """
        if repeated:
            return True
        return call.name == "get_free_slots" and "get_free_slots" in names_before

    def _system_message(self) -> Message:
        prompt = build_system_prompt(self.business, self.now_fn())
        prompt += f"\n\nПриветствие уже показано клиенту в чате: «{self.business.greeting}». Не здоровайся повторно."
        return Message("system", prompt)

    async def reply(self, session: Session, text: str) -> AgentReply:
        started_at = len(session.history)
        session.history.append(Message("user", text))
        self.storage.log_message(session.id, "user", text, self.now_fn())
        booking: Booking | None = None
        called: list[str] = []
        reviewed = False
        text_only = False
        markup_retried = False
        # Черновик ответа и замечание к нему: уходят в модель один раз и не сохраняются в историю.
        review_messages: list[Message] = []

        try:
            for step in range(MAX_TOOL_STEPS + 1):
                messages = [self._system_message(), *trim_history(session.history), *review_messages]
                review_messages = []
                force_text = text_only or step == MAX_TOOL_STEPS  # последний шаг — только текст
                # Для ответа «только текст» функции не передаём вовсе: с function_call="none"
                # GigaChat иногда печатает разметку вызова прямо в текст ответа.
                functions = [] if force_text else self.tools.specs()
                result = await self.llm.chat(messages, functions, allow_calls=not force_text)
                if force_text and result.function_call is not None:
                    # Модель всё равно попросила функцию — отвечаем тем, что она написала.
                    result = LLMResult(result.content, None, result.state_id, result.model, result.total_tokens)
                if result.function_call is None and looks_like_markup(result.content or ""):
                    log.warning("session=%s: в ответе разметка вызова функции, переспрашиваю", session.id[:8])
                    result = LLMResult("", None, result.state_id, result.model, result.total_tokens)
                    if not markup_retried:
                        # Пробуем в другом режиме: без функций модель могла хотеть их вызвать,
                        # с функциями — запутаться в них.
                        markup_retried = True
                        text_only = not force_text
                        continue

                if result.function_call is None:
                    answer = clean_reply(result.content) or LOST_REPLY
                    if self.business.assistant_gender == "female":
                        answer = to_feminine(answer)
                    note = (
                        None
                        if reviewed
                        else review_answer(answer, called, session.history, self.business, text_only=force_text)
                    )
                    if note:
                        reviewed = True
                        log.info("session=%s: ответ отправлен на доработку: %s", session.id[:8], answer[:120])
                        review_messages = [Message("assistant", answer), Message("user", note)]
                        continue
                    session.history.append(Message("assistant", answer))
                    self.storage.log_message(session.id, "assistant", answer, self.now_fn())
                    log.info("session=%s model=%s tokens=%s", session.id[:8], result.model, result.total_tokens)
                    return AgentReply(answer, booking)

                call = result.function_call
                signature = f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"
                repeated = signature in called
                names_before = [s.split(":", 1)[0] for s in called]
                called.append(signature)
                session.history.append(
                    Message("assistant", result.content or "", function_call=call, state_id=result.state_id)
                )
                outcome = self.tools.execute(call.name, call.arguments, session.tool_ctx)
                if self._answer_now(call, names_before, repeated):
                    text_only = True
                if outcome.booking is not None:
                    booking = outcome.booking
                if repeated and call.name != "create_booking":
                    # Модель иногда повторяет тот же запрос: подсказываем, что пора отвечать клиенту.
                    outcome.result["note"] = "Это повторный запрос с теми же параметрами, результат тот же. Ответь клиенту."
                payload = json.dumps(outcome.result, ensure_ascii=False)
                session.history.append(Message("function", payload, name=call.name))
                self.storage.log_message(
                    session.id,
                    "function",
                    f"{call.name}({json.dumps(call.arguments, ensure_ascii=False)}) -> {payload}",
                    self.now_fn(),
                )
        except LLMError:
            if booking is None:
                # Откатываем ход, чтобы клиент мог просто повторить сообщение.
                del session.history[started_at:]
                raise
            # Запись уже создана — сообщаем о ней без модели.
            answer = booking_confirmation(booking)
            session.history.append(Message("assistant", answer))
            return AgentReply(answer, booking)

        log.warning("session=%s: модель зациклилась на вызовах функций", session.id[:8])
        answer = booking_confirmation(booking) if booking else LOST_REPLY
        session.history.append(Message("assistant", answer))
        return AgentReply(answer, booking)
