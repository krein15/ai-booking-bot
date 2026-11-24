"""Выгрузка записей в Google Таблицу.

Основное хранилище — SQLite на сервере. Таблица — публичная витрина:
имя и телефон в ней маскируются. Записи, которые не удалось отправить
(нет сети, лимит Google API), догружаются фоновой задачей.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import gspread
from gspread.utils import rowcol_to_a1

from app.business import Business
from app.privacy import mask_name, mask_phone, mask_phones_in_text
from app.schedule import WEEKDAYS_SHORT, hhmm
from app.storage import Booking, Storage

log = logging.getLogger(__name__)

STATUS_TEXT = {"new": "Новая, ждёт звонка", "cancelled": "Отменена"}
CHANNEL_TEXT = {"web": "Сайт", "telegram": "Telegram"}


def sheet_header(business: Business) -> list[str]:
    return [
        "№",
        "Создана",
        "Услуга",
        "Дата визита",
        "Время",
        "Клиент",
        "Телефон",
        *[f.label for f in business.extra_fields],
        "Запчасти",
        "Работа от, ₽",
        "Итого от, ₽",
        "Суть обращения (ИИ)",
        "Комментарий клиента",
        "Канал",
        "Статус",
    ]


def booking_row(booking: Booking, business: Business) -> list[Any]:
    d = booking.visit_date
    return [
        booking.id,
        booking.created_at.strftime("%d.%m.%Y %H:%M"),
        booking.service_name,
        f"{d:%d.%m.%Y} ({WEEKDAYS_SHORT[d.weekday()]})",
        f"{hhmm(booking.start_time)}–{hhmm(booking.end_time)}",
        mask_name(booking.client_name),
        mask_phone(booking.phone),
        *[booking.extra.get(f.id, "") for f in business.extra_fields],
        booking.parts_choice or "не нужны",
        booking.price_from,
        booking.estimate_from or booking.price_from,
        mask_phones_in_text(booking.summary),
        mask_phones_in_text(booking.comment),
        CHANNEL_TEXT.get(booking.channel, booking.channel),
        STATUS_TEXT.get(booking.status, booking.status),
    ]


class SheetsSync:
    def __init__(
        self,
        *,
        credentials_file: str,
        sheet_id: str,
        worksheet_title: str,
        business: Business,
        storage: Storage,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.sheet_id = sheet_id
        self.worksheet_title = worksheet_title
        self.business = business
        self.storage = storage
        self._client_factory = client_factory or (lambda: gspread.service_account(filename=credentials_file))
        self._worksheet: Any = None
        self._event = asyncio.Event()

    # --- синхронная часть (выполняется в отдельном потоке) --------------

    def _open_worksheet(self) -> Any:
        if self._worksheet is not None:
            return self._worksheet
        spreadsheet = self._client_factory().open_by_key(self.sheet_id)
        header = sheet_header(self.business)
        try:
            worksheet = spreadsheet.worksheet(self.worksheet_title)
        except gspread.WorksheetNotFound:
            sheets = spreadsheet.worksheets()
            # для пустого листа Google отдаёт [[]] или строки из пустых ячеек, а не []
            if len(sheets) == 1 and not any(any(cell for cell in row) for row in sheets[0].get_all_values()):
                # Новая таблица с пустым «Лист1»: переименовываем его, чтобы по ссылке
                # посетитель сразу видел записи, а не пустой лист.
                worksheet = sheets[0]
                worksheet.update_title(self.worksheet_title)
            else:
                worksheet = spreadsheet.add_worksheet(title=self.worksheet_title, rows=1000, cols=len(header))
        if worksheet.row_values(1) != header:
            last_cell = rowcol_to_a1(1, len(header))
            worksheet.update(range_name=f"A1:{last_cell}", values=[header])
            worksheet.format(f"A1:{last_cell}", {"textFormat": {"bold": True}})
            worksheet.freeze(rows=1)
        self._worksheet = worksheet
        return worksheet

    def _append(self, rows: list[list[Any]]) -> None:
        self._open_worksheet().append_rows(rows, value_input_option="RAW", table_range="A1")

    # --- асинхронная часть ---------------------------------------------

    async def check(self) -> None:
        """Открывает таблицу и проверяет заголовок — ошибки доступа видны сразу при старте."""
        await asyncio.to_thread(self._open_worksheet)

    async def sync_pending(self) -> int:
        pending = self.storage.unsynced_bookings()
        if not pending:
            return 0
        rows = [booking_row(b, self.business) for b in pending]
        await asyncio.to_thread(self._append, rows)
        self.storage.mark_synced([b.id for b in pending])
        return len(pending)

    def trigger(self) -> None:
        self._event.set()

    async def run(self, interval_seconds: float = 60.0) -> None:
        try:
            await self.check()
            log.info("Google Таблица подключена, лист «%s»", self.worksheet_title)
        except Exception:
            log.exception("Не удалось открыть Google Таблицу — проверьте доступ сервисного аккаунта")
            self._worksheet = None
        while True:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
            self._event.clear()
            try:
                sent = await self.sync_pending()
                if sent:
                    log.info("В Google Таблицу выгружено записей: %s", sent)
            except Exception:
                log.exception("Не удалось выгрузить записи в Google Таблицу, повторю позже")
                self._worksheet = None
