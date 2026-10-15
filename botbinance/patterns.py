"""Patrones de velas japonesas, divergencias y condiciones de agotamiento.

Convención: `df` contiene SOLO velas cerradas (con indicadores). `df.iloc[-1]` es la vela gatillo,
equivalente a `df.iloc[-2]` del documento original (allí la última fila es la vela aún en formación).
Por tanto: gatillo = [-1], vela previa = [-2], vela madre = [-3].
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Patrones de velas (fórmulas exactas de la especificación)
# ---------------------------------------------------------------------------
def candle_parts(o: float, h: float, l: float, c: float) -> dict:
    return {
        "rango": h - l,
        "cuerpo": abs(c - o),
        "mecha_inf": min(o, c) - l,
        "mecha_sup": h - max(o, c),
    }


def pin_bar(df: pd.DataFrame, side: str, cfg) -> tuple[bool, str]:
    """Martillo alcista (LONG) / Estrella fugaz bajista (SHORT)."""
    t = df.iloc[-1]
    p = candle_parts(t.open, t.high, t.low, t.close)
    if not p["rango"] > 0:
        return False, ""
    if side == "LONG":
        ok = (p["mecha_inf"] >= cfg.PIN_MECHA_CUERPO * p["cuerpo"]
              and p["mecha_sup"] <= cfg.PIN_MECHA_OPUESTA_MAX * p["rango"]
              and t.close > t.bb_low)
    else:
        ok = (p["mecha_sup"] >= cfg.PIN_MECHA_CUERPO * p["cuerpo"]
              and p["mecha_inf"] <= cfg.PIN_MECHA_OPUESTA_MAX * p["rango"]
              and t.close < t.bb_up)
    return bool(ok), "Pin Bar" if ok else ""


def engulfing(df: pd.DataFrame, side: str, cfg) -> tuple[bool, str]:
    """Barra envolvente con volumen > 1.2 x SMA(volumen, 20)."""
    if len(df) < 2:
        return False, ""
    t, p = df.iloc[-1], df.iloc[-2]
    vol_ok = bool(t.volume > cfg.ENGULFING_VOL_MULT * t.vol_sma20)
    if side == "LONG":
        ok = p.close < p.open and t.close > t.open and t.close > p.open and t.open <= p.close and vol_ok
    else:
        ok = p.close > p.open and t.close < t.open and t.close < p.open and t.open >= p.close and vol_ok
    return bool(ok), "Barra Envolvente con volumen" if ok else ""


def inside_bar_fakeout(df: pd.DataFrame, side: str, cfg=None) -> tuple[bool, str]:
    """Falsa ruptura de Inside Bar (stop hunting): madre [-3], inside [-2], gatillo [-1]."""
    if len(df) < 3:
        return False, ""
    m, i, t = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    inside = i.high < m.high and i.low > m.low
    if side == "LONG":
        ok = inside and t.low < m.low and t.close > m.low
    else:
        ok = inside and t.high > m.high and t.close < m.high
    return bool(ok), "Falsa ruptura de Inside Bar" if ok else ""


def detect_patterns(df: pd.DataFrame, side: str, cfg) -> list[str]:
    found = []
    for fn in (pin_bar, engulfing, inside_bar_fakeout):
        ok, name = fn(df, side, cfg)
        if ok:
            found.append(name)
    return found


# ---------------------------------------------------------------------------
# Divergencias (precio vs RSI / MACD)
# ---------------------------------------------------------------------------
def _last_pivot(arr: np.ndarray, t: int, n: int, lookback: int, min_sep: int, kind: str) -> int | None:
    """Último pivote confirmado (n velas a cada lado) anterior a la vela t. kind: 'low' | 'high'."""
    newest = t - max(min_sep, n + 1)
    oldest = max(n, t - lookback)
    for p in range(newest, oldest - 1, -1):
        window = arr[p - n: p + n + 1]
        if np.isnan(window).any():
            continue
        if kind == "low" and arr[p] <= window.min():
            return p
        if kind == "high" and arr[p] >= window.max():
            return p
    return None


def _divergence(df: pd.DataFrame, side: str, cfg) -> tuple[bool, str]:
    need = cfg.DIV_LOOKBACK + cfg.DIV_PIVOTE_N + 2
    d = df.iloc[-need:]
    t = len(d) - 1
    if t < cfg.DIV_PIVOTE_N * 2 + cfg.DIV_SEPARACION_MIN:
        return False, ""
    n = cfg.DIV_PIVOTE_N
    rsi = d["rsi14"].to_numpy(dtype=float)
    macd = d["macd"].to_numpy(dtype=float)
    if side == "LONG":
        px = d["low"].to_numpy(dtype=float)
        if not px[t] <= px[t - n:t].min():          # la vela gatillo debe marcar el mínimo reciente
            return False, ""
        p = _last_pivot(px, t, n, cfg.DIV_LOOKBACK, cfg.DIV_SEPARACION_MIN, "low")
        if p is None or not px[t] < px[p]:           # Lower Low en precio
            return False, ""
        by_rsi = rsi[t] > rsi[p]                      # Higher Low en RSI
        by_macd = macd[t] > macd[p]                   # Higher Low en MACD
        name = "Divergencia alcista"
    else:
        px = d["high"].to_numpy(dtype=float)
        if not px[t] >= px[t - n:t].max():
            return False, ""
        p = _last_pivot(px, t, n, cfg.DIV_LOOKBACK, cfg.DIV_SEPARACION_MIN, "high")
        if p is None or not px[t] > px[p]:           # Higher High en precio
            return False, ""
        by_rsi = rsi[t] < rsi[p]                      # Lower High en RSI
        by_macd = macd[t] < macd[p]                   # Lower High en MACD
        name = "Divergencia bajista"
    if np.isnan(rsi[p]) or np.isnan(rsi[t]):
        by_rsi = False
    if np.isnan(macd[p]) or np.isnan(macd[t]):
        by_macd = False
    if by_rsi or by_macd:
        src = "RSI" if by_rsi else "MACD"
        if by_rsi and by_macd:
            src = "RSI y MACD"
        return True, f"{name} precio/{src} ({t - p} velas atrás)"
    return False, ""


# ---------------------------------------------------------------------------
# Condición de agotamiento
# ---------------------------------------------------------------------------
def exhaustion(df: pd.DataFrame, side: str, cfg) -> tuple[bool, str]:
    """Bollinger perforada + RSI/Estocástico extremo, o divergencia."""
    window = max(1, int(cfg.VENTANA_AGOTAMIENTO))
    t = df.iloc[-1]
    for back in range(min(window, len(df))):
        r = df.iloc[-1 - back]
        if side == "LONG":
            pierced = r.low < r.bb_low
            osc = r.rsi14 < cfg.RSI_SOBREVENTA or r.stoch_k < cfg.STOCH_SOBREVENTA
            if pierced and osc:
                return True, (f"Mínimo {r.low:,.1f} < BB inf {r.bb_low:,.1f} con RSI {r.rsi14:.1f} / "
                              f"Estoc %K {r.stoch_k:.1f}")
        else:
            pierced = r.high > r.bb_up
            osc = r.rsi14 > cfg.RSI_SOBRECOMPRA or r.stoch_k > cfg.STOCH_SOBRECOMPRA
            if pierced and osc:
                return True, (f"Máximo {r.high:,.1f} > BB sup {r.bb_up:,.1f} con RSI {r.rsi14:.1f} / "
                              f"Estoc %K {r.stoch_k:.1f}")
    ok, txt = _divergence(df, side, cfg)
    if ok:
        return True, txt
    if side == "LONG":
        return False, f"Sin agotamiento: mín {t.low:,.1f} vs BB inf {t.bb_low:,.1f}, RSI {t.rsi14:.1f}, %K {t.stoch_k:.1f}"
    return False, f"Sin agotamiento: máx {t.high:,.1f} vs BB sup {t.bb_up:,.1f}, RSI {t.rsi14:.1f}, %K {t.stoch_k:.1f}"
