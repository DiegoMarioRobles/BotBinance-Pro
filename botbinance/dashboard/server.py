"""Dashboard web local (http://127.0.0.1:5000): gráfico, fases de la estrategia, posición y configuración.

Seguridad: escucha SOLO en 127.0.0.1; valida el encabezado Host (anti DNS-rebinding), el Origin y un
token aleatorio por arranque en toda petición que modifica algo (anti CSRF desde otras webs).
Nunca devuelve claves: solo si están guardadas y los últimos 4 caracteres de la API key.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request

from .. import __version__, secrets_store, strategy
from ..binance_api import get_public_ip
from ..config import URL_DEMO, URL_TESTNET
from ..util import CHART_TFS, PULLBACK_TFS, TREND_TFS, TRIGGER_TFS

log = logging.getLogger("botbinance.dashboard")
HERE = Path(__file__).resolve().parent
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def create_app(engine, state, cfg, store) -> Flask:
    app = Flask(__name__, static_folder=str(HERE / "static"), template_folder=str(HERE / "templates"))
    app.json.ensure_ascii = False
    token = secrets.token_urlsafe(24)

    # ------------------------------------------------------------------ seguridad
    @app.before_request
    def guard():
        host = request.host.rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
        if host not in LOCAL_HOSTS:
            abort(403)
        if request.method != "GET":
            if request.headers.get("X-Token") != token:
                abort(403)
            origin = request.headers.get("Origin")
            if origin and (urlparse(origin).hostname or "") not in {"127.0.0.1", "localhost", "::1"}:
                abort(403)

    @app.after_request
    def headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        return resp

    def body() -> dict:
        return request.get_json(silent=True) or {}

    # ------------------------------------------------------------------ páginas
    @app.get("/")
    def index():
        return render_template("index.html", token=token, version=__version__)

    # ------------------------------------------------------------------ estado
    @app.get("/api/state")
    def api_state():
        since = int(request.args.get("since", 0) or 0)
        now = time.time()
        feed = engine.feed
        tg = dict(state.telegram)
        tg["pairing_left"] = max(0, int(tg.get("pairing_expires", 0) - now)) if tg.get("pairing_code") else 0
        return jsonify({
            "now": now,
            "version": __version__,
            "mode": cfg.mode_label(),
            "symbol": cfg.SYMBOL,
            "paused": state.paused,
            "pause_reason": state.pause_reason,
            "trading_ready": state.trading_ready,
            "trading_note": state.trading_note,
            "fatal": state.fatal,
            "booted": engine.booted.is_set(),
            "price": state.price,
            "price_age": (now - state.price_ts) if state.price_ts else None,
            "feed": {"ws": bool(feed and feed.ws_connected), "last_msg_age": (now - feed.last_msg_ts) if feed and feed.last_msg_ts else None},
            "api": state.api,
            "telegram": tg,
            "public_ip": state.public_ip,
            "alerts": [{"key": k, **v} for k, v in state.alerts.items()],
            "evaluation": state.evaluation,
            "board": state.board,
            "last_plan": state.last_plan,
            "position": state.position,
            "trade": state.trade,
            "balance": state.balance,
            "history": state.history[-20:],
            "logs": state.recent_logs(since),
            "trigger_tf": cfg.TF_GATILLO,
            "zone_tf": cfg.TF_ZONA,
            "tf_roles": {"tendencia": cfg.TF_TENDENCIA, "superior": cfg.TF_TENDENCIA_SUPERIOR, "zona": cfg.TF_ZONA,
                         "fibonacci": cfg.TF_FIBONACCI, "gatillo": cfg.TF_GATILLO},
            "strategy": cfg.ESTRATEGIA,
            "strategy_info": cfg.strategy_info(),
            "execution_points": (state.evaluation or {}).get("execution_points") if state.evaluation else None,
            "backtest": {**{k: v for k, v in state.backtest.items() if k != "result"},
                         "has_result": state.backtest.get("result") is not None},
        })

    @app.get("/api/candles")
    def api_candles():
        feed = engine.feed
        tf = request.args.get("tf", cfg.TF_GATILLO)
        if feed is None or tf not in CHART_TFS:
            return jsonify({"series": None, "forming": None, "tfs": CHART_TFS})
        n = max(50, min(int(request.args.get("n", 300) or 300), 1000))
        data = feed.chart_data(tf, n)
        ev = state.evaluation or {}
        trade = state.trade
        ann: dict = {}
        if trade:
            ann["trade"] = {k: trade.get(k) for k in ("side", "entry", "sl", "tp", "liq_price", "be_done")}
        if ev.get("fib") and ev["fib"].get("valid"):
            ann["fib"] = ev["fib"]
        if ev.get("sr"):
            ann["sr"] = ev["sr"]
        if trade and trade.get("levels"):
            ann["levels"] = [{"price": lv["price"], "qty": lv["qty"], "filled": lv["filled"]} for lv in trade["levels"]]
        data["annotations"] = ann
        data["tfs"] = CHART_TFS
        data["tf"] = tf
        data["history"] = [h for h in state.history if h.get("close_time")][-30:]
        return jsonify(data)

    # ------------------------------------------------------------------ configuración
    @app.get("/api/config")
    def api_get_config():
        creds = {}
        for name in (secrets_store.TESTNET_KEY, secrets_store.REAL_KEY):
            v = secrets_store.get(name)
            creds[name] = {"set": bool(v), "mask": secrets_store.mask(v)}
        for name in (secrets_store.TESTNET_SECRET, secrets_store.REAL_SECRET, secrets_store.TELEGRAM_TOKEN):
            creds[name] = {"set": secrets_store.has(name), "mask": ""}
        return jsonify({
            "values": cfg.to_dict(),
            "credentials": creds,
            "backend": secrets_store.backend_name(),
            "test_urls": {"testnet.binancefuture.com": URL_TESTNET, "demo-fapi.binance.com": URL_DEMO},
            "tf_options": {"tendencia": TREND_TFS, "zona": PULLBACK_TFS, "gatillo": TRIGGER_TFS},
            "strategies": strategy.STRATEGIES,
            "active_strategy": cfg.ESTRATEGIA,
        })

    @app.post("/api/config")
    def api_set_config():
        b = body()
        confirm = (b.get("confirm_real") or "").strip().upper() == "OPERAR REAL"
        errors, warnings = engine.apply_config(b.get("values") or {}, confirm_real=confirm)
        return jsonify({"ok": not errors, "errors": errors, "warnings": warnings}), (200 if not errors else 400)

    @app.post("/api/credentials")
    def api_credentials():
        errors = engine.save_credentials(body())
        return jsonify({"ok": not errors, "errors": errors}), (200 if not errors else 400)

    @app.post("/api/test_connection")
    def api_test_connection():
        env = body().get("env", "testnet")
        res = engine.test_connection("real" if env == "real" else "testnet")
        return jsonify(res)

    @app.get("/api/public_ip")
    def api_ip():
        ip = get_public_ip()
        if ip:
            state.public_ip = ip
        return jsonify({"ip": ip})

    # ------------------------------------------------------------------ acciones
    @app.post("/api/pause")
    def api_pause():
        engine.set_paused(True, "Pausado desde el dashboard")
        return jsonify({"ok": True})

    @app.post("/api/resume")
    def api_resume():
        if state.fatal:
            return jsonify({"ok": False, "error": state.fatal}), 400
        engine.set_paused(False)
        return jsonify({"ok": True})

    @app.post("/api/close_position")
    def api_close():
        msg = engine.close_position_now()
        return jsonify({"ok": True, "message": msg})

    # ------------------------------------------------------------------ Telegram
    @app.post("/api/telegram/pair")
    def api_tg_pair():
        if not secrets_store.has(secrets_store.TELEGRAM_TOKEN):
            return jsonify({"ok": False, "error": "Primero guarda el token de tu bot de Telegram."}), 400
        engine.notifier.reload_token()
        info = engine.notifier.test_token()
        if not info.get("ok"):
            return jsonify({"ok": False, "error": f"El token no es válido: {info.get('error')}"}), 400
        code = engine.notifier.new_pairing_code()
        return jsonify({"ok": True, "code": code, "bot": info.get("username", "")})

    @app.post("/api/telegram/test")
    def api_tg_test():
        if not cfg.TELEGRAM_CHAT_ID:
            return jsonify({"ok": False, "error": "Telegram aún no está vinculado."}), 400
        engine.notify("✅ Mensaje de prueba desde el bot.")
        return jsonify({"ok": True})

    @app.post("/api/telegram/unlink")
    def api_tg_unlink():
        engine.notifier.unlink()
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ backtest
    @app.post("/api/backtest/start")
    def api_bt_start():
        err = engine.start_backtest(body())
        return jsonify({"ok": not err, "error": err}), (200 if not err else 400)

    @app.get("/api/backtest/result")
    def api_bt_result():
        return jsonify(state.backtest.get("result"))

    @app.post("/api/backtest/cancel")
    def api_bt_cancel():
        engine.cancel_backtest()
        return jsonify({"ok": True})

    return app
