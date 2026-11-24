from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.agent import BookingAgent, SessionStore
from app.chat_service import ChatService
from app.llm.base import FunctionCall, LLMResult, LLMUnavailableError
from app.llm.fake import ScriptedLLM
from app.ratelimit import SlidingWindowLimiter
from app.telegram import TelegramBot
from tests.conftest import booking_args


class FakeTelegram:
    """Подменяет api.telegram.org: запоминает вызовы и отвечает ok."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        result = {"username": "demo_motor_bot"} if method == "getMe" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self) -> list[dict]:
        return [body for method, body in self.calls if method == "sendMessage"]


def message(text: str | None, chat_id: int = 42, chat_type: str = "private") -> dict:
    msg = {"chat": {"id": chat_id, "type": chat_type}, "from": {"id": 7}}
    if text is not None:
        msg["text"] = text
    return {"update_id": 1, "message": msg}


@pytest.fixture
def make_bot(business, tools, storage, now_fn, make_settings):
    def _make(steps):
        llm = ScriptedLLM(steps)
        agent = BookingAgent(business, llm, tools, storage, now_fn)
        chat = ChatService(make_settings(), agent, SessionStore(3600, 5), SlidingWindowLimiter())
        fake = FakeTelegram()
        bot = TelegramBot(
            token="123:ABC",
            chat=chat,
            business=business,
            client_key=lambda user_id: f"tg{user_id}",
            sheet_url="https://docs.google.com/spreadsheets/d/x/edit",
            transport=httpx.MockTransport(fake.handler),
        )
        return bot, fake, llm

    return _make


async def test_start_sends_greeting_with_quick_replies(make_bot):
    bot, fake, _ = make_bot([])
    await bot.handle_update(message("/start"))
    greeting = fake.sent()[0]
    assert greeting["chat_id"] == 42
    assert greeting["text"].startswith("Здравствуйте! Я Ева")
    assert "вымышленные" in greeting["text"]
    assert greeting["reply_markup"]["keyboard"][0][0]["text"] == "Хочу поменять масло"


async def test_message_goes_through_agent(make_bot):
    bot, fake, llm = make_bot([LLMResult("Подскажите, какая у вас машина?")])
    await bot.handle_update(message("Хочу поменять масло"))
    assert fake.sent()[-1]["text"] == "Подскажите, какая у вас машина?"
    assert ("sendChatAction", {"chat_id": 42, "action": "typing"}) in fake.calls
    assert llm.calls[0][-1].content == "Хочу поменять масло"


async def test_booking_sends_card_with_sheet_button(make_bot, storage):
    bot, fake, _ = make_bot(
        [
            LLMResult("", FunctionCall("create_booking", booking_args(parts_option="optimum"))),
            LLMResult("Готово, запись №1."),
        ]
    )
    await bot.handle_update(message("Подтверждаю"))
    reply, card = fake.sent()[-2:]
    assert reply["text"] == "Готово, запись №1."
    assert card["text"].startswith("✅ Запись №1 создана")
    assert "Запчасти: Оптимум: Синтетика Shell" in card["text"]
    assert "от 6 000 ₽" in card["text"]
    assert card["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("https://docs.google.com")
    assert storage.recent_bookings()[0].channel == "telegram"


async def test_new_command_starts_fresh_dialog(make_bot):
    bot, fake, llm = make_bot([LLMResult("ответ 1"), LLMResult("ответ 2")])
    await bot.handle_update(message("первое"))
    first_session = bot.session_id(42)
    await bot.handle_update(message("/new"))
    assert bot.session_id(42) != first_session
    await bot.handle_update(message("второе"))
    # во втором диалоге модель не видит первое сообщение
    assert [m.content for m in llm.calls[1] if m.role == "user"] == ["второе"]


async def test_groups_and_non_text_are_handled(make_bot):
    bot, fake, _ = make_bot([])
    await bot.handle_update(message("привет", chat_type="group"))
    assert fake.sent() == []
    await bot.handle_update(message(None))
    assert "только текст" in fake.sent()[0]["text"]


async def test_model_outage_is_reported_to_user(make_bot):
    def fail(*_):
        raise LLMUnavailableError("down")

    bot, fake, _ = make_bot([fail])
    await bot.handle_update(message("Привет"))
    assert "недоступна" in fake.sent()[-1]["text"]


async def test_network_errors_do_not_leak_token(make_bot, caplog):
    bot, _, _ = make_bot([])
    calls = 0

    async def failing_call(method, **params):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError
        raise httpx.ConnectError("https://api.telegram.org/bot123:ABC/getMe unreachable")

    bot.call = failing_call
    with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
        await bot.run()
    assert "123:ABC" not in caplog.text and "***" in caplog.text
    await bot.aclose()


async def test_setup_reads_username(make_bot):
    bot, fake, _ = make_bot([])
    await bot.setup()
    assert bot.url == "https://t.me/demo_motor_bot"
    assert [m for m, _ in fake.calls] == ["getMe", "deleteWebhook", "setMyCommands"]
    await bot.aclose()
