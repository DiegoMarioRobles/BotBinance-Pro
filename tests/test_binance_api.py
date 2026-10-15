import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl, urlparse

import pytest

from botbinance.binance_api import BinanceAPIError, BinanceFutures, NetworkError, SymbolRules, num


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers, self.text = status, data, headers or {}, str(data)

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, headers=None, timeout=None):
        self.requests.append((method, url, headers or {}))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses, key="K" * 64, secret="S" * 64):
    c = BinanceFutures("https://example.test", key, secret)
    s = FakeSession(responses)
    c._session = lambda: s
    return c, s


def test_signature_is_valid_hmac():
    c, s = client([Resp(200, {"ok": 1})], secret="topsecret" * 4)
    c.request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.002}, signed=True)
    method, url, headers = s.requests[0]
    assert method == "POST" and headers["X-MBX-APIKEY"] == "K" * 64
    q = urlparse(url).query
    body, sig = q.rsplit("&signature=", 1)
    assert hmac.new(("topsecret" * 4).encode(), body.encode(), hashlib.sha256).hexdigest() == sig
    params = dict(parse_qsl(body))
    assert params["symbol"] == "BTCUSDT" and params["quantity"] == "0.002" and "timestamp" in params and "recvWindow" in params
    assert "signature" not in params


def test_secret_never_in_url_and_errors_do_not_leak_signature():
    import requests as rq
    c, s = client([rq.ConnectionError("HTTPSConnectionPool(...?signature=abc123) failed")])
    with pytest.raises(NetworkError) as ei:
        c.request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, signed=True)
    assert "abc123" not in str(ei.value) and "signature" not in str(ei.value)
    assert "S" * 64 not in s.requests[0][1]


def test_api_error_mapping():
    c, _ = client([Resp(401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action"})])
    with pytest.raises(BinanceAPIError) as ei:
        c.request("GET", "/fapi/v2/balance", signed=True)
    assert ei.value.code == -2015 and ei.value.is_auth


def test_success_with_code_200_body_is_not_an_error():
    c, _ = client([Resp(200, {"code": 200, "msg": "The operation of cancel all open order is done."})])
    assert c.request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": "BTCUSDT"}, signed=True)["code"] == 200


def test_order_post_is_never_retried_on_network_error():
    import requests as rq
    c, s = client([rq.Timeout(), Resp(200, {"orderId": 1})])
    with pytest.raises(NetworkError):
        c.request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, signed=True)
    assert len(s.requests) == 1                      # no se reintenta: podría duplicar la posición


def test_get_is_retried(monkeypatch):
    import requests as rq
    monkeypatch.setattr("time.sleep", lambda s: None)
    c, s = client([rq.Timeout(), Resp(200, {"serverTime": 5})])
    assert c.request("GET", "/fapi/v1/time")["serverTime"] == 5 and len(s.requests) == 2


def test_timestamp_error_resyncs_and_retries_once(monkeypatch):
    c, s = client([Resp(400, {"code": -1021, "msg": "Timestamp outside recvWindow"}),
                   Resp(200, {"serverTime": 123}),         # respuesta a sync_time
                   Resp(200, {"ok": True})])
    assert c.request("POST", "/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 10}, signed=True) == {"ok": True}
    assert len(s.requests) == 3


def test_missing_keys_raise_auth_error():
    c = BinanceFutures("https://example.test")
    with pytest.raises(BinanceAPIError) as ei:
        c.request("GET", "/fapi/v2/balance", signed=True)
    assert ei.value.is_auth


def test_conditional_order_uses_algo_endpoint():
    c, s = client([Resp(200, {"algoId": 77, "clientAlgoId": "sl1"})])
    res = c.new_conditional_order("BTCUSDT", "SELL", "STOP_MARKET", 79_100.5, 0.002, "MARK_PRICE", True, "sl1")
    method, url, _ = s.requests[0]
    q = dict(parse_qsl(urlparse(url).query))
    assert urlparse(url).path == "/fapi/v1/algoOrder"
    assert q["algoType"] == "CONDITIONAL" and q["type"] == "STOP_MARKET" and q["triggerPrice"] == "79100.5"
    assert q["reduceOnly"] == "true" and q["workingType"] == "MARK_PRICE" and q["side"] == "SELL"
    assert res == {"algo": True, "id": 77, "client_id": "sl1", "raw": {"algoId": 77, "clientAlgoId": "sl1"}}


def test_conditional_order_falls_back_to_classic_on_404():
    html = Resp(404, None)
    c, s = client([html, Resp(200, {"orderId": 5, "clientOrderId": "x"})])
    res = c.new_conditional_order("BTCUSDT", "SELL", "TAKE_PROFIT_MARKET", 82_000, 0.002)
    assert res["algo"] is False and res["id"] == 5
    assert urlparse(s.requests[1][1]).path == "/fapi/v1/order"
    assert dict(parse_qsl(urlparse(s.requests[1][1]).query))["stopPrice"] == "82000"
    # y recuerda que el endpoint algo no existe
    assert c._algo_supported is False


def test_open_conditional_orders_normalized():
    algo = [{"algoId": 1, "clientAlgoId": "a", "orderType": "STOP_MARKET", "side": "SELL", "triggerPrice": "79000", "quantity": "0.002"}]
    c, _ = client([Resp(200, algo), Resp(200, [])])
    out = c.open_conditional_orders("BTCUSDT")
    assert out == [{"id": 1, "client_id": "a", "type": "STOP_MARKET", "side": "SELL", "trigger": 79000.0, "qty": 0.002, "algo": True}]


def test_position_parsing_and_flat_position():
    pr = [{"symbol": "BTCUSDT", "positionAmt": "0.002", "entryPrice": "80000.0", "markPrice": "80100", "liquidationPrice": "72000",
           "unRealizedProfit": "0.2", "marginType": "isolated", "leverage": "10", "updateTime": 1}]
    c, _ = client([Resp(200, pr)])
    p = c.position("BTCUSDT")
    assert p["amt"] == 0.002 and p["entry"] == 80_000.0 and p["liq"] == 72_000.0 and p["margin_type"] == "ISOLATED"
    flat = [{"symbol": "BTCUSDT", "positionAmt": "0.000", "entryPrice": "0.0", "markPrice": "80100"}]
    c, _ = client([Resp(200, flat)])
    assert c.position("BTCUSDT") is None


def test_position_falls_back_to_v2_when_v3_missing():
    c, s = client([Resp(404, None), Resp(200, [])])
    assert c.position("BTCUSDT") is None
    assert urlparse(s.requests[1][1]).path == "/fapi/v2/positionRisk"


def test_margin_type_already_set_is_ok():
    c, _ = client([Resp(400, {"code": -4046, "msg": "No need to change margin type."})])
    c.change_margin_type("BTCUSDT")        # no debe lanzar


def test_cancel_already_gone_is_ok():
    c, _ = client([Resp(400, {"code": -2011, "msg": "Unknown order sent."})])
    c.cancel_conditional_order("BTCUSDT", {"id": 1, "algo": True})


def test_num_formatting():
    assert num(0.00001) == "0.00001" and num(100.0) == "100" and num(79100.5) == "79100.5" and num(True) == "true"
    assert num(Decimal("0.0010")) == "0.001" and num("12.50") == "12.50"


def test_symbol_rules_rounding_and_parsing():
    r = SymbolRules()
    assert r.floor_qty(0.002444) == 0.002 and r.floor_qty(0.0019999) == 0.001 and r.floor_qty(0.0009) == 0.0
    assert r.round_price(79_100.04) == 79_100.0 and r.round_price(79_100.06) == 79_100.1
    info = {"symbols": [{"symbol": "BTCUSDT", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"}, {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
        {"filterType": "MIN_NOTIONAL", "notional": "100"}]}]}
    r = SymbolRules.from_exchange_info(info, "BTCUSDT")
    assert r.tick_size == Decimal("0.10") and r.max_qty == Decimal("120") and r.min_notional == Decimal("100")
    with pytest.raises(KeyError):
        SymbolRules.from_exchange_info(info, "XXXUSDT")


def test_check_credentials_reports_auth_error():
    c, _ = client([Resp(200, {"serverTime": 1}), Resp(401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action"})])
    res = c.check_credentials("BTCUSDT", verify_trade=False)
    assert not res["ok"] and res["auth"] and res["code"] == -2015
