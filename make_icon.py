"""Genera assets/botbtc.ico (se usa al construir el .exe)."""
from pathlib import Path

from botbinance.icon import draw_icon

out = Path(__file__).parent / "assets" / "botbtc.ico"
out.parent.mkdir(exist_ok=True)
draw_icon(256).save(out, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("Icono generado:", out)
