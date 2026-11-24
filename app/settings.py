"""Настройки из переменных окружения (и файла .env при локальном запуске)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env: не перезаписывает уже заданные переменные."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    return int(_str(name) or default)


def _float(name: str, default: float | None) -> float | None:
    value = _str(name)
    return float(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = _str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "да"}


def _list(name: str, default: list[str]) -> list[str]:
    value = _str(name)
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _path(name: str, default: str) -> Path:
    path = Path(_str(name, default) or default)
    return path if path.is_absolute() else BASE_DIR / path


def _optional_file(name: str) -> str | None:
    value = _str(name)
    if not value:
        return None
    path = Path(value)
    return str(path if path.is_absolute() else BASE_DIR / path)


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    gigachat_credentials: str | None
    gigachat_scope: str
    gigachat_models: list[str]
    gigachat_ca_bundle_file: str | None
    gigachat_verify_ssl: bool
    gigachat_timeout: float
    gigachat_max_concurrency: int
    llm_queue_timeout: float
    llm_temperature: float | None

    business_config: Path
    database_path: Path
    ip_hash_salt: str

    google_credentials_file: str | None
    google_sheet_id: str | None
    google_worksheet: str
    sheet_public_url: str | None
    sheet_embed_url: str | None

    cors_origins: list[str]

    telegram_bot_token: str | None
    telegram_api_base: str

    site_relay_url: str | None
    site_relay_token: str | None

    max_message_chars: int
    max_user_messages_per_session: int
    ip_messages_per_minute: int
    ip_messages_per_day: int
    ip_new_sessions_per_day: int
    ip_bookings_per_day: int
    daily_llm_turns_limit: int
    session_ttl_minutes: int

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.google_credentials_file and self.google_sheet_id)

    @property
    def sheet_url(self) -> str | None:
        if self.sheet_public_url:
            return self.sheet_public_url
        if self.google_sheet_id:
            return f"https://docs.google.com/spreadsheets/d/{self.google_sheet_id}/edit?usp=sharing"
        return None

    @classmethod
    def from_env(cls, env_file: Path | None = BASE_DIR / ".env") -> Settings:
        if env_file is not None:
            load_dotenv(env_file)
        return cls(
            llm_provider=(_str("LLM_PROVIDER", "gigachat") or "gigachat").lower(),
            gigachat_credentials=_str("GIGACHAT_CREDENTIALS"),
            gigachat_scope=_str("GIGACHAT_SCOPE", "GIGACHAT_API_PERS") or "GIGACHAT_API_PERS",
            gigachat_models=_list(
                "GIGACHAT_MODELS", ["GigaChat-3-Ultra", "GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2"]
            ),
            gigachat_ca_bundle_file=_optional_file("GIGACHAT_CA_BUNDLE_FILE"),
            gigachat_verify_ssl=_bool("GIGACHAT_VERIFY_SSL_CERTS", True),
            gigachat_timeout=_float("GIGACHAT_TIMEOUT", 60.0) or 60.0,
            gigachat_max_concurrency=_int("GIGACHAT_MAX_CONCURRENCY", 1),
            llm_queue_timeout=_float("LLM_QUEUE_TIMEOUT", 40.0) or 40.0,
            llm_temperature=_float("LLM_TEMPERATURE", None),
            business_config=_path("BUSINESS_CONFIG", "config/business.yaml"),
            database_path=_path("DATABASE_PATH", "data/bot.db"),
            ip_hash_salt=_str("IP_HASH_SALT", "change-me") or "change-me",
            google_credentials_file=_optional_file("GOOGLE_CREDENTIALS_FILE"),
            google_sheet_id=_str("GOOGLE_SHEET_ID"),
            google_worksheet=_str("GOOGLE_WORKSHEET", "Записи") or "Записи",
            sheet_public_url=_str("SHEET_PUBLIC_URL"),
            sheet_embed_url=_str("SHEET_EMBED_URL"),
            cors_origins=_list("CORS_ORIGINS", ["*"]),
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_api_base=_str("TELEGRAM_API_BASE", "https://api.telegram.org") or "https://api.telegram.org",
            site_relay_url=_str("SITE_RELAY_URL"),
            site_relay_token=_str("SITE_RELAY_TOKEN"),
            max_message_chars=_int("MAX_MESSAGE_CHARS", 800),
            max_user_messages_per_session=_int("MAX_USER_MESSAGES_PER_SESSION", 40),
            ip_messages_per_minute=_int("IP_MESSAGES_PER_MINUTE", 12),
            ip_messages_per_day=_int("IP_MESSAGES_PER_DAY", 200),
            ip_new_sessions_per_day=_int("IP_NEW_SESSIONS_PER_DAY", 15),
            ip_bookings_per_day=_int("IP_BOOKINGS_PER_DAY", 5),
            daily_llm_turns_limit=_int("DAILY_LLM_TURNS_LIMIT", 3000),
            session_ttl_minutes=_int("SESSION_TTL_MINUTES", 120),
        )
