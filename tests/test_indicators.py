import numpy as np

from botbinance import indicators as I
from conftest import candles


def test_ema_of_linear_series_lags_by_known_amount():
    # EMA de una recta de pendiente 1 se retrasa (1-a)/a = 4.5 barras con n=10
    e = I.ema(np.arange(1, 31, dtype=float), 10)
    assert abs(e[-1] - 25.5) < 1e-9
    assert np.isnan(e[:9]).all() and not np.isnan(e[9])


def test_rsi_matches_wilder_reference():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    r = I.rsi(closes, 14)
    assert abs(r[14] - 70.53) < 0.25      # referencia clásica de StockCharts (usan datos redondeados)
    assert abs(r[15] - 66.32) < 0.25
    assert np.isnan(r[13])


def test_rsi_extremes():
    up = I.rsi(np.arange(1, 60, dtype=float), 14)
    assert up[-1] == 100.0
    down = I.rsi(np.arange(60, 1, -1, dtype=float), 14)
    assert down[-1] < 1.0


def test_indicators_are_causal():
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    df = candles(closes)
    k = 250
    full = I.enrich(df)
    part = I.enrich(df.iloc[: k + 1])
    for col in ("ema20", "ema50", "rsi14", "atr14", "adx14", "bb_up", "stoch_k", "macd", "vol_sma20"):
        assert np.isclose(full[col].iloc[k], part[col].iloc[k], equal_nan=True), col


def test_value_ranges():
    rng = np.random.default_rng(2)
    closes = 100 + np.cumsum(rng.normal(0, 1, 500))
    df = I.enrich(candles(closes))
    last = df.iloc[-1]
    assert 0 <= last.rsi14 <= 100 and 0 <= last.adx14 <= 100 and 0 <= last.stoch_k <= 100
    assert last.atr14 > 0 and last.bb_low < last.bb_mid < last.bb_up


def test_bollinger_uses_population_std():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    mid, up, low = I.bollinger(x, 5, 2.0)
    assert mid[-1] == 3.0
    assert abs(up[-1] - (3.0 + 2.0 * np.sqrt(2.0))) < 1e-12     # ddof=0 -> sigma = sqrt(2)
