"""Загрузка описания демо-бизнеса из YAML."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


PART_OPTION_IDS = ("economy", "optimum", "premium")
PART_OPTION_TIERS = {"economy": "Эконом", "optimum": "Оптимум", "premium": "Премиум", "own": "Свои запчасти"}


@dataclass(frozen=True)
class PartOption:
    id: str  # economy / optimum / premium
    name: str
    price_from: int
    note: str = ""

    @property
    def tier(self) -> str:
        return PART_OPTION_TIERS.get(self.id, self.id)


@dataclass(frozen=True)
class Part:
    id: str
    name: str
    options: list[PartOption]

    def option(self, option_id: str) -> PartOption | None:
        return next((o for o in self.options if o.id == option_id), None)


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    duration_min: int
    price_from: int
    note: str = ""
    parts: str | None = None  # id из каталога запчастей


@dataclass(frozen=True)
class SymptomItem:
    signs: list[str]
    causes: list[str]  # от самой вероятной к менее вероятной
    services: list[str]  # id услуг, первой — диагностика
    danger: str = ""
    ask: list[str] = field(default_factory=list)
    # Причина зависит от деталей (цвет дыма, запах): сначала спросить, потом называть причину
    ask_first: bool = False


@dataclass(frozen=True)
class SymptomSystem:
    id: str
    title: str
    items: list[SymptomItem]


@dataclass(frozen=True)
class ExtraField:
    id: str
    label: str
    description: str = ""
    required: bool = True


@dataclass(frozen=True)
class Business:
    name: str
    niche: str
    city: str
    address: str
    phone: str
    timezone: ZoneInfo
    about: str
    assistant_name: str
    assistant_gender: str
    greeting: str
    quick_replies: list[str]
    demo_note: str
    slot_minutes: int
    posts: int
    horizon_days: int
    min_lead_minutes: int
    demo_busy_ratio: float
    max_bookings_per_session: int
    # 0 = понедельник; None — выходной
    working_hours: dict[int, tuple[time, time] | None]
    services: list[Service]
    extra_fields: list[ExtraField] = field(default_factory=list)
    faq: list[tuple[str, str]] = field(default_factory=list)
    parts: dict[str, Part] = field(default_factory=dict)
    symptoms: dict[str, SymptomSystem] = field(default_factory=dict)

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def parts_for(self, service: Service) -> Part | None:
        return self.parts.get(service.parts) if service.parts else None


def _parse_hours(value: str | None) -> tuple[time, time] | None:
    if not value:
        return None
    start, end = (part.strip() for part in str(value).split("-", 1))
    opens, closes = time.fromisoformat(start), time.fromisoformat(end)
    if closes <= opens:
        raise ValueError(f"Время закрытия раньше открытия: {value}")
    return opens, closes


def load_business(path: Path) -> Business:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    biz, assistant, booking = raw["business"], raw["assistant"], raw["booking"]

    hours_raw = raw.get("working_hours", {})
    working_hours = {i: _parse_hours(hours_raw.get(key)) for i, key in enumerate(WEEKDAY_KEYS)}

    parts: dict[str, Part] = {}
    for part_id, p in (raw.get("parts") or {}).items():
        options = [
            PartOption(
                id=str(o["id"]),
                name=str(o["name"]),
                price_from=int(o["price_from"]),
                note=str(o.get("note", "")),
            )
            for o in p["options"]
        ]
        unknown = {o.id for o in options} - set(PART_OPTION_IDS)
        if unknown:
            raise ValueError(f"Запчасти {part_id}: варианты должны называться {', '.join(PART_OPTION_IDS)}")
        parts[str(part_id)] = Part(id=str(part_id), name=str(p["name"]), options=options)

    services = [
        Service(
            id=str(s["id"]),
            name=str(s["name"]),
            duration_min=int(s["duration_min"]),
            price_from=int(s["price_from"]),
            note=str(s.get("note", "")),
            parts=str(s["parts"]) if s.get("parts") else None,
        )
        for s in raw["services"]
    ]
    ids = [s.id for s in services]
    if len(ids) != len(set(ids)):
        raise ValueError("У услуг повторяются id")
    missing = [s.id for s in services if s.parts and s.parts not in parts]
    if missing:
        raise ValueError(f"Нет запчастей в каталоге для услуг: {', '.join(missing)}")

    symptoms: dict[str, SymptomSystem] = {}
    for system_id, system in (raw.get("symptom_guide") or {}).items():
        items = [
            SymptomItem(
                signs=[str(x) for x in item["signs"]],
                causes=[str(x) for x in item["causes"]],
                services=[str(x) for x in item["services"]],
                danger=str(item.get("danger", "")),
                ask=[str(x) for x in item.get("ask") or []],
                ask_first=bool(item.get("ask_first", False)),
            )
            for item in system["items"]
        ]
        unknown = sorted({sid for item in items for sid in item.services} - set(ids))
        if unknown:
            raise ValueError(f"Справочник неисправностей «{system_id}»: нет услуг {', '.join(unknown)}")
        symptoms[str(system_id)] = SymptomSystem(id=str(system_id), title=str(system["title"]), items=items)

    extra_fields = [
        ExtraField(
            id=str(f["id"]),
            label=str(f["label"]),
            description=str(f.get("description", "")),
            required=bool(f.get("required", True)),
        )
        for f in raw.get("extra_fields") or []
    ]
    reserved = {"service_id", "date", "time", "client_name", "phone", "comment", "summary", "parts_option"}
    clash = reserved & {f.id for f in extra_fields}
    if clash:
        raise ValueError(f"Имена дополнительных полей заняты: {', '.join(sorted(clash))}")

    return Business(
        name=str(biz["name"]),
        niche=str(biz.get("niche", "")),
        city=str(biz.get("city", "")),
        address=str(biz.get("address", "")),
        phone=str(biz.get("phone", "")),
        timezone=ZoneInfo(str(biz.get("timezone", "Europe/Moscow"))),
        about=str(biz.get("about", "")).strip(),
        assistant_name=str(assistant.get("name", "Ассистент")),
        assistant_gender="male" if str(assistant.get("gender", "female")).lower() == "male" else "female",
        greeting=str(assistant["greeting"]).strip(),
        quick_replies=[str(q) for q in assistant.get("quick_replies") or []],
        demo_note=str(assistant.get("demo_note", "")).strip(),
        slot_minutes=int(booking.get("slot_minutes", 30)),
        posts=int(booking.get("posts", 1)),
        horizon_days=int(booking.get("horizon_days", 14)),
        min_lead_minutes=int(booking.get("min_lead_minutes", 60)),
        demo_busy_ratio=float(booking.get("demo_busy_ratio", 0.0)),
        max_bookings_per_session=int(booking.get("max_bookings_per_session", 3)),
        working_hours=working_hours,
        services=services,
        extra_fields=extra_fields,
        faq=[(str(item["q"]), str(item["a"])) for item in raw.get("faq") or []],
        parts=parts,
        symptoms=symptoms,
    )
