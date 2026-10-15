"""Ejecución atómica y gestión de órdenes en Binance Futures.

Flujo de una operación:
  1. Margen AISLADO + apalancamiento configurado.
  2. Orden de entrada MARKET.
  3. Inmediatamente: STOP_MARKET y TAKE_PROFIT_MARKET (reduceOnly) en los servidores de Binance.
     Si el Stop Loss no se puede colocar -> cierre de emergencia (nunca se queda una posición sin protección).
  4. Verificación del precio de liquidación real contra el SL.
  5. Bucle de monitoreo: break-even al alcanzar 1:1, reposición de órdenes perdidas y cierre/contabilidad.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable

from . import paths
from .binance_api import BinanceAPIError, BinanceFutures, NetworkError, SymbolRules
from .util import now_ms, read_json, write_json

log = logging.getLogger("botbinance.exec")


class TradingError(Exception):
    """Error de operativa que debe mostrarse al usuario."""


@dataclass
class Trade:
    id: str
    side: str
    qty: float
    entry: float
    sl: float
    tp: float
    sl_initial: float
    dist_sl: float
    dist_tp: float
    margin: float
    leverage: int
    liq_price: float
    loss_sl_usdt: float
    profit_tp_usdt: float
    open_time: int
    pattern: str = ""
    mode: str = ""
    be_done: bool = False
    sl_order: dict | None = None
    tp_order: dict | None = None
    martingale: bool = False
    rr: float = 3.0
    levels: list | None = None      # [{price, qty, filled, order}] (entradas escalonadas)


class TradeManager:
    def __init__(self, cfg, state, notify: Callable[[str], None]):
        self.cfg = cfg
        self.state = state
        self.notify = notify
        self.client: BinanceFutures | None = None
        self.rules = SymbolRules()
        self.trade: Trade | None = None
        self.history: list[dict] = read_json(paths.HISTORY_FILE, []) or []
        state.history = self.history[-50:]
        self._lock = threading.RLock()
        self._last_protection_check = 0.0
        self._be_failures = 0

    # ------------------------------------------------------------------ utilidades
    @property
    def sym(self) -> str:
        return self.cfg.SYMBOL

    @staticmethod
    def _close_side(side: str) -> str:
        return "SELL" if side == "LONG" else "BUY"

    @staticmethod
    def _sign(side: str) -> float:
        return 1.0 if side == "LONG" else -1.0

    def busy(self) -> bool:
        return self.trade is not None or self.state.foreign_position is not None

    def _persist(self) -> None:
        write_json(paths.TRADE_FILE, asdict(self.trade) if self.trade else {})

    def prepare_account(self) -> None:
        c = self.client
        assert c is not None
        if c.get_position_mode():
            raise TradingError(
                "Tu cuenta de Futuros está en 'Hedge Mode'. Cámbiala a 'One-way Mode' en Binance "
                "(Futuros → Preferencias → Modo de posición) y reinicia el bot."
            )
        c.change_margin_type(self.sym, "ISOLATED")
        c.change_leverage(self.sym, int(self.cfg.APALANCAMIENTO))

    # ------------------------------------------------------------------ apertura
    def open_trade(self, plan, signal) -> Trade:
        c = self.client
        if c is None or not c.has_keys:
            raise TradingError("No hay API keys configuradas.")
        with self._lock:
            if self.trade is not None:
                raise TradingError("Ya hay una operación en gestión.")
            if c.position(self.sym):
                raise TradingError("Ya existe una posición abierta en la cuenta.")
            self.prepare_account()
            c.cancel_all_conditional(self.sym)  # limpia órdenes huérfanas de operaciones anteriores

            order_side = "BUY" if plan.side == "LONG" else "SELL"
            cid = "bot" + uuid.uuid4().hex[:16]
            log.info("Enviando orden MARKET %s %s %s BTC…", order_side, self.sym, f"{plan.qty:g}")
            try:
                res = c.new_market_order(self.sym, order_side, plan.qty, client_id=cid)
            except NetworkError:
                res = self._recover_entry(cid)
                if res is None:
                    raise TradingError("Sin respuesta de Binance al enviar la entrada y no se detectó posición.")
            entry = float(res.get("avgPrice") or 0)
            qty = float(res.get("executedQty") or 0) or plan.qty
            if entry <= 0:
                pos = self._wait_position()
                if pos is None:
                    raise TradingError("La orden de entrada no aparece como posición abierta.")
                entry, qty = pos["entry"], abs(pos["amt"])

            sg = self._sign(plan.side)
            sl = self.rules.round_price(entry * (1.0 - sg * plan.dist_sl))
            tp = self.rules.round_price(entry * (1.0 + sg * plan.dist_tp))
            trade = Trade(
                id=cid, side=plan.side, qty=qty, entry=entry, sl=sl, tp=tp, sl_initial=sl,
                dist_sl=plan.dist_sl, dist_tp=plan.dist_tp, margin=qty * entry / plan.leverage,
                leverage=plan.leverage, liq_price=plan.liq_price, loss_sl_usdt=plan.loss_sl_usdt,
                profit_tp_usdt=plan.profit_tp_usdt, open_time=c.now_ms(), pattern=signal.pattern,
                mode=self.cfg.mode_label(), martingale=plan.martingale,
                rr=(plan.dist_tp / plan.dist_sl) if plan.dist_sl else self.cfg.RATIO_RIESGO_BENEFICIO,
            )
            self.trade = trade
            self._persist()
            log.info("Entrada ejecutada: %s %g BTC @ %.2f. Colocando protecciones…", plan.side, qty, entry)

            # --- Stop Loss: obligatorio
            cp = plan.martingale          # con niveles, SL/TP cierran TODA la posición (crece al ejecutarse los niveles)
            trade.sl_order = self._place_conditional("SL", sl, qty, plan.side, close_position=cp)
            if trade.sl_order is None:
                self.notify("🚨 No se pudo colocar el Stop Loss. Cierro la posición por seguridad.")
                self.emergency_close("Stop Loss no colocado")
                raise TradingError("El Stop Loss no se pudo colocar; la posición fue cerrada por seguridad.")
            # --- Take Profit
            trade.tp_order = self._place_conditional("TP", tp, qty, plan.side, close_position=cp)
            if plan.martingale:
                self._place_levels(trade, plan)
            if trade.tp_order is None:
                self.notify("⚠️ No se pudo colocar el Take Profit (el Stop Loss sí está activo). Reintentaré.")
            self._persist()

            # --- Verificación de liquidación real
            pos = c.position(self.sym)
            if pos and pos["liq"] > 0:
                trade.liq_price = pos["liq"]
                beyond = (plan.side == "LONG" and sl <= pos["liq"]) or (plan.side == "SHORT" and sl >= pos["liq"])
                if beyond:
                    self.notify(f"🚨 El Stop Loss ({sl:,.1f}) quedaría más lejos que la liquidación real "
                                f"({pos['liq']:,.1f}). Cierro por seguridad.")
                    self.emergency_close("SL más lejos que la liquidación")
                    raise TradingError("SL más lejos que la liquidación real; posición cerrada.")
            self._persist()
            self._last_protection_check = time.time()
            self.notify(self._open_message(trade))
            self._publish()
            return trade

    def _place_levels(self, trade: Trade, plan) -> None:
        """Niveles 2, 3… de la martingala: órdenes límite en los soportes/resistencias (en % respecto a la entrada real)."""
        c = self.client
        assert c is not None
        order_side = "BUY" if trade.side == "LONG" else "SELL"
        trade.levels = [{"price": trade.entry, "qty": trade.qty, "filled": True, "order": None}]
        for lv in plan.levels[1:]:
            price = self.rules.round_price(trade.entry * (1.0 + lv["offset"]))
            item = {"price": price, "qty": lv["qty"], "filled": False, "order": None}
            try:
                o = c.new_limit_order(self.sym, order_side, lv["qty"], price, client_id="lv" + uuid.uuid4().hex[:14])
                item["order"] = o["id"]
            except (BinanceAPIError, NetworkError) as exc:
                log.error("No se pudo colocar el nivel %.1f: %s", price, exc)
                self.notify(f"⚠️ No pude colocar el nivel de entrada a ${price:,.2f} ({exc}). Sigo sin ese nivel.")
            trade.levels.append(item)

    def _open_message(self, t: Trade) -> str:
        icon = "🟢" if t.side == "LONG" else "🔴"
        text = self._open_message_base(t, icon)
        if t.martingale and t.levels:
            text += "\nMartingala: " + " · ".join(
                f"{lv['qty']:g} BTC @ ${lv['price']:,.2f}{' (ahora)' if lv['filled'] else ''}" for lv in t.levels)
        return text

    def _open_message_base(self, t: Trade, icon: str) -> str:
        return (
            f"{icon} OPERACIÓN {t.side} ABIERTA ({t.mode})\n"
            f"Patrón: {t.pattern}\n"
            f"Entrada: ${t.entry:,.2f} · Cantidad: {t.qty:g} BTC · Margen: ${t.margin:,.2f} ({t.leverage}x)\n"
            f"Stop Loss: ${t.sl:,.2f} ({t.dist_sl * 100:.2f}%) · Take Profit: ${t.tp:,.2f} ({t.dist_tp * 100:.2f}%)\n"
            f"Pérdida máx. si salta SL: ${t.loss_sl_usdt:,.2f} · Ganancia neta si llega a TP: ${t.profit_tp_usdt:,.2f}\n"
            f"Liquidación: ${t.liq_price:,.2f}"
        )

    def _recover_entry(self, cid: str) -> dict | None:
        """Tras un timeout, comprueba si la orden de entrada llegó a ejecutarse (evita duplicar o perder la posición)."""
        c = self.client
        assert c is not None
        time.sleep(1.5)
        try:
            pos = c.position(self.sym)
            if pos:
                return {"avgPrice": pos["entry"], "executedQty": abs(pos["amt"])}
            o = c.get_order(self.sym, cid)
            if o.get("status") == "FILLED":
                return o
        except (BinanceAPIError, NetworkError):
            pass
        return None

    def _wait_position(self, tries: int = 6) -> dict | None:
        assert self.client is not None
        for _ in range(tries):
            pos = self.client.position(self.sym)
            if pos:
                return pos
            time.sleep(0.5)
        return None

    def _place_conditional(self, kind: str, price: float, qty: float, side: str, attempts: int = 3,
                           close_position: bool = False) -> dict | None:
        c = self.client
        assert c is not None
        typ = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
        for i in range(attempts):
            try:
                o = c.new_conditional_order(
                    self.sym, self._close_side(side), typ, self.rules.round_price(price), qty,
                    working_type=self.cfg.WORKING_TYPE, reduce_only=True,
                    client_id=f"{kind.lower()}{uuid.uuid4().hex[:14]}", close_position=close_position,
                )
                log.info("%s colocado en %.2f (%s)", kind, price, "algoOrder" if o["algo"] else "order")
                return {"id": o["id"], "client_id": o["client_id"], "algo": o["algo"]}
            except BinanceAPIError as exc:
                log.error("No se pudo colocar %s (intento %d/%d): %s", kind, i + 1, attempts, exc)
                if exc.code == -2022:        # ReduceOnly rechazada: ya no hay posición
                    return None
                if exc.code == -2021 and kind == "SL":   # dispararía de inmediato: el precio ya lo superó
                    return None
            except NetworkError as exc:
                log.error("Red al colocar %s (intento %d/%d): %s", kind, i + 1, attempts, exc)
            time.sleep(0.6)
        return None

    # ------------------------------------------------------------------ cierre de emergencia
    def emergency_close(self, reason: str) -> bool:
        c = self.client
        if c is None:
            return False
        with self._lock:
            try:
                try:
                    c.cancel_all_open_orders(self.sym)        # que ningún nivel pendiente reabra la posición
                except (BinanceAPIError, NetworkError):
                    pass
                pos = c.position(self.sym)
                if pos:
                    side = "LONG" if pos["amt"] > 0 else "SHORT"
                    c.new_market_order(self.sym, self._close_side(side), abs(pos["amt"]), reduce_only=True)
                    log.warning("Cierre de emergencia ejecutado (%s)", reason)
                try:
                    c.cancel_all_conditional(self.sym)
                except (BinanceAPIError, NetworkError):
                    pass
            except (BinanceAPIError, NetworkError) as exc:
                log.error("FALLÓ el cierre de emergencia: %s", exc)
                self.notify("🚨🚨 NO se pudo cerrar la posición automáticamente. Ciérrala MANUALMENTE en Binance. "
                            f"Motivo del cierre: {reason}. Error: {exc}")
                return False
            if self.trade is not None:
                self._finalize_closed(reason_override=f"CIERRE DE EMERGENCIA ({reason})")
            return True

    def close_now(self) -> str:
        """Cierre manual a mercado (botón del dashboard / comando de Telegram)."""
        with self._lock:
            if self.client is None or not self.client.has_keys:
                return "No hay conexión con Binance."
            pos = self.client.position(self.sym)
            if not pos:
                return "No hay posición abierta."
            ok = self.emergency_close("cierre manual")
            return "Posición cerrada." if ok else "No se pudo cerrar; hazlo manualmente en Binance."

    # ------------------------------------------------------------------ monitoreo
    def monitor_tick(self) -> None:
        c = self.client
        if c is None or not c.has_keys:
            return
        with self._lock:
            pos = c.position(self.sym)
            if self.trade is None:
                self._sync_foreign(pos)
                return
            if pos is None:
                self._finalize_closed()
                return
            t = self.trade
            mark = pos["mark"]
            if t.martingale:
                self._check_level_fills(pos)
            if (not t.be_done and self.cfg.ACTIVAR_BREAK_EVEN and self._reached_one_r(t, mark)):
                self._move_to_break_even(mark)
            if time.time() - self._last_protection_check > 20:
                self._last_protection_check = time.time()
                self._ensure_protection()
            self._publish(pos)

    def _reached_one_r(self, t: Trade, mark: float) -> bool:
        target = t.entry + self._sign(t.side) * abs(t.entry - t.sl_initial)      # 1R respecto al precio medio
        return mark >= target if t.side == "LONG" else mark <= target

    def _check_level_fills(self, pos: dict) -> None:
        """Si la posición creció es que se ejecutó un nivel: actualiza precio medio, TP y avisa."""
        t, c = self.trade, self.client
        assert t is not None and c is not None
        if not t.levels or abs(pos["amt"]) <= t.qty * 1.0001:
            return
        try:
            open_ids = {o["id"] for o in c.open_orders(self.sym)}
        except (BinanceAPIError, NetworkError):
            return
        for lv in t.levels:
            if not lv["filled"] and lv["order"] is not None and lv["order"] not in open_ids:
                lv["filled"] = True
        t.qty, t.entry = abs(pos["amt"]), pos["entry"]
        t.margin = t.qty * t.entry / t.leverage
        new_tp = self.rules.round_price(t.entry + self._sign(t.side) * t.rr * abs(t.entry - t.sl_initial))
        new = self._place_conditional("TP", new_tp, t.qty, t.side, close_position=True)
        if new is not None:
            old, t.tp_order, t.tp = t.tp_order, new, new_tp
            if old:
                try:
                    c.cancel_conditional_order(self.sym, old)
                except (BinanceAPIError, NetworkError):
                    pass
        if pos["liq"] > 0 and ((t.side == "LONG" and t.sl <= pos["liq"]) or (t.side == "SHORT" and t.sl >= pos["liq"])):
            self.notify("🚨 Tras ejecutarse un nivel, la liquidación quedó antes que el Stop Loss. Cierro por seguridad.")
            self.emergency_close("SL más lejos que la liquidación tras un nivel")
            return
        done = sum(1 for lv in t.levels if lv["filled"])
        self._persist()
        self.notify(f"🪜 NIVEL {done}/{len(t.levels)} EJECUTADO ({t.side})\nPosición: {t.qty:g} BTC · precio medio "
                    f"${t.entry:,.2f} · SL ${t.sl:,.2f} · nuevo TP ${t.tp:,.2f}")

    def _move_to_break_even(self, mark: float) -> None:
        c, t = self.client, self.trade
        assert c is not None and t is not None
        buf = self.cfg.BREAK_EVEN_BUFFER_PCT
        be_price = self.rules.round_price(t.entry * (1.0 + self._sign(t.side) * buf))
        # El precio actual debe estar claramente más allá del nuevo SL (si no, se dispararía al instante).
        if (t.side == "LONG" and mark <= be_price) or (t.side == "SHORT" and mark >= be_price):
            return
        new = self._place_conditional("SL", be_price, t.qty, t.side, attempts=2, close_position=t.martingale)
        if new is None:
            self._be_failures += 1
            if self._be_failures in (1, 5):
                self.notify("⚠️ No pude mover el Stop Loss a break-even; seguiré intentando. El SL original sigue activo.")
            return
        old, t.sl_order = t.sl_order, new
        t.sl, t.be_done = be_price, True
        if t.martingale:                  # con riesgo cero ya no se añaden niveles: se cancelan los pendientes
            try:
                c.cancel_all_open_orders(self.sym)
            except (BinanceAPIError, NetworkError) as exc:
                log.warning("No pude cancelar los niveles pendientes: %s", exc)
        self._be_failures = 0
        if old:
            try:
                c.cancel_conditional_order(self.sym, old)
            except (BinanceAPIError, NetworkError) as exc:
                log.warning("No pude cancelar el SL anterior (queda inofensivo): %s", exc)
        self._persist()
        self.notify(f"🛡️ BREAK-EVEN activado: el Stop Loss se movió a ${be_price:,.2f} (entrada +{buf * 100:.1f}%). "
                    "Riesgo cero.")

    def _ensure_protection(self) -> None:
        """Repone SL/TP si alguno desapareció (p. ej. cancelado a mano) mientras la posición sigue abierta."""
        c, t = self.client, self.trade
        assert c is not None and t is not None
        try:
            orders = c.open_conditional_orders(self.sym)
        except (BinanceAPIError, NetworkError):
            return
        cs = self._close_side(t.side)
        has_sl = any(o["type"] in ("STOP_MARKET", "STOP") and o["side"] == cs for o in orders)
        has_tp = any(o["type"] in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") and o["side"] == cs for o in orders)
        if has_sl and has_tp:
            return
        try:
            if c.position(self.sym) is None:      # se cerró mientras revisábamos
                return
        except (BinanceAPIError, NetworkError):
            return
        if not has_sl:
            log.warning("¡Falta el Stop Loss en Binance! Lo repongo en %.2f", t.sl)
            t.sl_order = self._place_conditional("SL", t.sl, t.qty, t.side, close_position=t.martingale)
            if t.sl_order is None:
                if c.position(self.sym) is not None:
                    self.notify("🚨 El Stop Loss desapareció y no pude reponerlo. Cierro la posición.")
                    self.emergency_close("SL perdido")
                return
            self.notify(f"🛡️ El Stop Loss había desaparecido; lo repuse en ${t.sl:,.2f}.")
        if not has_tp:
            log.warning("Falta el Take Profit en Binance; lo repongo en %.2f", t.tp)
            t.tp_order = self._place_conditional("TP", t.tp, t.qty, t.side, close_position=t.martingale)
        self._persist()

    # ------------------------------------------------------------------ cierre y contabilidad
    def _finalize_closed(self, reason_override: str | None = None) -> None:
        c, t = self.client, self.trade
        assert c is not None and t is not None
        try:
            c.cancel_all_open_orders(self.sym)
            c.cancel_all_conditional(self.sym)
        except (BinanceAPIError, NetworkError) as exc:
            log.warning("No pude limpiar las órdenes pendientes: %s", exc)

        realized = fees = 0.0
        exit_px = None
        try:
            fills = c.user_trades(self.sym, max(t.open_time - 10_000, 0))
            cs = self._close_side(t.side)
            closing = [f for f in fills if f.get("side") == cs]
            realized = sum(float(f.get("realizedPnl", 0)) for f in fills)
            for f in fills:
                if f.get("commissionAsset", "USDT") == "USDT":
                    fees += float(f.get("commission", 0))
                else:   # comisión pagada en BNB: se estima con la tarifa taker
                    fees += float(f.get("quoteQty", 0)) * self.cfg.TASA_COMISION_TAKER
            qsum = sum(float(f["qty"]) for f in closing)
            if qsum > 0:
                exit_px = sum(float(f["price"]) * float(f["qty"]) for f in closing) / qsum
        except (BinanceAPIError, NetworkError) as exc:
            log.warning("No pude leer las ejecuciones para calcular el resultado: %s", exc)

        net = realized - fees
        if reason_override:
            reason = reason_override
        elif exit_px is None:
            reason = "CERRADA (resultado no disponible)"
        else:
            tol = 0.0015
            if abs(exit_px - t.tp) / t.tp < tol:
                reason = "TAKE PROFIT"
            elif abs(exit_px - t.sl) / t.sl < tol:
                reason = "BREAK-EVEN" if t.be_done else "STOP LOSS"
            else:
                reason = "CIERRE MANUAL / OTRO"
        record = {
            "id": t.id, "side": t.side, "entry": t.entry, "exit": exit_px, "qty": t.qty,
            "pnl": realized, "fees": fees, "net": net, "reason": reason,
            "r": (net / t.loss_sl_usdt) if t.loss_sl_usdt else None,
            "open_time": t.open_time, "close_time": now_ms(), "mode": t.mode, "pattern": t.pattern,
        }
        self.history.append(record)
        write_json(paths.HISTORY_FILE, self.history[-500:])
        self.state.history = self.history[-50:]
        icon = "✅" if net > 0 else ("➖" if abs(net) < 0.05 else "❌")
        self.notify(
            f"{icon} OPERACIÓN CERRADA – {reason}\n{t.side} · entrada ${t.entry:,.2f} → salida "
            f"{'$' + format(exit_px, ',.2f') if exit_px else 'n/d'}\n"
            f"Resultado neto: {'+' if net >= 0 else ''}{net:,.2f} USDT (comisiones ${fees:,.2f})"
        )
        log.info("Operación cerrada: %s · neto %.2f USDT", reason, net)
        self.trade = None
        self._be_failures = 0
        self._persist()
        self._publish(None)

    # ------------------------------------------------------------------ estado y reinicios
    def _sync_foreign(self, pos: dict | None) -> None:
        """Hay una posición que el bot no abrió: no se toca, pero se avisa y se bloquean nuevas entradas."""
        if pos:
            if self.state.foreign_position is None:
                self.notify("⚠️ Hay una posición abierta que el bot no gestiona. No abriré nuevas operaciones mientras exista.")
            self.state.foreign_position = pos
            self.state.set_alert("foreign", "Hay una posición abierta en Binance que no es del bot. "
                                            "El bot no abrirá operaciones hasta que se cierre.", "warn")
        else:
            self.state.foreign_position = None
            self.state.set_alert("foreign", None)
        self.state.position = pos
        self.state.trade = None

    def reconcile_on_start(self) -> None:
        """Recupera el estado tras reiniciar el bot (o el PC) con una operación abierta."""
        c = self.client
        if c is None or not c.has_keys:
            return
        with self._lock:
            saved = read_json(paths.TRADE_FILE, {}) or {}
            pos = c.position(self.sym)
            if saved.get("id"):
                try:
                    self.trade = Trade(**saved)
                except TypeError:
                    self.trade = None
            if self.trade is not None:
                if pos:
                    log.info("Operación %s recuperada tras el reinicio; compruebo sus protecciones.", self.trade.side)
                    self.notify(f"♻️ Bot reiniciado: sigo gestionando la operación {self.trade.side} abierta.")
                    self._last_protection_check = 0.0
                else:
                    log.info("La operación guardada se cerró mientras el bot estaba apagado.")
                    self._finalize_closed()
            elif pos:
                self._sync_foreign(pos)
            self._publish()

    def _publish(self, pos: dict | None | bool = False) -> None:
        """Actualiza el estado que lee el dashboard."""
        if pos is False:
            try:
                pos = self.client.position(self.sym) if self.client and self.client.has_keys else None
            except (BinanceAPIError, NetworkError):
                pos = None
        t = self.trade
        self.state.position = pos
        if t is None or pos is None:
            self.state.trade = None if t is None else asdict(t)
            return
        d = asdict(t)
        sg = self._sign(t.side)
        risk_px = abs(t.entry - t.sl_initial)
        d["mark"] = pos["mark"]
        d["upnl"] = pos["upnl"]
        d["r_now"] = ((pos["mark"] - t.entry) * sg / risk_px) if risk_px else 0.0
        self.state.trade = d
