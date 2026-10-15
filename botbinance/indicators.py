"""Indicadores técnicos calculados localmente (Binance no los entrega por API).

Se implementan con el mismo suavizado que TradingView (RMA de Wilder y EMA sembradas con SMA)
para que los valores coincidan con lo que ves en el gráfico.
Todos son causales: el valor de la vela k solo depende de las velas <= k.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(x, n: int) -> np.ndarray:
    return pd.Series(np.asarray(x, dtype=float)).rolling(n, min_periods=n).mean().to_numpy()


def _recursive_ma(x, n: int, alpha: float) -> np.ndarray:
    """Media móvil recursiva y[i] = y[i-1]*(1-alpha) + x[i]*alpha, sembrada con la SMA de los n primeros datos válidos."""
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape, np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if valid.size == 0:
        return out
    first = int(valid[0])
    if len(x) - first < n:
        return out
    seed_end = first + n
    out[seed_end - 1] = x[first:seed_end].mean()
    keep = 1.0 - alpha
    prev = out[seed_end - 1]
    for i in range(seed_end, len(x)):
        xi = x[i]
        if xi == xi:  # no NaN
            prev = prev * keep + xi * alpha
        out[i] = prev
    return out


def ema(x, n: int) -> np.ndarray:
    return _recursive_ma(x, n, 2.0 / (n + 1))


def rma(x, n: int) -> np.ndarray:
    return _recursive_ma(x, n, 1.0 / n)


def rsi(close, n: int = 14) -> np.ndarray:
    c = np.asarray(close, dtype=float)
    delta = np.diff(c, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[0] = loss[0] = np.nan
    avg_gain = rma(gain, n)
    avg_loss = rma(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    out = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, out)
    out = np.where((avg_loss == 0) & (avg_gain == 0), 50.0, out)
    out[np.isnan(avg_gain) | np.isnan(avg_loss)] = np.nan
    return out


def true_range(high, low, close) -> np.ndarray:
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    prev_c = np.roll(c, 1)
    prev_c[0] = np.nan
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    tr[0] = np.nan
    return tr


def atr(high, low, close, n: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close), n)


def adx(high, low, close, n: int = 14):
    """Devuelve (ADX, +DI, -DI) con suavizado de Wilder."""
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    up = np.diff(h, prepend=np.nan)
    down = -np.diff(l, prepend=np.nan)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm[0] = minus_dm[0] = np.nan
    tr_s = rma(true_range(high, low, close), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, n) / tr_s
        minus_di = 100.0 * rma(minus_dm, n) / tr_s
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    return rma(dx, n), plus_di, minus_di


def bollinger(close, n: int = 20, k: float = 2.0):
    """Bandas de Bollinger (desviación poblacional, igual que TradingView)."""
    s = pd.Series(np.asarray(close, dtype=float))
    mid = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return mid.to_numpy(), (mid + k * sd).to_numpy(), (mid - k * sd).to_numpy()


def stochastic(high, low, close, k_period: int = 14, k_smooth: int = 3, d_smooth: int = 3):
    """Estocástico lento: %K suavizado con SMA(k_smooth) y %D = SMA(d_smooth) de %K."""
    h = pd.Series(np.asarray(high, dtype=float))
    l = pd.Series(np.asarray(low, dtype=float))
    c = pd.Series(np.asarray(close, dtype=float))
    hh = h.rolling(k_period, min_periods=k_period).max()
    ll = l.rolling(k_period, min_periods=k_period).min()
    rng = (hh - ll).replace(0.0, np.nan)
    raw = 100.0 * (c - ll) / rng
    k = raw.rolling(k_smooth, min_periods=k_smooth).mean()
    d = k.rolling(d_smooth, min_periods=d_smooth).mean()
    return k.to_numpy(), d.to_numpy()


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    c = np.asarray(close, dtype=float)
    line = ema(c, fast) - ema(c, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def enrich(df: pd.DataFrame, bb_period: int = 20, bb_dev: float = 2.0) -> pd.DataFrame:
    """Devuelve una copia de `df` (velas cerradas OHLCV) con todas las columnas de indicadores."""
    out = df.copy()
    c, h, l, v = (out[k].to_numpy(dtype=float) for k in ("close", "high", "low", "volume"))
    out["ema20"] = ema(c, 20)
    out["ema50"] = ema(c, 50)
    out["sma200"] = sma(c, 200)
    out["rsi14"] = rsi(c, 14)
    out["atr14"] = atr(h, l, c, 14)
    a, pdi, mdi = adx(h, l, c, 14)
    out["adx14"], out["plus_di"], out["minus_di"] = a, pdi, mdi
    out["bb_mid"], out["bb_up"], out["bb_low"] = bollinger(c, bb_period, bb_dev)
    out["stoch_k"], out["stoch_d"] = stochastic(h, l, c)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(c)
    out["vol_sma20"] = sma(v, 20)
    return out
