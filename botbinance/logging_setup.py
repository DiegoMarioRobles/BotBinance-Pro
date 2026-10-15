"""Configuración de logs: consola + archivo rotativo + panel del dashboard."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import paths
from .state import BotState, StateLogHandler


def setup(state: BotState | None = None, level: int = logging.INFO) -> None:
    # La consola de Windows puede no soportar emojis/acentos si la salida está redirigida.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    root = logging.getLogger("botbinance")
    root.setLevel(level)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    if sys.stdout is not None:                      # en el .exe sin consola no hay stdout
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

    file_h = RotatingFileHandler(paths.LOG_DIR / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(file_h)

    if state is not None:
        root.addHandler(StateLogHandler(state))

    # Librerías ruidosas
    for noisy in ("werkzeug", "urllib3", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
