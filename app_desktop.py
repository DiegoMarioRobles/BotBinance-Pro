"""BotBTC: aplicación de escritorio de Windows (se empaqueta como BotBTC.exe).

Arranca el bot y abre el panel en su PROPIA ventana (Microsoft Edge/Chrome en «modo aplicación», sin barra de
navegador) con un icono en la bandeja del sistema:
  * Cerrar la ventana NO detiene el bot (sigue operando en segundo plano).
  * Icono de la bandeja → «Abrir panel» o «Salir» (detiene el bot; los SL/TP siguen activos en Binance).
Si ya hay otra copia corriendo, solo abre su ventana (nunca se ejecutan dos bots a la vez).

    python app_desktop.py [--port 5000] [--no-window]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import webbrowser

# En un .exe «windowed» no hay consola: sys.stdout / sys.stderr son None y algunas librerías fallarían al escribir.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

from werkzeug.serving import make_server  # noqa: E402

from botbinance import __version__, logging_setup, paths  # noqa: E402
from botbinance.config import ConfigStore  # noqa: E402
from botbinance.dashboard.server import create_app  # noqa: E402
from botbinance.engine import Engine  # noqa: E402
from botbinance.state import BotState  # noqa: E402
from botbinance.util import keep_awake  # noqa: E402

import logging  # noqa: E402

log = logging.getLogger("botbinance.desktop")


def find_browser() -> str | None:
    """Edge viene con Windows 10/11; si no está se usa Chrome."""
    roots = [os.environ.get(v) for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for r in rel:
        for root in filter(None, roots):
            p = os.path.join(root, r)
            if os.path.exists(p):
                return p
    return shutil.which("msedge") or shutil.which("chrome")


def open_window(url: str) -> subprocess.Popen | None:
    """Abre el panel como ventana de aplicación con un perfil aislado (sin tus pestañas, extensiones ni autorrelleno)."""
    browser = find_browser()
    if browser:
        try:
            return subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={paths.WINDOW_PROFILE_DIR}",
                                     "--window-size=1320,900", "--no-first-run", "--no-default-browser-check"])
        except OSError as exc:
            log.warning("No se pudo abrir la ventana de aplicación: %s", exc)
    webbrowser.open(url)
    return None


def run_tray(url: str, win: dict, stop_event: threading.Event) -> bool:
    """Icono en la bandeja. Devuelve False si no está disponible (se usa la ventana como único control)."""
    try:
        import pystray
        from botbinance.icon import draw_icon
    except Exception as exc:  # pragma: no cover
        log.warning("Bandeja del sistema no disponible: %s", exc)
        return False

    def abrir(_icon=None, _item=None):
        p = win.get("proc")
        if p is None or p.poll() is not None:
            win["proc"] = open_window(url)

    def salir(icon, _item):
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(pystray.MenuItem("Abrir panel", abrir, default=True),
                        pystray.MenuItem("Salir (detener el bot)", salir))
    icon = pystray.Icon("BotBTC", draw_icon(64), f"Bot BTC/USDT v{__version__}", menu)
    icon.run()           # bloquea hasta «Salir»
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Bot BTC/USDT (aplicación de escritorio)")
    ap.add_argument("--port", type=int, help="puerto del panel (por defecto el de la configuración)")
    ap.add_argument("--no-window", action="store_true", help="no abrir ventana ni icono (solo pruebas)")
    args = ap.parse_args()

    state = BotState()
    logging_setup.setup(state)
    store = ConfigStore()
    cfg = store.load()
    if args.port:
        cfg.DASHBOARD_PORT = args.port
    else:
        store.save(cfg)
    url = f"http://127.0.0.1:{cfg.DASHBOARD_PORT}"

    engine = Engine(cfg, store, state)
    app = create_app(engine, state, cfg, store)
    try:
        server = make_server("127.0.0.1", cfg.DASHBOARD_PORT, app, threaded=True)
    except OSError:
        log.info("Ya hay una copia del bot en ejecución: solo se abre su ventana.")
        if not args.no_window:
            open_window(url)
        return 0

    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    log.info("Bot BTC/USDT v%s (aplicación) · datos en %s", __version__, paths.ROOT)
    keep_awake(True)
    engine.start()

    stop_event = threading.Event()
    win: dict = {}
    try:
        if args.no_window:
            while not stop_event.wait(1):
                pass
        else:
            win["proc"] = open_window(url)
            if not run_tray(url, win, stop_event):
                # Sin bandeja: la ventana es el control; al cerrarla se detiene el bot.
                p = win.get("proc")
                if p is not None:
                    p.wait()
                else:
                    while not stop_event.wait(1):
                        pass
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        server.shutdown()
        keep_awake(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
