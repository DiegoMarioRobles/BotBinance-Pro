"""Utilidades de tiempo, formato y persistencia JSON."""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Duración de cada temporalidad en milisegundos (1M es aproximada: usar period_ms/next_open con meses reales).
TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
    "12h": 43_200_000, "16h": 57_600_000, "20h": 72_000_000,
    "1d": 86_400_000, "2d": 172_800_000, "3d": 259_200_000, "1w": 604_800_000, "1M": 2_592_000_000,
}
# Binance NO ofrece 16h, 20h ni 2d: se construyen agrupando velas de otra temporalidad (base, nº de velas).
RESAMPLED = {"16h": ("4h", 4), "20h": ("4h", 5), "2d": ("1d", 2)}

# Temporalidades elegibles en cada función
TREND_TFS = ["4h", "8h", "12h", "16h", "20h", "1d", "2d", "3d", "1w", "1M"]          # ¿alcista o bajista?
PULLBACK_TFS = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]       # retrocesos / Fibonacci
TRIGGER_TFS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"]                      # gatillo de entrada
CHART_TFS = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "16h", "20h", "1d", "2d", "3d", "1w", "1M"]

DAY_MS = 86_400_000
# Anclaje (desfase respecto al epoch) de cada temporalidad. La semana de Binance abre el lunes 00:00 UTC (el epoch fue
# jueves). Las demás se registran con register_anchor() a partir de las velas reales (las de 3 días no tienen un
# anclaje fijo).
ANCHORS: dict[str, int] = {"1w": 4 * DAY_MS}


def register_anchor(tf: str, open_time: int) -> None:
    if tf != "1M" and tf not in RESAMPLED:
        ANCHORS[tf] = int(open_time) % TF_MS[tf]


def tf_offset(tf: str) -> int:
    return ANCHORS.get(tf, 0)


def _month_start(ms: int, add: int = 0) -> int:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    m = d.year * 12 + (d.month - 1) + add
    return int(datetime(m // 12, m % 12 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def next_open(tf: str, open_ms: int) -> int:
    """open_time de la vela siguiente a la que abre en `open_ms`."""
    return _month_start(open_ms, 1) if tf == "1M" else open_ms + TF_MS[tf]


def period_ms(tf: str, open_ms: int | None = None) -> int:
    return (next_open(tf, open_ms) - open_ms) if (tf == "1M" and open_ms is not None) else TF_MS[tf]


def last_closed_open_time(tf: str, now_ms: int) -> int:
    """open_time de la última vela CERRADA de la temporalidad `tf` en el instante `now_ms`."""
    if tf == "1M":
        return _month_start(now_ms, -1)
    iv = TF_MS[tf]
    off = tf_offset(tf)
    return ((now_ms - off) // iv) * iv + off - iv


def now_ms() -> int:
    return int(time.time() * 1000)


def keep_awake(on: bool) -> None:
    """Evita que Windows suspenda el PC mientras el bot corre (la pantalla sí puede apagarse)."""
    import ctypes
    import sys
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
    except Exception:  # pragma: no cover
        pass


def fmt_ts(ms: int | float | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Instante en UTC legible."""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(fmt) + " UTC"


def fmt_usd(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "-"
    return f"${x:,.{nd}f}"


def fmt_pct(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "-"
    return f"{x * 100:.{nd}f}%"


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data) -> None:
    """Escritura atómica: evita dejar un JSON a medias si el PC se apaga."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
