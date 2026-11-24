"""Расписание: сетка слотов, занятость и проверка свободного времени.

Все времена — «локальные» naive datetime в часовом поясе бизнеса.
Свободное время считает только этот модуль: модель его не придумывает.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from app.business import Business, Service

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# Длины «чужих» записей в слотах: чаще 1–1,5 часа, иногда до 3 часов
_BLOCK_LENGTHS = (1, 2, 2, 2, 3, 3, 4, 6)


def _hour_weight(hour: int) -> float:
    """Профиль дня: утром и вечером после работы плотнее, в обед свободнее."""
    if hour < 10:
        return 1.25
    if hour < 12:
        return 1.0
    if hour < 15:
        return 0.7
    if hour < 17:
        return 0.9
    if hour < 20:
        return 1.3
    return 0.8


# Занятые интервалы внутри одного дня: (начало, конец)
DayBookings = list[tuple[time, time]]
BookingsLookup = Callable[[date], DayBookings]


def date_text(d: date) -> str:
    """«22 ноября, суббота»."""
    return f"{d.day} {MONTHS_GEN[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def spread_pick(items: list, limit: int) -> list:
    """Равномерно выбирает до limit элементов, чтобы показать и утро, и вечер."""
    if len(items) <= limit:
        return list(items)
    if limit <= 1:
        return items[:1]
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)]


def pick_slots(free: list[time], preferred: time | None, limit: int) -> list[time]:
    """Слоты для показа клиенту: ближайшие к пожеланию или равномерно по дню."""
    if preferred is None:
        return spread_pick(free, limit)
    pref_min = preferred.hour * 60 + preferred.minute
    closest = sorted(free, key=lambda t: (abs(t.hour * 60 + t.minute - pref_min), t))[:limit]
    return sorted(closest)


class Scheduler:
    def __init__(self, business: Business, demo_seed: str = "demo") -> None:
        self.business = business
        self.demo_seed = demo_seed
        self.step = timedelta(minutes=business.slot_minutes)

    # --- базовые вещи -------------------------------------------------

    def hours(self, d: date) -> tuple[time, time] | None:
        return self.business.working_hours.get(d.weekday())

    def horizon_end(self, today: date) -> date:
        return today + timedelta(days=self.business.horizon_days - 1)

    def units(self, service: Service) -> int:
        return max(1, math.ceil(service.duration_min / self.business.slot_minutes))

    def end_time(self, start: time, service: Service) -> time:
        end = datetime.combine(date.min, start) + self.step * self.units(service)
        return end.time()

    def _grid(self, d: date) -> list[datetime] | None:
        hours = self.hours(d)
        if hours is None:
            return None
        current, close = datetime.combine(d, hours[0]), datetime.combine(d, hours[1])
        grid: list[datetime] = []
        while current + self.step <= close:
            grid.append(current)
            current += self.step
        return grid

    def _rng(self, d: date, key: str) -> random.Random:
        digest = hashlib.sha256(f"{self.demo_seed}|{d.isoformat()}|{key}".encode()).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def day_load(self, d: date) -> float:
        """Насколько загружен день относительно среднего: от почти пустого до плотного."""
        factor = self._rng(d, "day").uniform(0.5, 1.5)
        if d.weekday() >= 5:
            factor *= 1.15  # в выходные приезжают чаще
        return factor

    def _demo_busy(self, d: date, grid: list[datetime]) -> list[int]:
        """«Чужие» записи: детерминированы по дате, разные по дням и часам.

        На каждом подъёмнике в каждом свободном слоте с некоторой вероятностью
        начинается запись длиной от 30 минут до 3 часов. Вероятность подобрана
        так, чтобы доля занятого времени была ≈ demo_busy_ratio × загрузка дня
        × профиль часа: при средней длине записи L и загрузке p запись должна
        начинаться в свободном слоте с вероятностью p / (p + L·(1 − p)).
        """
        size = len(grid)
        occupancy = [0] * size
        base = self.business.demo_busy_ratio
        if base <= 0 or size == 0:
            return occupancy
        day_factor = self.day_load(d)
        mean_length = sum(_BLOCK_LENGTHS) / len(_BLOCK_LENGTHS)
        for post in range(self.business.posts):
            rng = self._rng(d, f"post-{post}")
            unit = 0
            while unit < size:
                load = min(0.92, max(0.05, base * day_factor * _hour_weight(grid[unit].hour)))
                if rng.random() < load / (load + mean_length * (1 - load)):
                    length = rng.choice(_BLOCK_LENGTHS)
                    for j in range(unit, min(size, unit + length)):
                        occupancy[j] += 1
                    unit += length
                else:
                    unit += 1
        return occupancy

    def _occupancy(self, d: date, bookings: DayBookings) -> tuple[list[datetime], list[int]] | None:
        grid = self._grid(d)
        if grid is None:
            return None
        occupancy = self._demo_busy(d, grid)
        for start, end in bookings:
            b_start, b_end = datetime.combine(d, start), datetime.combine(d, end)
            for i, slot in enumerate(grid):
                if slot < b_end and slot + self.step > b_start:
                    occupancy[i] += 1
        return grid, occupancy

    # --- публичные методы ----------------------------------------------

    def free_starts(self, d: date, service: Service, bookings: DayBookings, now: datetime) -> list[time] | None:
        """Все свободные времена начала услуги в день d. None — выходной."""
        result = self._occupancy(d, bookings)
        if result is None:
            return None
        grid, occupancy = result
        need = self.units(service)
        earliest = now + timedelta(minutes=self.business.min_lead_minutes)
        free: list[time] = []
        for i in range(len(grid) - need + 1):
            if grid[i] < earliest:
                continue
            if all(occupancy[j] < self.business.posts for j in range(i, i + need)):
                free.append(grid[i].time())
        return free

    def check_slot(
        self, d: date, start: time, service: Service, bookings: DayBookings, now: datetime
    ) -> str | None:
        """Текст ошибки, если записаться нельзя, иначе None."""
        today = now.date()
        if d < today:
            return "Эта дата уже прошла."
        if d > self.horizon_end(today):
            return f"Запись открыта только до {date_text(self.horizon_end(today))} включительно."
        free = self.free_starts(d, service, bookings, now)
        if free is None:
            return f"{date_text(d).capitalize()} — выходной."
        if start not in free:
            return "Это время уже занято или недоступно для этой услуги."
        return None

    def nearest_days(
        self,
        service: Service,
        start_date: date,
        now: datetime,
        bookings_for: BookingsLookup,
        days: int = 3,
    ) -> list[tuple[date, list[time]]]:
        """Ближайшие дни, где есть свободное время на услугу."""
        today = now.date()
        current = max(start_date, today)
        last = self.horizon_end(today)
        found: list[tuple[date, list[time]]] = []
        while current <= last and len(found) < days:
            free = self.free_starts(current, service, bookings_for(current), now)
            if free:
                found.append((current, free))
            current += timedelta(days=1)
        return found
