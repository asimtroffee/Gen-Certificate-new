from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont as PILImageFont

FONTS_DIR = Path(__file__).resolve().parent / "fonts"

_FONT_PATHS = {
    "Great Vibes": FONTS_DIR / "GreatVibes-Regular.ttf",
    "Inter": FONTS_DIR / "Inter-Regular.ttf",
    "Arial": FONTS_DIR / "Arial.ttf",
}


def _load_font(family: str, size: int) -> PILImageFont.FreeTypeFont:
    path = _FONT_PATHS.get(family, _FONT_PATHS["Great Vibes"])
    if not path.exists():
        path = _FONT_PATHS["Great Vibes"]
    return PILImageFont.truetype(str(path), size)
def _fit_text_font(draw: ImageDraw.ImageDraw, text: str, font: PILImageFont.FreeTypeFont, max_width: float, min_size: int = 10, fallback_factory=None) -> PILImageFont.FreeTypeFont:
    """Scale down the font size if the text exceeds max_width."""
    text_width = draw.textlength(text, font=font)
    if text_width <= max_width:
        return font
    
    ratio = max_width / text_width
    new_size = max(int(font.size * ratio), min_size)
    
    if fallback_factory:
        return fallback_factory(new_size)
    elif hasattr(font, 'path'):
        return PILImageFont.truetype(font.path, new_size)
    else:
        return font


def generate_certificate(
    template: Image.Image,
    config: dict,
    output_format: str = "pdf",
    # New multi-field API (teacher_api)
    data: dict | None = None,
    fonts: dict | None = None,
    # Legacy single-name API (main.py, batch.py)
    name: str | None = None,
    font: PILImageFont.FreeTypeFont | None = None,
) -> bytes:
    """
    Unified certificate generator supporting two calling conventions:

    Multi-field (teacher flow):
        generate_certificate(data=record, fonts=font_factory_dict, config=event_config, template=img, output_format="pdf")

    Single-name (direct / batch flow):
        generate_certificate(name="John", font=pil_font, config=legacy_config_dict, template=img, output_format="png")
    """
    img = template.copy()
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    draw = ImageDraw.Draw(img)

    fields = config.get("fields")

    if fields:
        # ── Multi-field path (teacher flow) ──────────────────────────────────
        record = data or {}

        def get_font(family: str, size: int) -> PILImageFont.FreeTypeFont:
            if fonts and family in fonts:
                return fonts[family](size)
            return _load_font(family, size)

        for field in fields:
            field_id = field.get("id", "")
            text_val = str(record.get(field_id, "")).strip()
            if not text_val:
                continue

            pixel_x = (field.get("x", 50) / 100) * img.width
            pixel_y = (field.get("y", 50) / 100) * img.height
            align = field.get("textAlign", "center")
            anchor_map = {"left": "lm", "center": "mm", "right": "rm"}
            anchor = anchor_map.get(align, "mm")

            font_family = field.get("fontFamily", "Great Vibes")
            fnt = get_font(
                font_family,
                field.get("fontSize", 72),
            )
            
            # Prevent text from exceeding 85% of image width
            max_w = img.width * 0.85
            fnt = _fit_text_font(draw, text_val, fnt, max_w, fallback_factory=lambda size: get_font(font_family, size))

            draw.text(
                (pixel_x, pixel_y),
                text_val,
                font=fnt,
                fill=field.get("fontColor", "#000000"),
                anchor=anchor,
            )

    else:
        # ── Legacy single-name path (main.py / batch.py) ─────────────────────
        text_val = (name or "").strip()
        if not text_val:
            text_val = "Your Name"

        px = (config.get("x", 50) / 100) * img.width
        py = (config.get("y", 50) / 100) * img.height
        align = config.get("textAlign", "center")
        anchor_map = {"left": "lm", "center": "mm", "right": "rm"}
        anchor = anchor_map.get(align, "mm")

        font_family = config.get("fontFamily", "Great Vibes")
        fnt = font or _load_font(font_family, config.get("fontSize", 72))

        # Prevent text from exceeding 85% of image width
        max_w = img.width * 0.85
        fnt = _fit_text_font(draw, text_val, fnt, max_w, fallback_factory=lambda size: _load_font(font_family, size))

        draw.text(
            (px, py),
            text_val,
            font=fnt,
            fill=config.get("fontColor", "#000000"),
            anchor=anchor,
        )

    buffer = BytesIO()
    if output_format == "pdf":
        img = img.convert("RGB")
        img.save(buffer, format="PDF", resolution=300)
    else:
        img.save(buffer, format="PNG")
    return buffer.getvalue()
