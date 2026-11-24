"""Проверка подключения к GigaChat. Сам ключ никогда не печатается.

    python scripts/check_gigachat.py --offline   # только формат ключа в .env
    python scripts/check_gigachat.py             # + токен, список моделей и тест вызова функции
"""

from __future__ import annotations

import argparse
import base64
import binascii
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.settings import Settings  # noqa: E402

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def check_format(key: str | None) -> bool:
    if not key:
        print("✗ GIGACHAT_CREDENTIALS пустой: впишите ключ авторизации в .env")
        return False
    if any(ch in key for ch in " \t\"'<>"):
        print("✗ В ключе есть пробелы, кавычки или скобки — вставьте только саму строку ключа")
        return False
    if UUID.match(key):
        print("✗ Похоже на Client ID или Client Secret (формат xxxxxxxx-xxxx-...). Нужен «Ключ авторизации» —")
        print("  длинная строка, которая заканчивается обычно на «==»; в личном кабинете: «Получить ключ»")
        return False
    try:
        decoded = base64.b64decode(key, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        print("✗ Ключ не похож на ключ авторизации: это должна быть строка base64")
        return False
    client_id, sep, secret = decoded.partition(":")
    if not sep or not UUID.match(client_id) or not secret:
        print("✗ Строка base64, но внутри не «Client ID:Client Secret» — проверьте, что скопирован ключ авторизации")
        return False
    print(f"✓ Формат ключа правильный (длина {len(key)} символов, внутри Client ID и Client Secret)")
    return True


def check_live(settings: Settings) -> bool:
    from gigachat import GigaChat
    from gigachat.models import Chat, Function, Messages

    ca = settings.gigachat_ca_bundle_file
    if settings.gigachat_verify_ssl and (not ca or not Path(ca).exists()):
        print(f"✗ Нет файла сертификата Минцифры: {ca}. Запустите scripts/get_certs.sh")
        return False

    client = GigaChat(
        credentials=settings.gigachat_credentials,
        scope=settings.gigachat_scope,
        ca_bundle_file=ca,
        verify_ssl_certs=settings.gigachat_verify_ssl,
        timeout=60,
    )
    try:
        models = sorted(m.id_ for m in client.get_models().data)
    except Exception as exc:  # noqa: BLE001 — показываем любую причину
        print(f"✗ Не удалось получить токен или список моделей: {type(exc).__name__}: {exc}")
        return False
    print("✓ Токен получен. Доступные модели:", ", ".join(models))

    function = Function.model_validate(
        {
            "name": "get_free_slots",
            "description": "Возвращает свободное время для записи",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type": "string", "description": "Дата ГГГГ-ММ-ДД"}},
                "required": ["date"],
            },
        }
    )
    ok = True
    for model in settings.gigachat_models:
        try:
            response = client.chat(
                Chat(
                    model=model,
                    messages=[Messages(role="user", content="Есть свободное время на 2026-12-01?")],
                    functions=[function],
                    function_call="auto",
                )
            )
            call = response.choices[0].message.function_call
            status = f"вызвала функцию {call.name}({call.arguments})" if call else "ответила текстом без вызова функции"
            print(f"✓ {model}: {status}, токенов: {response.usage.total_tokens}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"✗ {model}: {type(exc).__name__}: {str(exc)[:200]}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="только проверить формат ключа")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not check_format(settings.gigachat_credentials):
        return 1
    if args.offline:
        return 0
    return 0 if check_live(settings) else 1


if __name__ == "__main__":
    raise SystemExit(main())
