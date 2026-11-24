"""Телефоны и маскирование персональных данных для публичной таблицы."""

from __future__ import annotations

import re

_PHONE_IN_TEXT = re.compile(r"(?<!\d)(?:\+?7|8)?[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")


def normalize_phone(raw: str) -> str | None:
    """Российский номер → 11 цифр, начиная с 7. None, если на номер не похоже."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        return "7" + digits[1:]
    if len(digits) == 10 and digits[0] != "0":
        return "7" + digits
    return None


def format_phone(digits: str) -> str:
    """79001234567 → +7 (900) 123-45-67."""
    return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def mask_phone(digits: str) -> str:
    """79001234567 → +7 (900) ***-**-67."""
    if len(digits) != 11:
        return "скрыт"
    return f"+7 ({digits[1:4]}) ***-**-{digits[9:11]}"


def mask_name(name: str) -> str:
    """«Иван Петров» → «Иван П.»: в публичной таблице видно только имя."""
    parts = name.split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0]}."


def mask_phones_in_text(text: str) -> str:
    """Заменяет похожие на телефон последовательности в свободном тексте."""

    def _replace(match: re.Match[str]) -> str:
        digits = normalize_phone(match.group(0))
        return mask_phone(digits) if digits else match.group(0)

    return _PHONE_IN_TEXT.sub(_replace, text or "")
