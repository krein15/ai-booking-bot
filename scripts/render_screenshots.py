"""Картинки для README из настоящих ответов бота.

Шаг 1 — запись диалогов (нужен ключ GigaChat в .env):

    python -m scripts.render_screenshots --record

Сценарии прогоняются через настоящую модель с фиксированными датой и временем и временной базой:
в Google Таблицу и Telegram ничего не уходит. Ответы модели, вызовы функций и итоговые записи
сохраняются в docs/dialogs.json — его можно прочитать и проверить глазами.

Шаг 2 — картинки (ключ не нужен):

    python -m scripts.render_screenshots

Переписка в Telegram рисуется в стиле Telegram из записанных ответов. Виджет на сайте снимается
настоящим widget.js: локальный сервер бота повторяет записанные ответы модели, а расписание
и запись выполняются заново — поэтому время, цены и номер записи на снимке настоящие.
Снимает Microsoft Edge через Playwright (pip install playwright, браузер скачивать не нужно).

    --site-dir "../My Site/dist" --site-path /uslugi/ii-assistent/zapis-klientov/
        снять виджет на готовой странице сайта вместо встроенной демо-страницы
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import functools
import http.server
import json
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import BookingAgent, SessionStore  # noqa: E402
from app.business import Business, load_business  # noqa: E402
from app.llm.base import FunctionCall, FunctionSpec, LLMResult, Message  # noqa: E402
from app.main import create_app, create_llm  # noqa: E402
from app.schedule import Scheduler  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.sheets import booking_row, sheet_header  # noqa: E402
from app.storage import Storage  # noqa: E402
from app.telegram import booking_card  # noqa: E402
from app.tools import BookingTools  # noqa: E402

DOCS = ROOT / "docs"
DIALOGS = DOCS / "dialogs.json"
SHOTS = DOCS / "screenshots"
AVATAR = ROOT / "assets" / "telegram" / "avatar.png"
BOT_NAME = "Ева · ИИ-администратор (демо)"
SHEET_BUTTON = "Посмотреть в Google Таблице"
CONFIRM = "Да, всё верно"
CHAT_W, CHAT_H = 400, 800


@dataclass
class Scenario:
    id: str
    channel: str  # telegram или web
    now: datetime
    messages: list[str]


# Время зафиксировано: расписание и «занятость» в демо зависят от даты, а так снимки
# повторяются один в один. Если бот попросит подтвердить данные — скрипт ответит «Да, всё верно».
SCENARIOS = [
    Scenario("site", "web", datetime(2026, 11, 23, 10, 20), [
        "Здравствуйте! Что-то стучит спереди на кочках. Можно приехать во вторник после обеда?",
        "Давайте самое первое время из тех, что вы предложили",
        "Андрей, 8 900 000-12-34, Hyundai Solaris 2019",
    ]),
    Scenario("smoke", "telegram", datetime(2026, 11, 23, 11, 5), [
        "Идёт дым из-под капота",
        "Белый, пахнет сладковато, стрелка температуры почти в красной зоне",
        "Понял, своим ходом не поеду. Можно завтра утром?",
        "Ольга, 8 900 000-56-78, Skoda Octavia 2016",
    ]),
    Scenario("parts", "telegram", datetime(2026, 11, 23, 12, 40), [
        "Нужно поменять колодки, какие есть варианты?",
        "Передние. Давайте оптимум. Можно завтра вечером?",
        "Давайте самое раннее из вечерних",
        "Мария, 8 900 000-90-12, Kia Rio 2017",
    ]),
    Scenario("oil", "telegram", datetime(2026, 11, 23, 14, 15), [
        "Хочу поменять масло на этой неделе",
        "Масло ваше, оптимум. В четверг утром получится?",
        "Давайте первое время",
        "Сергей, 8 900 000-34-56, Toyota Camry 2018",
    ]),
]


# ---------- запись и повтор ответов модели ----------

def fingerprint(messages: list[Message]) -> str:
    last = messages[-1]
    return f"{last.role}:{last.content[:160]}"


class RecordingLLM:
    """Пропускает запросы к настоящей модели и запоминает каждый ответ."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.name = inner.name
        self.log: list[dict[str, Any]] = []
        self.tokens = 0

    async def chat(self, messages: list[Message], functions: list[FunctionSpec], allow_calls: bool = True) -> LLMResult:
        result = await self.inner.chat(messages, functions, allow_calls)
        self.tokens += result.total_tokens or 0
        call = result.function_call
        self.log.append({
            "after": fingerprint(messages),
            "content": result.content,
            "function_call": {"name": call.name, "arguments": call.arguments} if call else None,
            "state_id": result.state_id,
            "model": result.model,
        })
        if call:
            print(f"      → {call.name}({json.dumps(call.arguments, ensure_ascii=False)})  [{result.model}]")
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()


class ReplayLLM:
    """Возвращает записанные ответы модели по порядку и проверяет, что бот спрашивает то же самое."""

    name = "replay"

    def __init__(self, log: list[dict[str, Any]]) -> None:
        self.log = list(log)

    async def chat(self, messages: list[Message], functions: list[FunctionSpec], allow_calls: bool = True) -> LLMResult:
        if not self.log:
            raise RuntimeError("Записанные ответы закончились — перезапишите диалоги: --record")
        entry = self.log.pop(0)
        if fingerprint(messages) != entry["after"]:
            raise RuntimeError(
                "Бот спросил модель не то, что при записи — код изменился, перезапишите диалоги: --record\n"
                f"  ждали:    {entry['after']}\n  пришло:   {fingerprint(messages)}"
            )
        call = entry["function_call"]
        return LLMResult(
            content=entry["content"],
            function_call=FunctionCall(call["name"], call["arguments"]) if call else None,
            state_id=entry["state_id"],
            model=entry["model"],
        )

    async def aclose(self) -> None:
        return None


async def record(settings: Settings, only: set[str]) -> None:
    business = load_business(settings.business_config)
    data = json.loads(DIALOGS.read_text(encoding="utf-8")) if DIALOGS.exists() else {}
    for scenario in SCENARIOS:
        if only and scenario.id not in only:
            continue
        print(f"\n=== {scenario.id} ({scenario.channel}, {scenario.now:%d.%m.%Y %H:%M})")
        now_fn = functools.partial(lambda moment: moment, scenario.now)
        storage = Storage(Path(tempfile.mkdtemp()) / "record.db")
        tools = BookingTools(business, storage, Scheduler(business), now_fn)
        llm = RecordingLLM(create_llm(settings))
        agent = BookingAgent(business, llm, tools, storage, now_fn)
        session = SessionStore(3600, 5).get_or_create(f"readme-{scenario.id}", f"readme-{scenario.id}", scenario.channel)
        queue = list(scenario.messages)
        turns: list[dict[str, Any]] = []
        try:
            while queue:
                text = queue.pop(0)
                started = time.monotonic()
                reply = await agent.reply(session, text)
                turn: dict[str, Any] = {"user": text, "bot": reply.text, "seconds": round(time.monotonic() - started, 1)}
                print(f"КЛИЕНТ: {text}\nБОТ ({turn['seconds']} с): {reply.text}\n")
                if reply.booking:
                    turn["booking"] = {"card": booking_card(reply.booking), "row": booking_row(reply.booking, business)}
                    print(f"      ✓ {turn['booking']['card']}\n")
                turns.append(turn)
                booked = any("booking" in t for t in turns)
                if not queue and not booked and text != CONFIRM:
                    queue.append(CONFIRM)
        finally:
            print(f"Токенов: {llm.tokens}")
            await llm.aclose()
            storage.close()
        data[scenario.id] = {
            "channel": scenario.channel,
            "now": scenario.now.isoformat(timespec="minutes"),
            "turns": turns,
            "llm": llm.log,
        }
    DOCS.mkdir(exist_ok=True)
    DIALOGS.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\nСохранено: {DIALOGS.relative_to(ROOT)}")


# ---------- переписка в стиле Telegram ----------

@dataclass
class Item:
    side: str  # in — бот, out — клиент
    text: str
    time: str
    buttons: list[str] = field(default_factory=list)


@dataclass
class Screen:
    items: list[Item]
    keyboard: list[str] = field(default_factory=list)
    typing: bool = False


TG_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { width: %(w)dpx; height: %(h)dpx; overflow: hidden; display: flex; flex-direction: column;
       font: 15px/1.36 "Segoe UI", system-ui, sans-serif; color: #000; background: #fff; }
.header { height: 56px; display: flex; align-items: center; gap: 12px; padding: 0 14px;
          background: #fff; border-bottom: 1px solid #e3e6ea; flex-shrink: 0; }
.header .back { font-size: 22px; color: #8a96a3; }
.header img { width: 40px; height: 40px; border-radius: 50%%; }
.header b { display: block; font-size: 16px; }
.header span { font-size: 13px; color: #8a96a3; }
.header span.typing { color: #3a95d8; }
.chat { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; overflow: hidden;
        gap: 6px; padding: 10px 10px 8px;
        background: linear-gradient(160deg, #c4d9b3 0%%, #a9cba6 45%%, #93bdb0 100%%); }
.msg { max-width: 86%%; }
.msg.in { align-self: flex-start; }
.msg.out { align-self: flex-end; }
.bubble { padding: 7px 10px 6px; border-radius: 14px; box-shadow: 0 1px 1px rgba(0,0,0,.12);
          word-wrap: break-word; white-space: pre-wrap; }
.bubble::after { content: ""; display: block; clear: both; }
.in .bubble { background: #fff; border-bottom-left-radius: 5px; }
.out .bubble { background: #effdde; border-bottom-right-radius: 5px; }
.time { float: right; margin: 4px 0 -2px 10px; font-size: 11.5px; color: #a0acb6; white-space: nowrap; }
.out .time { color: #5dab4f; }
.buttons { margin-top: 3px; display: flex; flex-direction: column; gap: 3px; }
.btn { position: relative; text-align: center; padding: 8px 6px; border-radius: 9px; color: #fff; font-weight: 600;
       font-size: 14px; background: rgba(40, 70, 55, .34); }
.btn::after { content: "↗"; position: absolute; top: 2px; right: 6px; font-size: 11px; }
.input { height: 50px; flex-shrink: 0; display: flex; align-items: center; gap: 14px; padding: 0 16px;
         background: #fff; border-top: 1px solid #e3e6ea; color: #9aa6b1; font-size: 16px; }
.input .grow { flex: 1; }
.keyboard { flex-shrink: 0; background: #f0f2f5; padding: 6px 6px 10px; display: flex; flex-direction: column; gap: 6px; }
.key { text-align: center; background: #fff; border-radius: 9px; padding: 10px 4px;
       font-size: 14.5px; box-shadow: 0 1px 1px rgba(0,0,0,.15); }
"""


def tg_page(screen: Screen) -> str:
    avatar = base64.b64encode(AVATAR.read_bytes()).decode()
    items = []
    for item in screen.items:
        time_html = f'<span class="time">{item.time}{" ✓✓" if item.side == "out" else ""}</span>'
        buttons = ""
        if item.buttons:
            buttons = '<div class="buttons">' + "".join(f'<div class="btn">{escape(b)}</div>' for b in item.buttons) + "</div>"
        items.append(f'<div class="msg {item.side}"><div class="bubble">{escape(item.text)}{time_html}</div>{buttons}</div>')
    keyboard = ""
    if screen.keyboard:
        keyboard = '<div class="keyboard">' + "".join(f'<div class="key">{escape(k)}</div>' for k in screen.keyboard) + "</div>"
    status = '<span class="typing">печатает…</span>' if screen.typing else "<span>бот</span>"
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<style>{TG_CSS % {"w": CHAT_W, "h": CHAT_H}}</style></head><body>
<div class="header"><span class="back">←</span><img src="data:image/png;base64,{avatar}">
<div><b>{escape(BOT_NAME)}</b>{status}</div></div>
<div class="chat">{"".join(items)}</div>
<div class="input"><span>📎</span><span class="grow">Сообщение</span><span>🎤</span></div>
{keyboard}
</body></html>"""


class Clock:
    def __init__(self, start: datetime) -> None:
        self.minutes = start.hour * 60 + start.minute

    def tick(self, minutes: int = 1) -> str:
        self.minutes += minutes
        return f"{self.minutes // 60:02d}:{self.minutes % 60:02d}"


def telegram_items(business: Business, rec: dict[str, Any], start_command: bool = True) -> list[tuple[str, Item]]:
    """Переписка Telegram целиком: (метка, сообщение). Метки — чтобы выбрать кусок для снимка."""
    clock = Clock(datetime.fromisoformat(rec["now"]))
    items: list[tuple[str, Item]] = []
    if start_command:
        moment = clock.tick(0)
        items.append(("start", Item("out", "/start", moment)))
        greeting = business.greeting + (f"\n\n{business.demo_note}" if business.demo_note else "")
        items.append(("greeting", Item("in", greeting, moment)))
    for number, turn in enumerate(rec["turns"], start=1):
        moment = clock.tick()
        items.append((f"user{number}", Item("out", turn["user"], moment)))
        items.append((f"bot{number}", Item("in", turn["bot"], moment)))
        if "booking" in turn:
            items.append((f"card{number}", Item("in", turn["booking"]["card"], moment, [SHEET_BUTTON])))
    return items


def pick(items: list[tuple[str, Item]], start: str, end: str | None = None) -> list[Item]:
    labels = [label for label, _ in items]
    first = labels.index(start)
    last = labels.index(end) if end else len(labels) - 1
    return [item for _, item in items[first:last + 1]]


def gif_frames(business: Business, rec: dict[str, Any]) -> list[tuple[Screen, int]]:
    """Кадры анимации: приветствие → клиент пишет → «печатает…» → ответ, и так до карточки записи."""
    items = telegram_items(business, rec)
    frames: list[tuple[Screen, int]] = []
    shown: list[Item] = []
    for label, item in items:
        if label == "start":
            continue
        if label == "greeting":
            shown.append(item)
            frames.append((Screen(list(shown), keyboard=business.quick_replies), 1800))
            continue
        if item.side == "out":
            shown.append(item)
            frames.append((Screen(list(shown)), 700))
            frames.append((Screen(list(shown), typing=True), 1000))
        else:
            shown.append(item)
            words = len(item.text.split())
            frames.append((Screen(list(shown)), min(5500, 1800 + words * 110)))
    frames[-1] = (frames[-1][0], 4500)
    return frames


# ---------- Google Таблица ----------

SHEET_COLUMNS = 12  # № … «Суть обращения»: дальше — «за краем окна», как при прокрутке
SHEET_WIDTHS = [40, 118, 250, 112, 96, 76, 132, 150, 230, 104, 96, 330]


def sheet_page(business: Business, rows: list[list[Any]]) -> tuple[str, int, int]:
    header = sheet_header(business)[:SHEET_COLUMNS]
    letters = "ABCDEFGHIJKLMNOP"
    cols = "".join(f'<col style="width:{w}px">' for w in SHEET_WIDTHS)
    head = '<tr class="letters"><th class="corner"></th>' + "".join(
        f"<th>{letters[i]}</th>" for i in range(SHEET_COLUMNS)) + "</tr>"
    body = '<tr class="hdr"><th>1</th>' + "".join(f"<td>{escape(h)}</td>" for h in header) + "</tr>"
    for number, row in enumerate(rows, start=2):
        cells = ""
        for i, value in enumerate(row[:SHEET_COLUMNS]):
            numeric = isinstance(value, int)
            text = f"{value:,}".replace(",", " ") if numeric and i > 0 else str(value)
            cells += f'<td class="{"num" if numeric else ""}">{escape(text)}</td>'
        body += f"<tr><th>{number}</th>{cells}</tr>"
    for number in range(len(rows) + 2, len(rows) + 6):
        body += f"<tr><th>{number}</th>" + "<td></td>" * SHEET_COLUMNS + "</tr>"
    width = 46 + sum(SHEET_WIDTHS) + 20
    height = 150 + 30 + 34 * (len(rows) + 5) + 40
    page = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ width: {width}px; height: {height}px; overflow: hidden; font: 13px Arial, "Segoe UI", sans-serif;
        background: #f9fbfd; color: #202124; position: relative; }}
.top {{ height: 64px; display: flex; align-items: center; gap: 12px; padding: 0 16px; }}
.logo {{ width: 28px; height: 38px; background: #0f9d58; border-radius: 3px; position: relative; }}
.logo::after {{ content: ""; position: absolute; left: 6px; right: 6px; top: 14px; height: 14px;
               border: 2px solid #fff; border-radius: 1px; }}
.title b {{ font-size: 18px; font-weight: 400; display: block; }}
.title span {{ color: #444746; font-size: 14px; letter-spacing: .1px; }}
.title span i {{ font-style: normal; margin-right: 14px; }}
.share {{ margin-left: auto; background: #c2e7ff; border-radius: 20px; padding: 10px 22px; font-size: 14px; font-weight: 500; }}
.bar {{ margin: 0 10px; height: 40px; background: #edf2fa; border-radius: 22px; display: flex; align-items: center;
        gap: 22px; padding: 0 18px; color: #444746; font-size: 14px; }}
.bar .note {{ margin-left: auto; color: #146c2e; }}
.fx {{ height: 32px; margin-top: 8px; display: flex; align-items: center; border-top: 1px solid #e1e3e1;
       border-bottom: 1px solid #e1e3e1; background: #fff; }}
.fx .ref {{ width: 88px; padding-left: 12px; border-right: 1px solid #e1e3e1; color: #444746; }}
.fx .f {{ padding: 0 12px; color: #80868b; font-style: italic; }}
table {{ border-collapse: collapse; table-layout: fixed; background: #fff; width: {46 + sum(SHEET_WIDTHS)}px; }}
th {{ background: #f8f9fa; color: #5f6368; font-weight: 400; font-size: 11px; border: 1px solid #e1e3e1; height: 24px; }}
th.corner {{ width: 46px; }}
tr th:first-child {{ width: 46px; }}
td {{ border: 1px solid #e1e3e1; height: 34px; padding: 0 6px; white-space: nowrap; overflow: hidden;
      text-overflow: ellipsis; font-size: 13px; }}
td.num {{ text-align: right; }}
tr.hdr td {{ font-weight: 700; border-bottom: 3px solid #c4c7c5; }}
.tabs {{ position: absolute; bottom: 0; left: 0; right: 0; height: 40px; background: #f9fbfd; border-top: 1px solid #e1e3e1;
         display: flex; align-items: center; padding-left: 90px; font-size: 14px; }}
.tabs span {{ background: #e1e9f7; color: #0b57d0; padding: 9px 20px; border-radius: 6px 6px 0 0; font-weight: 500; }}
</style></head><body>
<div class="top"><div class="logo"></div><div class="title"><b>Демо Мотор — записи из чата</b>
<span><i>Файл</i><i>Правка</i><i>Вид</i><i>Вставка</i><i>Формат</i><i>Данные</i><i>Инструменты</i><i>Справка</i></span></div>
<div class="share">Доступ: все, у кого есть ссылка</div></div>
<div class="bar"><span>↶</span><span>↷</span><span>100%</span><span>₽</span><span>%</span><span>Arial</span><span>10</span>
<b>B</b><i>I</i><span class="note">Имя и телефон скрыты — полные данные только у автосервиса</span></div>
<div class="fx"><span class="ref">A1</span><span class="f">fx</span><span>№</span></div>
<table><colgroup><col style="width:46px">{cols}</colgroup>{head}{body}</table>
<div class="tabs"><span>{escape("Записи")}</span></div>
</body></html>"""
    return page, width, height


# ---------- обложка для соцсетей ----------

def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₽"


def social_page(business: Business, card: str) -> str:
    service = business.service("brake_pads")
    part = business.parts_for(service)
    prices = {o.id: money(service.price_from + o.price_from) for o in part.options}
    offer = (f"Эконом от {prices['economy']}, оптимум от {prices['optimum']}, премиум от {prices['premium']} "
             "с работой. Какой вариант ближе?")
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ width: 1280px; height: 640px; overflow: hidden; font-family: "Segoe UI", system-ui, sans-serif; color: #e9ecef;
        background: radial-gradient(900px 500px at 85% 20%, #12372a 0%, transparent 60%), #0a0c0f; display: flex;
        align-items: center; padding: 0 72px; gap: 56px; }}
.left {{ flex: 1; }}
.tag {{ display: inline-block; padding: 7px 16px; border-radius: 20px; border: 1px solid #1f6b45; color: #5cf28e;
        background: rgba(92, 242, 142, .08); font-size: 20px; font-weight: 600; }}
h1 {{ font-size: 60px; line-height: 1.08; margin: 26px 0 22px; font-weight: 800; letter-spacing: -.5px; }}
h1 em {{ font-style: normal; color: #5cf28e; }}
p {{ font-size: 23px; line-height: 1.42; color: #aab4bf; max-width: 610px; }}
.cta {{ display: inline-block; margin-top: 34px; background: #5cf28e; color: #03130a; font-weight: 700; font-size: 23px;
        padding: 16px 30px; border-radius: 12px; }}
.phone {{ width: 400px; background: #0e1621; border: 1px solid #1d2a38; border-radius: 22px; padding: 18px;
          display: flex; flex-direction: column; gap: 10px; box-shadow: 0 30px 80px rgba(0, 0, 0, .5); }}
.m {{ max-width: 88%; padding: 10px 14px; border-radius: 16px; font-size: 16.5px; line-height: 1.38; white-space: pre-wrap; }}
.u {{ align-self: flex-end; background: #2b5278; border-bottom-right-radius: 5px; }}
.b {{ align-self: flex-start; background: #182533; border-bottom-left-radius: 5px; }}
.ok {{ align-self: stretch; max-width: none; background: #10291c; border: 1px solid #1f6b45; }}
.ok b {{ color: #5cf28e; }}
.sheet {{ align-self: stretch; text-align: center; padding: 10px; border-radius: 10px; background: #1d2a38;
          font-size: 15.5px; font-weight: 600; }}
</style></head><body>
<div class="left">
  <span class="tag">ИИ-администратор · GigaChat</span>
  <h1>Записывает клиентов<br><em>круглосуточно</em></h1>
  <p>Понимает клиента, подбирает услугу и запчасти, находит свободное время и записывает. Каждая запись — сразу в Google Таблицу.</p>
  <span class="cta">Попробовать демо в Telegram</span>
</div>
<div class="phone">
  <div class="m u">Нужно поменять колодки, какие есть варианты?</div>
  <div class="m b">{escape(offer)}</div>
  <div class="m u">Оптимум, завтра вечером</div>
  <div class="m ok">{card}</div>
  <div class="sheet">Посмотреть в Google Таблице ↗</div>
</div>
</body></html>"""


# ---------- съёмка ----------

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Background:
    """uvicorn или статический сервер в отдельном потоке."""

    def __init__(self, target, stop) -> None:
        self.thread = threading.Thread(target=target, daemon=True)
        self.stop = stop

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
        self.thread.join(timeout=10)


def bot_server(settings: Settings, rec: dict[str, Any], db_dir: Path) -> tuple[Background, int]:
    import uvicorn

    local = dataclasses.replace(
        settings,
        database_path=db_dir / "site.db",
        google_sheet_id=None, google_credentials_file=None,
        telegram_bot_token=None, site_relay_url=None, site_relay_token=None,
        sheet_public_url=settings.sheet_url, sheet_embed_url=None,
    )
    moment = datetime.fromisoformat(rec["now"])
    app = create_app(local, llm=ReplayLLM(rec["llm"]), now_fn=lambda: moment)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    def stop() -> None:
        server.should_exit = True

    background = Background(server.run, stop)
    background.port = port  # type: ignore[attr-defined]
    return background, port


def static_server(directory: Path) -> tuple[Background, int]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *args: None  # type: ignore[method-assign]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    return Background(httpd.serve_forever, httpd.shutdown), httpd.server_address[1]


async def wait_http(url: str, timeout: float = 20) -> None:
    import httpx

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise SystemExit(f"Сервер не поднялся: {url}")
            await asyncio.sleep(0.2)


async def shoot_html(browser, html: str, png: Path, width: int, height: int, scale: float = 2) -> bytes:
    page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
    await page.set_content(html, wait_until="load")
    data = await page.screenshot(type="png")
    await page.close()
    if png:
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(data)
        print("  ✓", png.relative_to(ROOT))
    return data


async def chat_in_widget(page, turns: list[dict[str, Any]]) -> None:
    """Пишет в настоящий виджет реплики клиента и ждёт ответы бота."""
    await page.wait_for_selector(".aibw-input")
    for turn in turns:
        bots = await page.locator(".aibw-bot").count()
        await page.fill(".aibw-input", turn["user"])
        await page.press(".aibw-input", "Enter")
        await page.wait_for_function(
            "n => document.querySelectorAll('.aibw-bot').length > n && !document.querySelector('.aibw-typing')",
            arg=bots, timeout=30000,
        )
    await page.wait_for_timeout(1500)


async def shoot_site(browser, settings: Settings, rec: dict[str, Any], args, out: Path, mobile: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bot, bot_port = bot_server(settings, rec, Path(tmp))
        with bot:
            await wait_http(f"http://127.0.0.1:{bot_port}/health")
            viewport = {"width": 390, "height": 844} if mobile else {"width": 1280, "height": 900}
            context = await browser.new_context(
                viewport=viewport, device_scale_factor=2 if mobile else 1.5, is_mobile=mobile, has_touch=mobile,
                color_scheme="light", locale="ru-RU",
            )
            page = await context.new_page()
            if args.site_dir:
                site, site_port = static_server(Path(args.site_dir))
                with site:
                    async def to_bot(route):
                        path = route.request.url.split("/bot-api/", 1)[1]
                        response = await route.fetch(url=f"http://127.0.0.1:{bot_port}/{path}")
                        await route.fulfill(response=response)

                    await page.route("**/bot-api/**", to_bot)
                    await page.goto(f"http://127.0.0.1:{site_port}{args.site_path}", wait_until="networkidle")
                    # липкая шапка сайта и ссылка «к содержанию» на снимке блока не нужны
                    await page.add_style_tag(content="*{animation:none!important;transition:none!important}"
                                                      "[data-hdr],.skip,[class*=cookie]{display:none!important}"
                                                      ".live-demo{padding:28px!important}")
                    await chat_in_widget(page, rec["turns"])
                    await page.evaluate("document.activeElement && document.activeElement.blur()")
                    target = page.locator("#live-chat" if mobile else ".live-demo")
                    await target.scroll_into_view_if_needed()
                    await target.screenshot(path=str(out), animations="disabled")
            else:
                await page.goto(f"http://127.0.0.1:{bot_port}/", wait_until="networkidle")
                await chat_in_widget(page, rec["turns"])
                await page.evaluate("document.activeElement && document.activeElement.blur()")
                await page.wait_for_timeout(2500)  # таблица записей на демо-странице обновляется сама
                await page.screenshot(path=str(out), animations="disabled")
            await context.close()
    print("  ✓", out.relative_to(ROOT))


async def render(settings: Settings, args) -> None:
    from PIL import Image
    from playwright.async_api import async_playwright

    if not DIALOGS.exists():
        raise SystemExit("Нет docs/dialogs.json — сначала запишите диалоги: --record")
    data = json.loads(DIALOGS.read_text(encoding="utf-8"))
    business = load_business(settings.business_config)
    SHOTS.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge")

        smoke = telegram_items(business, data["smoke"], start_command=False)
        parts = telegram_items(business, data["parts"], start_command=False)
        screens = {
            "01-diagnosis": Screen(pick(smoke, "user1", "bot2")),
            "02-parts": Screen(pick(parts, "user1", "bot2")),
            "03-booking": Screen(pick(parts, "user3")),
        }
        for name, screen in screens.items():
            await shoot_html(browser, tg_page(screen), SHOTS / f"{name}.png", CHAT_W, CHAT_H)

        await shoot_site(browser, settings, data["site"], args, SHOTS / "04-site.png", mobile=False)
        await shoot_site(browser, settings, data["site"], args, SHOTS / "05-site-mobile.png", mobile=True)

        rows = []
        for key in ("site", "smoke", "parts", "oil"):
            for turn in data.get(key, {}).get("turns", []):
                if "booking" in turn:
                    rows.append(list(turn["booking"]["row"]))
        for number, row in enumerate(rows, start=1):
            row[0] = number
        page, width, height = sheet_page(business, rows)
        await shoot_html(browser, page, SHOTS / "06-sheet.png", width, height, scale=1.5)

        card = next(t["booking"]["card"] for t in data["parts"]["turns"] if "booking" in t)
        card_html = escape(card).replace("\n", "<br>", 1)
        card_html = card_html.replace(escape(card.split("\n")[0]), f"<b>{escape(card.split(chr(10))[0])}</b>", 1)
        await shoot_html(browser, social_page(business, card_html), DOCS / "social-preview.png", 1280, 640, scale=1)

        images = []
        durations = []
        for screen, duration in gif_frames(business, data["parts"]):
            png = await shoot_html(browser, tg_page(screen), None, CHAT_W, CHAT_H, scale=1.5)
            images.append(Image.open(BytesIO(png)).convert("RGB").quantize(colors=160, dither=Image.Dither.NONE))
            durations.append(duration)
        gif = DOCS / "demo.gif"
        images[0].save(gif, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
        print(f"  ✓ {gif.relative_to(ROOT)} — {gif.stat().st_size // 1024} КБ, кадров: {len(images)}")
        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", action="store_true", help="записать диалоги с настоящей моделью")
    parser.add_argument("--only", nargs="*", default=[], help="записать только эти сценарии")
    parser.add_argument("--site-dir", help="папка готового сайта, где виджет подключён через /bot-api")
    parser.add_argument("--site-path", default="/uslugi/ii-assistent/zapis-klientov/")
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.record:
        asyncio.run(record(settings, set(args.only)))
    else:
        asyncio.run(render(settings, args))


if __name__ == "__main__":
    main()
