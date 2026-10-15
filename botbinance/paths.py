"""Rutas del proyecto (todas relativas a la carpeta raíz del bot)."""
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))      # True dentro del .exe (PyInstaller)
if FROZEN:
    # El .exe puede estar en cualquier carpeta (incluso sin permiso de escritura): los datos van al perfil del usuario.
    ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BotBinanceBTC"
else:
    ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
HIST_DIR = DATA_DIR / "hist"
BT_DIR = DATA_DIR / "backtests"

CONFIG_FILE = DATA_DIR / "config.json"
RUNTIME_FILE = DATA_DIR / "runtime_state.json"
TRADE_FILE = DATA_DIR / "trade_state.json"
HISTORY_FILE = DATA_DIR / "trades_history.json"
SECRETS_FALLBACK_FILE = DATA_DIR / "secrets.fallback.json"

WINDOW_PROFILE_DIR = ROOT / "ventana"             # perfil aislado de la ventana de la aplicación

for _d in (DATA_DIR, LOG_DIR, HIST_DIR, BT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
