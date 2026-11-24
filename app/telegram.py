"""Telegram-бот: тот же администратор, что и на сайте.

Работает через long polling (getUpdates), поэтому не нужен ни домен, ни
вебхук: достаточно токена от @BotFather. Запускается вместе с сервером,
если задан TELEGRAM_BOT_TOKEN.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import httpx

from app.business import Business
from app.chat_service import ChatRejected, ChatService
from app.schedule import date_text, hhmm
from app.storage import Booking

log = logging.getLogger(__name__)

COMMANDS = [
    {"command": "new", "description": "Начать диалог заново"},
    {"command": "start", "description": "Приветствие"},
]


class TelegramError(Exception):
    pass


def format_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def booking_card(booking: Booking) -> str:
    lines = [f"✅ Запись №{booking.id} создана", booking.service_name]
    if booking.parts_choice:
        lines.append(f"Запчасти: {booking.parts_choice}")
    lines.append(f"{date_text(booking.visit_date)}, {hhmm(booking.start_time)}")
    lines.append(f"Ориентир: от {format_price(booking.estimate_from or booking.price_from)}")
    return "\n".join(lines)


class TelegramBot:
    def __init__(
        self,
        *,
        token: str,
        chat: ChatService,
        business: Business,
        client_key: Callable[[int], str],
        sheet_url: str | None = None,
        api_base: str = "https://api.telegram.org",
        poll_timeout: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.chat = chat
        self.business = business
        self.client_key = client_key
        self.sheet_url = sheet_url
        self.poll_timeout = poll_timeout
        self.username: str | None = None
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=f"{api_base}/bot{token}/",
            timeout=httpx.Timeout(poll_timeout + 15, connect=10),
            transport=transport,
        )
        self._offset = 0
        self._generation: dict[int, int] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def url(self) -> str | None:
        return f"https://t.me/{self.username}" if self.username else None

    async def call(self, method: str, **params: Any) -> Any:
        response = await self._http.post(method, json=params)
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', response.status_code)}")
        return data["result"]

    async def setup(self) -> None:
        me = await self.call("getMe")
        self.username = me.get("username")
        await self.call("deleteWebhook", drop_pending_updates=False)
        await self.call("setMyCommands", commands=COMMANDS)
        log.info("Telegram-бот запущен: @%s", self.username)

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                if self.username is None:
                    await self.setup()
                updates = await self.call(
                    "getUpdates", offset=self._offset, timeout=self.poll_timeout, allowed_updates=["message"]
                )
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — сеть, неверный токен, ответ Telegram
                reason = str(exc).replace(self._token, "***")  # токен не должен попасть в лог
                log.warning("Telegram недоступен (%s: %s), повтор через %.0f с", type(exc).__name__, reason, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for update in updates:
                self._offset = update["update_id"] + 1
                task = asyncio.create_task(self.handle_update(update))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._http.aclose()

    # --- обработка сообщений -------------------------------------------------

    def session_id(self, chat_id: int) -> str:
        return f"tg-{chat_id}-{self._generation.get(chat_id, 0)}"

    def _keyboard(self) -> dict[str, Any]:
        return {
            "keyboard": [[{"text": q}] for q in self.business.quick_replies],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }

    async def send(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_markup:
            params["reply_markup"] = reply_markup
        await self.call("sendMessage", **params)

    async def _typing(self, chat_id: int) -> None:
        """Повторяет «печатает…», пока модель думает (Telegram сбрасывает статус через 5 с)."""
        with contextlib.suppress(Exception):
            while True:
                await asyncio.sleep(4.5)
                await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def handle_update(self, update: dict[str, Any]) -> None:
        try:
            await self._handle(update)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram: не удалось обработать сообщение")

    async def _handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return  # в группах демо не работает
        chat_id = chat["id"]
        user_id = (message.get("from") or {}).get("id", chat_id)
        text = (message.get("text") or "").strip()

        if not text:
            await self.send(chat_id, "Я понимаю только текст. Напишите, что случилось с машиной.")
            return

        command = text.split()[0].split("@")[0].lower()
        if command in ("/start", "/new"):
            self.chat.reset(self.session_id(chat_id))
            self._generation[chat_id] = self._generation.get(chat_id, 0) + 1
            greeting = self.business.greeting
            if self.business.demo_note:
                greeting += f"\n\n{self.business.demo_note}"
            await self.send(chat_id, greeting, reply_markup=self._keyboard())
            return
        if command.startswith("/"):
            await self.send(chat_id, "Напишите вопрос обычным сообщением. /new — начать диалог заново.")
            return

        with contextlib.suppress(Exception):
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            outcome = await self.chat.handle(
                session_id=self.session_id(chat_id),
                client_key=self.client_key(user_id),
                text=text,
                channel="telegram",
                wait_if_busy=True,
            )
        except ChatRejected as exc:
            await self.send(chat_id, exc.message)
            return
        finally:
            typing.cancel()

        await self.send(chat_id, outcome.reply, reply_markup={"remove_keyboard": True})
        if outcome.booking is not None:
            markup = None
            if self.sheet_url:
                markup = {"inline_keyboard": [[{"text": "Посмотреть в Google Таблице", "url": self.sheet_url}]]}
            await self.send(chat_id, booking_card(outcome.booking), reply_markup=markup)
