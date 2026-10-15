"""Icono de la aplicación (círculo dorado con una B), generado por código: sin archivos de imagen que perder."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def draw_icon(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, size - 2, size - 2), fill=(240, 185, 11, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.62))
        d.text((size / 2, size / 2 + size * 0.02), "B", fill=(17, 17, 17, 255), font=font, anchor="mm")
    except OSError:
        d.rectangle((size * 0.38, size * 0.25, size * 0.62, size * 0.75), fill=(17, 17, 17, 255))
    return img
