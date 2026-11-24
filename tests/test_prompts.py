from datetime import datetime

from app.prompts import build_system_prompt


def test_today_is_marked_closed_after_hours(business):
    late = build_system_prompt(business, datetime(2026, 11, 23, 20, 57))  # понедельник, закрыто в 20:00
    assert "понедельник, сегодня: уже закрыто (работали 09:00–20:00)" in late

    open_now = build_system_prompt(business, datetime(2026, 11, 23, 10, 0))
    assert "понедельник, сегодня: 09:00–20:00" in open_now


def test_prompt_lists_services_with_parts_and_rules(business):
    prompt = build_system_prompt(business, datetime(2026, 11, 23, 10, 0))
    assert "oil — Замена масла и фильтров (ТО) — 1 ч — работа от 2 500 ₽ — есть варианты запчастей" in prompt
    assert "get_symptom_guide" in prompt and "ask_first" in prompt
    assert "О себе говори в женском роде." in prompt
