"""Функции, которые модель вызывает во время диалога.

Модель только понимает клиента и заполняет аргументы. Свободное время,
проверка слота и сохранение записи — здесь, в обычном коде.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from app.business import PART_OPTION_IDS, PART_OPTION_TIERS, Business
from app.llm.base import FunctionSpec
from app.privacy import format_phone, normalize_phone
from app.schedule import Scheduler, date_text, hhmm, pick_slots
from app.storage import Booking, Storage

MAX_SLOTS_SHOWN = 6  # на конкретный день
MAX_SLOTS_PER_DAY_IN_LIST = 3  # когда показываем несколько дней — иначе модель вываливает десятки вариантов
NEAREST_DAYS = 3


@dataclass
class ToolContext:
    """Что знает функция о текущем диалоге."""

    session_id: str
    ip_hash: str
    booking_ids: list[int] = field(default_factory=list)
    ip_bookings_limit: int = 5
    channel: str = "web"


@dataclass
class ToolOutcome:
    result: dict[str, Any]
    booking: Booking | None = None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(value or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


class BookingTools:
    def __init__(
        self,
        business: Business,
        storage: Storage,
        scheduler: Scheduler,
        now_fn: Callable[[], datetime],
    ) -> None:
        self.business = business
        self.storage = storage
        self.scheduler = scheduler
        self.now_fn = now_fn

    # --- описание функций для модели -----------------------------------

    def specs(self) -> list[FunctionSpec]:
        biz = self.business
        now = self.now_fn()
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        service_ids = [s.id for s in biz.services]
        first, second = biz.services[0], biz.services[min(1, len(biz.services) - 1)]

        get_free_slots = FunctionSpec(
            name="get_free_slots",
            description=(
                "Возвращает свободное время для записи на услугу. Вызывай каждый раз, прежде чем "
                "предлагать клиенту время. Если дата не указана, вернёт ближайшие дни со свободным временем."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "service_id": {
                        "type": "string",
                        "enum": service_ids,
                        "description": "id услуги из списка услуг",
                    },
                    "date": {
                        "type": "string",
                        "description": "Дата в формате ГГГГ-ММ-ДД. Не указывай, если клиенту подходит любой ближайший день",
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": "Желаемое время ЧЧ:ММ, если клиент его назвал («после обеда» — 14:00, «вечером» — 18:00)",
                    },
                },
                "required": ["service_id"],
            },
            few_shot_examples=[
                {
                    "request": f"Можно завтра после обеда на «{first.name.lower()}»?",
                    "params": {"service_id": first.id, "date": tomorrow, "preferred_time": "14:00"},
                },
                {
                    "request": f"Когда ближайшее свободное время на «{second.name.lower()}»?",
                    "params": {"service_id": second.id},
                },
            ],
        )

        properties: dict[str, Any] = {
            "service_id": {"type": "string", "enum": service_ids, "description": "id услуги"},
            "date": {"type": "string", "description": "Дата визита ГГГГ-ММ-ДД"},
            "time": {"type": "string", "description": "Время начала ЧЧ:ММ из ответа get_free_slots"},
            "client_name": {"type": "string", "description": "Имя клиента"},
            "phone": {"type": "string", "description": "Телефон клиента как он его написал"},
        }
        required = ["service_id", "date", "time", "client_name", "phone"]
        for extra in biz.extra_fields:
            properties[extra.id] = {"type": "string", "description": f"{extra.label}. {extra.description}".strip()}
            if extra.required:
                required.append(extra.id)
        properties["parts_option"] = {
            "type": "string",
            "enum": [*PART_OPTION_IDS, "own", "master"],
            "description": (
                "Вариант запчастей, который выбрал клиент, из get_part_options: economy, optimum, premium; "
                "own — привезёт свои; master — клиент доверил выбор мастеру. "
                "Обязателен для услуг с запчастями, для остальных не указывай"
            ),
        }
        properties["comment"] = {
            "type": "string",
            "description": "Пожелания и детали от клиента, если есть",
        }
        properties["summary"] = {
            "type": "string",
            "description": "Суть обращения одной фразой для администратора, например «Стук спереди справа на неровностях»",
        }
        required.append("summary")

        create_booking = FunctionSpec(
            name="create_booking",
            description=(
                "Создаёт запись. Вызывай только после того, как клиент подтвердил услугу, дату, время и свои данные."
            ),
            parameters={"type": "object", "properties": properties, "required": required},
        )

        with_parts = [s for s in biz.services if s.parts]
        specs = [get_free_slots, create_booking]
        if biz.symptoms:
            systems = list(biz.symptoms)
            described = "; ".join(f"{k} — {v.title.lower()}" for k, v in biz.symptoms.items())
            specs.insert(
                0,
                FunctionSpec(
                    name="get_symptom_guide",
                    description=(
                        "Справочник неисправностей: признаки, вероятные причины по убыванию вероятности, "
                        "опасно ли ехать, уточняющие вопросы и нужные услуги. Вызывай, как только клиент "
                        "описывает неисправность или симптом, до того как называть причины и услугу."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "system": {
                                "type": "string",
                                "enum": systems,
                                "description": f"Система автомобиля: {described}",
                            }
                        },
                        "required": ["system"],
                    },
                    few_shot_examples=[
                        {"request": request, "params": {"system": system}}
                        for request, system in (
                            ("Горит красный индикатор тормоза и педаль проваливается", "brakes"),
                            ("Стучит спереди на кочках", "suspension"),
                            ("Троит двигатель и горит Check Engine", "engine"),
                        )
                        if system in biz.symptoms
                    ],
                ),
            )
        if with_parts:
            example = with_parts[0]
            specs.insert(
                1,
                FunctionSpec(
                    name="get_part_options",
                    description=(
                        "Возвращает варианты запчастей для услуги (эконом, оптимум, премиум) с ценами "
                        "и итогом «работа + запчасти». Вызывай, когда клиенту нужна замена деталей или жидкостей."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "service_id": {
                                "type": "string",
                                "enum": [s.id for s in with_parts],
                                "description": "id услуги, для которой нужны запчасти",
                            }
                        },
                        "required": ["service_id"],
                    },
                    few_shot_examples=[
                        {
                            "request": f"Какие есть варианты, если нужна «{example.name.lower()}»?",
                            "params": {"service_id": example.id},
                        }
                    ],
                ),
            )
        return specs

    # --- выполнение ------------------------------------------------------

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        if name == "get_free_slots":
            return ToolOutcome(self.get_free_slots(args))
        if name == "get_part_options":
            return ToolOutcome(self.get_part_options(args))
        if name == "get_symptom_guide":
            return ToolOutcome(self.get_symptom_guide(args))
        if name == "create_booking":
            return self.create_booking(args, ctx)
        return ToolOutcome({"error": f"Неизвестная функция {name}"})

    def _services_hint(self) -> list[dict[str, str]]:
        return [{"id": s.id, "name": s.name} for s in self.business.services]

    def _date_label(self, d: date) -> str:
        """«завтра, 23 ноября, понедельник»: без явного «завтра» модели путают дни."""
        relative = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get((d - self.now_fn().date()).days)
        return f"{relative}, {date_text(d)}" if relative else date_text(d)

    def _day_payload(
        self, d: date, slots: list[time], preferred: time | None, limit: int = MAX_SLOTS_SHOWN
    ) -> dict[str, Any]:
        shown = pick_slots(slots, preferred, limit)
        payload: dict[str, Any] = {
            "date": d.isoformat(),
            "date_text": self._date_label(d),
            "free_slots": [hhmm(t) for t in shown],
            "total_free": len(slots),
        }
        if preferred is not None:
            # Модели склонны повторять желаемое время клиента — говорим прямо, свободно ли оно.
            payload["requested_time"] = hhmm(preferred)
            payload["requested_time_free"] = preferred in slots
        return payload

    def _days_near_time(
        self, service: Any, after: date, preferred: time, now: datetime, days: int = 2, window_min: int = 60
    ) -> list[dict[str, Any]]:
        """Ближайшие дни после `after`, где есть время в пределах часа от желаемого."""
        pref = preferred.hour * 60 + preferred.minute
        found: list[dict[str, Any]] = []
        current = after + timedelta(days=1)
        last = self.scheduler.horizon_end(now.date())
        while current <= last and len(found) < days:
            free = self.scheduler.free_starts(current, service, self.storage.day_intervals(current), now) or []
            near = [t for t in free if abs(t.hour * 60 + t.minute - pref) <= window_min]
            if near:
                found.append(
                    {"date": current.isoformat(), "date_text": date_text(current), "free_slots": [hhmm(t) for t in near[:4]]}
                )
            current += timedelta(days=1)
        return found

    def get_free_slots(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.business.service(str(args.get("service_id", "")))
        if service is None:
            return {"error": "Нет такой услуги. Выбери id из списка.", "services": self._services_hint()}

        now = self.now_fn()
        preferred = _parse_time(args.get("preferred_time"))
        base = {
            "service": service.name,
            "duration_min": service.duration_min,
            "price_from": service.price_from,
        }
        lookup = self.storage.day_intervals

        raw_date = args.get("date")
        if not raw_date:
            days = self.scheduler.nearest_days(service, now.date(), now, lookup, NEAREST_DAYS)
            if not days:
                return {**base, "error": "В ближайшие дни свободного времени нет."}
            return {
                **base,
                "days": [self._day_payload(d, slots, preferred, MAX_SLOTS_PER_DAY_IN_LIST) for d, slots in days],
            }

        d = _parse_date(raw_date)
        if d is None:
            return {**base, "error": "Дата не распознана, нужен формат ГГГГ-ММ-ДД."}
        if d < now.date():
            return {**base, "error": "Эта дата уже прошла."}
        horizon = self.scheduler.horizon_end(now.date())
        if d > horizon:
            return {**base, "error": f"Запись открыта только до {date_text(horizon)} включительно."}

        free = self.scheduler.free_starts(d, service, lookup(d), now)
        if free:
            payload = {**base, **self._day_payload(d, free, preferred)}
            if preferred is not None and preferred not in free:
                near = self._days_near_time(service, d, preferred, now)
                if near:
                    # Клиент хотел конкретное время суток — подскажем ближайшие дни, где оно есть.
                    payload["other_days_near_requested_time"] = near
            return payload

        reason = "выходной" if free is None else "нет свободного времени"
        alternatives = self.scheduler.nearest_days(service, d + timedelta(days=1), now, lookup, NEAREST_DAYS)
        return {
            **base,
            "date": d.isoformat(),
            "date_text": self._date_label(d),
            "free_slots": [],
            "reason": reason,
            "alternatives": [
                self._day_payload(ad, slots, preferred, MAX_SLOTS_PER_DAY_IN_LIST) for ad, slots in alternatives
            ],
        }

    def get_symptom_guide(self, args: dict[str, Any]) -> dict[str, Any]:
        system = self.business.symptoms.get(str(args.get("system", "")))
        if system is None:
            return {
                "error": "Нет такой системы в справочнике.",
                "systems": {k: v.title for k, v in self.business.symptoms.items()},
            }
        items = []
        for item in system.items:
            entry: dict[str, Any] = {"signs": item.signs, "likely_causes": item.causes}
            if item.ask_first:
                entry["ask_first"] = True
            if item.danger:
                entry["danger"] = item.danger
            if item.ask:
                entry["ask"] = item.ask
            entry["services"] = [
                {"service_id": s.id, "name": s.name, "price_from": s.price_from}
                for s in (self.business.service(sid) for sid in item.services)
                if s is not None
            ]
            items.append(entry)
        return {
            "system": system.title,
            "items": items,
            "how_to_use": (
                "Найди пункт, чьи признаки совпадают со словами клиента. Если есть danger — начни с него. "
                "Если у пункта ask_first — причина зависит от деталей: задай 1–2 вопроса из ask, не называй "
                "причину и не предлагай время, пока клиент не ответит. Иначе назови 1–2 первые причины из likely_causes в этом порядке "
                "и задай один вопрос из ask. Предлагай первую услугу из services. Если ни один пункт не совпал — "
                "не называй причин наугад: скажи, что нужен осмотр мастером, и предложи осмотр."
            ),
        }

    def get_part_options(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self.business.service(str(args.get("service_id", "")))
        if service is None:
            return {"error": "Нет такой услуги.", "services": self._services_hint()}
        part = self.business.parts_for(service)
        if part is None:
            return {
                "service": service.name,
                "work_price_from": service.price_from,
                "options": [],
                "note": "Для этой услуги запчасти не нужны.",
            }
        return {
            "service": service.name,
            "work_price_from": service.price_from,
            "parts": part.name,
            "options": [
                {
                    "option_id": o.id,
                    "tier": o.tier,
                    "name": o.name,
                    "parts_price_from": o.price_from,
                    "total_from": service.price_from + o.price_from,
                    **({"note": o.note} if o.note else {}),
                }
                for o in part.options
            ],
            "own_parts": "Можно привезти свои запчасти: тогда гарантия только на работу.",
            "prices_note": "Цены ориентировочные, точнее после подбора по VIN.",
        }

    def _parts_choice(self, service_id: str, option_id: str) -> tuple[str, int] | str:
        """(описание выбора, стоимость запчастей) или текст ошибки."""
        service = self.business.service(service_id)
        part = self.business.parts_for(service) if service else None
        if part is None:
            return "", 0
        if not option_id:
            # модель иногда теряет выбор клиента между шагами — пусть укажет явно, а не запишет «без выбора»
            return (
                "Не указан parts_option. Если клиент выбрал вариант запчастей, передай его: economy, optimum, "
                "premium или own (свои). Если не выбирал — спроси его или передай master, если выбор за мастером."
            )
        if option_id == "master":
            return "Не выбраны, подберёт мастер", 0
        if option_id == "own":
            return PART_OPTION_TIERS["own"], 0
        option = part.option(option_id)
        if option is None:
            return f"Нет варианта запчастей {option_id}. Варианты: {', '.join(o.id for o in part.options)}, own."
        return f"{option.tier}: {option.name}", option.price_from

    def create_booking(self, args: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
        biz = self.business

        def fail(message: str, **extra: Any) -> ToolOutcome:
            return ToolOutcome({"ok": False, "error": message, **extra})

        if len(ctx.booking_ids) >= biz.max_bookings_per_session:
            return fail(f"В демо можно сделать не больше {biz.max_bookings_per_session} записей за один диалог.")
        now = self.now_fn()
        if ctx.ip_hash and ctx.ip_bookings_limit > 0:
            recent = self.storage.count_bookings_by_ip_since(ctx.ip_hash, now - timedelta(days=1))
            if recent >= ctx.ip_bookings_limit:
                return fail("Достигнут дневной лимит демо-записей. Попробуйте завтра.")

        service = biz.service(str(args.get("service_id", "")))
        if service is None:
            return fail("Нет такой услуги.", services=self._services_hint())
        d = _parse_date(args.get("date"))
        if d is None:
            return fail("Дата не распознана, нужен формат ГГГГ-ММ-ДД.")
        start = _parse_time(args.get("time"))
        if start is None:
            return fail("Время не распознано, нужен формат ЧЧ:ММ.")

        name = " ".join(str(args.get("client_name", "")).split())[:60]
        if len(name) < 2:
            return fail("Нужно имя клиента.")
        phone = normalize_phone(str(args.get("phone", "")))
        if phone is None:
            return fail("Телефон не похож на российский номер: нужно 10 или 11 цифр.")

        extra: dict[str, str] = {}
        for field_ in biz.extra_fields:
            value = " ".join(str(args.get(field_.id, "")).split())[:120]
            if field_.required and not value:
                return fail(f"Не хватает поля «{field_.label}».")
            if value:
                extra[field_.id] = value

        choice = self._parts_choice(service.id, str(args.get("parts_option") or "").strip().lower())
        if isinstance(choice, str):
            return fail(choice)
        parts_choice, parts_price = choice

        error = self.scheduler.check_slot(d, start, service, self.storage.day_intervals(d), now)
        if error:
            free = self.scheduler.free_starts(d, service, self.storage.day_intervals(d), now) or []
            return fail(error, free_slots=[hhmm(t) for t in pick_slots(free, start, MAX_SLOTS_SHOWN)])

        booking = self.storage.add_booking(
            Booking(
                id=0,
                created_at=now,
                session_id=ctx.session_id,
                ip_hash=ctx.ip_hash,
                service_id=service.id,
                service_name=service.name,
                price_from=service.price_from,
                visit_date=d,
                start_time=start,
                end_time=self.scheduler.end_time(start, service),
                client_name=name,
                phone=phone,
                extra=extra,
                comment=" ".join(str(args.get("comment", "")).split())[:300],
                summary=" ".join(str(args.get("summary", "")).split())[:200],
                channel=ctx.channel,
                parts_choice=parts_choice,
                estimate_from=service.price_from + parts_price,
            )
        )
        ctx.booking_ids.append(booking.id)
        result: dict[str, Any] = {
            "ok": True,
            "booking_number": booking.id,
            "service": service.name,
            "date_text": date_text(d),
            "time": hhmm(start),
            "work_price_from": service.price_from,
            "client_name": name,
            "phone": format_phone(phone),
            **extra,
        }
        if parts_choice:
            result["parts"] = parts_choice
            result["total_from"] = booking.estimate_from
        return ToolOutcome(result, booking=booking)
