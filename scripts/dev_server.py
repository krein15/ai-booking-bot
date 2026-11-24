"""Локальный запуск сервера.

    python scripts/dev_server.py          # с GigaChat (ключ и сертификат из .env)
    python scripts/dev_server.py --fake   # заглушка вместо модели: проверить виджет и таблицу
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="работать без модели")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # В консоли Windows вывод по умолчанию в cp1251 — русский текст в логах ломается
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.fake:
        os.environ["LLM_PROVIDER"] = "fake"
        os.environ.setdefault("DATABASE_PATH", str(ROOT / "data" / "dev-fake.db"))
        # Заглушка не должна забирать настоящие сообщения у бота на сервере
        os.environ["TELEGRAM_BOT_TOKEN"] = ""
        os.environ["SITE_RELAY_URL"] = ""

    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=args.port, app_dir=str(ROOT))


if __name__ == "__main__":
    main()
