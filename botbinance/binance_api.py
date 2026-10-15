"""Cliente REST mínimo para Binance USDⓈ-M Futures (firma HMAC-SHA256).

Se escribe a mano (en vez de usar ccxt/python-binance) por dos razones:
  * Binance movió las órdenes condicionales (STOP_MARKET / TAKE_PROFIT_MARKET) a /fapi/v1/algoOrder
    y muchas librerías todavía envían el SL/TP al endpoint viejo (error -4120).
  * Control total del reintento: una orden de entrada NUNCA se reintenta a ciegas.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import requests

log = logging.getLogger("botbinance.api")

MAINNET_URL = "https://fapi.binance.com"
SPOT_URL = "https://api.binance.com"


class BinanceAPIError(Exception):
    def __init__(self, code: int, msg: str, status: int | None = None, path: str = ""):
        super().__init__(f"[{code}] {msg} ({path})")
        self.code = code
        self.msg = msg
        self.status = status
        self.path = path

    @property
    def is_auth(self) -> bool:
        """Clave inválida, sin permisos o IP no autorizada."""
        return self.code in (-2014, -2015, -1022) or self.status in (401, 403)


class NetworkError(Exception):
    """Fallo de red / timeout. En órdenes, el resultado puede ser desconocido."""


def num(x) -> str:
    """Número -> texto sin notación científica."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, str):
        return x
    d = Decimal(repr(float(x))) if isinstance(x, float) else Decimal(x)
    return format(d.normalize(), "f")


@dataclass
class SymbolRules:
    symbol: str = "BTCUSDT"
    tick_size: Decimal = field(default_factory=lambda: Decimal("0.1"))
    step_size: Decimal = field(default_factory=lambda: Decimal("0.001"))
    min_qty: Decimal = field(default_factory=lambda: Decimal("0.001"))
    max_qty: Decimal = field(default_factory=lambda: Decimal("1000"))
    min_notional: Decimal = field(default_factory=lambda: Decimal("100"))

    def round_price(self, price: float) -> float:
        q = (Decimal(repr(float(price))) / self.tick_size).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return float(q * self.tick_size)

    def floor_qty(self, qty: float) -> float:
        q = (Decimal(repr(float(qty))) / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        return float(q * self.step_size)

    @classmethod
    def from_exchange_info(cls, info: dict, symbol: str) -> "SymbolRules":
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            r = cls(symbol=symbol)
            for f in s.get("filters", []):
                t = f.get("filterType")
                if t == "PRICE_FILTER":
                    r.tick_size = Decimal(f["tickSize"])
                elif t == "LOT_SIZE":
                    r.step_size = Decimal(f["stepSize"])
                    r.min_qty = Decimal(f["minQty"])
                    r.max_qty = Decimal(f["maxQty"])
                elif t == "MARKET_LOT_SIZE":
                    r.max_qty = min(r.max_qty, Decimal(f["maxQty"]))
                elif t == "MIN_NOTIONAL":
                    r.min_notional = Decimal(f.get("notional", f.get("minNotional", "100")))
            return r
        raise KeyError(f"Símbolo {symbol} no encontrado en exchangeInfo")


class BinanceFutures:
    """Cliente de Binance USDⓈ-M Futures. Sin claves solo sirve para endpoints públicos."""

    def __init__(self, base_url: str = MAINNET_URL, api_key: str = "", api_secret: str = "",
                 recv_window: int = 10_000, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.recv_window = recv_window
        self.timeout = timeout
        self._offset_ms = 0
        self._local = threading.local()
        self._algo_supported: bool | None = None

    # ------------------------------------------------------------------ infraestructura
    @property
    def has_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": "BotBinanceBTC/1.0"})
            self._local.s = s
        return s

    def now_ms(self) -> int:
        return int(time.time() * 1000 + self._offset_ms)

    def sync_time(self) -> int:
        """Calcula la diferencia entre el reloj de Windows y el de Binance (evita el error -1021)."""
        t0 = time.time() * 1000
        server = self.server_time()
        t1 = time.time() * 1000
        self._offset_ms = int(server - (t0 + t1) / 2)
        return self._offset_ms

    def request(self, method: str, path: str, params: dict | None = None, signed: bool = False,
                retries: int = 2):
        params = {k: (num(v) if not isinstance(v, str) else v) for k, v in (params or {}).items() if v is not None}
        idempotent = method.upper() == "GET"
        attempt = 0
        resynced = False
        while True:
            attempt += 1
            headers = {}
            p = dict(params)
            if signed:
                if not self.has_keys:
                    raise BinanceAPIError(-2014, "No hay API key configurada", 401, path)
                p["recvWindow"] = self.recv_window
                p["timestamp"] = self.now_ms()
                qs = urlencode(p)
                sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
                url = f"{self.base_url}{path}?{qs}&signature={sig}"
                headers["X-MBX-APIKEY"] = self.api_key
            else:
                url = f"{self.base_url}{path}" + (f"?{urlencode(p)}" if p else "")
            try:
                resp = self._session().request(method, url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if idempotent and attempt <= retries:
                    time.sleep(0.5 * attempt)
                    continue
                # Solo el tipo de error: el texto de requests incluye la URL completa (con la firma).
                raise NetworkError(f"{method} {path}: {type(exc).__name__}") from None

            try:
                data = resp.json()
            except ValueError:
                data = None

            if resp.status_code == 200 and not (isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0):
                return data

            code = data.get("code", -1) if isinstance(data, dict) else -1
            msg = data.get("msg", resp.text[:200]) if isinstance(data, dict) else resp.text[:200]
            if code == -1021 and not resynced:  # reloj desfasado: resincronizar y reintentar una vez
                resynced = True
                try:
                    self.sync_time()
                except Exception:
                    pass
                continue
            if resp.status_code in (429, 418):
                wait = float(resp.headers.get("Retry-After", 2))
                if idempotent and attempt <= retries:
                    time.sleep(min(wait, 10))
                    continue
            if resp.status_code >= 500 and idempotent and attempt <= retries:
                time.sleep(0.5 * attempt)
                continue
            raise BinanceAPIError(code, msg, resp.status_code, path)

    def _get_first(self, paths: list[str], params: dict | None = None, signed: bool = True):
        last: Exception | None = None
        for p in paths:
            try:
                return self.request("GET", p, params, signed=signed)
            except BinanceAPIError as exc:
                if exc.status == 404:
                    last = exc
                    continue
                raise
        raise last or RuntimeError("sin endpoints")

    # ------------------------------------------------------------------ públicos
    def server_time(self) -> int:
        return int(self.request("GET", "/fapi/v1/time")["serverTime"])

    def exchange_info(self) -> dict:
        return self.request("GET", "/fapi/v1/exchangeInfo")

    def symbol_rules(self, symbol: str) -> SymbolRules:
        return SymbolRules.from_exchange_info(self.exchange_info(), symbol)

    def klines(self, symbol: str, interval: str, limit: int = 500, start_time: int | None = None,
               end_time: int | None = None) -> list:
        return self.request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit,
            "startTime": start_time, "endTime": end_time,
        })

    def ticker_price(self, symbol: str) -> float:
        return float(self.request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])

    def mark_price(self, symbol: str) -> float:
        return float(self.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])

    # ------------------------------------------------------------------ cuenta (firmados)
    def usdt_balance(self) -> dict:
        data = self._get_first(["/fapi/v3/balance", "/fapi/v2/balance"])
        for a in data:
            if a.get("asset") == "USDT":
                return {
                    "wallet": float(a.get("balance", 0)),
                    "available": float(a.get("availableBalance", 0)),
                    "upnl": float(a.get("crossUnPnl", 0)),
                }
        return {"wallet": 0.0, "available": 0.0, "upnl": 0.0}

    def get_position_mode(self) -> bool:
        """True si la cuenta está en Hedge Mode (posiciones LONG/SHORT separadas)."""
        return bool(self.request("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"))

    def position(self, symbol: str) -> dict | None:
        data = self._get_first(["/fapi/v3/positionRisk", "/fapi/v2/positionRisk"], {"symbol": symbol})
        for p in data:
            if p.get("symbol") == symbol and abs(float(p.get("positionAmt", 0) or 0)) > 0:
                return {
                    "amt": float(p["positionAmt"]),
                    "entry": float(p.get("entryPrice", 0) or 0),
                    "mark": float(p.get("markPrice", 0) or 0),
                    "liq": float(p.get("liquidationPrice", 0) or 0),
                    "upnl": float(p.get("unRealizedProfit", 0) or 0),
                    "margin_type": str(p.get("marginType", "")).upper(),
                    "leverage": int(float(p.get("leverage", 0) or 0)),
                    "update_time": int(p.get("updateTime", 0) or 0),
                }
        return None

    def change_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> None:
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceAPIError as exc:
            if exc.code == -4046:  # "No need to change margin type"
                return
            raise

    def change_leverage(self, symbol: str, leverage: int) -> dict:
        return self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def user_trades(self, symbol: str, start_time: int, limit: int = 1000) -> list:
        return self.request("GET", "/fapi/v1/userTrades",
                            {"symbol": symbol, "startTime": start_time, "limit": limit}, signed=True)

    # ------------------------------------------------------------------ órdenes
    def new_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False,
                         client_id: str | None = None) -> dict:
        """Orden a mercado. NO se reintenta automáticamente (podría duplicar la posición)."""
        return self.request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity,
            "reduceOnly": reduce_only if reduce_only else None,
            "newClientOrderId": client_id, "newOrderRespType": "RESULT",
        }, signed=True)

    def new_limit_order(self, symbol: str, side: str, quantity: float, price: float,
                        client_id: str | None = None) -> dict:
        """Orden límite GTC (niveles 2, 3… de las entradas escalonadas). Sin reintento automático."""
        res = self.request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": "GTC", "quantity": quantity,
            "price": price, "newClientOrderId": client_id, "newOrderRespType": "RESULT",
        }, signed=True)
        return {"id": res.get("orderId"), "client_id": res.get("clientOrderId")}

    def open_orders(self, symbol: str) -> list[dict]:
        """Órdenes normales pendientes (límite), normalizadas."""
        out = []
        for o in self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True):
            if o.get("type") == "LIMIT":
                out.append({"id": o.get("orderId"), "client_id": o.get("clientOrderId"), "side": o.get("side"),
                            "price": float(o.get("price") or 0), "qty": float(o.get("origQty") or 0)})
        return out

    def cancel_all_open_orders(self, symbol: str) -> None:
        """Cancela las órdenes LÍMITE pendientes (no toca las condicionales SL/TP)."""
        for o in self.open_orders(symbol):
            try:
                self.request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": o["id"]}, signed=True)
            except BinanceAPIError as exc:
                if exc.code not in (-2011, -2013):      # ya ejecutada o inexistente
                    raise

    def get_order(self, symbol: str, client_id: str) -> dict:
        return self.request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def new_conditional_order(self, symbol: str, side: str, order_type: str, trigger_price: float,
                              quantity: float | None, working_type: str = "MARK_PRICE", reduce_only: bool = True,
                              client_id: str | None = None, close_position: bool = False) -> dict:
        """STOP_MARKET / TAKE_PROFIT_MARKET protegiendo la posición.

        Usa el endpoint nuevo /fapi/v1/algoOrder (obligatorio desde 2025-12-09).
        Si el servidor no lo conociera (404) cae al endpoint clásico.
        `close_position=True` cierra TODA la posición al dispararse (no lleva cantidad ni reduceOnly): es lo que se usa
        con entradas escalonadas, porque la posición crece cuando se ejecutan los niveles.
        """
        qty_p = None if close_position else quantity
        ro_p = None if close_position else reduce_only
        cp_p = True if close_position else None
        if self._algo_supported is not False:
            try:
                res = self.request("POST", "/fapi/v1/algoOrder", {
                    "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": order_type,
                    "quantity": qty_p, "triggerPrice": trigger_price, "workingType": working_type,
                    "reduceOnly": ro_p, "closePosition": cp_p, "priceProtect": False, "clientAlgoId": client_id,
                    "newOrderRespType": "RESULT",
                }, signed=True)
                self._algo_supported = True
                return {"algo": True, "id": res.get("algoId"), "client_id": res.get("clientAlgoId"), "raw": res}
            except BinanceAPIError as exc:
                if exc.status != 404:
                    raise
                self._algo_supported = False
                log.warning("algoOrder no disponible (404); uso el endpoint clásico /fapi/v1/order")
        res = self.request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": order_type, "quantity": qty_p,
            "stopPrice": trigger_price, "workingType": working_type, "reduceOnly": ro_p, "closePosition": cp_p,
            "newClientOrderId": client_id,
        }, signed=True)
        return {"algo": False, "id": res.get("orderId"), "client_id": res.get("clientOrderId"), "raw": res}

    def open_conditional_orders(self, symbol: str) -> list[dict]:
        """SL/TP pendientes, normalizados: {id, client_id, type, side, trigger, qty, algo}."""
        out: list[dict] = []
        if self._algo_supported is not False:
            try:
                data = self.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
                items = data if isinstance(data, list) else (data.get("orders") or data.get("data") or [])
                for o in items:
                    out.append({
                        "id": o.get("algoId"), "client_id": o.get("clientAlgoId"),
                        "type": o.get("orderType") or o.get("type"), "side": o.get("side"),
                        "trigger": float(o.get("triggerPrice") or 0), "qty": float(o.get("quantity") or 0),
                        "algo": True,
                    })
            except BinanceAPIError as exc:
                if exc.status != 404:
                    raise
                self._algo_supported = False
        try:
            for o in self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True):
                if o.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
                    out.append({
                        "id": o.get("orderId"), "client_id": o.get("clientOrderId"), "type": o.get("type"),
                        "side": o.get("side"), "trigger": float(o.get("stopPrice") or 0),
                        "qty": float(o.get("origQty") or 0), "algo": False,
                    })
        except BinanceAPIError:
            pass
        return out

    def cancel_conditional_order(self, symbol: str, order: dict) -> None:
        try:
            if order.get("algo"):
                self.request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": order["id"]}, signed=True)
            else:
                self.request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order["id"]}, signed=True)
        except BinanceAPIError as exc:
            if exc.code in (-2011, -2013, -4130):  # ya no existe / ya ejecutada
                return
            raise

    def cancel_all_conditional(self, symbol: str) -> None:
        for o in self.open_conditional_orders(symbol):
            self.cancel_conditional_order(symbol, o)

    # ------------------------------------------------------------------ diagnóstico
    def api_restrictions(self) -> dict | None:
        """Permisos de la clave (solo cuenta real; en spot-API). Devuelve None si no se puede consultar."""
        if not self.has_keys:
            return None
        spot = BinanceFutures(SPOT_URL, self.api_key, self.api_secret, self.recv_window, self.timeout)
        spot._offset_ms = self._offset_ms
        try:
            return spot.request("GET", "/sapi/v1/account/apiRestrictions", signed=True)
        except (BinanceAPIError, NetworkError):
            return None

    def check_credentials(self, symbol: str, leverage: int | None = None, verify_trade: bool = True) -> dict:
        """Prueba de conexión: claves, IP, permisos de lectura y (opcional) permiso de trading."""
        res: dict = {"ok": False, "auth": False, "error": "", "code": None, "env": self.base_url}
        try:
            self.sync_time()
            res["hedge_mode"] = self.get_position_mode()
            bal = self.usdt_balance()
            res.update(wallet=bal["wallet"], available=bal["available"])
            pos = self.position(symbol)
            res["position_open"] = pos is not None
            res["read_ok"] = True
            if verify_trade and leverage and pos is None:
                self.change_leverage(symbol, leverage)  # requiere permiso de Futures/Trade
            res["trade_ok"] = bool(verify_trade and leverage and pos is None) or None
            res["ok"] = True
        except BinanceAPIError as exc:
            res.update(error=exc.msg, code=exc.code, auth=exc.is_auth)
        except NetworkError as exc:
            res.update(error=f"Sin conexión con Binance: {exc}")
        return res


def get_public_ip(timeout: float = 6.0) -> str:
    """IP pública IPv4 de este PC (la que Binance ve y la que debe estar en la whitelist)."""
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip"):
        try:
            r = requests.get(url, timeout=timeout)
            ip = r.text.strip()
            if r.status_code == 200 and 7 <= len(ip) <= 15 and ip.count(".") == 3:
                return ip
        except requests.RequestException:
            continue
    return ""
