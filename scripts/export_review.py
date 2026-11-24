"""Таблица для проверки справочника механиком.

    python scripts/export_review.py

Собирает из config/business.yaml файл review/Справочник для проверки механиком.xlsx:
неисправности, услуги и запчасти — каждое с колонками «Верно?» и «Правки механика».
В скрытой последней колонке каждого листа — ключ строки, по нему правки
переносятся обратно в конфиг.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.business import Business, load_business  # noqa: E402

OUT = ROOT / "review" / "Справочник для проверки механиком.xlsx"

FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F5E3B")
EDIT_FILL = PatternFill("solid", fgColor="FFF2A8")  # жёлтое — заполняет механик
EDIT_HEADER_FILL = PatternFill("solid", fgColor="E0B400")
DANGER_FONT = Font(name=FONT, size=10, color="B42318")
THIN = Side(style="thin", color="C9CED6")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TOP = Alignment(vertical="top", wrap_text=True)
LINE_HEIGHT = 13  # пт на строку текста Arial 10


def price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def bullets(items: list[str]) -> str:
    return "\n".join(f"— {x}" for x in items)


def numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))


def duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    return f"{hours} ч" if hours else f"{mins} мин"


def text_lines(value: object, width: float) -> int:
    """Сколько строк займёт текст в колонке такой ширины (оценка для высоты строки)."""
    per_line = max(1, int(width * 1.15))
    return sum(max(1, math.ceil(len(part) / per_line)) for part in str(value or "").split("\n"))


def write_table(ws, columns: list[tuple[str, float, bool]], rows: list[list[object]], key_header: str = "Ключ") -> None:
    """columns: (заголовок, ширина, редактирует ли механик). Последний элемент строки — ключ."""
    headers = [c[0] for c in columns] + [key_header]
    for col, title in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=title)
        editable = col <= len(columns) and columns[col - 1][2]
        cell.font = Font(name=FONT, size=10, bold=True, color="FFFFFF" if not editable else "1B1F27")
        cell.fill = EDIT_HEADER_FILL if editable else HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[1].height = 32

    for r, row in enumerate(rows, 2):
        lines = 1
        for c, value in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = Font(name=FONT, size=10)
            cell.alignment = TOP
            cell.border = BORDER
            if c <= len(columns):
                width = columns[c - 1][1]
                lines = max(lines, text_lines(value, width))
                if columns[c - 1][2]:
                    cell.fill = EDIT_FILL
        ws.row_dimensions[r].height = min(409, lines * LINE_HEIGHT + 6)

    for c, (_, width, _) in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(c)].width = width
    key_col = get_column_letter(len(columns) + 1)
    ws.column_dimensions[key_col].hidden = True
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{key_col}{len(rows) + 1}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"


def add_verdict_validation(ws, column: str, last_row: int) -> None:
    dv = DataValidation(type="list", formula1='"Да,Нет,Частично"', allow_blank=True)
    dv.prompt = "Да — всё верно; Нет — неверно; Частично — поправить (напишите как в «Правки механика»)"
    dv.promptTitle = "Верно?"
    ws.add_data_validation(dv)
    dv.add(f"{column}2:{column}{last_row}")


def sheet_intro(ws, business: Business, counts: dict[str, int]) -> None:
    ws.title = "Как проверять"
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 95
    ws.sheet_view.showGridLines = False
    rows: list[tuple[str, str, Font | None]] = [
        ("Справочник ИИ-администратора автосервиса", "", Font(name=FONT, size=14, bold=True)),
        ("", f"Бот «{business.assistant_name}» отвечает клиентам по этим данным: называет причины неисправности в указанном "
             "порядке, предупреждает, если ехать опасно, задаёт уточняющие вопросы и записывает на услугу. "
             "Чем точнее справочник, тем профессиональнее бот.", None),
        ("Что проверить", f"Лист «Неисправности» — {counts['symptoms']} строк: верны ли причины и их порядок, "
                          "предупреждения, вопросы и предлагаемые услуги.\n"
                          f"Лист «Услуги» — {counts['services']} строк: длительность и цена работ.\n"
                          f"Лист «Запчасти» — {counts['parts']} строк: варианты и цены запчастей.\n"
                          "Лист «Добавить» — неисправности, которых не хватает.", None),
        ("Как отметить", "Заполняйте только жёлтые колонки.\n"
                         "«Верно?» — выберите из списка: Да, Нет или Частично.\n"
                         "«Правки механика» — как должно быть. Можно коротко, своими словами.", None),
        ("Пример заполнения", "Верно?: Частично\n"
                              "Правки механика: первой причиной поставить утечку из суппорта, шланг — второй. "
                              "Добавить вопрос: «Меняли ли недавно колодки или прокачивали тормоза?»", None),
        ("Пометки", "«Сначала уточнить: да» — бот сначала задаёт вопросы и не называет причину, пока клиент не ответит.\n"
                    "«Опасно ехать» — текст, с которого бот начинает ответ; дальше он предлагает эвакуатор.\n"
                    "Цены — «от», за работу; запчасти считаются отдельно.", None),
        ("После проверки", "Сохраните файл и перешлите обратно — правки перенесутся в бота.", None),
    ]
    for r, (label, text, font) in enumerate(rows, 1):
        a, b = ws.cell(row=r, column=1, value=label), ws.cell(row=r, column=2, value=text)
        a.font = font or Font(name=FONT, size=10, bold=True)
        b.font = Font(name=FONT, size=10)
        a.alignment = b.alignment = TOP
        if label == "Пример заполнения":
            b.fill = EDIT_FILL
        ws.row_dimensions[r].height = max(20, text_lines(text, 95) * LINE_HEIGHT + 8)


def main() -> None:
    business = load_business(ROOT / "config" / "business.yaml")
    wb = Workbook()

    # --- Неисправности ------------------------------------------------------
    symptom_rows = []
    for system in business.symptoms.values():
        for i, item in enumerate(system.items, 1):
            services = [business.service(sid) for sid in item.services]
            symptom_rows.append(
                [
                    len(symptom_rows) + 1,
                    system.title,
                    bullets(item.signs),
                    numbered(item.causes),
                    "да" if item.ask_first else "",
                    item.danger or "—",
                    bullets(item.ask) if item.ask else "—",
                    bullets([f"{s.name} — от {price(s.price_from)}" for s in services if s]),
                    "",
                    "",
                    f"{system.id}/{i}",
                ]
            )
    ws = wb.create_sheet("Неисправности")
    write_table(
        ws,
        [
            ("№", 5, False),
            ("Система", 14, False),
            ("Как описывает клиент (признаки)", 34, False),
            ("Вероятные причины — от частой к редкой", 48, False),
            ("Сначала уточнить", 10, False),
            ("Опасно ехать? (что бот говорит)", 34, False),
            ("Уточняющие вопросы", 34, False),
            ("Что предлагает бот", 30, False),
            ("Верно?", 11, True),
            ("Правки механика", 44, True),
        ],
        symptom_rows,
    )
    for r in range(2, len(symptom_rows) + 2):
        if ws.cell(row=r, column=6).value != "—":
            ws.cell(row=r, column=6).font = DANGER_FONT
    add_verdict_validation(ws, "I", len(symptom_rows) + 1)

    # --- Услуги ---------------------------------------------------------------
    service_rows = [
        [n, s.name, duration(s.duration_min), s.price_from, s.note or "—", "", "", s.id]
        for n, s in enumerate(business.services, 1)
    ]
    ws = wb.create_sheet("Услуги")
    write_table(
        ws,
        [
            ("№", 5, False),
            ("Услуга", 46, False),
            ("Длительность", 13, False),
            ("Работа от, ₽", 13, False),
            ("Пояснение", 46, False),
            ("Верно?", 11, True),
            ("Правки механика", 44, True),
        ],
        service_rows,
    )
    for r in range(2, len(service_rows) + 2):
        ws.cell(row=r, column=4).number_format = "#,##0"
    add_verdict_validation(ws, "F", len(service_rows) + 1)

    # --- Запчасти -------------------------------------------------------------
    parts_rows = []
    for part in business.parts.values():
        for option in part.options:
            parts_rows.append(
                [len(parts_rows) + 1, part.name, option.tier, option.name, option.price_from,
                 option.note or "—", "", "", f"{part.id}/{option.id}"]
            )
    ws = wb.create_sheet("Запчасти")
    write_table(
        ws,
        [
            ("№", 5, False),
            ("Что меняем", 34, False),
            ("Уровень", 11, False),
            ("Что ставим", 44, False),
            ("Запчасти от, ₽", 13, False),
            ("Пояснение", 36, False),
            ("Верно?", 11, True),
            ("Правки механика", 40, True),
        ],
        parts_rows,
    )
    for r in range(2, len(parts_rows) + 2):
        ws.cell(row=r, column=5).number_format = "#,##0"
    add_verdict_validation(ws, "G", len(parts_rows) + 1)

    # --- Добавить -------------------------------------------------------------
    ws = wb.create_sheet("Добавить")
    write_table(
        ws,
        [
            ("Система", 16, True),
            ("Как описывает клиент (признаки)", 40, True),
            ("Вероятные причины — от частой к редкой", 48, True),
            ("Опасно ехать? Что сказать", 36, True),
            ("Что спросить у клиента", 36, True),
            ("Какую услугу предложить", 30, True),
        ],
        [["", "", "", "", "", "", ""] for _ in range(20)],
    )
    for r in range(2, 22):
        ws.row_dimensions[r].height = 48

    sheet_intro(
        wb.worksheets[0],
        business,
        {"symptoms": len(symptom_rows), "services": len(service_rows), "parts": len(parts_rows)},
    )
    wb.active = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"{OUT.relative_to(ROOT)}: неисправностей {len(symptom_rows)}, услуг {len(service_rows)}, запчастей {len(parts_rows)}")


if __name__ == "__main__":
    main()
