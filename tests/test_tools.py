from __future__ import annotations

import pytest

from app.tools import ToolContext
from tests.conftest import booking_args


def test_symptom_guide(tools):
    result = tools.get_symptom_guide({"system": "brakes"})
    item = result["items"][0]
    assert item["likely_causes"][0] == "утечка тормозной жидкости"
    assert item["danger"] == "Ехать своим ходом опасно."
    assert item["services"][0] == {"service_id": "computer_diag", "name": "Компьютерная диагностика", "price_from": 1000}
    assert "danger" not in tools.get_symptom_guide({"system": "suspension"})["items"][0]
    assert "ask_first" not in item
    assert "наугад" in result["how_to_use"]
    assert "systems" in tools.get_symptom_guide({"system": "rocket"})


def test_symptom_guide_rejects_unknown_services(tmp_path):
    from app.business import load_business
    from tests.conftest import TEST_BUSINESS

    broken = TEST_BUSINESS.read_text(encoding="utf-8").replace("services: [suspension_diag]", "services: [teleport]")
    path = tmp_path / "business.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError, match="teleport"):
        load_business(path)


def test_specs_include_extra_fields_and_enum(tools):
    specs = {s.name: s for s in tools.specs()}
    # порядок как у живого администратора: диагноз → запчасти → время → запись
    assert list(specs) == ["get_symptom_guide", "get_part_options", "get_free_slots", "create_booking"]
    guide = specs["get_symptom_guide"]
    assert guide.parameters["properties"]["system"]["enum"] == ["brakes", "suspension"]
    assert [e["params"]["system"] for e in guide.few_shot_examples] == ["brakes", "suspension"]
    get_slots, parts, create = specs["get_free_slots"], specs["get_part_options"], specs["create_booking"]
    assert get_slots.name == "get_free_slots"
    assert "oil" in get_slots.parameters["properties"]["service_id"]["enum"]
    assert get_slots.few_shot_examples[0]["params"]["date"] == "2026-11-24"
    assert parts.name == "get_part_options"
    assert parts.parameters["properties"]["service_id"]["enum"] == ["oil", "brakes"]
    assert "car" in create.parameters["properties"]
    assert create.parameters["properties"]["parts_option"]["enum"] == ["economy", "optimum", "premium", "own", "master"]
    assert {"car", "phone", "summary"} <= set(create.parameters["required"])
    assert "parts_option" not in create.parameters["required"]


def test_part_options(tools):
    result = tools.get_part_options({"service_id": "brakes"})
    assert [o["option_id"] for o in result["options"]] == ["economy", "optimum", "premium"]
    optimum = result["options"][1]
    assert optimum["tier"] == "Оптимум" and optimum["parts_price_from"] == 2900
    assert optimum["total_from"] == 1500 + 2900
    assert "свои" in result["own_parts"]

    no_parts = tools.get_part_options({"service_id": "computer_diag"})
    assert no_parts["options"] == [] and "не нужны" in no_parts["note"]
    assert "services" in tools.get_part_options({"service_id": "rocket"})


def test_booking_with_parts(tools, ctx):
    outcome = tools.create_booking(booking_args(parts_option="optimum"), ctx)
    assert outcome.result["parts"] == "Оптимум: Синтетика Shell"
    assert outcome.result["total_from"] == 2500 + 3500
    assert outcome.booking.parts_choice == "Оптимум: Синтетика Shell"
    assert outcome.booking.estimate_from == 6000

    own = tools.create_booking(booking_args(time="14:00", parts_option="own"), ctx).booking
    assert own.parts_choice == "Свои запчасти" and own.estimate_from == 2500

    unknown = tools.create_booking(booking_args(time="16:00", parts_option="gold"), ctx)
    assert unknown.result["ok"] is False and "Нет варианта" in unknown.result["error"]


def test_booking_without_parts_choice(tools, ctx):
    # модель потеряла выбор клиента — запись не создаётся, модель должна указать вариант явно
    missing = tools.create_booking(booking_args(parts_option=None), ctx)
    assert missing.booking is None and "parts_option" in missing.result["error"]
    with_parts = tools.create_booking(booking_args(parts_option="master"), ctx).booking
    assert with_parts.parts_choice == "Не выбраны, подберёт мастер"
    no_parts = tools.create_booking(booking_args(service_id="computer_diag", time="15:00"), ctx).booking
    assert no_parts.parts_choice == "" and no_parts.estimate_from == 1000


def test_free_slots_nearest_days(tools):
    result = tools.get_free_slots({"service_id": "oil"})
    assert len(result["days"]) == 3
    assert result["days"][0]["date"] == "2026-11-23"
    # для нескольких дней — не больше трёх вариантов на день, чтобы модель не вываливала десятки
    assert all(0 < len(day["free_slots"]) <= 3 for day in result["days"])
    assert result["price_from"] == 2500


def test_free_slots_for_day_with_preference(tools):
    result = tools.get_free_slots({"service_id": "oil", "date": "2026-11-24", "preferred_time": "18:00"})
    assert result["date_text"] == "завтра, 24 ноября, вторник"
    later = tools.get_free_slots({"service_id": "oil", "date": "2026-11-27"})
    assert later["date_text"] == "27 ноября, пятница"
    assert "18:00" in result["free_slots"]
    assert result["requested_time"] == "18:00" and result["requested_time_free"] is True


def test_busy_requested_time_is_flagged(tools, ctx):
    tools.create_booking(booking_args(time="14:00"), ToolContext("s1", "ip1"))
    tools.create_booking(booking_args(time="14:00"), ToolContext("s2", "ip2"))
    result = tools.get_free_slots({"service_id": "oil", "date": "2026-11-24", "preferred_time": "14:00"})
    assert result["requested_time_free"] is False
    assert "14:00" not in result["free_slots"]
    # подсказка: в среду 14:00 свободно
    near = result["other_days_near_requested_time"][0]
    assert near["date"] == "2026-11-25" and "14:00" in near["free_slots"]


def test_free_slots_closed_day_offers_alternatives(tools):
    result = tools.get_free_slots({"service_id": "oil", "date": "2026-11-29"})
    assert result["free_slots"] == []
    assert result["reason"] == "выходной"
    assert result["alternatives"][0]["date"] == "2026-11-30"


def test_free_slots_bad_input(tools):
    assert "services" in tools.get_free_slots({"service_id": "rocket"})
    assert "прошла" in tools.get_free_slots({"service_id": "oil", "date": "2026-11-01"})["error"]
    assert "формат" in tools.get_free_slots({"service_id": "oil", "date": "завтра"})["error"]


def test_create_booking_success(tools, storage, ctx):
    outcome = tools.create_booking(booking_args(), ctx)
    assert outcome.result["ok"] is True
    assert outcome.result["phone"] == "+7 (900) 123-45-67"
    booking = outcome.booking
    assert booking.id == 1 and booking.phone == "79001234567"
    assert booking.extra == {"car": "Kia Rio 2017"}
    assert str(booking.end_time) == "13:00:00"
    assert ctx.booking_ids == [1]
    assert storage.unsynced_bookings()[0].client_name == "Иван Петров"


def test_slot_is_blocked_when_all_posts_busy(tools):
    first = tools.create_booking(booking_args(), ToolContext("s1", "ip1"))
    second = tools.create_booking(booking_args(), ToolContext("s2", "ip2"))
    third = tools.create_booking(booking_args(), ToolContext("s3", "ip3"))
    assert first.result["ok"] and second.result["ok"]
    assert third.result["ok"] is False
    assert "занято" in third.result["error"]
    assert "12:00" not in third.result["free_slots"]


def test_create_booking_validation(tools, ctx):
    assert "Телефон" in tools.create_booking(booking_args(phone="123"), ctx).result["error"]
    assert "Автомобиль" in tools.create_booking(booking_args(car=""), ctx).result["error"]
    assert "имя" in tools.create_booking(booking_args(client_name=" "), ctx).result["error"]
    assert "Время" in tools.create_booking(booking_args(time="полдень"), ctx).result["error"]
    assert "выходной" in tools.create_booking(booking_args(date="2026-11-29"), ctx).result["error"]


def test_session_and_ip_limits(tools):
    ctx = ToolContext("s1", "ip1", ip_bookings_limit=10)
    for hour in ("10:00", "12:00", "14:00"):
        assert tools.create_booking(booking_args(time=hour), ctx).result["ok"]
    assert "не больше 3" in tools.create_booking(booking_args(time="16:00"), ctx).result["error"]

    limited = ToolContext("s2", "ip1", ip_bookings_limit=3)
    assert "лимит" in tools.create_booking(booking_args(time="16:00"), limited).result["error"]


def test_unknown_function(tools, ctx):
    assert "Неизвестная функция" in tools.execute("drop_tables", {}, ctx).result["error"]
