from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from botbinance import risk, strategy
from botbinance.backtester import _simulate
from botbinance.data_feed import DataFeed, klines_to_df, resample
from botbinance.executor import TradeManager
from botbinance.binance_api import SymbolRules
from botbinance.state import BotState
from botbinance.strategy import Signal
from botbinance.util import last_closed_open_time, next_open
from conftest import enriched
from fake_exchange import FakeFutures

H4, D1 = 14_400_000, 86_400_000


def ms(y, m, d, hh=0):
    return int(datetime(y, m, d, hh, tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- temporalidades
def test_monthly_and_derived_timeframes_alignment():
    assert last_closed_open_time("1M", ms(2026, 10, 14)) == ms(2026, 9, 1)
    assert last_closed_open_time("1M", ms(2026, 1, 3)) == ms(2025, 12, 1)
    assert next_open("1M", ms(2026, 2, 1)) == ms(2026, 3, 1) and next_open("1M", ms(2025, 12, 1)) == ms(2026, 1, 1)
    now = ms(2026, 10, 14, 13)
    per = 16 * 3_600_000
    assert last_closed_open_time("16h", now) == (now // per) * per - per
    assert last_closed_open_time("2d", now) % (2 * D1) == 0


def _candles_4h(n, t0):
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.concatenate([[c[0]], c[:-1]])
    ot = t0 + np.arange(n) * H4
    return pd.DataFrame({"open_time": ot, "open": o, "high": np.maximum(o, c) + 1, "low": np.minimum(o, c) - 1,
                         "close": c, "volume": np.full(n, 10.0), "close_time": ot + H4 - 1})


def test_resample_16h_only_complete_blocks():
    per = 16 * 3_600_000
    t0 = per * 1000                                   # alineado a un múltiplo de 16h
    df = _candles_4h(10, t0)                          # 2 bloques completos + 2 velas del tercero
    r = resample(df, "16h")
    assert len(r) == 2 and list(r["open_time"]) == [t0, t0 + per]
    b = df.iloc[:4]
    assert r.loc[0, "open"] == b["open"].iloc[0] and r.loc[0, "close"] == b["close"].iloc[-1]
    assert r.loc[0, "high"] == b["high"].max() and r.loc[0, "low"] == b["low"].min() and r.loc[0, "volume"] == 40.0
    assert r.loc[0, "close_time"] == t0 + per - 1


def test_resample_20h_and_2d():
    per20 = 20 * 3_600_000
    r = resample(_candles_4h(10, per20 * 500), "20h")
    assert len(r) == 2 and r["volume"].iloc[0] == 50.0                      # 5 velas de 4h por bloque
    d = pd.DataFrame({"open_time": D1 * 2000 + np.arange(5) * D1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                      "volume": 1.0, "close_time": D1 * 2000 + np.arange(5) * D1 + D1 - 1})
    assert len(resample(d, "2d")) == 2 and resample(d, "2d")["volume"].iloc[0] == 2.0


class FeedClient:
    def __init__(self, rows):
        self.rows = rows

    def now_ms(self):
        return self.rows[-1][6] + 5_000

    def klines(self, symbol, tf, limit=500, start_time=None, end_time=None):
        return [r for r in self.rows if start_time is None or r[0] >= start_time][-limit:]


def _krow(t, price=100.0, per=H4):
    return [t, str(price), str(price + 1), str(price - 1), str(price + 0.5), "10", t + per - 1, "0", 1, "0", "0", "0"]


def test_derived_candle_is_built_when_base_closes_and_fires_once():
    per = 16 * 3_600_000
    t0 = per * 1000
    cfg = SimpleNamespace(SYMBOL="BTCUSDT", TF_TENDENCIA="16h", TF_TENDENCIA_SUPERIOR="", TF_ZONA="4h", TF_GATILLO="4h",
                          TF_FIBONACCI="4h", BB_PERIODO=20, BB_DESVIACION=2.0)
    rows = [_krow(t0 + i * H4) for i in range(12)]
    fired = []
    feed = DataFeed(FeedClient(rows), cfg, fired.append)
    assert feed.native_tfs == ["4h"] and feed.timeframes == ["4h", "16h"]
    feed._store("4h", klines_to_df(rows[:8]))
    feed._store("16h", resample(feed.raw["4h"], "16h"))
    assert len(feed.raw["16h"]) == 2
    for r in rows[8:]:
        k = {"t": r[0], "T": r[6], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
        feed._append_ws("4h", k)
    assert len(feed.raw["16h"]) == 3 and fired.count("16h") == 1 and fired.count("4h") == 4
    assert "ema20" in feed.frames["16h"].columns


def test_chart_on_demand_for_a_timeframe_not_used_by_the_strategy():
    t0 = 1_700_000_000_000 // 900_000 * 900_000
    rows = [_krow(t0 + i * 900_000, 100 + i * 0.1, per=900_000) for i in range(300)]
    cfg = SimpleNamespace(SYMBOL="BTCUSDT", TF_TENDENCIA="1d", TF_TENDENCIA_SUPERIOR="", TF_ZONA="4h", TF_GATILLO="1h",
                          TF_FIBONACCI="1d", BB_PERIODO=20, BB_DESVIACION=2.0)
    feed = DataFeed(FeedClient(rows), cfg, lambda tf: None)
    d = feed.chart_data("15m", 100)
    assert d["series"] is not None and len(d["series"]["open_time"]) == 100 and d["series"]["ema20"][-1] is not None


# ---------------------------------------------------------------- estrategia con temporalidades propias
def trending(n, start, step, noise=0.0, seed=0):
    return start + step * np.arange(n) + np.random.default_rng(seed).normal(0, noise, n)


def test_trend_on_4h_without_upper_filter_and_pullback_on_15m(cfg):
    cfg.TF_TENDENCIA, cfg.TF_TENDENCIA_SUPERIOR = "4h", ""
    cfg.TF_ZONA, cfg.TF_FIBONACCI, cfg.TF_GATILLO = "15m", "15m", "5m"
    assert cfg.validate() == []
    frames = {"4h": enriched(trending(300, 1000, 2.0, 1.5), tf_ms=14_400_000),
              "15m": enriched(trending(400, 1000, 0.3, 1.0), tf_ms=900_000),
              "5m": enriched(trending(300, 1000, 0.1, 0.5), tf_ms=300_000)}
    ev = strategy.evaluate(frames, cfg, {}, with_sr=True)
    assert ev.trend == "LONG" and len(ev.phase1) == 3                        # sin filtro superior: 3 condiciones
    assert ev.phase1[0].name.startswith("4h")
    assert isinstance(ev.sr, list)


def test_monthly_trend_skips_sma200_when_history_is_short(cfg):
    cfg.TF_TENDENCIA, cfg.TF_TENDENCIA_SUPERIOR = "1M", ""
    cfg.TF_ZONA, cfg.TF_FIBONACCI, cfg.TF_GATILLO = "1d", "1d", "1h"
    frames = {"1M": enriched(trending(86, 1000, 25.0, 3.0), tf_ms=2_592_000_000),
              "1d": enriched(trending(300, 1000, 1.0, 1.0), tf_ms=86_400_000),
              "1h": enriched(trending(300, 1000, 0.1, 0.5), tf_ms=3_600_000)}
    ev = strategy.evaluate(frames, cfg, {})
    sma = next(c for c in ev.phase1 if "SMA(200)" in c.name)
    assert sma.long is None and "n/d" in sma.value
    assert ev.trend == "LONG"                                               # la condición omitida no bloquea la tendencia


def test_timeframe_order_validation(cfg):
    cfg.TF_GATILLO = "4h"
    cfg.TF_ZONA = "1h"
    assert any("gatillo" in e for e in cfg.validate())
    cfg.TF_GATILLO, cfg.TF_ZONA, cfg.TF_TENDENCIA = "5m", "1d", "4h"
    assert any("retrocesos" in e for e in cfg.validate())
    cfg.TF_ZONA, cfg.TF_TENDENCIA, cfg.TF_TENDENCIA_SUPERIOR = "4h", "1d", "12h"
    assert any("superior" in e for e in cfg.validate())
    cfg.TF_TENDENCIA_SUPERIOR, cfg.MARTINGALA_NIVELES = "1w", 9
    assert any("MARTINGALA_NIVELES" in e for e in cfg.validate())


def test_candidate_levels_fibonacci_pivots_and_atr_fallback(cfg):
    cfg.MARTINGALA_NIVELES = 3
    fib = {"valid": True, "start": 100.0, "end": 200.0}
    levels, src = strategy.candidate_levels("LONG", 145.0, 2.0, fib, None, cfg)
    assert levels[0] == 138.2 and levels[1] == 121.4 and src == "Fibonacci"     # el nivel 150 queda por encima del precio
    fib_s = {"valid": True, "start": 200.0, "end": 100.0}
    levels, _ = strategy.candidate_levels("SHORT", 155.0, 2.0, fib_s, None, cfg)
    assert levels[0] == 161.8 and levels[1] == 178.6
    levels, src = strategy.candidate_levels("LONG", 100.0, 0.1, None, None, cfg)  # sin Fibonacci ni pivotes: pasos de ATR
    assert src == "ATR" and levels == [99.7, 99.4]


# ---------------------------------------------------------------- martingala: riesgo
def msig(side="LONG", levels=(79_000.0,), price=80_000.0, low=79_700.0, high=80_300.0, atr=400.0):
    return Signal(side=side, pattern="Pin Bar", exhaustion="x", open_time=0, close_time=1, open=price, high=high, low=low,
                  close=price, atr=atr, levels=list(levels), levels_src="Fibonacci")


def test_martingale_does_not_fit_with_default_100_usd_account(cfg, rules):
    cfg.MARTINGALA_ACTIVA = True
    p = risk.build_plan(cfg, msig(), 80_000.0, rules)
    assert p.approved and not p.martingale and len(p.levels) == 0            # cae a entrada única
    assert any("NO cabe" in n for n in p.notes)


def test_martingale_two_levels_when_budget_and_risk_allow(cfg, rules):
    cfg.MARTINGALA_ACTIVA, cfg.MARGEN_EN_USO_POR_OPERACION, cfg.MARTINGALA_RIESGO_MAX_USDT = True, 100.0, 9.0
    p = risk.build_plan(cfg, msig(), 80_000.0, rules)
    assert p.approved and p.martingale and len(p.levels) == 2
    q0, q1 = p.levels[0]["qty"], p.levels[1]["qty"]
    assert q0 == 0.002 and q1 == 0.004                                       # 1x y 2x
    assert abs(p.sl_price - (79_000 - 600)) < 1e-6                           # SL común detrás del último nivel
    assert p.loss_sl_usdt <= 9.0 and p.notional <= 100.0 * 10 and p.qty == q0
    # la pérdida con TODOS los niveles ejecutados es mayor que con la entrada única: por eso el riesgo se amplía
    single = risk._single_plan(cfg, msig(), 80_000.0, rules)
    assert p.loss_sl_usdt > single.loss_sl_usdt


def test_martingale_degrades_to_fewer_levels(cfg, rules):
    cfg.MARTINGALA_ACTIVA, cfg.MARGEN_EN_USO_POR_OPERACION, cfg.MARTINGALA_RIESGO_MAX_USDT = True, 100.0, 9.0
    cfg.MARTINGALA_NIVELES = 3
    p = risk.build_plan(cfg, msig(levels=(79_000.0, 78_000.0)), 80_000.0, rules)
    assert p.approved and p.martingale and len(p.levels) == 2                # 1:2:4 no cabe (0.001 BTC < nominal mínimo)


def test_martingale_total_loss_never_exceeds_cap_for_short(cfg, rules):
    cfg.MARTINGALA_ACTIVA, cfg.MARGEN_EN_USO_POR_OPERACION, cfg.MARTINGALA_RIESGO_MAX_USDT = True, 100.0, 9.0
    p = risk.build_plan(cfg, msig("SHORT", levels=(81_000.0,), low=79_700.0, high=80_300.0), 80_000.0, rules)
    assert p.martingale and p.sl_price > 81_000 and p.loss_sl_usdt <= 9.0
    assert all(l["price"] >= 80_000 for l in p.levels)
    assert "MARTINGALA" in risk.format_board(cfg, 80_000.0, rules, p)


# ---------------------------------------------------------------- martingala: ejecución
def _mart_cfg(cfg):
    cfg.MARTINGALA_ACTIVA, cfg.MARGEN_EN_USO_POR_OPERACION, cfg.MARTINGALA_RIESGO_MAX_USDT = True, 100.0, 9.0
    return cfg


def _open_mart(cfg):
    state, notes = BotState(), []
    tm = TradeManager(_mart_cfg(cfg), state, notes.append)
    tm.client, tm.rules = FakeFutures(80_000.0), SymbolRules()
    plan = risk.build_plan(cfg, msig(), 80_000.0, tm.rules)
    assert plan.martingale
    return tm, tm.client, notes, tm.open_trade(plan, msig())


def test_martingale_open_places_limit_level_and_close_position_protections(cfg):
    tm, ex, notes, t = _open_mart(cfg)
    assert t.martingale and len(t.levels) == 2 and t.levels[0]["filled"] and not t.levels[1]["filled"]
    assert [c for c in ex.calls if c[0] == "limit"] == [("limit", "BUY", 0.004, 79_000.0)]
    conds = [c for c in ex.calls if c[0] == "conditional"]
    assert len(conds) == 2 and all(c[6] is True for c in conds)              # SL y TP cierran TODA la posición
    assert abs(t.sl - 78_400.0) < 0.11 and any("Martingala" in n for n in notes)


def test_level_fill_updates_average_entry_and_take_profit(cfg):
    tm, ex, notes, t = _open_mart(cfg)
    ex.fill_limit(0)                                                        # el precio llega al soporte
    tm.monitor_tick()
    assert t.levels[1]["filled"] and abs(t.qty - 0.006) < 1e-9
    assert abs(t.entry - (0.002 * 80_000 + 0.004 * 79_000) / 0.006) < 0.01
    tp = [o for o in ex.orders if o["type"] == "TAKE_PROFIT_MARKET"]
    assert len(tp) == 1 and abs(tp[0]["trigger"] - round(t.entry + 3 * (t.entry - t.sl_initial), 1)) < 0.11
    assert [o for o in ex.orders if o["type"] == "STOP_MARKET"][0]["trigger"] == t.sl      # el SL no se mueve
    assert any("NIVEL 2/2" in n for n in notes)


def test_break_even_after_level_fill_cancels_pending_levels_and_uses_average(cfg):
    tm, ex, _, t = _open_mart(cfg)
    ex.fill_limit(0)
    tm.monitor_tick()
    ex.price = t.entry + 1.05 * (t.entry - t.sl_initial)
    tm.monitor_tick()
    assert t.be_done and abs(t.sl - round(t.entry * 1.001, 1)) < 0.11
    assert ex.limits == []


def test_break_even_cancels_unfilled_levels(cfg):
    tm, ex, _, t = _open_mart(cfg)
    ex.price = t.entry + 1.05 * (t.entry - t.sl_initial)                     # sube sin haber tocado el nivel 2
    tm.monitor_tick()
    assert t.be_done and ex.limits == []                                     # ya no se añaden niveles con riesgo cero


def test_closing_cancels_pending_limit_levels(cfg):
    tm, ex, _, t = _open_mart(cfg)
    ex.trigger("TAKE_PROFIT_MARKET")                                         # TP antes de llegar al nivel 2
    tm.monitor_tick()
    assert tm.trade is None and ex.limits == [] and tm.history[-1]["reason"] == "TAKE PROFIT"


def test_stop_loss_after_all_levels_filled_accounts_the_full_loss(cfg):
    tm, ex, _, t = _open_mart(cfg)
    ex.fill_limit(0)
    tm.monitor_tick()
    ex.trigger("STOP_MARKET")
    tm.monitor_tick()
    rec = tm.history[-1]
    assert rec["reason"] == "STOP LOSS" and rec["net"] < 0 and abs(rec["net"]) <= 9.0 + 0.5


def test_level_order_rejected_does_not_block_the_trade(cfg):
    state, notes = BotState(), []
    tm = TradeManager(_mart_cfg(cfg), state, notes.append)
    tm.client, tm.rules = FakeFutures(80_000.0), SymbolRules()
    tm.client.fail_limits = True
    t = tm.open_trade(risk.build_plan(cfg, msig(), 80_000.0, tm.rules), msig())
    assert tm.trade is t and t.levels[1]["order"] is None and any("nivel de entrada" in n for n in notes)


def test_emergency_close_cancels_limit_levels(cfg):
    tm, ex, _, t = _open_mart(cfg)
    assert ex.limits
    tm.close_now()
    assert ex.limits == [] and ex.pos is None


# ---------------------------------------------------------------- martingala: simulador del backtest
def _arrays(rows):
    a = np.array(rows, dtype=float)
    n = len(a)
    ot = np.arange(n) * 3_600_000
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], ot, ot + 3_599_999


def test_backtest_sim_martingale_level_then_take_profit(cfg, rules):
    cfg.ACTIVAR_BREAK_EVEN = False
    plan = risk.build_plan(_mart_cfg(cfg), msig(), 80_000.0, rules)
    rows = [[80_000, 80_000, 80_000, 80_000], [80_000, 80_100, 79_900, 80_000],
            [79_800, 79_900, 78_900, 79_000], [79_900, 82_200, 79_800, 82_000]]
    o, h, lo, c, ot, ct = _arrays(rows)
    st, idx = _simulate(None, 0, plan, cfg, 0.0, o, h, lo, c, ot, ct)
    assert st.levels_filled == 2 and abs(st.entry - 79_333.33) < 0.01 and st.reason == "TAKE PROFIT" and idx == 3
    assert st.net > 15 and abs(st.exit - round(79_333.33 + 3 * (79_333.33 - 78_400), 1)) < 1.0


def test_backtest_sim_martingale_level_then_stop_loss(cfg, rules):
    cfg.ACTIVAR_BREAK_EVEN = False
    plan = risk.build_plan(_mart_cfg(cfg), msig(), 80_000.0, rules)
    rows = [[80_000, 80_000, 80_000, 80_000], [80_000, 80_100, 79_900, 80_000], [79_800, 79_900, 78_300, 78_500]]
    o, h, lo, c, ot, ct = _arrays(rows)
    st, _ = _simulate(None, 0, plan, cfg, 0.0, o, h, lo, c, ot, ct)
    assert st.levels_filled == 2 and st.reason == "STOP LOSS" and st.net < 0 and abs(st.net) <= 9.0 + 0.5


def test_backtest_sim_without_martingale_is_unchanged(cfg, rules):
    cfg.ACTIVAR_BREAK_EVEN = False
    plan = risk.build_plan(cfg, msig(), 80_000.0, rules)                    # martingala desactivada
    rows = [[80_000, 80_000, 80_000, 80_000], [80_000, 80_100, 79_900, 80_000], [80_000, 83_000, 79_950, 82_900]]
    o, h, lo, c, ot, ct = _arrays(rows)
    st, _ = _simulate(None, 0, plan, cfg, 0.0, o, h, lo, c, ot, ct)
    assert st.levels_filled == 1 and st.reason == "TAKE PROFIT"
