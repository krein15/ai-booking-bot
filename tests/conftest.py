from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from app.business import Business, load_business
from app.schedule import Scheduler
from app.settings import BASE_DIR, Settings
from app.storage import Storage
from app.tools import BookingTools, ToolContext

# Понедельник, 10:00 по времени бизнеса
FIXED_NOW = datetime(2026, 11, 23, 10, 0)
TEST_BUSINESS = BASE_DIR / "tests" / "fixtures" / "business.yaml"


@pytest.fixture
def business() -> Business:
    """Тестовый бизнес без «чужой» занятости — так проще проверять слоты."""
    return dataclasses.replace(load_business(TEST_BUSINESS), demo_busy_ratio=0.0)


@pytest.fixture
def now_fn():
    return lambda: FIXED_NOW


@pytest.fixture
def storage():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def scheduler(business) -> Scheduler:
    return Scheduler(business)


@pytest.fixture
def tools(business, storage, scheduler, now_fn) -> BookingTools:
    return BookingTools(business, storage, scheduler, now_fn)


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(session_id="session-1", ip_hash="iphash1", ip_bookings_limit=5)


@pytest.fixture
def make_settings(tmp_path):
    def _make(**overrides) -> Settings:
        base = Settings.from_env(env_file=None)
        defaults = {
            "llm_provider": "fake",
            "business_config": TEST_BUSINESS,
            "database_path": tmp_path / "bot.db",
            "telegram_bot_token": None,
            "google_credentials_file": None,
            "google_sheet_id": None,
            "sheet_public_url": None,
            "sheet_embed_url": None,
        }
        defaults.update(overrides)
        return dataclasses.replace(base, **defaults)

    return _make


def booking_args(**overrides) -> dict:
    args = {
        "service_id": "oil",
        "date": "2026-11-24",
        "time": "12:00",
        "client_name": "Иван Петров",
        "phone": "8 (900) 123-45-67",
        "car": "Kia Rio 2017",
        "summary": "Плановая замена масла",
        "parts_option": "master",
    }
    args.update(overrides)
    args = {key: value for key, value in args.items() if value is not None}
    return args
