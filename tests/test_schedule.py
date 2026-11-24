from __future__ import annotations

import dataclasses
from datetime import date, time, timedelta

from app.schedule import Scheduler, date_text, pick_slots, spread_pick
from tests.conftest import FIXED_NOW

MONDAY = date(2026, 11, 23)
TUESDAY = date(2026, 11, 24)
SUNDAY = date(2026, 11, 29)


def test_date_text():
    assert date_text(TUESDAY) == "24 ноября, вторник"


def test_today_respects_lead_time(scheduler, business):
    oil = business.service("oil")
    free = scheduler.free_starts(MONDAY, oil, [], FIXED_NOW)
    # сейчас 10:00, запись не раньше чем через час, последний старт — 19:00 (услуга 1 час до 20:00)
    assert free[0] == time(11, 0)
    assert free[-1] == time(19, 0)
    assert len(free) == 17


def test_sunday_is_closed(scheduler, business):
    assert scheduler.free_starts(SUNDAY, business.service("oil"), [], FIXED_NOW) is None


def test_long_service_fits_before_closing(scheduler, business):
    free = scheduler.free_starts(TUESDAY, business.service("timing_belt"), [], FIXED_NOW)
    assert free[0] == time(9, 0)
    assert free[-1] == time(17, 0)


def test_capacity_counts_posts(scheduler, business):
    oil = business.service("oil")
    one = [(time(12, 0), time(13, 0))]
    assert time(12, 0) in scheduler.free_starts(TUESDAY, oil, one, FIXED_NOW)

    both = one * 2  # оба подъёмника заняты 12:00–13:00
    free = scheduler.free_starts(TUESDAY, oil, both, FIXED_NOW)
    assert time(11, 0) in free
    assert time(11, 30) not in free  # 11:30–12:30 пересекается
    assert time(12, 0) not in free
    assert time(12, 30) not in free
    assert time(13, 0) in free


def test_check_slot_errors(scheduler, business):
    oil = business.service("oil")
    assert scheduler.check_slot(TUESDAY, time(12, 0), oil, [], FIXED_NOW) is None
    assert "прошла" in scheduler.check_slot(date(2026, 11, 20), time(12, 0), oil, [], FIXED_NOW)
    assert "только до" in scheduler.check_slot(date(2026, 12, 25), time(12, 0), oil, [], FIXED_NOW)
    assert "выходной" in scheduler.check_slot(SUNDAY, time(12, 0), oil, [], FIXED_NOW)
    assert "занято" in scheduler.check_slot(TUESDAY, time(12, 15), oil, [], FIXED_NOW)
    assert "занято" in scheduler.check_slot(MONDAY, time(10, 30), oil, [], FIXED_NOW)


def test_end_time_rounds_to_grid(scheduler, business):
    assert scheduler.end_time(time(10, 0), business.service("timing_belt")) == time(13, 0)
    assert scheduler.end_time(time(10, 0), business.service("computer_diag")) == time(10, 30)


def test_demo_busy_is_deterministic_and_reduces_slots(business):
    busy = Scheduler(dataclasses.replace(business, demo_busy_ratio=0.4))
    empty = Scheduler(business)
    oil = business.service("oil")
    days = [TUESDAY + timedelta(days=i) for i in range(14) if (TUESDAY + timedelta(days=i)).weekday() != 6]
    busy_total = sum(len(busy.free_starts(d, oil, [], FIXED_NOW)) for d in days)
    empty_total = sum(len(empty.free_starts(d, oil, [], FIXED_NOW)) for d in days)
    assert busy.free_starts(TUESDAY, oil, [], FIXED_NOW) == busy.free_starts(TUESDAY, oil, [], FIXED_NOW)
    assert busy_total < empty_total * 0.9
    assert busy_total > 0


def test_nearest_days_skips_closed(scheduler, business):
    days = scheduler.nearest_days(business.service("oil"), date(2026, 11, 28), FIXED_NOW, lambda d: [], days=2)
    assert [d for d, _ in days] == [date(2026, 11, 28), date(2026, 11, 30)]


def test_pick_slots():
    free = [time(h, m) for h in range(9, 20) for m in (0, 30)]
    assert pick_slots(free, time(14, 0), 4) == [time(13, 0), time(13, 30), time(14, 0), time(14, 30)]
    spread = spread_pick(free, 5)
    assert spread[0] == free[0] and spread[-1] == free[-1] and len(spread) == 5
