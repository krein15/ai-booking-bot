from app.privacy import format_phone, mask_name, mask_phone, mask_phones_in_text, normalize_phone


def test_normalize_phone_variants():
    for raw in ("8 (900) 123-45-67", "+7 900 123 45 67", "9001234567", "79001234567"):
        assert normalize_phone(raw) == "79001234567"
    for raw in ("12345", "", "+1 202 555 01234", "0123456789"):
        assert normalize_phone(raw) is None


def test_format_and_mask():
    assert format_phone("79001234567") == "+7 (900) 123-45-67"
    assert mask_phone("79001234567") == "+7 (900) ***-**-67"
    assert mask_name("Иван Петров") == "Иван П."
    assert mask_name("Иван") == "Иван"


def test_mask_phones_in_text():
    text = "Перезвоните на 8 900 123-45-67, после 18"
    assert mask_phones_in_text(text) == "Перезвоните на +7 (900) ***-**-67, после 18"
    assert mask_phones_in_text("Kia Rio 2017 года") == "Kia Rio 2017 года"
