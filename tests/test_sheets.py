from __future__ import annotations

import gspread

from app.sheets import SheetsSync, booking_row, sheet_header
from app.tools import ToolContext
from tests.conftest import booking_args


class FakeWorksheet:
    def __init__(self, title="Лист1"):
        self.title = title
        self.header: list[str] = []
        self.rows: list[list] = []
        self.formatted = None
        self.frozen = None

    def row_values(self, row):
        return list(self.header)

    def get_all_values(self):
        values = ([self.header] if self.header else []) + self.rows
        return values or [[]]  # как настоящий Google API для пустого листа

    def update_title(self, title):
        self.title = title

    def update(self, range_name, values):
        self.header = list(values[0])

    def format(self, range_name, fmt):
        self.formatted = (range_name, fmt)

    def freeze(self, rows):
        self.frozen = rows

    def append_rows(self, rows, value_input_option, table_range):
        assert value_input_option == "RAW"
        self.rows.extend(rows)


class FakeSpreadsheet:
    def __init__(self, default_sheet: FakeWorksheet | None = None):
        self.default = default_sheet
        self.sheets: dict[str, FakeWorksheet] = {}

    def worksheet(self, title):
        if title in self.sheets:
            return self.sheets[title]
        if self.default and self.default.title == title:
            return self.default
        raise gspread.WorksheetNotFound(title)

    def worksheets(self):
        return list(self.sheets.values()) + ([self.default] if self.default else [])

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]


class FakeClient:
    def __init__(self):
        self.spreadsheet = FakeSpreadsheet()
        self.opened = []

    def open_by_key(self, key):
        self.opened.append(key)
        return self.spreadsheet


def test_row_is_masked(tools, business):
    booking = tools.create_booking(booking_args(comment="звоните 8 900 123-45-67"), ToolContext("s", "ip")).booking
    row = dict(zip(sheet_header(business), booking_row(booking, business), strict=True))
    assert row["Клиент"] == "Иван П."
    assert row["Телефон"] == "+7 (900) ***-**-67"
    assert row["Автомобиль"] == "Kia Rio 2017"
    assert row["Дата визита"] == "24.11.2026 (вт)"
    assert row["Время"] == "12:00–13:00"
    assert "***" in row["Комментарий клиента"]
    assert "123-45-67" not in str(row)


async def test_empty_default_sheet_is_renamed(tools, business, storage):
    client = FakeClient()
    default = FakeWorksheet("Лист1")
    client.spreadsheet = FakeSpreadsheet(default_sheet=default)
    sync = SheetsSync(
        credentials_file="", sheet_id="id", worksheet_title="Записи",
        business=business, storage=storage, client_factory=lambda: client,
    )
    tools.create_booking(booking_args(), ToolContext("s", "ip"))
    assert await sync.sync_pending() == 1
    assert default.title == "Записи" and default.header == sheet_header(business)
    assert client.spreadsheet.sheets == {}  # новый лист не создавался


async def test_non_empty_default_sheet_is_kept(tools, business, storage):
    client = FakeClient()
    default = FakeWorksheet("Лист1")
    default.rows = [["чьи-то данные"]]
    client.spreadsheet = FakeSpreadsheet(default_sheet=default)
    sync = SheetsSync(
        credentials_file="", sheet_id="id", worksheet_title="Записи",
        business=business, storage=storage, client_factory=lambda: client,
    )
    tools.create_booking(booking_args(), ToolContext("s", "ip"))
    await sync.sync_pending()
    assert default.title == "Лист1" and "Записи" in client.spreadsheet.sheets


async def test_sync_creates_sheet_and_appends_once(tools, business, storage):
    client = FakeClient()
    sync = SheetsSync(
        credentials_file="",
        sheet_id="sheet-id",
        worksheet_title="Записи",
        business=business,
        storage=storage,
        client_factory=lambda: client,
    )
    tools.create_booking(booking_args(), ToolContext("s", "ip"))
    tools.create_booking(booking_args(time="15:00"), ToolContext("s2", "ip2"))

    assert await sync.sync_pending() == 2
    worksheet = client.spreadsheet.sheets["Записи"]
    assert worksheet.header == sheet_header(business)
    assert worksheet.frozen == 1
    assert [r[0] for r in worksheet.rows] == [1, 2]
    assert storage.unsynced_bookings() == []

    assert await sync.sync_pending() == 0
    assert len(worksheet.rows) == 2
    assert client.opened == ["sheet-id"]
