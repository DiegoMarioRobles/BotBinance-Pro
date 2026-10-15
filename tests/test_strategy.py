
import numpy as np

from botbinance import strategy
from botbinance.backtester import _simulate
from botbinance.risk import TradePlan
from conftest import enriched


def trending(n, start, step, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    return start + step * np.arange(n) + rng.normal(0, noise, n)


def test_zigzag_confirms_pivots():
    # 100 -> 200 -> 140 -> 220
    px = np.concatenate([np.linspace(100, 200, 40), np.linspace(200, 140, 30), np.linspace(140, 220, 40)])
    high, low = px + 0.5, px - 0.5
    piv = strategy.zigzag(high, low, np.full(len(px), 15.0))
    kinds = [k for _, _, k in piv]
    assert kinds == ["L", "H", "L"]
    assert abs(piv[1][1] - 200.5) < 1e-9 and abs(piv[2][1] - 139.5) < 1e-9


def test_fib_zone_long_after_impulse(cfg):
    px = np.concatenate([np.linspace(100, 100, 30), np.linspace(100, 200, 40), np.linspace(200, 145, 25)])
    df = enriched(px, spread=0.5, tf_ms=86_400_000)
    z = strategy.fib_zone(df, "LONG", cfg, {})
    assert z and z["valid"]
    # impulso 99.5 -> 200.5 : retroceso 50% = 150, 61.8% = 138.1
    assert abs(z["zone_hi"] - (z["end"] - 0.5 * z["impulse"])) < 1e-9
    assert z["zone_lo"] < 145 < z["zone_hi"] + 6


def test_fib_zone_short_mirrored(cfg):
    px = np.concatenate([np.linspace(300, 300, 30), np.linspace(300, 200, 40), np.linspace(200, 255, 25)])
    df = enriched(px, spread=0.5, tf_ms=86_400_000)
    z = strategy.fib_zone(df, "SHORT", cfg, {})
    assert z and z["valid"] and z["zone_lo"] < z["zone_hi"]
    assert z["end"] < z["start"]


def test_fib_small_impulse_is_invalid(cfg):
    rng = np.random.default_rng(3)
    px = 100 + np.cumsum(rng.normal(0, 3, 200))                # mucho ruido: el impulso nunca supera 4 ATR claramente
    cfg.FIB_IMPULSO_MIN_ATR = 50.0
    df = enriched(px, spread=1.0, tf_ms=86_400_000)
    z = strategy.fib_zone(df, "LONG", cfg, {})
    assert z is None or not z["valid"]


def make_frames(cfg, w_slope=40.0, d_slope=8.0):
    """Marcos sintéticos con tendencia alcista fuerte y limpia."""
    frames = {
        "1w": enriched(trending(120, 1000, w_slope, 5), tf_ms=604_800_000),
        "1d": enriched(trending(420, 1000, d_slope, 4), tf_ms=86_400_000),
        cfg.TF_ZONA: enriched(trending(500, 1000, 1.5, 2), tf_ms=14_400_000),
        cfg.TF_GATILLO: enriched(trending(300, 1000, 0.3, 1), tf_ms=3_600_000),
    }
    return frames


def test_evaluate_detects_uptrend_and_waits_for_pullback(cfg):
    ev = strategy.evaluate(make_frames(cfg), cfg, {})
    assert ev.trend == "LONG" and ev.phase1_ok
    assert ev.phase2_ok is False                       # el precio está lejos de la zona EMA20-EMA50: no hay retroceso
    assert ev.signal is None and len(ev.phase1) == 5


def test_evaluate_standby_when_adx_low(cfg):
    rng = np.random.default_rng(5)
    flat = 1000 + rng.normal(0, 2, 420)
    frames = make_frames(cfg)
    frames["1d"] = enriched(flat, tf_ms=86_400_000)
    ev = strategy.evaluate(frames, cfg, {})
    assert ev.trend == "STANDBY"


def test_evaluate_downtrend_is_short(cfg):
    frames = {
        "1w": enriched(trending(120, 6000, -40.0, 5), tf_ms=604_800_000),
        "1d": enriched(trending(420, 6000, -8.0, 4), tf_ms=86_400_000),
        cfg.TF_ZONA: enriched(trending(500, 6000, -1.5, 2), tf_ms=14_400_000),
        cfg.TF_GATILLO: enriched(trending(300, 6000, -0.3, 1), tf_ms=3_600_000),
    }
    ev = strategy.evaluate(frames, cfg, {})
    assert ev.trend == "SHORT"


def test_evaluate_with_insufficient_history(cfg):
    frames = make_frames(cfg)
    frames["1d"] = frames["1d"].iloc[:40]
    ev = strategy.evaluate(frames, cfg, {})
    assert ev.trend == "SIN_DATOS"


def test_full_signal_path(cfg):
    """Fuerza las 3 fases en Estrategia 2: tendencia alcista + retroceso a la zona EMA + Pin Bar con agotamiento."""
    cfg.ESTRATEGIA = "estrategia_2"
    frames = make_frames(cfg)
    trig = frames[cfg.TF_GATILLO]
    zone = frames[cfg.TF_ZONA]
    z = zone.iloc[-1]
    price = float((z.ema20 + z.ema50) / 2)              # precio dentro de la zona de valor
    d1 = frames["1d"].copy()
    d1.loc[d1.index[-1], "rsi14"] = 48.0                # RSI diario en rango 40-55
    frames["1d"] = d1
    t = trig.copy()
    i = t.index[-1]
    t.loc[i, ["open", "close", "high", "low"]] = [price, price + 1.0, price + 1.2, price - 8.0]   # martillo
    t.loc[i, ["bb_low"]] = price - 5.0                    # la mecha perfora la banda inferior, el cierre queda dentro
    t.loc[i, ["rsi14"]] = 30.0
    frames[cfg.TF_GATILLO] = t
    ev = strategy.evaluate(frames, cfg, {})
    assert ev.trend == "LONG" and ev.phase2_ok, ev.note
    assert ev.phase3_ok and ev.signal is not None
    assert ev.signal.side == "LONG" and "Pin Bar" in ev.signal.pattern
    assert ev.signal.low == price - 8.0


def test_estrategia_1_signal(cfg):
    """Verifica Estrategia 1 (por defecto): Tendencia alcista + RSI < 20 + toque de Banda de Bollinger inferior."""
    cfg.ESTRATEGIA = "estrategia_1"
    frames = make_frames(cfg)
    trig = frames[cfg.TF_GATILLO]
    z = frames[cfg.TF_ZONA].iloc[-1]
    price = float(z.ema20)
    t = trig.copy()
    i = t.index[-1]
    # Vela de martillo tocando banda inferior y con RSI < 20
    t.loc[i, ["open", "close", "high", "low"]] = [price, price + 1.0, price + 1.2, price - 8.0]
    t.loc[i, ["bb_low"]] = price - 5.0
    t.loc[i, ["rsi14"]] = 18.0
    frames[cfg.TF_GATILLO] = t
    ev = strategy.evaluate(frames, cfg, {})
    assert ev.trend == "LONG"
    assert ev.phase2_ok, ev.note
    assert ev.phase3_ok and ev.signal is not None
    assert ev.signal.side == "LONG"
    assert ev.execution_points is not None
    assert ev.execution_points["target_reached"] is True



# ---------------------------------------------------------------- simulador del backtest
def _plan(side="LONG", dist_sl=0.01, rr=3.0, qty=0.002, loss=2.0):
    return TradePlan(side=side, ref_price=100.0, qty=qty, notional=qty * 100, margin=20.0, leverage=10,
                     dist_sl=dist_sl, dist_tp=dist_sl * rr, loss_sl_usdt=loss, approved=True)


def _arrays(rows):
    a = np.array(rows, dtype=float)       # columnas: open, high, low, close
    n = len(a)
    ot = np.arange(n) * 3_600_000
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], ot, ot + 3_599_999


def _run(cfg, plan, rows, slip=0.0):
    o, h, lo, c, ot, ct = _arrays(rows)
    return _simulate(None, 0, plan, cfg, slip, o, h, lo, c, ot, ct)


def test_sim_long_take_profit(cfg):
    cfg.ACTIVAR_BREAK_EVEN = False
    st, idx = _run(cfg, _plan(), [[100, 100, 100, 100], [100, 101, 99.5, 100.5], [100.5, 104, 100, 103]])
    assert st.reason == "TAKE PROFIT" and idx == 2 and st.net > 0
    assert abs(st.exit - 103.0) < 1e-9                              # TP = 100 * (1 + 3%)


def test_sim_long_stop_loss(cfg):
    cfg.ACTIVAR_BREAK_EVEN = False
    st, _ = _run(cfg, _plan(), [[100, 100, 100, 100], [100, 100.5, 98.5, 99], [99, 99, 99, 99]])
    assert st.reason == "STOP LOSS" and abs(st.exit - 99.0) < 1e-9 and st.net < 0
    assert abs(st.r - st.net / 2.0) < 1e-9


def test_sim_same_candle_sl_and_tp_assumes_sl(cfg):
    cfg.ACTIVAR_BREAK_EVEN = False
    st, _ = _run(cfg, _plan(), [[100, 100, 100, 100], [100, 104, 98.5, 100]])
    assert st.reason == "STOP LOSS"


def test_sim_break_even_then_stop_at_entry(cfg):
    cfg.ACTIVAR_BREAK_EVEN = True
    rows = [[100, 100, 100, 100],
            [100, 101.2, 99.8, 101],           # toca +1R (101) -> BE activo desde la vela siguiente
            [101, 101.1, 99.5, 100.2]]         # vuelve a la entrada: salta el SL en 100.1
    st, _ = _run(cfg, _plan(), rows)
    assert st.be and st.reason == "BREAK-EVEN"
    assert abs(st.exit - 100.1) < 1e-9
    assert abs(st.gross - 0.002 * 0.1) < 1e-9


def test_sim_short_and_gap(cfg):
    cfg.ACTIVAR_BREAK_EVEN = False
    st, _ = _run(cfg, _plan("SHORT"), [[100, 100, 100, 100], [100, 100.4, 96.5, 97]])
    assert st.reason == "TAKE PROFIT" and abs(st.exit - 97.0) < 1e-9
    # entra en la vela 1 (apertura 100, SL=99); la vela 2 abre con un gap bajista a 97, a través del SL
    st, _ = _run(cfg, _plan(), [[100, 100, 100, 100], [100, 100.2, 99.9, 100], [97, 97.5, 96.5, 97]])
    assert st.reason == "STOP LOSS" and st.exit == 97.0


def test_sim_slippage_and_fees(cfg):
    cfg.ACTIVAR_BREAK_EVEN = False
    st, _ = _run(cfg, _plan(), [[100, 100, 100, 100], [100, 104, 100, 103]], slip=0.001)
    assert st.entry > 100.0 and st.exit < 103.5
    assert st.fees > 0
