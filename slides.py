"""
Draws Instagram carousel slides (1080x1350, 4:5) for Jackson's educational posts.

    render_carousel(cover_title, kicker, slides, cta_title, cta_text, handle, brand_color, cover_photo=None)
    → list of JPEG bytes: cover, one slide per tip, closing call-to-action slide.
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

BASE = Path(__file__).parent
W, H = 1080, 1350
PAD = 96
CREAM = (247, 242, 235)
INK = (28, 25, 23)
MUTED = (120, 113, 108)
WHITE = (255, 255, 255)


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    name = {"black": "Heebo_800ExtraBold.ttf", "bold": "Heebo_600SemiBold.ttf",
            "regular": "Heebo_400Regular.ttf"}[weight]
    try:
        return ImageFont.truetype(str(BASE / name), size)
    except OSError:
        return ImageFont.load_default(size)


def _hex(color: str) -> tuple[int, int, int]:
    c = (color or "#c2410c").lstrip("#")
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (194, 65, 12)


def _wrap(text: str, font, max_w: int) -> list[str]:
    lines = []
    for para in str(text).split("\n"):
        words, line = para.split(), ""
        for w in words:
            test = f"{line} {w}".strip()
            if font.getlength(test) <= max_w or not line:
                line = test
            else:
                lines.append(line)
                line = w
        lines.append(line)
    return [l for l in lines if l is not None]


def _fit(text: str, weight: str, start: int, min_size: int, max_w: int, max_h: int, spacing=1.18):
    """Largest font size (start → min_size) where the wrapped text fits the box."""
    size = start
    while True:
        font = _font(weight, size)
        lines = _wrap(text, font, max_w)
        height = int(len(lines) * size * spacing)
        if height <= max_h or size <= min_size:
            return font, lines, size
        size -= 4


def _draw_lines(draw, lines, font, size, x, y, fill, spacing=1.18):
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += int(size * spacing)
    return y


def _footer(draw, handle: str, color, page: str | None = None, swipe: bool = False):
    f = _font("bold", 30)
    y = H - PAD - 10
    draw.text((PAD, y), handle, font=f, fill=color, anchor="ls")
    if page:
        draw.text((W - PAD, y), page, font=f, fill=color, anchor="rs")
    if swipe:  # "Swipe" + a drawn arrow (the font has no arrow glyph)
        x_end = W - PAD - int(f.getlength(page or "")) - 28
        draw.polygon([(x_end, y - 11), (x_end - 16, y - 21), (x_end - 16, y - 1)], fill=color)
        draw.rectangle((x_end - 38, y - 13, x_end - 14, y - 9), fill=color)
        draw.text((x_end - 50, y), "Swipe", font=f, fill=color, anchor="rs")


def _to_jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


def _cover(title, kicker, handle, accent, photo: bytes | None, total: int) -> bytes:
    if photo:
        bg = ImageOps.fit(Image.open(io.BytesIO(photo)).convert("RGB"), (W, H))
        bg = bg.filter(ImageFilter.GaussianBlur(1.5))
        shade = Image.new("RGB", (W, H), (10, 8, 6))
        mask = Image.linear_gradient("L").resize((W, H)).point(lambda v: int(70 + v * 0.65))
        img = Image.composite(shade, bg, mask)
    else:
        img = Image.new("RGB", (W, H), accent)
    d = ImageDraw.Draw(img)
    # kicker pill
    kf = _font("bold", 32)
    k = (kicker or "TIPS").upper()
    kw = int(kf.getlength(k)) + 48
    d.rounded_rectangle((PAD, PAD, PAD + kw, PAD + 64), radius=32, fill=WHITE if photo else INK)
    d.text((PAD + 24, PAD + 32), k, font=kf, fill=accent if photo else WHITE, anchor="lm")
    # title, bottom-anchored
    font, lines, size = _fit(title, "black", 104, 60, W - 2 * PAD, 640, 1.08)
    block_h = int(len(lines) * size * 1.08)
    y = H - PAD - 150 - block_h
    _draw_lines(d, lines, font, size, PAD, y, WHITE, 1.08)
    d.rectangle((PAD, H - PAD - 110, PAD + 120, H - PAD - 100), fill=WHITE)
    _footer(d, handle, WHITE, f"1/{total}", swipe=True)
    return _to_jpeg(img)


def _tip(n, headline, body, handle, accent, page) -> bytes:
    img = Image.new("RGB", (W, H), CREAM)
    d = ImageDraw.Draw(img)
    # number badge
    d.ellipse((PAD, PAD, PAD + 120, PAD + 120), fill=accent)
    d.text((PAD + 60, PAD + 62), str(n), font=_font("black", 64), fill=WHITE, anchor="mm")
    top, bottom = PAD + 170, H - PAD - 90
    hf, hl, hs = _fit(headline, "black", 80, 48, W - 2 * PAD, 360, 1.1)
    head_h = int(len(hl) * hs * 1.1)
    bf, bl, bs = _fit(body, "regular", 50, 30, W - 2 * PAD, bottom - top - head_h - 120, 1.42)
    body_h = int(len(bl) * bs * 1.42)
    y = top + max(0, (bottom - top - (head_h + 100 + body_h)) // 2)   # center the text block
    y = _draw_lines(d, hl, hf, hs, PAD, y, INK, 1.1) + 36
    d.rectangle((PAD, y, PAD + 90, y + 8), fill=accent)
    y += 56
    _draw_lines(d, bl, bf, bs, PAD, y, (60, 55, 50), 1.42)
    _footer(d, handle, MUTED, page)
    return _to_jpeg(img)


def _cta(title, text, handle, accent, page) -> bytes:
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 18), fill=accent)
    tf, tl, ts = _fit(title, "black", 92, 56, W - 2 * PAD, 460, 1.08)
    y = 360
    y = _draw_lines(d, tl, tf, ts, PAD, y, WHITE, 1.08) + 40
    bf, bl, bs = _fit(text, "regular", 46, 32, W - 2 * PAD, 300, 1.4)
    y = _draw_lines(d, bl, bf, bs, PAD, y, (214, 211, 209), 1.4) + 60
    btn = "Send us a message"
    f = _font("bold", 40)
    bw = int(f.getlength(btn)) + 80
    d.rounded_rectangle((PAD, y, PAD + bw, y + 96), radius=48, fill=accent)
    d.text((PAD + 40, y + 48), btn, font=f, fill=WHITE, anchor="lm")
    _footer(d, handle, (168, 162, 158), page)
    return _to_jpeg(img)


def render_carousel(cover_title: str, kicker: str, slides: list[dict], cta_title: str, cta_text: str,
                    handle: str, brand_color: str = "#c2410c", cover_photo: bytes | None = None) -> list[bytes]:
    accent = _hex(brand_color)
    slides = [s for s in slides if s.get("headline")][:7]
    total = len(slides) + 2
    out = [_cover(cover_title, kicker, handle, accent, cover_photo, total)]
    for i, s in enumerate(slides, 1):
        out.append(_tip(i, s["headline"], s.get("body", ""), handle, accent, f"{i + 1}/{total}"))
    out.append(_cta(cta_title or "Planning a renovation?",
                    cta_text or "We'd love to help you plan it properly from day one.",
                    handle, accent, f"{total}/{total}"))
    return out
