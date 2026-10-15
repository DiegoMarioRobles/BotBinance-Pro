"""Exchange simulado con la misma interfaz que BinanceFutures, para probar el ejecutor sin red ni dinero."""
from __future__ import annotations

import itertools

from botbinance.binance_api import BinanceAPIError, NetworkError, SymbolRules


class FakeFutures:
    has_keys = True

    def __init__(self, price=80_000.0):
        self.price = price
        self.rules = SymbolRules()
        self.pos: dict | None = None          # {"amt","entry"}
        self.orders: list[dict] = []          # órdenes condicionales abiertas
        self.limits: list[dict] = []          # órdenes límite abiertas (niveles de la martingala)
        self.fail_limits = False
        self.fills: list[dict] = []           # ejecuciones (userTrades)
        self.calls: list[tuple] = []
        self.hedge = False
        self.fail_sl = False                  # simula fallo permanente al colocar el SL
        self.fail_tp = False
        self.timeout_entry = False            # la entrada se ejecuta pero la respuesta se pierde
        self.liq = 0.0                        # liquidationPrice que informa positionRisk
        self._ids = itertools.count(1)
        self.t = 1_000

    def now_ms(self):
        return 1_700_000_000_000

    # ---- cuenta
    def get_position_mode(self):
        return self.hedge

    def change_margin_type(self, symbol, mt="ISOLATED"):
        self.calls.append(("margin", symbol, mt))

    def change_leverage(self, symbol, lev):
        self.calls.append(("leverage", symbol, lev))
        return {"leverage": lev}

    def usdt_balance(self):
        return {"wallet": 100.0, "available": 100.0, "upnl": 0.0}

    def position(self, symbol):
        if not self.pos:
            return None
        amt, entry = self.pos["amt"], self.pos["entry"]
        return {"amt": amt, "entry": entry, "mark": self.price, "liq": self.liq, "upnl": (self.price - entry) * amt,
                "margin_type": "ISOLATED", "leverage": 10, "update_time": 0}

    # ---- órdenes
    def new_market_order(self, symbol, side, quantity, reduce_only=False, client_id=None):
        self.calls.append(("market", side, quantity, reduce_only))
        sign = 1 if side == "BUY" else -1
        if reduce_only:
            assert self.pos, "reduceOnly sin posición"
            realized = (self.price - self.pos["entry"]) * self.pos["amt"] if abs(quantity - abs(self.pos["amt"])) < 1e-12 else 0
            self._fill(side, quantity, self.price, realized)
            self.pos = None
            return {"avgPrice": str(self.price), "executedQty": str(quantity), "status": "FILLED"}
        self.pos = {"amt": sign * quantity, "entry": self.price}
        self._fill(side, quantity, self.price, 0.0)
        if self.timeout_entry:
            raise NetworkError("timeout")
        return {"avgPrice": str(self.price), "executedQty": str(quantity), "status": "FILLED"}

    def get_order(self, symbol, client_id):
        return {"status": "FILLED", "avgPrice": str(self.price), "executedQty": "0.002"}

    def new_conditional_order(self, symbol, side, order_type, trigger_price, quantity, working_type="MARK_PRICE",
                              reduce_only=True, client_id=None, close_position=False):
        self.calls.append(("conditional", order_type, trigger_price, quantity, reduce_only, working_type, close_position))
        if (order_type == "STOP_MARKET" and self.fail_sl) or (order_type == "TAKE_PROFIT_MARKET" and self.fail_tp):
            raise BinanceAPIError(-4120, "boom", 400, "/fapi/v1/algoOrder")
        oid = next(self._ids)
        o = {"id": oid, "client_id": client_id, "type": order_type, "side": side, "trigger": trigger_price,
             "qty": quantity, "algo": True}
        self.orders.append(o)
        return {"algo": True, "id": oid, "client_id": client_id, "raw": {}}

    def open_conditional_orders(self, symbol):
        return [dict(o) for o in self.orders]

    def cancel_conditional_order(self, symbol, order):
        self.calls.append(("cancel", order["id"]))
        self.orders = [o for o in self.orders if o["id"] != order["id"]]

    def cancel_all_conditional(self, symbol):
        self.calls.append(("cancel_all",))
        self.orders = []

    def new_limit_order(self, symbol, side, quantity, price, client_id=None):
        if self.fail_limits:
            raise BinanceAPIError(-2010, "rechazada", 400, "/fapi/v1/order")
        oid = next(self._ids)
        self.calls.append(("limit", side, quantity, price))
        self.limits.append({"id": oid, "client_id": client_id, "side": side, "price": price, "qty": quantity})
        return {"id": oid, "client_id": client_id}

    def open_orders(self, symbol):
        return [dict(o) for o in self.limits]

    def cancel_all_open_orders(self, symbol):
        self.calls.append(("cancel_limits",))
        self.limits = []

    def fill_limit(self, idx=0):
        """El precio llega al nivel: la orden límite se ejecuta y el precio medio de la posición se actualiza."""
        o = self.limits.pop(idx)
        sign = 1 if o["side"] == "BUY" else -1
        amt = self.pos["amt"]
        new_amt = amt + sign * o["qty"]
        entry = (self.pos["entry"] * abs(amt) + o["price"] * o["qty"]) / abs(new_amt)
        self.pos = {"amt": new_amt, "entry": entry}
        self.price = o["price"]
        self._fill(o["side"], o["qty"], o["price"], 0.0)

    def user_trades(self, symbol, start_time, limit=1000):
        return list(self.fills)

    # ---- helpers del test
    def _fill(self, side, qty, price, realized):
        self.t += 1
        self.fills.append({"side": side, "qty": str(qty), "price": str(price), "quoteQty": str(qty * price),
                           "realizedPnl": str(realized), "commission": str(qty * price * 0.0005),
                           "commissionAsset": "USDT", "time": self.t})

    def trigger(self, order_type):
        """Dispara una orden condicional (como haría Binance): cierra la posición al precio de disparo."""
        o = next(x for x in self.orders if x["type"] == order_type)
        amt = self.pos["amt"]
        close_side = "SELL" if amt > 0 else "BUY"
        self.price = o["trigger"]
        self._fill(close_side, abs(amt), o["trigger"], (o["trigger"] - self.pos["entry"]) * amt)
        self.pos = None
        self.orders = []
