import numpy as np
import pandas as pd

from botbinance import patterns as P


def frame(rows):
    """rows: lista de dicts parciales; completa columnas necesarias."""
    base = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100.0, "bb_low": 90.0,
            "bb_up": 110.0, "vol_sma20": 100.0, "rsi14": 50.0, "stoch_k": 50.0, "macd": 0.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_pin_bar_long_and_short(cfg):
    # martillo: cuerpo 1, mecha inferior 5 (>= 2x), mecha superior 0.2 (<= 25% del rango)
    df = frame([{}, {"open": 100, "close": 101, "high": 101.2, "low": 96}])
    assert P.pin_bar(df, "LONG", cfg)[0]
    assert not P.pin_bar(df, "SHORT", cfg)[0]
    # estrella fugaz
    df = frame([{}, {"open": 101, "close": 100, "high": 105, "low": 99.8}])
    assert P.pin_bar(df, "SHORT", cfg)[0]
    assert not P.pin_bar(df, "LONG", cfg)[0]


def test_pin_bar_requires_close_inside_bollinger(cfg):
    df = frame([{}, {"open": 100, "close": 101, "high": 101.2, "low": 96, "bb_low": 105}])
    assert not P.pin_bar(df, "LONG", cfg)[0]


def test_pin_bar_zero_range_candle_is_ignored(cfg):
    df = frame([{}, {"open": 100, "close": 100, "high": 100, "low": 100}])
    assert not P.pin_bar(df, "LONG", cfg)[0]


def test_engulfing_needs_volume(cfg):
    prev = {"open": 102, "close": 100, "high": 102.5, "low": 99.5}                 # bajista
    cur = {"open": 99.8, "close": 103, "high": 103.5, "low": 99.5, "volume": 130}  # alcista envolvente, vol 1.3x
    assert P.engulfing(frame([{}, prev, cur]), "LONG", cfg)[0]
    cur_low_vol = {**cur, "volume": 110}
    assert not P.engulfing(frame([{}, prev, cur_low_vol]), "LONG", cfg)[0]
    # versión bajista
    prev_s = {"open": 100, "close": 102, "high": 102.5, "low": 99.5}
    cur_s = {"open": 102.2, "close": 99, "high": 102.5, "low": 98.5, "volume": 130}
    assert P.engulfing(frame([{}, prev_s, cur_s]), "SHORT", cfg)[0]


def test_inside_bar_fakeout(cfg):
    mother = {"high": 110, "low": 100}
    inside = {"high": 108, "low": 102}
    trig_long = {"high": 106, "low": 98, "close": 104}      # perfora el mínimo de la madre y cierra dentro
    assert P.inside_bar_fakeout(frame([mother, inside, trig_long]), "LONG")[0]
    assert not P.inside_bar_fakeout(frame([mother, inside, {**trig_long, "close": 99}]), "LONG")[0]
    trig_short = {"high": 112, "low": 104, "close": 107}
    assert P.inside_bar_fakeout(frame([mother, inside, trig_short]), "SHORT")[0]
    # si la vela [-2] no es inside bar, no hay patrón
    assert not P.inside_bar_fakeout(frame([mother, {"high": 111, "low": 102}, trig_long]), "LONG")[0]


def test_exhaustion_bollinger_with_rsi(cfg):
    df = frame([{}, {"low": 89, "bb_low": 90, "rsi14": 30}])
    ok, txt = P.exhaustion(df, "LONG", cfg)
    assert ok and "BB inf" in txt
    # perfora la banda pero RSI y estocástico no están en extremo
    assert not P.exhaustion(frame([{}, {"low": 89, "bb_low": 90, "rsi14": 45, "stoch_k": 50}]), "LONG", cfg)[0]
    # estocástico sustituye al RSI
    assert P.exhaustion(frame([{}, {"low": 89, "bb_low": 90, "rsi14": 45, "stoch_k": 15}]), "LONG", cfg)[0]
    # corto
    assert P.exhaustion(frame([{}, {"high": 112, "bb_up": 110, "rsi14": 70}]), "SHORT", cfg)[0]


def test_exhaustion_window(cfg):
    prev = {"low": 89, "bb_low": 90, "rsi14": 30}
    df = frame([{}, prev, {"low": 99, "rsi14": 50}])
    assert not P.exhaustion(df, "LONG", cfg)[0]            # ventana 1: solo la vela gatillo
    cfg.VENTANA_AGOTAMIENTO = 2
    assert P.exhaustion(df, "LONG", cfg)[0]                # ventana 2: incluye la vela previa


def _div_frame(price_lows, rsi_vals, macd_vals):
    n = len(price_lows)
    lows = np.array(price_lows, dtype=float)
    return pd.DataFrame({"low": lows, "high": lows + 2, "rsi14": rsi_vals, "macd": macd_vals,
                         "bb_low": np.full(n, 0.0), "bb_up": np.full(n, 1e9), "stoch_k": np.full(n, 50.0)})


def test_bullish_divergence_detected(cfg):
    # mínimo previo (pivote) en idx 10 con precio 90 / RSI 25; mínimo nuevo en el último idx: precio 88, RSI 32
    n = 30
    lows = [100 - 0.5 * i if i < 10 else 95 + 0.4 * (i - 10) for i in range(n)]
    lows[10] = 90.0
    lows[-1] = 88.0
    for j in range(25, 29):                                  # el tramo final baja hacia el nuevo mínimo
        lows[j] = 96 - (j - 25)
    rsi = np.full(n, 45.0)
    rsi[10], rsi[-1] = 25.0, 32.0
    macd = np.full(n, 0.0)
    macd[10], macd[-1] = -5.0, -2.0
    df = _div_frame(lows, rsi, macd)
    cfg.DIV_LOOKBACK = 25
    ok, txt = P._divergence(df, "LONG", cfg)
    assert ok and "alcista" in txt


def test_no_divergence_when_oscillator_confirms_new_low(cfg):
    n = 30
    lows = [100 - 0.5 * i if i < 10 else 95 + 0.4 * (i - 10) for i in range(n)]
    lows[10], lows[-1] = 90.0, 88.0
    for j in range(25, 29):
        lows[j] = 96 - (j - 25)
    rsi = np.full(n, 45.0)
    rsi[10], rsi[-1] = 25.0, 20.0                            # RSI también hace mínimo más bajo: no hay divergencia
    macd = np.full(n, 0.0)
    macd[10], macd[-1] = -5.0, -8.0
    cfg.DIV_LOOKBACK = 25
    assert not P._divergence(_div_frame(lows, rsi, macd), "LONG", cfg)[0]
