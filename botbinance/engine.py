"""Motor del bot: orquesta datos, estrategia, riesgo, ejecución, Telegram y mantenimiento."""
from __future__ import annotations

import copy
import logging
import queue
import threading
import time

from . import paths, risk, secrets_store, strategy
from .backtester import parse_date, run_backtest, save_result
from .binance_api import (MAINNET_URL, BinanceAPIError, BinanceFutures, NetworkError, SymbolRules,
                          get_public_ip)
from .config import URL_DEMO, URL_TESTNET, ConfigStore, Settings
from .data_feed import DataFeed
from .executor import TradeManager, TradingError
from .notifier import Telegram
from .state import BotState
from .util import fmt_ts, last_closed_open_time, now_ms, read_json, write_json

log = logging.getLogger("botbinance.engine")

CRITICAL_KEYS = {"SANDBOX_MODE", "SYMBOL", "ENTORNO_PRUEBAS_URL"}
FEED_KEYS = {"TF_TENDENCIA", "TF_TENDENCIA_SUPERIOR", "TF_ZONA", "TF_GATILLO", "TF_FIBONACCI", "SYMBOL",
             "BB_PERIODO", "BB_DESVIACION", "ESTRATEGIA"}


class Engine:
    def __init__(self, cfg: Settings, store: ConfigStore, state: BotState):
        self.cfg = cfg
        self.store = store
        self.state = state
        self.data_client = BinanceFutures(MAINNET_URL)       # datos de mercado REALES (sin claves)
        self.trade_client: BinanceFutures | None = None
        self.rules = SymbolRules()
        self.feed: DataFeed | None = None
        self.notifier = Telegram(cfg, state, store, self.handle_command)
        self.tm = TradeManager(cfg, state, self.notify)
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._cache: dict = {}
        self._signal_lock = threading.Lock()
        self._client_lock = threading.RLock()
        self._runtime = read_json(paths.RUNTIME_FILE, {}) or {}
        self._last_signal_time = int(self._runtime.get("last_signal_time", 0))
        self._close_confirm_until = 0.0
        self._alerted: dict[str, float] = {}
        self._threads: list[threading.Thread] = []
        self.booted = threading.Event()

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        self.state.mode = self.cfg.mode_label()
        self.notifier.start()
        th = threading.Thread(target=self._boot, name="engine-boot", daemon=True)
        th.start()
        self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        if self.feed:
            self.feed.stop()
        self.notifier.stop()

    def _boot(self) -> None:
        errors = self.cfg.validate()
        if errors:
            self.state.fatal = "Configuración inválida: " + " | ".join(errors)
            self.state.trading_note = self.state.fatal
            log.error(self.state.fatal)
        # En REAL el bot siempre arranca en pausa: hay que reanudarlo a propósito.
        paused = bool(self._runtime.get("paused", False)) or not self.cfg.SANDBOX_MODE
        self.state.paused = paused
        self.state.pause_reason = "Arranque en modo REAL: reanuda manualmente" if not self.cfg.SANDBOX_MODE else \
            ("Pausado por el usuario" if paused else "")

        while not self._stop.is_set():
            try:
                self.data_client.sync_time()
                self.rules = self.data_client.symbol_rules(self.cfg.SYMBOL)
                self.tm.rules = self.rules
                self.feed = DataFeed(self.data_client, self.cfg, self.on_close)
                self.feed.warmup()
                break
            except (BinanceAPIError, NetworkError, KeyError) as exc:
                self.state.trading_note = f"Sin datos de mercado: {exc}. Reintentando…"
                log.error("Arranque: %s. Reintento en 10 s", exc)
                self._stop.wait(10)
        if self._stop.is_set() or self.feed is None:
            return

        price = self.feed.last_price or 0.0
        self.state.price = price
        if not errors:
            self._refresh_board(price)
            print("\n" + self.state.board + "\n", flush=True)
        self.feed.start()
        self.rebuild_trade_client()
        self._evaluate_status()
        self.booted.set()
        for target, name in ((self._worker, "engine-worker"), (self._monitor, "engine-monitor"),
                             (self._maintenance, "engine-maint")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        estado = ("EN PAUSA" if self.state.paused else
                  "operando" if self.state.trading_ready else f"solo monitoreo ({self.state.trading_note})")
        log.info("Bot listo. Modo %s · %s", self.cfg.mode_label(), estado)
        self.notify(f"🤖 Bot iniciado en modo {self.cfg.mode_label()}"
                    f"{' (en pausa)' if self.state.paused else ''}. Precio BTC: ${price:,.2f}")

    # ------------------------------------------------------------------ utilidades
    def notify(self, text: str) -> None:
        log.info("Telegram ▶ %s", text.replace("\n", " | ")[:300])
        self.notifier.send(text)

    def _save_runtime(self, **kw) -> None:
        self._runtime.update(kw)
        write_json(paths.RUNTIME_FILE, self._runtime)

    def _once_per(self, key: str, seconds: float) -> bool:
        now = time.time()
        if now - self._alerted.get(key, 0) >= seconds:
            self._alerted[key] = now
            return True
        return False

    def _refresh_board(self, price: float, plan=None) -> None:
        if price > 0:
            self.state.board = risk.format_board(self.cfg, price, self.rules, plan)

    # ------------------------------------------------------------------ cliente de trading
    def rebuild_trade_client(self) -> None:
        with self._client_lock:
            key, secret = secrets_store.credentials_for(self.cfg.SANDBOX_MODE)
            self.trade_client = BinanceFutures(self.cfg.trade_base_url(), key, secret)
            self.tm.client = self.trade_client
            self.state.mode = self.cfg.mode_label()
            if not self.trade_client.has_keys:
                self.state.trading_ready = False
                self.state.trading_note = (f"Faltan las API keys de {self.cfg.mode_label()}. "
                                           "Ve a ⚙ Configuración. Mientras tanto el bot solo monitorea el mercado.")
                self.state.set_alert("keys", self.state.trading_note, "info")
                self.state.api.update(ok=None, error="sin claves")
                return
            self.state.set_alert("keys", None)
            if self._check_account():
                try:
                    self.tm.reconcile_on_start()
                except (BinanceAPIError, NetworkError) as exc:
                    log.error("No pude reconciliar el estado: %s", exc)

    def _check_account(self) -> bool:
        c = self.trade_client
        if c is None or not c.has_keys:
            return False
        res = c.check_credentials(self.cfg.SYMBOL, verify_trade=False)
        self.state.api.update(ok=res["ok"], error=res.get("error", ""), checked=time.time())
        if res["ok"]:
            self.state.set_alert("ip", None)
            self.state.balance = {"wallet": res["wallet"], "available": res["available"]}
            if res.get("hedge_mode"):
                self.state.trading_ready = False
                self.state.trading_note = "La cuenta está en Hedge Mode; cámbiala a One-way Mode."
                self.state.set_alert("hedge", self.state.trading_note, "error")
            else:
                self.state.set_alert("hedge", None)
                self.state.trading_ready = not self.cfg.validate()
                self.state.trading_note = "Listo para operar" if self.state.trading_ready else self.state.fatal
            return True
        self.state.trading_ready = False
        if res.get("auth"):
            ip = self.state.public_ip or get_public_ip()
            self.state.public_ip = ip
            msg = (f"Binance rechazó la conexión ({res.get('code')}): clave inválida, sin permiso de Futuros o IP no "
                   f"autorizada. IP pública de este PC: {ip or 'desconocida'} → añádela a la whitelist de la clave.")
            self.state.set_alert("ip", msg, "error")
            self.state.trading_note = msg
            if self._once_per("auth", 3600):
                self.notify("🚫 " + msg)
        else:
            self.state.trading_note = f"No hay conexión con Binance: {res.get('error')}"
        return False

    # ------------------------------------------------------------------ eventos de velas
    def on_close(self, tf: str) -> None:
        self._q.put((tf, now_ms()))

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                tf, _ = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._on_candle(tf)
            except Exception:
                log.exception("Error evaluando la estrategia (%s)", tf)

    def _evaluate_status(self) -> strategy.Evaluation | None:
        if not self.feed:
            return None
        ev = strategy.evaluate(self.feed.snapshot(), self.cfg, self._cache, with_sr=True)
        self.state.evaluation = ev.to_dict()
        return ev

    def _sync_frames(self, timeout: float = 12.0) -> bool:
        """Espera a que TODAS las temporalidades tengan su última vela cerrada.

        Cada 4 h cierran a la vez 1h y 4h (y a las 00:00 UTC también 1d, los lunes 1w). Si el evento de 1h se
        evaluara antes de recibir la vela de 4h recién cerrada, la Fase 2 usaría una zona desfasada y el bot
        se comportaría distinto al backtest. Si el WebSocket se retrasa, se pide por REST.
        """
        feed = self.feed
        if feed is None or not hasattr(feed, "raw"):
            return True
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            now = self.data_client.now_ms()
            stale = []
            for tf in feed.timeframes:
                df = feed.raw.get(tf)
                if df is None or df.empty or int(df["open_time"].iloc[-1]) < last_closed_open_time(tf, now):
                    stale.append(tf)
            if not stale:
                return True
            for tf in stale:
                feed.refresh_tf(tf)
            self._stop.wait(0.8)
        log.warning("Evalúo sin tener todas las velas cerradas al día (pendientes: %s)", stale)
        return False

    def _on_candle(self, tf: str) -> None:
        if tf == self.cfg.TF_GATILLO:
            try:
                self._sync_frames()
            except Exception:
                log.exception("No se pudo sincronizar las temporalidades")
        ev = self._evaluate_status()
        if ev is None:
            return
        self._log_evaluation(tf, ev)
        if tf == self.cfg.TF_GATILLO and ev.signal:
            self._handle_signal(ev)

    def _log_evaluation(self, tf: str, ev: strategy.Evaluation) -> None:
        stamp = fmt_ts(ev.close_time + 1, "%Y-%m-%d %H:%M")      # close_time de Binance termina en :59.999
        if tf != self.cfg.TF_GATILLO:
            log.info("Vela %s cerrada · tendencia macro: %s · %s", tf, ev.trend, ev.note)
            return
        lines = [f"━━ Análisis al cierre de {tf} ({stamp}) · precio ${ev.price:,.2f}"]
        mark = lambda v: "✔" if v else "✘"  # noqa: E731
        if ev.phase1:
            lines.append(f"  FASE 1 · tendencia macro → {ev.trend}")
            for c in ev.phase1:
                lines.append(f"     L{mark(c.long)} S{mark(c.short)}  {c.name}: {c.value}")
        for title, checks, ok in (("FASE 2 · zona de valor", ev.phase2, ev.phase2_ok),
                                  ("FASE 3 · gatillo", ev.phase3, ev.phase3_ok)):
            if checks:
                lines.append(f"  {title} → {'CUMPLIDA' if ok else 'no cumplida'}")
                for c in checks:
                    lines.append(f"     {mark(c.ok)}  {c.name}: {c.value}")
        lines.append(f"  → {ev.note}")
        log.info("\n".join(lines))

    # ------------------------------------------------------------------ señales
    def _gate(self) -> str:
        """Motivo por el que NO se puede operar ahora (cadena vacía si todo está listo)."""
        if self.state.fatal:
            return "configuración inválida"
        if self.state.paused:
            return "el bot está en pausa"
        if not self.state.trading_ready:
            return self.state.trading_note or "sin conexión de trading"
        if self.tm.busy():
            return "ya hay una posición abierta"
        return ""

    def _handle_signal(self, ev: strategy.Evaluation) -> None:
        sig = ev.signal
        assert sig is not None
        with self._signal_lock:
            if sig.open_time <= self._last_signal_time:
                return
            self._last_signal_time = sig.open_time
            self._save_runtime(last_signal_time=sig.open_time)

        age = (self.data_client.now_ms() - sig.close_time) / 1000.0
        log.info("🎯 SEÑAL %s · %s · %s", sig.side, sig.pattern, sig.exhaustion)
        price = (self.feed.last_price if self.feed else None) or sig.close
        avail = (self.state.balance or {}).get("available")
        plan = risk.build_plan(self.cfg, sig, price, self.rules, avail)
        self._refresh_board(price, plan)
        self.state.last_plan = plan.to_dict()
        log.info("\n%s", self.state.board)

        if age > self.cfg.MAX_RETRASO_SENAL_SEG:
            log.warning("Señal descartada: llegó con %.0f s de retraso (máx. %d s).", age, self.cfg.MAX_RETRASO_SENAL_SEG)
            return
        head = (f"📡 SEÑAL {sig.side} detectada\n{sig.pattern}\n{sig.exhaustion}\n"
                f"Precio ${price:,.2f} · SL ${plan.sl_price:,.2f} ({plan.dist_sl * 100:.2f}%) · TP ${plan.tp_price:,.2f}")
        if plan.martingale or (self.cfg.MARTINGALA_ACTIVA and plan.notes):
            head += "\n" + (plan.notes[0] if plan.notes else "")
        if not plan.approved:
            log.warning("Orden bloqueada por el módulo de riesgo: %s", plan.blocked_reason)
            if self.cfg.TELEGRAM_AVISAR_SENALES_BLOQUEADAS:
                self.notify(f"{head}\n⛔ {plan.blocked_reason}")
            return
        gate = self._gate()
        if gate:
            log.warning("Señal aprobada por riesgo pero NO operada: %s", gate)
            self.notify(f"{head}\n⏸ No se opera: {gate}.")
            return
        try:
            self.tm.open_trade(plan, sig)
        except TradingError as exc:
            log.error("No se pudo abrir la operación: %s", exc)
            self.notify(f"{head}\n❌ {exc}")
        except BinanceAPIError as exc:
            log.error("Binance rechazó la operación: %s", exc)
            self.notify(f"{head}\n❌ Binance rechazó la orden: [{exc.code}] {exc.msg}")
            if exc.is_auth:
                self._check_account()
        except NetworkError as exc:
            log.error("Error de red al operar: %s", exc)
            self.notify(f"{head}\n❌ Error de red: {exc}")

    # ------------------------------------------------------------------ monitoreo de la posición
    def _monitor(self) -> None:
        while not self._stop.is_set():
            wait = self.cfg.MONITOR_INTERVALO_SEG if self.tm.trade is not None else 10.0
            if self._stop.wait(wait):
                break
            if not (self.trade_client and self.trade_client.has_keys and self.state.trading_ready):
                continue
            try:
                self.tm.monitor_tick()
            except BinanceAPIError as exc:
                log.error("Monitor: %s", exc)
                if exc.is_auth:
                    self._check_account()
            except NetworkError as exc:
                log.warning("Monitor sin red: %s", exc)
            except Exception:
                log.exception("Error en el monitor de posición")

    # ------------------------------------------------------------------ mantenimiento
    def _maintenance(self) -> None:
        tick = 0
        while not self._stop.wait(15):
            tick += 1
            try:
                feed = self.feed
                if feed:
                    self.state.price = feed.last_price
                    self.state.price_ts = feed.last_price_ts
                    self.state.feed.update(ws=feed.ws_connected, last_msg=feed.last_msg_ts,
                                           variant=feed.variant % 2)
                # IP pública, cada 5 minutos
                if tick == 1 or tick % 20 == 0:
                    self._check_ip()
                # Reloj
                if tick % 40 == 0:
                    for c in (self.data_client, self.trade_client):
                        if c is not None:
                            try:
                                c.sync_time()
                            except Exception:
                                pass
                # Cuenta: si estaba bloqueada, reintenta cada minuto (se recupera sola al arreglar la whitelist)
                if self.trade_client and self.trade_client.has_keys:
                    if not self.state.trading_ready and tick % 4 == 0 and not self.state.fatal:
                        self._check_account()
                    elif self.state.trading_ready and tick % 4 == 0:
                        self._refresh_balance()
                # Código de vinculación caducado
                tg = self.state.telegram
                if tg.get("pairing_code") and time.time() > tg.get("pairing_expires", 0):
                    tg.update(pairing_code="", pairing_expires=0.0)
            except Exception:
                log.exception("Error en el mantenimiento")

    def _refresh_balance(self) -> None:
        try:
            b = self.trade_client.usdt_balance()  # type: ignore[union-attr]
            self.state.balance = {"wallet": b["wallet"], "available": b["available"]}
            self.state.api.update(ok=True, error="", checked=time.time())
        except BinanceAPIError as exc:
            if exc.is_auth:
                self._check_account()
        except NetworkError:
            pass

    def _check_ip(self) -> None:
        ip = get_public_ip()
        if not ip:
            return
        old = self._runtime.get("last_ip", "")
        self.state.public_ip = ip
        if old and old != ip:
            log.warning("La IP pública cambió: %s → %s", old, ip)
            self.notify(f"⚠️ Tu IP pública cambió de {old} a {ip}. Si la clave de Binance usa whitelist, "
                        "actualízala o el bot dejará de poder operar.")
            self.state.set_alert("ipchange", f"Tu IP pública cambió ({old} → {ip}). Actualiza la whitelist de tu API key en Binance.", "warn")
        elif old == ip:
            self.state.set_alert("ipchange", None)
        self._save_runtime(last_ip=ip)

    # ------------------------------------------------------------------ acciones del usuario
    def set_paused(self, paused: bool, reason: str = "") -> None:
        self.state.paused = paused
        self.state.pause_reason = reason if paused else ""
        self._save_runtime(paused=paused if self.cfg.SANDBOX_MODE else False)
        log.info("Bot %s", "EN PAUSA" if paused else "REANUDADO")
        self.notify("⏸ Bot en pausa: no abrirá nuevas operaciones (las abiertas siguen protegidas)." if paused
                    else "▶️ Bot reanudado.")

    def apply_config(self, values: dict, confirm_real: bool = False) -> tuple[list[str], list[str]]:
        """Valida y aplica cambios de parámetros. Devuelve (errores, avisos)."""
        warnings: list[str] = []
        tmp = copy.copy(self.cfg)
        errors = tmp.apply(values)
        errors += tmp.validate()
        if errors:
            return errors, warnings
        changed = {k for k, v in tmp.to_dict().items() if v != getattr(self.cfg, k)}
        if not changed:
            return [], ["No hay cambios que guardar."]
        busy = self.tm.trade is not None or self.state.foreign_position is not None
        if busy and (changed & CRITICAL_KEYS):
            return ["No se puede cambiar el modo, el par ni el entorno mientras haya una posición abierta."], warnings
        if "SANDBOX_MODE" in changed and not tmp.SANDBOX_MODE and not confirm_real:
            return ["Para pasar a la cuenta REAL debes confirmarlo escribiendo OPERAR REAL."], warnings

        self.cfg.apply({k: getattr(tmp, k) for k in changed})
        self.store.save(self.cfg)
        log.info("Configuración actualizada: %s", ", ".join(sorted(changed)))
        if "SANDBOX_MODE" in changed:
            self.state.paused = True       # al cambiar de modo siempre se vuelve a pausar
            self.state.pause_reason = "Cambio de modo: reanuda manualmente"
            self._save_runtime(paused=False)
            self.notify(f"🔁 Modo cambiado a {self.cfg.mode_label()}. El bot quedó en pausa hasta que lo reanudes.")
        if changed & (CRITICAL_KEYS | {"APALANCAMIENTO"}):
            threading.Thread(target=self.rebuild_trade_client, daemon=True).start()
        if changed & FEED_KEYS:
            warnings.append("Se cambió una temporalidad: recargo el historial de datos.")
            threading.Thread(target=self._restart_feed, daemon=True).start()
        if changed & {"TELEGRAM_ACTIVO"}:
            self.notifier.reload_token()
        self._cache.clear()
        if self.feed and self.feed.last_price:
            self._refresh_board(self.feed.last_price)
        self._evaluate_status()
        return [], warnings

    def _restart_feed(self) -> None:
        try:
            old = self.feed
            if old:
                old.stop()
            feed = DataFeed(self.data_client, self.cfg, self.on_close)
            feed.warmup()
            self.feed = feed
            feed.start()
            self._evaluate_status()
        except Exception:
            log.exception("No se pudo reiniciar el feed de datos")

    def save_credentials(self, payload: dict) -> list[str]:
        """Guarda claves (los campos vacíos no se tocan). Devuelve errores."""
        allowed = {
            "testnet_api_key": secrets_store.TESTNET_KEY, "testnet_api_secret": secrets_store.TESTNET_SECRET,
            "real_api_key": secrets_store.REAL_KEY, "real_api_secret": secrets_store.REAL_SECRET,
            "telegram_token": secrets_store.TELEGRAM_TOKEN,
        }
        active = ("testnet_api_key", "testnet_api_secret") if self.cfg.SANDBOX_MODE else ("real_api_key", "real_api_secret")
        clearing = payload.get("clear") or []
        if (self.tm.trade is not None or self.state.foreign_position is not None) and any(
                (payload.get(k) or "").strip() or k in clearing for k in active):
            return ["No se pueden cambiar las claves del entorno activo mientras haya una posición abierta."]
        new = {k: (payload.get(k) or "").strip() for k in allowed}
        for k, v in new.items():
            if v and (len(v) < 20 or " " in v):
                return [f"{k}: el valor no parece válido (¿lo copiaste completo y sin espacios?)."]
        # Las claves de Testnet y de la cuenta REAL nunca pueden coincidir (señal de que se pegaron en el recuadro equivocado).
        for a, b in (("testnet_api_key", "real_api_key"), ("testnet_api_secret", "real_api_secret")):
            va = new[a] or secrets_store.get(allowed[a])
            vb = new[b] or secrets_store.get(allowed[b])
            if va and vb and va == vb:
                return ["Las claves de TESTNET y las de la cuenta REAL son idénticas. Cada entorno tiene sus propias "
                        "claves: borra las que pegaste en el recuadro equivocado."]
        touched = False
        for k, name in allowed.items():
            if new[k]:
                secrets_store.set(name, new[k])
                touched = True
        for k in payload.get("clear", []) or []:
            if k in allowed:
                secrets_store.set(allowed[k], "")
                touched = True
        if touched:
            self.notifier.reload_token()
            threading.Thread(target=self.rebuild_trade_client, daemon=True).start()
            log.info("Credenciales actualizadas (solo se guardan en el Administrador de credenciales de Windows)")
        return []

    def test_connection(self, env: str) -> dict:
        """Prueba de conexión del entorno 'testnet' o 'real' con las claves guardadas."""
        sandbox = env != "real"
        key, secret = secrets_store.credentials_for(sandbox)
        if not key or not secret:
            return {"ok": False, "error": f"No hay claves guardadas para {'TESTNET' if sandbox else 'REAL'}."}
        urls = [self.cfg.ENTORNO_PRUEBAS_URL] if not sandbox else \
            [self.cfg.ENTORNO_PRUEBAS_URL] + [u for u in (URL_TESTNET, URL_DEMO) if u != self.cfg.ENTORNO_PRUEBAS_URL]
        if not sandbox:
            urls = [MAINNET_URL]
        busy = self.tm.trade is not None or self.state.foreign_position is not None
        res: dict = {}
        for i, url in enumerate(urls):
            client = BinanceFutures(url, key, secret)
            res = client.check_credentials(self.cfg.SYMBOL, leverage=self.cfg.APALANCAMIENTO,
                                           verify_trade=not busy)
            res["url"] = url
            if res["ok"]:
                if sandbox and url != self.cfg.ENTORNO_PRUEBAS_URL:
                    res["suggest_url"] = url
                if not sandbox:
                    restr = client.api_restrictions()
                    if restr:
                        res["restrictions"] = {
                            "futures": restr.get("enableFutures"), "withdrawals": restr.get("enableWithdrawals"),
                            "ip_restrict": restr.get("ipRestrict"),
                        }
                break
            if not res.get("auth"):
                break
        res["public_ip"] = self.state.public_ip or get_public_ip()
        if res.get("ok") and res.get("trade_ok") is None and not busy:
            res["trade_ok"] = None
        return res

    def close_position_now(self) -> str:
        return self.tm.close_now()

    # ------------------------------------------------------------------ backtest (desde el dashboard)
    def start_backtest(self, p: dict) -> str:
        """Lanza un backtest en segundo plano. Devuelve un mensaje de error o '' si arrancó."""
        bt = self.state.backtest
        if bt.get("running"):
            return "Ya hay un backtest en ejecución."
        try:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            raw_desde = str(p.get("desde") or "2022-01-01").strip()
            raw_hasta = str(p.get("hasta") or today).strip()
            desde = parse_date(raw_desde)
            hasta = min(parse_date(raw_hasta) + 86_400_000 - 1, now_ms())
        except ValueError:
            return "Fechas inválidas (usa formato AAAA-MM-DD)."
        if desde >= hasta:
            return "La fecha inicial debe ser anterior a la final."
        c = copy.copy(self.cfg)
        mart = {"si": True, "no": False}.get(str(p.get("martingala", "")).strip().lower())
        over = {
            "ESTRATEGIA": p.get("estrategia"),
            "APALANCAMIENTO": p.get("apalancamiento"),
            "TF_TENDENCIA": p.get("tendencia"), "TF_TENDENCIA_SUPERIOR": p.get("superior"),
            "TF_ZONA": p.get("zona"), "TF_GATILLO": p.get("gatillo"), "VENTANA_AGOTAMIENTO": p.get("ventana"),
            "MARTINGALA_ACTIVA": mart, "MARTINGALA_NIVELES": p.get("niveles"),
            "MARTINGALA_MULTIPLICADOR": p.get("multiplicador"), "MARTINGALA_RIESGO_MAX_USDT": p.get("riesgo_mart"),
            "MARTINGALA_FUENTE": p.get("fuente"), "MARGEN_EN_USO_POR_OPERACION": p.get("margen"),
        }
        # "__cfg__" / vacío = usar la configuración; la superior admite "" (= ninguna) de forma explícita.
        errs = c.apply({k: v for k, v in over.items()
                        if v is not None and v != "__cfg__" and (v != "" or k == "TF_TENDENCIA_SUPERIOR")})
        errs += c.validate()
        if errs:
            return "; ".join(errs)
        try:
            raw_slip = str(p.get("slippage", "0.0002") or "0.0002").strip().replace(",", ".")
            slip = min(max(float(raw_slip), 0.0), 0.01)
        except (TypeError, ValueError):
            return "Slippage inválido (usa valores como 0.0002)."
        self._bt_cancel = threading.Event()
        bt.update(running=True, progress=0.01, message="Conectando con Binance para descargar historial…", result=None, error="")

        def progress(frac: float, msg: str) -> None:
            bt.update(progress=round(frac, 3), message=msg)

        def job() -> None:
            try:
                client = BinanceFutures(MAINNET_URL)
                client.sync_time()
                rules = client.symbol_rules(c.SYMBOL)
                res = run_backtest(c, client, desde, hasta, slip, progress, self._bt_cancel, rules)
                path = save_result(res)
                bt.update(result=res, message=f"Listo. Guardado en {path}", progress=1.0)
            except InterruptedError:
                bt.update(error="Backtest cancelado.")
            except Exception as exc:  # pragma: no cover
                log.exception("Backtest falló")
                bt.update(error=f"{type(exc).__name__}: {exc}")
            finally:
                bt.update(running=False)

        threading.Thread(target=job, name="backtest", daemon=True).start()
        return ""

    def cancel_backtest(self) -> None:
        ev = getattr(self, "_bt_cancel", None)
        if ev is not None:
            ev.set()

    # ------------------------------------------------------------------ Telegram
    def handle_command(self, cmd: str, args: list[str]) -> str:
        s = self.state
        if cmd == "/estado":
            ev = s.evaluation or {}
            rows = [
                f"Modo: {self.cfg.mode_label()} · {'⏸ EN PAUSA' if s.paused else '▶️ operando'}",
                f"Precio BTC: ${s.price:,.2f}" if s.price else "Precio: n/d",
                f"Tendencia macro (Fase 1): {ev.get('trend', 'n/d')}",
                f"Zona de valor (Fase 2): {self._tri(ev.get('phase2_ok'))}",
                f"Gatillo (Fase 3): {self._tri(ev.get('phase3_ok'))}",
                f"Nota: {ev.get('note', '')}",
                f"Trading: {s.trading_note}",
            ]
            return "\n".join(rows)
        if cmd == "/posicion":
            t = s.trade
            if not t:
                return "Sin operación abierta."
            return (f"{t['side']} · entrada ${t['entry']:,.2f} · {t['qty']:g} BTC\n"
                    f"SL ${t['sl']:,.2f}{' (break-even)' if t.get('be_done') else ''} · TP ${t['tp']:,.2f}\n"
                    f"Marca ${t.get('mark', 0):,.2f} · PnL no realizado {t.get('upnl', 0):+.2f} USDT "
                    f"({t.get('r_now', 0):+.2f} R)")
        if cmd == "/balance":
            b = s.balance
            return (f"Saldo: ${b['wallet']:,.2f} USDT (disponible ${b['available']:,.2f})" if b else "Saldo no disponible.")
        if cmd == "/pausar":
            self.set_paused(True, "Pausado desde Telegram")
            return ""
        if cmd == "/reanudar":
            self.set_paused(False)
            return ""
        if cmd == "/cerrar":
            if args and args[0].upper() in ("SI", "SÍ", "YES") and time.time() < self._close_confirm_until:
                self._close_confirm_until = 0
                return self.close_position_now()
            self._close_confirm_until = time.time() + 60
            return "¿Cerrar la posición a mercado? Responde /cerrar SI en los próximos 60 segundos."
        return "Comando no reconocido. Usa /ayuda."

    @staticmethod
    def _tri(v) -> str:
        return "n/d" if v is None else ("✔ cumplida" if v else "✘ no cumplida")
