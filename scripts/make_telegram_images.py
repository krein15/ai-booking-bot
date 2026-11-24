"""Картинки для Telegram-бота в фирменном стиле krein.

    python scripts/make_telegram_images.py

Результат в assets/telegram/:
  avatar.png            640×640  — аватар бота (Telegram обрежет в круг)
  welcome.png           1280×720 — приветственная картинка (What can this bot do?)
  welcome-640x360.png   640×360  — она же в размере, который рекомендует Telegram

Рисуется в 2–3 раза крупнее и уменьшается — так края получаются гладкими.
Шрифты Onest и JetBrains Mono (лицензия OFL) лежат в assets/fonts.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "assets" / "fonts"
OUT = ROOT / "assets" / "telegram"

BG = "#0a0c0f"
TEXT = "#e9ecef"
SUB = "#8a94a0"
ACCENT = "#5cf28e"
INK = "#03130a"
CARD = "#0e1621"
CARD_LINE = "#1d2a38"
OUT_BUBBLE = "#2b5278"
IN_BUBBLE = "#182533"
BUBBLE_TEXT = "#eef2f6"


def font(name: str, size: float, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONTS / name), round(size))
    try:
        axes = f.get_variation_axes()
    except OSError:
        return f
    values = []
    for axis in axes:
        axis_name = axis.get("name", b"")
        axis_name = axis_name.decode() if isinstance(axis_name, bytes) else str(axis_name)
        values.append(weight if "eight" in axis_name else axis["default"])
    f.set_variation_by_axes(values)
    return f


def mix(color: str, base: str, amount: float) -> tuple[int, int, int]:
    """Цвет `color` поверх `base` с непрозрачностью `amount`."""
    c, b = ImageColor.getrgb(color), ImageColor.getrgb(base)
    return tuple(round(b[i] + (c[i] - b[i]) * amount) for i in range(3))


def add_glow(img: Image.Image, center: tuple[float, float], radius: float, color: str, alpha: int) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    r, g, b = ImageColor.getrgb(color)
    cx, cy = center
    ImageDraw.Draw(layer).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(r, g, b, alpha))
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius * 0.45)))


def prompt_glyph(d: ImageDraw.ImageDraw, x: float, y: float, h: float, color: str) -> None:
    """Знак студии «>_»: шеврон и подчёркивание со скруглёнными концами."""
    stroke = h * 0.26
    half = stroke / 2
    top, mid, bottom = (x + half, y + half), (x + h * 0.66, y + h / 2), (x + half, y + h - half)
    d.line([top, mid, bottom], fill=color, width=round(stroke), joint="curve")
    for px, py in (top, bottom, mid):
        d.ellipse((px - half, py - half, px + half, py + half), fill=color)
    ux = x + h * 0.66 + h * 0.2
    d.rounded_rectangle((ux, y + h - h * 0.2, ux + h * 0.6, y + h), radius=h * 0.1, fill=color)


def make_avatar(size: int = 640, scale: int = 3) -> Image.Image:
    n = size * scale
    img = Image.new("RGBA", (n, n), BG)
    add_glow(img, (n * 0.5, n * 0.54), n * 0.38, ACCENT, 70)
    d = ImageDraw.Draw(img)

    # «Пузырь» чата с хвостиком снизу слева — всё в центральном круге
    box = (n * 0.21, n * 0.25, n * 0.79, n * 0.69)
    d.rounded_rectangle(box, radius=n * 0.12, fill=ACCENT)
    d.polygon([(n * 0.31, n * 0.64), (n * 0.25, n * 0.80), (n * 0.46, n * 0.68)], fill=ACCENT)

    h = n * 0.19
    width = h * 1.46
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    prompt_glyph(d, cx - width / 2, cy - h / 2, h, INK)
    return img.convert("RGB").resize((size, size), Image.Resampling.LANCZOS)


def make_welcome(width: int = 1280, height: int = 720, scale: int = 2) -> Image.Image:
    s = lambda v: round(v * scale)  # noqa: E731 — координаты макета в пикселях 1280×720
    img = Image.new("RGBA", (s(width), s(height)), BG)
    add_glow(img, (s(1180), s(40)), s(430), ACCENT, 46)
    add_glow(img, (s(60), s(720)), s(380), "#2b9be0", 30)
    d = ImageDraw.Draw(img)

    # Шапка: ~/krein · демо-бот
    mono = font("JetBrainsMono.ttf", s(30), 600)
    x, y = s(80), s(58)
    for text, color in (("~/", ACCENT), ("krein", TEXT), (" · демо-бот", SUB)):
        d.text((x, y), text, font=mono, fill=color)
        x += d.textlength(text, font=mono)

    d.text((s(80), s(100)), "ИИ-администратор", font=font("Onest.ttf", s(84), 800), fill=TEXT)
    d.text((s(80), s(204)), "Записывает клиентов прямо из переписки", font=font("Onest.ttf", s(38), 400), fill=SUB)

    # Карточка с диалогом
    body = font("Onest.ttf", s(33), 450)
    pad_x, pad_y, gap = s(24), s(15), s(14)
    line_h = s(33 * 1.25)
    card_left, card_right, card_top = s(80), s(1200), s(282)
    messages = [
        ("out", "Стучит спереди. Можно в пятницу после 6?"),
        ("in", "Проверим подвеску. Свободно 18:30 и 19:00"),
        ("out", "18:30, Андрей, Kia Rio 2017"),
    ]
    bubbles = []
    cursor = card_top + s(28)
    for side, text in messages:
        w = d.textlength(text, font=body) + pad_x * 2
        h = line_h + pad_y * 2
        left = card_right - s(28) - w if side == "out" else card_left + s(28)
        bubbles.append((side, text, (left, cursor, left + w, cursor + h)))
        cursor += h + gap

    chip_font = font("JetBrainsMono.ttf", s(28), 600)
    chip_text = "Запись №24 → Google Таблица"
    check_w = s(26)  # галочку рисуем линиями: в JetBrains Mono нет символа ✓
    chip_w = check_w + s(12) + d.textlength(chip_text, font=chip_font) + s(44)
    chip_h = s(28 * 1.3) + s(22)
    chip_top = cursor + s(10)
    card_bottom = chip_top + chip_h + s(26)

    d.rounded_rectangle((card_left, card_top, card_right, card_bottom), radius=s(28), fill=CARD, outline=CARD_LINE, width=s(2))
    for side, text, (l, t, r, b) in bubbles:
        d.rounded_rectangle((l, t, r, b), radius=s(22), fill=OUT_BUBBLE if side == "out" else IN_BUBBLE)
        d.text((l + pad_x, t + pad_y + s(2)), text, font=body, fill=BUBBLE_TEXT)
    chip_left = (card_left + card_right - chip_w) / 2
    d.rounded_rectangle(
        (chip_left, chip_top, chip_left + chip_w, chip_top + chip_h),
        radius=chip_h / 2,
        fill=mix(ACCENT, CARD, 0.1),
        outline=mix(ACCENT, CARD, 0.4),
        width=s(2),
    )
    cx, cy = chip_left + s(22), chip_top + chip_h / 2
    check = [(cx, cy), (cx + check_w * 0.38, cy + check_w * 0.36), (cx + check_w, cy - check_w * 0.42)]
    d.line(check, fill=ACCENT, width=s(4), joint="curve")
    d.text((cx + check_w + s(12), chip_top + s(11)), chip_text, font=chip_font, fill=ACCENT)

    assert card_bottom <= s(height - 24), "диалог не помещается в картинку"
    return img.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_avatar().save(OUT / "avatar.png", optimize=True)
    welcome = make_welcome()
    welcome.save(OUT / "welcome.png", optimize=True)
    welcome.resize((640, 360), Image.Resampling.LANCZOS).save(OUT / "welcome-640x360.png", optimize=True)
    for path in sorted(OUT.glob("*.png")):
        print(f"{path.relative_to(ROOT)}  {Image.open(path).size}")


if __name__ == "__main__":
    main()
