"""Проверки боевого демо-конфига и «живой» занятости."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from app.business import load_business
from app.schedule import Scheduler
from app.settings import BASE_DIR
from app.storage import Storage

DEMO = load_business(BASE_DIR / "config" / "business.yaml")
NOW = datetime(2026, 11, 21, 7, 0)


def test_demo_config_has_no_days_off():
    assert all(DEMO.working_hours[day] for day in range(7))


def test_demo_has_diagnostics_for_main_systems():
    ids = {s.id for s in DEMO.services}
    assert {"brake_diag", "suspension_diag", "computer_diag", "inspection"} <= ids


def test_smoke_under_hood_asks_first_and_needs_no_scanner():
    smoke = next(i for i in DEMO.symptoms["engine"].items if any("дым" in s for s in i.signs))
    assert smoke.ask_first and smoke.danger
    assert smoke.services[0] == "inspection"
    assert "computer_diag" not in smoke.services
    assert "антифриз" in smoke.causes[0], "самая частая причина дыма из-под капота — антифриз"


def test_whistle_points_to_drive_belt_not_timing_belt():
    whistle = next(i for i in DEMO.symptoms["engine"].items if any("свист" in s.lower() for s in i.signs))
    assert whistle.ask_first
    assert "приводной" in whistle.causes[0]
    assert "timing_belt" not in whistle.services
    assert whistle.services == ["inspection", "drive_belt"]


def test_demo_services_with_replacement_offer_three_options():
    with_parts = [s for s in DEMO.services if s.parts]
    assert len(with_parts) >= 8
    for service in with_parts:
        part = DEMO.parts_for(service)
        assert [o.id for o in part.options] == ["economy", "optimum", "premium"]
        prices = [o.price_from for o in part.options]
        assert prices == sorted(prices), f"{part.id}: эконом должен быть дешевле премиума"


def test_busy_slots_differ_between_days():
    scheduler = Scheduler(DEMO)
    service = DEMO.service("brake_pads")
    days = [NOW.date() + timedelta(days=i) for i in range(14)]
    free = {d: scheduler.free_starts(d, service, [], NOW) for d in days}
    counts = [len(f) for f in free.values()]
    assert max(counts) - min(counts) >= 6, "загрузка дней должна заметно отличаться"
    assert len({tuple(f) for f in free.values()}) == len(days), "у каждого дня своё расписание"
    assert all(counts), "в каждый день должно оставаться хоть какое-то свободное время"
    # расписание стабильно: повторный расчёт даёт то же самое
    assert scheduler.free_starts(days[3], service, [], NOW) == free[days[3]]


def test_evening_is_busier_than_lunch():
    scheduler = Scheduler(DEMO)
    lunch = evening = 0
    for i in range(60):
        d = date(2026, 12, 1) + timedelta(days=i)
        grid = scheduler._grid(d)
        occupancy = scheduler._demo_busy(d, grid)
        lunch += sum(o for t, o in zip(grid, occupancy, strict=True) if 12 <= t.hour < 15)
        evening += sum(o for t, o in zip(grid, occupancy, strict=True) if 17 <= t.hour < 20)
    assert evening > lunch * 1.3


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
           session_id TEXT NOT NULL, ip_hash TEXT NOT NULL DEFAULT '', service_id TEXT NOT NULL,
           service_name TEXT NOT NULL, price_from INTEGER NOT NULL DEFAULT 0, visit_date TEXT NOT NULL,
           start_time TEXT NOT NULL, end_time TEXT NOT NULL, client_name TEXT NOT NULL, phone TEXT NOT NULL,
           extra_json TEXT NOT NULL DEFAULT '{}', comment TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
           channel TEXT NOT NULL DEFAULT 'web', status TEXT NOT NULL DEFAULT 'new',
           sheet_synced INTEGER NOT NULL DEFAULT 0)"""
    )
    conn.execute(
        "INSERT INTO bookings (created_at, session_id, service_id, service_name, visit_date, start_time, end_time,"
        " client_name, phone) VALUES ('2026-11-21T10:00:00', 's', 'oil', 'Масло', '2026-11-24', '10:00', '11:00',"
        " 'Иван', '79001234567')"
    )
    conn.commit()
    conn.close()

    storage = Storage(path)
    old = storage.recent_bookings()[0]
    assert old.parts_choice == "" and old.estimate_from == 0
    storage.close()
