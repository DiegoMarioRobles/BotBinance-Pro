import pytest

from botbinance import paths
from botbinance.binance_api import SymbolRules
from botbinance.executor import TradeManager, TradingError
from botbinance.risk import build_plan
from botbinance.state import BotState
from botbinance.strategy import Signal
from botbinance.util import read_json
from fake_exchange import FakeFutures


def make(cfg, price=80_000.0):
    state, notes = BotState(), []
    tm = TradeManager(cfg, state, notes.append)
    tm.client = FakeFutures(price)
    tm.rules = SymbolRules()
    return tm, tm.client, state, notes


def signal(side="LONG", price=80_000.0):
    low, high = (price - 300, price + 300)
    return Signal(side=side, pattern="Pin Bar", exhaustion="x", open_time=0, close_time=1, open=price, high=high,
                  low=low, close=price, atr=400.0)


def open_long(cfg, tm, ex, price=80_000.0):
    sig = signal("LONG", price)
    plan = build_plan(cfg, sig, price, tm.rules)
    assert plan.approved
    return plan, tm.open_trade(plan, sig)


def test_open_trade_places_isolated_leverage_and_protections(cfg):
    tm, ex, state, notes = make(cfg)
    plan, t = open_long(cfg, tm, ex)
    assert ("margin", "BTCUSDT", "ISOLATED") in ex.calls and ("leverage", "BTCUSDT", 10) in ex.calls
    # entrada a mercado + SL + TP, ambos reduceOnly y con precio de marca
    market = [c for c in ex.calls if c[0] == "market"]
    cond = [c for c in ex.calls if c[0] == "conditional"]
    assert len(market) == 1 and market[0][1] == "BUY" and market[0][2] == plan.qty
    types = {c[1]: c for c in cond}
    assert set(types) == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    assert all(c[4] is True and c[5] == "MARK_PRICE" for c in cond)
    assert t.sl < 80_000 < t.tp and abs(t.sl - types["STOP_MARKET"][2]) < 1e-9
    assert abs(t.sl - round(80_000 * (1 - plan.dist_sl), 1)) < 0.11               # redondeado al tick (0.1)
    assert abs((t.tp - t.entry) - 3 * (t.entry - t.sl)) < 0.5
    assert tm.trade is t and read_json(paths.TRADE_FILE)["id"] == t.id            # persistido
    assert len(ex.orders) == 2 and any("ABIERTA" in n for n in notes)


def test_short_trade_sides(cfg):
    tm, ex, _, _ = make(cfg)
    sig = signal("SHORT")
    plan = build_plan(cfg, sig, 80_000.0, tm.rules)
    t = tm.open_trade(plan, sig)
    assert [c for c in ex.calls if c[0] == "market"][0][1] == "SELL"
    assert t.sl > 80_000 > t.tp
    assert all(o["side"] == "BUY" for o in ex.orders)


def test_stop_loss_failure_triggers_emergency_close(cfg):
    tm, ex, _, notes = make(cfg)
    ex.fail_sl = True
    sig = signal()
    plan = build_plan(cfg, sig, 80_000.0, tm.rules)
    with pytest.raises(TradingError):
        tm.open_trade(plan, sig)
    assert ex.pos is None                                  # la posición NO se queda sin Stop Loss
    assert tm.trade is None
    assert any("Stop Loss" in n for n in notes)
    assert tm.history and "EMERGENCIA" in tm.history[-1]["reason"]


def test_take_profit_failure_keeps_trade_with_stop(cfg):
    tm, ex, _, notes = make(cfg)
    ex.fail_tp = True
    _, t = open_long(cfg, tm, ex)
    assert ex.pos is not None and t.sl_order and t.tp_order is None
    assert any("Take Profit" in n for n in notes)


def test_sl_beyond_real_liquidation_closes_position(cfg):
    tm, ex, _, notes = make(cfg)
    ex.liq = 79_900.0                                      # liquidación real más cerca que el SL (~79_100)
    sig = signal()
    plan = build_plan(cfg, sig, 80_000.0, tm.rules)
    with pytest.raises(TradingError):
        tm.open_trade(plan, sig)
    assert ex.pos is None and any("liquidación" in n for n in notes)


def test_entry_timeout_recovers_existing_position(cfg, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    tm, ex, _, _ = make(cfg)
    ex.timeout_entry = True
    _, t = open_long(cfg, tm, ex)
    assert ex.pos is not None and len(ex.orders) == 2      # se detectó la posición y se protegió


def test_hedge_mode_is_refused(cfg):
    tm, ex, _, _ = make(cfg)
    ex.hedge = True
    sig = signal()
    plan = build_plan(cfg, sig, 80_000.0, tm.rules)
    with pytest.raises(TradingError, match="Hedge"):
        tm.open_trade(plan, sig)
    assert ex.pos is None


def test_no_second_trade_while_open(cfg):
    tm, ex, _, _ = make(cfg)
    open_long(cfg, tm, ex)
    sig = signal()
    with pytest.raises(TradingError):
        tm.open_trade(build_plan(cfg, sig, 80_000.0, tm.rules), sig)


def test_break_even_moves_stop_at_1r(cfg):
    tm, ex, _, notes = make(cfg)
    _, t = open_long(cfg, tm, ex)
    old_sl, old_id = t.sl, t.sl_order["id"]
    risk = t.entry - old_sl
    ex.price = t.entry + 0.5 * risk
    tm.monitor_tick()
    assert not t.be_done and t.sl == old_sl                # aún no llegó a 1:1
    ex.price = t.entry + 1.01 * risk
    tm.monitor_tick()
    assert t.be_done
    assert abs(t.sl - round(t.entry * 1.001, 1)) < 0.11    # entrada + 0.1%
    sls = [o for o in ex.orders if o["type"] == "STOP_MARKET"]
    assert len(sls) == 1 and sls[0]["id"] != old_id        # el SL anterior fue cancelado
    assert any("BREAK-EVEN" in n for n in notes)
    assert read_json(paths.TRADE_FILE)["be_done"] is True


def test_break_even_disabled(cfg):
    cfg.ACTIVAR_BREAK_EVEN = False
    tm, ex, _, _ = make(cfg)
    _, t = open_long(cfg, tm, ex)
    ex.price = t.entry + 2 * (t.entry - t.sl)
    tm.monitor_tick()
    assert not t.be_done


def test_take_profit_hit_is_accounted(cfg):
    tm, ex, state, notes = make(cfg)
    _, t = open_long(cfg, tm, ex)
    ex.trigger("TAKE_PROFIT_MARKET")
    tm.monitor_tick()
    assert tm.trade is None and ex.pos is None and ex.orders == []
    rec = tm.history[-1]
    assert rec["reason"] == "TAKE PROFIT" and rec["net"] > 0
    expected_gross = (t.tp - t.entry) * t.qty
    assert abs(rec["pnl"] - expected_gross) < 1e-6 and rec["fees"] > 0
    assert any("CERRADA" in n and "TAKE PROFIT" in n for n in notes)
    assert read_json(paths.TRADE_FILE) == {}               # estado persistido limpio


def test_stop_loss_hit_and_break_even_labels(cfg):
    tm, ex, _, _ = make(cfg)
    _, t = open_long(cfg, tm, ex)
    ex.trigger("STOP_MARKET")
    tm.monitor_tick()
    assert tm.history[-1]["reason"] == "STOP LOSS" and tm.history[-1]["net"] < 0
    # con break-even activado, el cierre en el SL movido se etiqueta BREAK-EVEN
    tm2, ex2, _, _ = make(cfg)
    ex2.fills.clear()
    _, t2 = open_long(cfg, tm2, ex2)
    ex2.price = t2.entry + 1.1 * (t2.entry - t2.sl)
    tm2.monitor_tick()
    ex2.trigger("STOP_MARKET")
    tm2.monitor_tick()
    assert tm2.history[-1]["reason"] == "BREAK-EVEN"


def test_missing_stop_is_replaced(cfg):
    tm, ex, _, notes = make(cfg)
    _, t = open_long(cfg, tm, ex)
    ex.orders = [o for o in ex.orders if o["type"] != "STOP_MARKET"]     # alguien canceló el SL a mano
    tm._last_protection_check = 0
    tm.monitor_tick()
    assert any(o["type"] == "STOP_MARKET" for o in ex.orders)
    assert any("Stop Loss había desaparecido" in n for n in notes)


def test_missing_stop_unrecoverable_closes_position(cfg, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    tm, ex, _, _ = make(cfg)
    _, t = open_long(cfg, tm, ex)
    ex.orders = [o for o in ex.orders if o["type"] != "STOP_MARKET"]
    ex.fail_sl = True
    tm._last_protection_check = 0
    tm.monitor_tick()
    assert ex.pos is None and tm.trade is None


def test_restart_recovers_open_trade(cfg):
    tm, ex, _, _ = make(cfg)
    _, t = open_long(cfg, tm, ex)
    tm2 = TradeManager(cfg, BotState(), lambda s: None)       # "reinicio": nuevo gestor, mismo exchange y archivo
    tm2.client, tm2.rules = ex, tm.rules
    tm2.reconcile_on_start()
    assert tm2.trade is not None and tm2.trade.id == t.id and tm2.trade.sl == t.sl


def test_restart_when_position_closed_while_offline(cfg):
    tm, ex, _, _ = make(cfg)
    open_long(cfg, tm, ex)
    ex.trigger("STOP_MARKET")
    tm2 = TradeManager(cfg, BotState(), lambda s: None)
    tm2.client, tm2.rules = ex, tm.rules
    tm2.reconcile_on_start()
    assert tm2.trade is None and tm2.history[-1]["reason"] == "STOP LOSS"


def test_foreign_position_is_not_managed(cfg):
    tm, ex, state, notes = make(cfg)
    ex.pos = {"amt": 0.01, "entry": 79_000.0}                  # posición abierta a mano por el usuario
    tm.monitor_tick()
    assert tm.trade is None and state.foreign_position is not None and tm.busy()
    assert "foreign" in state.alerts and ex.orders == []        # no se le añaden órdenes ni se toca
    sig = signal()
    with pytest.raises(TradingError):
        tm.open_trade(build_plan(cfg, sig, 80_000.0, tm.rules), sig)


def test_manual_close(cfg):
    tm, ex, _, _ = make(cfg)
    open_long(cfg, tm, ex)
    assert "cerrada" in tm.close_now().lower()
    assert ex.pos is None and tm.trade is None and ex.orders == []
    assert "No hay posición" in tm.close_now()
