"""HTTP API бота, виджет, демо-страница и Telegram-бот.

Запуск: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import BookingAgent, SessionStore
from app.business import load_business
from app.chat_service import ChatOutcome, ChatRejected, ChatService
from app.llm.base import LLMProvider
from app.privacy import mask_name, mask_phone, mask_phones_in_text
from app.ratelimit import SlidingWindowLimiter
from app.schedule import Scheduler, date_text, hhmm
from app.settings import BASE_DIR, Settings
from app.sheets import SheetsSync
from app.site_relay import SiteRelay
from app.storage import Booking, Storage
from app.telegram import TelegramBot
from app.tools import BookingTools

log = logging.getLogger("app")

STATIC_DIR = BASE_DIR / "static"


class ChatRequest(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    message: str = Field(min_length=1, max_length=4000)


class BookingCard(BaseModel):
    number: int
    service: str
    date_text: str
    time: str
    price_from: int
    parts: str = ""
    total_from: int = 0

    @classmethod
    def of(cls, b: Booking) -> BookingCard:
        return cls(
            number=b.id,
            service=b.service_name,
            date_text=date_text(b.visit_date),
            time=hhmm(b.start_time),
            price_from=b.price_from,
            parts=b.parts_choice,
            total_from=b.estimate_from or b.price_from,
        )


class ChatResponse(BaseModel):
    reply: str
    booking: BookingCard | None = None
    sheet_url: str | None = None
    remaining_messages: int


def create_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        from app.llm.fake import OfflineDemoLLM

        return OfflineDemoLLM()
    if settings.llm_provider == "gigachat":
        if not settings.gigachat_credentials:
            raise RuntimeError(
                "Не задан GIGACHAT_CREDENTIALS в .env. Для проверки без модели поставьте LLM_PROVIDER=fake."
            )
        from app.llm.gigachat_provider import GigaChatProvider

        return GigaChatProvider(
            models=settings.gigachat_models,
            credentials=settings.gigachat_credentials,
            scope=settings.gigachat_scope,
            ca_bundle_file=settings.gigachat_ca_bundle_file,
            verify_ssl=settings.gigachat_verify_ssl,
            timeout=settings.gigachat_timeout,
            max_concurrency=settings.gigachat_max_concurrency,
            queue_timeout=settings.llm_queue_timeout,
            temperature=settings.llm_temperature,
        )
    raise RuntimeError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider}")


def create_app(
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sheets_client_factory: Callable[[], object] | None = None,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
    relay_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx пишет в лог полный адрес запроса, а в адресе Telegram API — токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = settings or Settings.from_env()
    business = load_business(settings.business_config)
    now_fn = now_fn or (lambda: datetime.now(business.timezone).replace(tzinfo=None))
    llm = llm or create_llm(settings)

    storage = Storage(settings.database_path)
    scheduler = Scheduler(business)
    tools = BookingTools(business, storage, scheduler, now_fn)
    agent = BookingAgent(business, llm, tools, storage, now_fn)
    sessions = SessionStore(
        ttl_seconds=settings.session_ttl_minutes * 60,
        ip_bookings_limit=settings.ip_bookings_per_day,
    )
    sheets: SheetsSync | None = None
    if settings.sheets_enabled or sheets_client_factory is not None:
        sheets = SheetsSync(
            credentials_file=settings.google_credentials_file or "",
            sheet_id=settings.google_sheet_id or "",
            worksheet_title=settings.google_worksheet,
            business=business,
            storage=storage,
            client_factory=sheets_client_factory,
        )
    relay: SiteRelay | None = None

    def booking_created(_: Booking) -> None:
        if sheets:
            sheets.trigger()
        if relay:
            relay.state_changed()

    chat = ChatService(settings, agent, sessions, SlidingWindowLimiter(), on_booking=booking_created)

    def client_key(raw: str) -> str:
        """IP или id в Telegram храним только в виде хэша."""
        return hashlib.sha256(f"{settings.ip_hash_salt}|{raw}".encode()).hexdigest()[:16]

    telegram: TelegramBot | None = None
    if settings.telegram_bot_token:
        telegram = TelegramBot(
            token=settings.telegram_bot_token,
            chat=chat,
            business=business,
            client_key=lambda user_id: client_key(f"tg:{user_id}"),
            sheet_url=settings.sheet_url,
            api_base=settings.telegram_api_base,
            transport=telegram_transport,
        )

    # --- данные, которые видит виджет (через API или через посредника на сайте) ---

    def config_payload() -> dict:
        return {
            "business_name": business.name,
            "assistant_name": business.assistant_name,
            "greeting": business.greeting,
            "quick_replies": business.quick_replies,
            "demo_note": business.demo_note,
            "sheet_url": settings.sheet_url,
            "sheet_embed_url": settings.sheet_embed_url,
            "telegram_url": telegram.url if telegram else None,
            "max_message_chars": settings.max_message_chars,
        }

    def recent_payload() -> dict:
        items = []
        for b in storage.recent_bookings(15):
            items.append(
                {
                    "number": b.id,
                    "created": b.created_at.strftime("%d.%m %H:%M"),
                    "service": b.service_name,
                    "visit": f"{date_text(b.visit_date)}, {hhmm(b.start_time)}",
                    "client": mask_name(b.client_name),
                    "phone": mask_phone(b.phone),
                    "extra": {f.label: b.extra.get(f.id, "") for f in business.extra_fields},
                    "parts": b.parts_choice,
                    "total_from": b.estimate_from or b.price_from,
                    "channel": b.channel,
                    "summary": mask_phones_in_text(b.summary),
                    "in_sheet": b.sheet_synced,
                }
            )
        return {"items": items, "sheet_url": settings.sheet_url}

    def chat_payload(outcome: ChatOutcome) -> ChatResponse:
        return ChatResponse(
            reply=outcome.reply,
            booking=BookingCard.of(outcome.booking) if outcome.booking else None,
            sheet_url=settings.sheet_url,
            remaining_messages=outcome.remaining_messages,
        )

    if settings.site_relay_url and settings.site_relay_token:
        relay = SiteRelay(
            base_url=settings.site_relay_url,
            token=settings.site_relay_token,
            chat=chat,
            client_key=lambda ip: client_key(ip),
            config_payload=config_payload,
            recent_payload=recent_payload,
            chat_response=lambda outcome: chat_payload(outcome).model_dump(mode="json"),
            transport=relay_transport,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info(
            "Модель: %s; Google Таблица: %s; Telegram: %s; сайт: %s",
            llm.name,
            "подключена" if sheets else "не настроена",
            "включён" if telegram else "выключен",
            settings.site_relay_url if relay else "напрямую через API",
        )
        tasks = []
        if sheets:
            tasks.append(asyncio.create_task(sheets.run()))
            sheets.trigger()
        if telegram:
            tasks.append(asyncio.create_task(telegram.run()))
        if relay:
            tasks.append(asyncio.create_task(relay.run()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if telegram:
                await telegram.aclose()
            if relay:
                await relay.aclose()
            await llm.aclose()
            storage.close()

    app = FastAPI(title="AI Booking Demo", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.state.storage = storage
    app.state.sessions = sessions
    app.state.sheets = sheets
    app.state.telegram = telegram
    app.state.relay = relay

    @app.get("/", include_in_schema=False)
    async def demo_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "demo.html")

    @app.get("/widget.js", include_in_schema=False)
    async def widget_script() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "widget.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "llm": llm.name,
            "sheets": sheets is not None,
            "telegram": telegram.username if telegram else None,
            "site_relay": relay is not None,
            "sessions": len(sessions),
        }

    @app.get("/api/config")
    async def config() -> dict:
        return config_payload()

    @app.get("/api/bookings/recent")
    async def recent_bookings() -> dict:
        return recent_payload()

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat_endpoint(body: ChatRequest, request: Request) -> ChatResponse:
        ip = request.client.host if request.client else "unknown"
        try:
            outcome = await chat.handle(session_id=body.session_id, client_key=client_key(ip), text=body.message)
        except ChatRejected as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
        return chat_payload(outcome)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
