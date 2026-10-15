"""Arranque del bot: motor de trading + Telegram + dashboard.

    python main.py                 # abre el dashboard en el navegador
    python main.py --no-browser    # sin abrir el navegador (p. ej. al iniciar con Windows)
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser

from werkzeug.serving import make_server

from botbinance import __version__, logging_setup
from botbinance.config import ConfigStore
from botbinance.dashboard.server import create_app
from botbinance.engine import Engine
from botbinance.state import BotState
from botbinance.util import keep_awake

log = logging.getLogger("botbinance.main")

def main() -> int:
    ap = argparse.ArgumentParser(description="Bot BTC/USDT Top-Down Pullback")
    ap.add_argument("--no-browser", action="store_true", help="no abrir el dashboard en el navegador")
    args = ap.parse_args()

    state = BotState()
    logging_setup.setup(state)
    store = ConfigStore()
    cfg = store.load()
    store.save(cfg)  # deja data/config.json con todos los parámetros visibles
    log.info("Bot BTC/USDT Top-Down Pullback v%s · modo %s", __version__, cfg.mode_label())

    engine = Engine(cfg, store, state)
    app = create_app(engine, state, cfg, store)
    try:
        server = make_server("127.0.0.1", cfg.DASHBOARD_PORT, app, threaded=True)
    except OSError:
        print(f"\n✘ El puerto {cfg.DASHBOARD_PORT} está ocupado: probablemente el bot ya está en ejecución.\n"
              "  Nunca ejecutes dos copias a la vez (duplicarían las operaciones).")
        return 1

    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    url = f"http://127.0.0.1:{cfg.DASHBOARD_PORT}"
    print(f"\n  Dashboard: {url}\n  Para detener el bot: Ctrl+C\n", flush=True)
    if not cfg.SANDBOX_MODE:
        print("  ⚠⚠  MODO CUENTA REAL ACTIVO: el bot arranca en PAUSA; reanúdalo desde el dashboard.\n", flush=True)

    keep_awake(True)
    engine.start()
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDeteniendo el bot…")
    finally:
        # Las órdenes SL/TP viven en los servidores de Binance: seguirán protegiendo la posición.
        engine.stop()
        server.shutdown()
        keep_awake(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
