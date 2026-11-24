"""Обработка сообщения клиента — общая для сайта и Telegram.

Здесь лимиты от злоупотреблений, очередь внутри диалога и вызов агента.
Каналы отличаются только тем, как получают сообщение и показывают ответ.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.agent import BookingAgent, SessionStore
from app.llm.base import LLMBusyError, LLMUnavailableError
from app.ratelimit import SlidingWindowLimiter
from app.settings import Settings
from app.storage import Booking

log = logging.getLogger(__name__)

DAY = 24 * 60 * 60
RESTART_HINT = {
    "web": "Начните новый кнопкой ↻ вверху чата.",
    "telegram": "Начните новый командой /new.",
}


class ChatRejected(Exception):
    """Сообщение не принято: текст ошибки показывается клиенту."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class ChatOutcome:
    reply: str
    booking: Booking | None
    remaining_messages: int


class ChatService:
    def __init__(
        self,
        settings: Settings,
        agent: BookingAgent,
        sessions: SessionStore,
        limiter: SlidingWindowLimiter,
        on_booking: Callable[[Booking], None] | None = None,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.sessions = sessions
        self.limiter = limiter
        self.on_booking = on_booking

    def reset(self, session_id: str) -> None:
        self.sessions.drop(session_id)

    async def handle(
        self,
        *,
        session_id: str,
        client_key: str,
        text: str,
        channel: str = "web",
        wait_if_busy: bool = False,
    ) -> ChatOutcome:
        s = self.settings
        text = text.strip()
        if not text:
            raise ChatRejected(400, "Пустое сообщение.")
        if len(text) > s.max_message_chars:
            raise ChatRejected(400, f"Сообщение слишком длинное: максимум {s.max_message_chars} символов.")

        if not self.limiter.hit(f"minute:{client_key}", s.ip_messages_per_minute, 60):
            raise ChatRejected(429, "Слишком много сообщений подряд. Подождите минуту.")
        if not self.limiter.hit(f"day:{client_key}", s.ip_messages_per_day, DAY):
            raise ChatRejected(429, "Дневной лимит сообщений в демо исчерпан. Возвращайтесь завтра.")
        if session_id not in self.sessions and not self.limiter.hit(
            f"sessions:{client_key}", s.ip_new_sessions_per_day, DAY
        ):
            raise ChatRejected(
                429, "Слишком много новых диалогов за день. Продолжите текущий или возвращайтесь завтра."
            )

        session = self.sessions.get_or_create(session_id, client_key, channel=channel)
        if session.user_messages >= s.max_user_messages_per_session:
            raise ChatRejected(429, f"Диалог получился длинным. {RESTART_HINT.get(channel, '')}".strip())
        if session.lock.locked() and not wait_if_busy:
            raise ChatRejected(409, "Подождите ответа на предыдущее сообщение.")
        if not self.limiter.hit("turns:global", s.daily_llm_turns_limit, DAY):
            raise ChatRejected(503, "Демо на сегодня исчерпало лимит. Возвращайтесь завтра.")

        async with session.lock:
            session.user_messages += 1
            try:
                result = await self.agent.reply(session, text)
            except LLMBusyError:
                session.user_messages -= 1
                raise ChatRejected(
                    503, "Сейчас много диалогов одновременно. Повторите сообщение через минуту."
                ) from None
            except LLMUnavailableError:
                session.user_messages -= 1
                log.exception("Модель недоступна")
                raise ChatRejected(503, "Модель временно недоступна. Попробуйте чуть позже.") from None

        if result.booking is not None and self.on_booking:
            self.on_booking(result.booking)
        return ChatOutcome(
            reply=result.text,
            booking=result.booking,
            remaining_messages=s.max_user_messages_per_session - session.user_messages,
        )
