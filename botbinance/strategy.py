"""Estrategia 'Francotirador de Contrapunto (Top-Down Pullback)'.

Tres fases en cascada, siempre sobre velas CERRADAS. Cada función usa su propia temporalidad (configurable):
  Fase 1  Tendencia              TF_TENDENCIA (+ filtro opcional TF_TENDENCIA_SUPERIOR)
  Fase 2  Retroceso a zona valor TF_ZONA (EMA20/EMA50) + Fibonacci en TF_FIBONACCI + RSI de la temporalidad de tendencia
  Fase 3  Gatillo                TF_GATILLO: agotamiento + patrón de velas

`evaluate()` recibe los DataFrames ya enriquecidos con indicadores (solo velas cerradas) y devuelve una
`Evaluation` con el detalle de cada condición (el dashboard la muestra tal cual) y, si procede, una `Signal`.
El backtest usa exactamente esta misma función, así que lo que se prueba es lo que se ejecuta.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import patterns

MIN_UPPER, MIN_TREND = 25, 60      # velas mínimas (EMA50 y ADX necesitan ~60; la SMA200 se omite si no hay 200)


@dataclass
class Check:
    name: str
    value: str = ""
    ok: bool | None = None       # fases 2 y 3 (para el lado de la tendencia)
    long: bool | None = None     # fase 1 (se muestran ambos lados)
    short: bool | None = None


@dataclass
class Signal:
    side: str                    # "LONG" | "SHORT"
    pattern: str
    exhaustion: str
    open_time: int               # open_time de la vela gatillo
    close_time: int
    open: float
    high: float
    low: float
    close: float
    atr: float
    levels: list[float] = field(default_factory=list)    # soportes/resistencias para las entradas escalonadas
    levels_src: str = ""


STRATEGIES = {
    "estrategia_1": {
        "id": "estrategia_1",
        "name": "Estrategia 1 · Rebote en Sobreventa (RSI Sobreventa + Bollinger)",
        "short_name": "Estrategia 1 (RSI Sobreventa + BB)",
        "desc": "Tendencia en 1d/12h + Retroceso con RSI en sobreventa y vela tocando Banda Inferior de Bollinger en 15m/30m.",
        "default_tfs": {"TF_TENDENCIA": "1d", "TF_TENDENCIA_SUPERIOR": "1w", "TF_ZONA": "15m", "TF_GATILLO": "15m"},
        "indicators": ["bb", "rsi", "strat"],
    },
    "estrategia_2": {
        "id": "estrategia_2",
        "name": "Estrategia 2 · Top-Down Pullback & Fibonacci (Institucional)",
        "short_name": "Estrategia 2 (Pullback & Fib)",
        "desc": "Tendencia 1d + Retroceso a zona EMA20/50 o Fibonacci 50-61.8% en 4h + Gatillo de acción del precio en 1h.",
        "default_tfs": {"TF_TENDENCIA": "1d", "TF_TENDENCIA_SUPERIOR": "1w", "TF_ZONA": "4h", "TF_GATILLO": "1h"},
        "indicators": ["ema20", "ema50", "sma200", "fib", "strat"],
    },
    "estrategia_3": {
        "id": "estrategia_3",
        "name": "Estrategia 3 · Ruptura de Squeeze de Bollinger (Breakout)",
        "short_name": "Estrategia 3 (Squeeze Breakout)",
        "desc": "Compresión de volatilidad (Bandas de Bollinger estrechas) y ruptura direccional explosiva con volumen.",
        "default_tfs": {"TF_TENDENCIA": "1d", "TF_TENDENCIA_SUPERIOR": "1w", "TF_ZONA": "4h", "TF_GATILLO": "1h"},
        "indicators": ["bb", "strat"],
    },
    "estrategia_4": {
        "id": "estrategia_4",
        "name": "Estrategia 4 · Cruce Dorado EMA 20/50 + Re-testeo",
        "short_name": "Estrategia 4 (Cruce Dorado)",
        "desc": "Cruce alcista EMA20 sobre EMA50 en 4h y entrada al re-testear la media EMA50 con rebote en 15m.",
        "default_tfs": {"TF_TENDENCIA": "4h", "TF_TENDENCIA_SUPERIOR": "1d", "TF_ZONA": "1h", "TF_GATILLO": "15m"},
        "indicators": ["ema20", "ema50", "strat"],
    },
    "estrategia_5": {
        "id": "estrategia_5",
        "name": "Estrategia 5 · Cazador de Divergencias Alcistas en Soportes",
        "short_name": "Estrategia 5 (Divergencias)",
        "desc": "Divergencia regular (precio hace mínimo más bajo y RSI/MACD mínimo más alto) en zonas de soporte clave.",
        "default_tfs": {"TF_TENDENCIA": "1d", "TF_TENDENCIA_SUPERIOR": "1w", "TF_ZONA": "4h", "TF_GATILLO": "1h"},
        "indicators": ["rsi", "sr", "strat"],
    },
}


@dataclass
class Evaluation:
    close_time: int = 0
    trend: str = "SIN_DATOS"     # LONG | SHORT | NONE | STANDBY | SIN_DATOS
    phase1: list[Check] = field(default_factory=list)
    phase1_ok: bool = False
    phase2: list[Check] = field(default_factory=list)
    phase2_ok: bool | None = None
    phase3: list[Check] = field(default_factory=list)
    phase3_ok: bool | None = None
    signal: Signal | None = None
    fib: dict | None = None
    sr: list[dict] = field(default_factory=list)         # soportes/resistencias (pivotes) para dibujar en el gráfico
    price: float = 0.0
    note: str = ""
    strategy_id: str = "estrategia_1"
    strategy_name: str = ""
    execution_points: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _f(x, nd=1) -> str:
    return "n/d" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{nd}f}"


# ---------------------------------------------------------------------------
# ZigZag + Fibonacci
# ---------------------------------------------------------------------------
def zigzag(high: np.ndarray, low: np.ndarray, thr: np.ndarray) -> list[tuple[int, float, str]]:
    """Pivotes confirmados. Un máximo se confirma cuando el precio cae >= thr desde él (y viceversa)."""
    pivots: list[tuple[int, float, str]] = []
    direction = 0
    hi_i = lo_i = -1
    hi = lo = np.nan
    for i in range(len(high)):
        t = thr[i]
        if np.isnan(t):
            continue
        if hi_i < 0:
            hi, hi_i, lo, lo_i = high[i], i, low[i], i
            continue
        if direction == 0:
            if high[i] > hi:
                hi, hi_i = high[i], i
            if low[i] < lo:
                lo, lo_i = low[i], i
            if high[i] - lo >= t and lo_i < i:
                pivots.append((lo_i, lo, "L"))
                direction, hi, hi_i = 1, high[i], i
            elif hi - low[i] >= t and hi_i < i:
                pivots.append((hi_i, hi, "H"))
                direction, lo, lo_i = -1, low[i], i
        elif direction == 1:
            if high[i] > hi:
                hi, hi_i = high[i], i
            elif hi - low[i] >= t:
                pivots.append((hi_i, hi, "H"))
                direction, lo, lo_i = -1, low[i], i
        else:
            if low[i] < lo:
                lo, lo_i = low[i], i
            elif high[i] - lo >= t:
                pivots.append((lo_i, lo, "L"))
                direction, hi, hi_i = 1, high[i], i
    return pivots


def get_pivots(df: pd.DataFrame, cfg, cache: dict | None = None) -> list[tuple[int, float, str]]:
    key = ("zz", cfg.TF_FIBONACCI, int(df["open_time"].iloc[-1]), cfg.FIB_ZIGZAG_ATR)
    pivots = cache.get(key) if cache is not None else None
    if pivots is None:
        pivots = zigzag(df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float),
                        cfg.FIB_ZIGZAG_ATR * df["atr14"].to_numpy(dtype=float))
        if cache is not None:
            for k in [k for k in cache if isinstance(k, tuple) and k and k[0] == "zz"]:
                del cache[k]
            cache[key] = pivots
    return pivots


def fib_zone(df: pd.DataFrame, side: str, cfg, cache: dict | None = None) -> dict | None:
    """Zona de retroceso 50%-61.8% del último impulso mayor en el sentido de la tendencia."""
    if df is None or len(df) < 60:
        return None
    pivots = get_pivots(df, cfg, cache)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    atr = df["atr14"].to_numpy(dtype=float)

    if side == "LONG":
        base = next((p for p in reversed(pivots) if p[2] == "L"), None)
        if base is None:
            return None
        b_idx, b_px = base[0], base[1]
        seg = high[b_idx:]
        off = int(np.argmax(seg))
        ext_idx, ext_px = b_idx + off, float(seg[off])
        impulse = ext_px - b_px
        zone_lo = ext_px - cfg.FIB_MAX * impulse
        zone_hi = ext_px - cfg.FIB_MIN * impulse
    else:
        base = next((p for p in reversed(pivots) if p[2] == "H"), None)
        if base is None:
            return None
        b_idx, b_px = base[0], base[1]
        seg = low[b_idx:]
        off = int(np.argmin(seg))
        ext_idx, ext_px = b_idx + off, float(seg[off])
        impulse = b_px - ext_px
        zone_lo = ext_px + cfg.FIB_MIN * impulse
        zone_hi = ext_px + cfg.FIB_MAX * impulse
    atr_ref = float(atr[ext_idx]) if not np.isnan(atr[ext_idx]) else float(atr[-1])
    valid = bool(impulse > 0 and impulse >= cfg.FIB_IMPULSO_MIN_ATR * atr_ref)
    return {
        "valid": valid,
        "start": float(b_px), "end": ext_px, "impulse": float(impulse),
        "start_time": int(df["open_time"].iloc[b_idx]), "end_time": int(df["open_time"].iloc[ext_idx]),
        "zone_lo": float(zone_lo), "zone_hi": float(zone_hi),
    }


def candidate_levels(side: str, price: float, atr: float, fib: dict | None, fib_df: pd.DataFrame, cfg,
                     cache: dict | None = None) -> tuple[list[float], str]:
    """Niveles de soporte (LONG) / resistencia (SHORT) para las entradas escalonadas, del más cercano al más lejano.

    Fuente preferida: Fibonacci (50% / 61.8% / 78.6% del impulso) o pivotes de soporte/resistencia; si no alcanzan,
    se usa la otra fuente y, como último recurso, pasos de ATR.
    """
    n = int(cfg.MARTINGALA_NIVELES) - 1
    gap = max(price * 0.0015, 0.25 * atr)
    long = side == "LONG"
    sg = 1.0 if long else -1.0

    fibs: list[float] = []
    if fib and fib.get("valid"):
        fibs = [fib["end"] - r * (fib["end"] - fib["start"]) for r in (0.5, 0.618, 0.786)]
    piv: list[float] = []
    if fib_df is not None and len(fib_df) >= 60:
        piv = [px for _, px, kind in get_pivots(fib_df, cfg, cache) if kind == ("L" if long else "H")]
    sources = [("Fibonacci", fibs), ("soportes/resistencias", piv)]
    if cfg.MARTINGALA_FUENTE == "soportes":
        sources.reverse()

    levels: list[float] = []
    used: list[str] = []
    for name, cands in sources:
        ok = [x for x in cands if (x < price - gap if long else x > price + gap)]
        for lv in sorted(set(round(x, 2) for x in ok), reverse=long):
            if len(levels) >= n:
                break
            ref = levels[-1] if levels else price
            if abs(lv - ref) >= gap:
                levels.append(lv)
                if name not in used:
                    used.append(name)
    step = max(atr, price * 0.003)
    while len(levels) < n:
        levels.append(round((levels[-1] if levels else price) - sg * step, 2))
        if "ATR" not in used:
            used.append("ATR")
    return levels, " + ".join(used)


def sr_markers(df: pd.DataFrame, price: float, cfg, cache: dict | None = None, limit: int = 8) -> list[dict]:
    """Últimos pivotes del ZigZag como soportes (debajo del precio) o resistencias (encima), para el gráfico."""
    if df is None or len(df) < 60:
        return []
    return [{"price": float(px), "kind": "S" if px < price else "R"}
            for _, px, _k in get_pivots(df, cfg, cache)[-limit:]]


# ---------------------------------------------------------------------------
# Evaluación de Estrategias
# ---------------------------------------------------------------------------
def evaluate_estrategia_1(frames: dict[str, pd.DataFrame], cfg, cache: dict | None = None, with_sr: bool = False) -> Evaluation:
    """Estrategia 1 (Predeterminada): Tendencia 1d/12h + Rebote en RSI sobreventa y toque de Banda Inferior de Bollinger en 15m/30m."""
    strat_info = STRATEGIES["estrategia_1"]
    ev = Evaluation(strategy_id="estrategia_1", strategy_name=strat_info["name"])
    tft, tfu = cfg.TF_TENDENCIA, cfg.TF_TENDENCIA_SUPERIOR
    trig = frames.get(cfg.TF_GATILLO)
    d1 = frames.get(tft)
    w1 = frames.get(tfu) if tfu else None
    fib_df = frames.get(cfg.TF_FIBONACCI)

    if trig is None or d1 is None or (tfu and w1 is None):
        ev.note = "Faltan datos de alguna temporalidad."
        return ev
    if len(d1) < MIN_TREND or (w1 is not None and len(w1) < MIN_UPPER) or len(trig) < 30:
        ev.note = "Historial insuficiente para calcular los indicadores."
        return ev

    t = trig.iloc[-1]
    ev.close_time = int(t.close_time)
    ev.price = float(t.close)
    if with_sr and fib_df is not None:
        ev.sr = sr_markers(fib_df, ev.price, cfg, cache)

    # ---------------- FASE 1: tendencia macro (1d / 12h) ----------------
    d = d1.iloc[-1]
    needed = [d.close, d.ema20, d.ema50, d.adx14]
    if w1 is not None:
        w = w1.iloc[-1]
        needed += [w.close, w.ema20, w.rsi14]
    if any(pd.isna(v) for v in needed):
        ev.note = "Indicadores de tendencia aún sin valor (poco historial)."
        return ev

    adx_ok = d.adx14 > cfg.ADX_TENDENCIA_MIN
    checks: list[Check] = []
    if w1 is not None:
        checks += [
            Check(f"{tfu} · Close vs EMA(20)", f"{_f(w.close, 0)} vs {_f(w.ema20, 0)}",
                  long=bool(w.close > w.ema20), short=bool(w.close < w.ema20)),
            Check(f"{tfu} · RSI(14) vs 50", _f(w.rsi14), long=bool(w.rsi14 > 50), short=bool(w.rsi14 < 50)),
        ]
    if pd.isna(d.sma200):
        checks.append(Check(f"{tft} · Close vs SMA(200)", "n/d (menos de 200 velas): no se exige"))
    else:
        checks.append(Check(f"{tft} · Close vs SMA(200)", f"{_f(d.close, 0)} vs {_f(d.sma200, 0)}",
                            long=bool(d.close > d.sma200), short=bool(d.close < d.sma200)))
    checks += [
        Check(f"{tft} · EMA(20) vs EMA(50)", f"{_f(d.ema20, 0)} vs {_f(d.ema50, 0)}",
              long=bool(d.ema20 > d.ema50), short=bool(d.ema20 < d.ema50)),
        Check(f"{tft} · ADX(14) > {cfg.ADX_TENDENCIA_MIN:g}", _f(d.adx14), long=bool(adx_ok), short=bool(adx_ok)),
    ]
    ev.phase1 = checks

    if d.adx14 < cfg.ADX_LATERAL_MAX:
        ev.trend = "STANDBY"
        ev.note = f"Mercado lateral en {tft} (ADX {d.adx14:.1f} < {cfg.ADX_LATERAL_MAX:g}): bot en STANDBY."
        ev.execution_points = {
            "strategy_id": "estrategia_1",
            "strategy_name": strat_info["name"],
            "status": "STANDBY",
            "message": f"Mercado lateral en {tft} (ADX={d.adx14:.1f} < {cfg.ADX_LATERAL_MAX:g}). Esperando tendencia clara.",
            "current_price": ev.price,
            "target_price": None,
            "current_rsi": float(t.rsi14) if hasattr(t, "rsi14") else None,
            "target_rsi": float(getattr(cfg, "RSI_SOBREVENTA", 35.0)),
            "dist_price": 0.0,
            "dist_rsi": 0.0,
            "trigger_tf": cfg.TF_GATILLO,
        }
        return ev

    long_ok = all(c.long for c in checks if c.long is not None)
    short_ok = all(c.short for c in checks if c.short is not None)
    if long_ok:
        ev.trend, ev.phase1_ok = "LONG", True
    elif short_ok:
        ev.trend, ev.phase1_ok = "SHORT", True
    else:
        ev.trend = "NONE"
        ev.note = "Sin tendencia clara en temporalidad macro."
        ev.execution_points = {
            "strategy_id": "estrategia_1",
            "strategy_name": strat_info["name"],
            "status": "SIN_TENDENCIA",
            "message": "Sin tendencia definida en gráfico diario/macro. Esperando alineación.",
            "current_price": ev.price,
            "target_price": None,
            "current_rsi": float(t.rsi14) if hasattr(t, "rsi14") else None,
            "target_rsi": float(getattr(cfg, "RSI_SOBREVENTA", 35.0)),
            "dist_price": 0.0,
            "dist_rsi": 0.0,
            "trigger_tf": cfg.TF_GATILLO,
        }
        return ev

    side = ev.trend
    window = max(3, int(getattr(cfg, "VENTANA_AGOTAMIENTO", 3)))
    rsi_ov = float(getattr(cfg, "RSI_SOBREVENTA", 35.0))
    rsi_ob = float(getattr(cfg, "RSI_SOBRECOMPRA", 65.0))

    # ---------------- FASE 2: retroceso con RSI <= sobreventa (LONG) o >= sobrecompra (SHORT) ----------------
    rsi_period = int(getattr(cfg, "RSI_PERIODO", 14))
    recent_rsi = [float(trig.iloc[-1 - k].rsi14) for k in range(min(window, len(trig))) if not pd.isna(trig.iloc[-1 - k].rsi14)]
    if side == "LONG":
        rsi_target = rsi_ov
        rsi_hit = any(val <= rsi_target for val in recent_rsi)
        rsi_val_str = f"{float(t.rsi14):.1f} (mínimo reciente: {min(recent_rsi):.1f})" if recent_rsi else "n/d"
        phase2_name = f"{cfg.TF_GATILLO} · RSI({rsi_period}) en sobreventa (≤ {rsi_target:g})"
    else:
        rsi_target = rsi_ob
        rsi_hit = any(val >= rsi_target for val in recent_rsi)
        rsi_val_str = f"{float(t.rsi14):.1f} (máximo reciente: {max(recent_rsi):.1f})" if recent_rsi else "n/d"
        phase2_name = f"{cfg.TF_GATILLO} · RSI({rsi_period}) en sobrecompra (≥ {rsi_target:g})"

    ev.phase2 = [Check(phase2_name, rsi_val_str, ok=rsi_hit)]
    ev.phase2_ok = rsi_hit

    # ---------------- FASE 3: vela tocando o perforando Banda de Bollinger ----------------
    if side == "LONG":
        target_price = float(t.bb_low)
        bb_hit = any(float(trig.iloc[-1 - k].low) <= float(trig.iloc[-1 - k].bb_low) for k in range(min(window, len(trig))))
        phase3_name = f"{cfg.TF_GATILLO} · Mínimo toca Banda Inferior Bollinger"
        phase3_val = f"Mín {_f(t.low, 1)} vs BB Inf {_f(t.bb_low, 1)}"
    else:
        target_price = float(t.bb_up)
        bb_hit = any(float(trig.iloc[-1 - k].high) >= float(trig.iloc[-1 - k].bb_up) for k in range(min(window, len(trig))))
        phase3_name = f"{cfg.TF_GATILLO} · Máximo toca Banda Superior Bollinger"
        phase3_val = f"Máx {_f(t.high, 1)} vs BB Sup {_f(t.bb_up, 1)}"

    ev.phase3 = [Check(phase3_name, phase3_val, ok=bb_hit)]
    ev.phase3_ok = bb_hit

    dist_price = ev.price - target_price
    dist_rsi = float(t.rsi14) - rsi_target
    if side == "LONG":
        if ev.phase2_ok and ev.phase3_ok:
            status_msg = f"🟢 ¡CONDICIÓN DE COMPRA CUMPLIDA! RSI({cfg.TF_GATILLO})={t.rsi14:.1f} ≤ {rsi_target:g} y precio tocó Banda Inferior (${target_price:,.1f})."
        elif ev.phase3_ok and not ev.phase2_ok:
            status_msg = f"🟡 Precio tocó Banda Inferior (${target_price:,.1f}), esperando que RSI({cfg.TF_GATILLO}) baje a ≤ {rsi_target:g} (actual: {t.rsi14:.1f}, faltan {dist_rsi:.1f} pts)."
        elif ev.phase2_ok and not ev.phase3_ok:
            status_msg = f"🟡 RSI({cfg.TF_GATILLO}) en sobreventa (≤ {rsi_target:g}), esperando que el precio baje a Banda Inferior (${target_price:,.1f}, faltan ${dist_price:,.1f})."
        else:
            status_msg = f"⚪ Esperando retroceso alcista: Precio debe bajar a Banda Inferior (${target_price:,.1f}, dist: ${dist_price:,.1f}) y RSI({cfg.TF_GATILLO}) caer a ≤ {rsi_target:g} (actual: {t.rsi14:.1f})."
    else:
        if ev.phase2_ok and ev.phase3_ok:
            status_msg = f"🟢 ¡CONDICIÓN DE VENTA CUMPLIDA! RSI({cfg.TF_GATILLO})={t.rsi14:.1f} ≥ {rsi_target:g} y precio tocó Banda Superior (${target_price:,.1f})."
        elif ev.phase3_ok and not ev.phase2_ok:
            status_msg = f"🟡 Precio tocó Banda Superior (${target_price:,.1f}), esperando que RSI({cfg.TF_GATILLO}) suba a ≥ {rsi_target:g} (actual: {t.rsi14:.1f}, faltan {abs(dist_rsi):.1f} pts)."
        elif ev.phase2_ok and not ev.phase3_ok:
            status_msg = f"🟡 RSI({cfg.TF_GATILLO}) en sobrecompra (≥ {rsi_target:g}), esperando que el precio suba a Banda Superior (${target_price:,.1f}, faltan ${abs(dist_price):.1f})."
        else:
            status_msg = f"⚪ Esperando retroceso bajista: Precio debe subir a Banda Superior (${target_price:,.1f}) y RSI({cfg.TF_GATILLO}) subir a ≥ {rsi_target:g} (actual: {t.rsi14:.1f})."

    ev.execution_points = {
        "strategy_id": "estrategia_1",
        "strategy_name": strat_info["name"],
        "side": side,
        "current_price": ev.price,
        "target_price": target_price,
        "current_rsi": float(t.rsi14),
        "target_rsi": rsi_target,
        "dist_price": dist_price,
        "dist_rsi": dist_rsi,
        "trigger_tf": cfg.TF_GATILLO,
        "status": "LISTO" if (ev.phase2_ok and ev.phase3_ok) else "ESPERANDO",
        "target_reached": bool(ev.phase2_ok and ev.phase3_ok),
        "message": status_msg,
    }

    if ev.phase1_ok and ev.phase2_ok and ev.phase3_ok:
        pattern_name = f"Rebote en RSI ≤ {rsi_target:g} + Bollinger Inf" if side == "LONG" else f"Rechazo en RSI ≥ {rsi_target:g} + Bollinger Sup"
        ev.signal = Signal(
            side=side, pattern=pattern_name,
            exhaustion=f"RSI={t.rsi14:.1f} · BB={target_price:,.1f}",
            open_time=int(t.open_time), close_time=int(t.close_time),
            open=float(t.open), high=float(t.high), low=float(t.low), close=float(t.close),
            atr=float(t.atr14),
        )
        ev.note = f"¡SEÑAL {side}! {pattern_name}"
    elif not ev.phase2_ok and not ev.phase3_ok:
        ev.note = f"Tendencia {side} confirmada; esperando retroceso con RSI extremo y toque de Bollinger."
    elif not ev.phase2_ok:
        ev.note = f"Tocó Bollinger; esperando RSI ≤ {rsi_target:g}."
    else:
        ev.note = f"RSI ≤ {rsi_target:g}; esperando toque de Banda de Bollinger."

    return ev


def evaluate_estrategia_2(frames: dict[str, pd.DataFrame], cfg, cache: dict | None = None, with_sr: bool = False) -> Evaluation:
    """Estrategia 2: Top-Down Pullback & Fibonacci (Institucional en 3 fases)."""
    strat_info = STRATEGIES["estrategia_2"]
    ev = Evaluation(strategy_id="estrategia_2", strategy_name=strat_info["name"])
    tft, tfu = cfg.TF_TENDENCIA, cfg.TF_TENDENCIA_SUPERIOR
    trig = frames.get(cfg.TF_GATILLO)
    d1 = frames.get(tft)
    w1 = frames.get(tfu) if tfu else None
    zone_df = frames.get(cfg.TF_ZONA)
    fib_df = frames.get(cfg.TF_FIBONACCI)
    if trig is None or d1 is None or zone_df is None or fib_df is None or (tfu and w1 is None):
        ev.note = "Faltan datos de alguna temporalidad."
        return ev
    if len(d1) < MIN_TREND or (w1 is not None and len(w1) < MIN_UPPER) or len(trig) < 60 or len(zone_df) < 60:
        ev.note = "Historial insuficiente para calcular los indicadores."
        return ev

    t = trig.iloc[-1]
    ev.close_time = int(t.close_time)
    ev.price = float(t.close)
    if with_sr:
        ev.sr = sr_markers(fib_df, ev.price, cfg, cache)

    # ---------------- FASE 1: tendencia ----------------
    d = d1.iloc[-1]
    needed = [d.close, d.ema20, d.ema50, d.adx14]
    if w1 is not None:
        w = w1.iloc[-1]
        needed += [w.close, w.ema20, w.rsi14]
    if any(pd.isna(v) for v in needed):
        ev.note = "Indicadores de tendencia aún sin valor (poco historial)."
        return ev

    adx_ok = d.adx14 > cfg.ADX_TENDENCIA_MIN
    checks: list[Check] = []
    if w1 is not None:
        checks += [
            Check(f"{tfu} · Close vs EMA(20)", f"{_f(w.close, 0)} vs {_f(w.ema20, 0)}",
                  long=bool(w.close > w.ema20), short=bool(w.close < w.ema20)),
            Check(f"{tfu} · RSI(14) vs 50", _f(w.rsi14), long=bool(w.rsi14 > 50), short=bool(w.rsi14 < 50)),
        ]
    if pd.isna(d.sma200):
        checks.append(Check(f"{tft} · Close vs SMA(200)", "n/d (menos de 200 velas): no se exige"))
    else:
        checks.append(Check(f"{tft} · Close vs SMA(200)", f"{_f(d.close, 0)} vs {_f(d.sma200, 0)}",
                            long=bool(d.close > d.sma200), short=bool(d.close < d.sma200)))
    checks += [
        Check(f"{tft} · EMA(20) vs EMA(50)", f"{_f(d.ema20, 0)} vs {_f(d.ema50, 0)}",
              long=bool(d.ema20 > d.ema50), short=bool(d.ema20 < d.ema50)),
        Check(f"{tft} · ADX(14) > {cfg.ADX_TENDENCIA_MIN:g}", _f(d.adx14), long=bool(adx_ok), short=bool(adx_ok)),
    ]
    ev.phase1 = checks

    if d.adx14 < cfg.ADX_LATERAL_MAX:
        ev.trend = "STANDBY"
        ev.note = f"Mercado lateral en {tft} (ADX {d.adx14:.1f} < {cfg.ADX_LATERAL_MAX:g}): el bot permanece en STANDBY."
        ev.execution_points = {"strategy_id": "estrategia_2", "strategy_name": strat_info["name"], "status": "STANDBY", "message": ev.note}
        return ev
    long_ok = all(c.long for c in checks if c.long is not None)
    short_ok = all(c.short for c in checks if c.short is not None)
    if long_ok:
        ev.trend, ev.phase1_ok = "LONG", True
    elif short_ok:
        ev.trend, ev.phase1_ok = "SHORT", True
    else:
        ev.trend = "NONE"
        ev.note = "Sin tendencia clara (no se cumplen todas las condiciones de la Fase 1)."
        ev.execution_points = {"strategy_id": "estrategia_2", "strategy_name": strat_info["name"], "status": "SIN_TENDENCIA", "message": ev.note}
        return ev
    side = ev.trend

    # ---------------- FASE 2: retroceso a zona de valor ----------------
    price = float(t.close)
    z = zone_df.iloc[-1]
    zlo, zhi = float(min(z.ema20, z.ema50)), float(max(z.ema20, z.ema50))
    tol = cfg.ZONA_TOLERANCIA_ATR * float(z.atr14)
    in_ema = bool(zlo - tol <= price <= zhi + tol)
    fib = fib_zone(fib_df, side, cfg, cache)
    ev.fib = fib
    in_fib = bool(fib and fib["valid"] and fib["zone_lo"] <= price <= fib["zone_hi"])
    if side == "LONG":
        rlo, rhi = cfg.LONG_RSI_DIARIO_MIN, cfg.LONG_RSI_DIARIO_MAX
    else:
        rlo, rhi = cfg.SHORT_RSI_DIARIO_MIN, cfg.SHORT_RSI_DIARIO_MAX
    rsi_ok = bool(rlo <= d.rsi14 <= rhi)

    fib_txt = "sin impulso mayor válido"
    if fib:
        fib_txt = (f"{_f(fib['zone_lo'], 0)}–{_f(fib['zone_hi'], 0)} (impulso {_f(fib['start'], 0)}→{_f(fib['end'], 0)}"
                   f"{'' if fib['valid'] else ', demasiado pequeño'})")
    ev.phase2 = [
        Check(f"{cfg.TF_ZONA} · Zona EMA20–EMA50 (±{cfg.ZONA_TOLERANCIA_ATR:g} ATR)",
              f"{_f(zlo, 0)}–{_f(zhi, 0)} · precio {_f(price, 0)}", ok=in_ema),
        Check(f"{cfg.TF_FIBONACCI} · Fibonacci {cfg.FIB_MIN * 100:g}%–{cfg.FIB_MAX * 100:.1f}% (basta una de las dos zonas)",
              fib_txt, ok=in_fib),
        Check(f"{tft} · RSI(14) entre {rlo:g} y {rhi:g}", _f(d.rsi14), ok=rsi_ok),
    ]
    ev.phase2_ok = bool((in_ema or in_fib) and rsi_ok)

    target_price = zlo if side == "LONG" else zhi
    ev.execution_points = {
        "strategy_id": "estrategia_2",
        "strategy_name": strat_info["name"],
        "side": side,
        "current_price": price,
        "target_price": target_price,
        "current_rsi": float(d.rsi14),
        "target_rsi": f"{rlo:g}–{rhi:g}",
        "dist_price": price - target_price,
        "dist_rsi": 0.0,
        "trigger_tf": cfg.TF_GATILLO,
        "status": "EN_ZONA" if ev.phase2_ok else "ESPERANDO",
        "message": f"Tendencia {side}: esperando retroceso a zona EMA20-50 (${zlo:,.0f}–${zhi:,.0f}) y patrón de vela en {cfg.TF_GATILLO}.",
    }

    if not ev.phase2_ok:
        ev.note = "Tendencia confirmada; esperando retroceso a la zona de valor."
        return ev

    # ---------------- FASE 3: gatillo en la temporalidad menor ----------------
    exh_ok, exh_txt = patterns.exhaustion(trig, side, cfg)
    found = patterns.detect_patterns(trig, side, cfg)
    ev.phase3 = [
        Check(f"{cfg.TF_GATILLO} · Condición de agotamiento", exh_txt, ok=exh_ok),
        Check(f"{cfg.TF_GATILLO} · Patrón de velas gatillo", ", ".join(found) if found else "ninguno", ok=bool(found)),
    ]
    ev.phase3_ok = bool(exh_ok and found)
    if ev.phase3_ok:
        ev.signal = Signal(
            side=side, pattern=" + ".join(found), exhaustion=exh_txt,
            open_time=int(t.open_time), close_time=int(t.close_time),
            open=float(t.open), high=float(t.high), low=float(t.low), close=float(t.close),
            atr=float(t.atr14),
        )
        if cfg.MARTINGALA_ACTIVA:
            ev.signal.levels, ev.signal.levels_src = candidate_levels(
                side, price, float(t.atr14), fib, fib_df, cfg, cache)
        ev.note = f"¡SEÑAL {side}! {ev.signal.pattern}"
        ev.execution_points["status"] = "LISTO"
        ev.execution_points["message"] = f"🟢 ¡SEÑAL {side}! Patrón: {ev.signal.pattern}"
    else:
        ev.note = "En zona de valor; esperando agotamiento + patrón de velas en el gatillo."
    return ev


def evaluate_estrategia_3(frames: dict[str, pd.DataFrame], cfg, cache: dict | None = None, with_sr: bool = False) -> Evaluation:
    """Estrategia 3: Ruptura de Squeeze de Bollinger (Breakout de Alta Volatilidad)."""
    strat_info = STRATEGIES["estrategia_3"]
    ev = Evaluation(strategy_id="estrategia_3", strategy_name=strat_info["name"])
    tft = cfg.TF_TENDENCIA
    trig = frames.get(cfg.TF_GATILLO)
    d1 = frames.get(tft)
    if trig is None or d1 is None or len(trig) < 30 or len(d1) < MIN_TREND:
        ev.note = "Faltan datos de temporalidades."
        return ev

    t = trig.iloc[-1]
    ev.close_time = int(t.close_time)
    ev.price = float(t.close)
    d = d1.iloc[-1]

    # Fase 1: Tendencia general en 1d
    trend_up = bool(d.close > d.ema50)
    ev.trend = "LONG" if trend_up else "SHORT"
    ev.phase1_ok = True
    ev.phase1 = [
        Check(f"{tft} · Tendencia Close vs EMA(50)", f"{_f(d.close, 0)} vs {_f(d.ema50, 0)}", ok=trend_up)
    ]

    # Fase 2: Compresión de Bollinger en gatillo (ancho de banda < 3.5%)
    bandwidth = float((t.bb_up - t.bb_low) / max(t.bb_mid, 1.0))
    squeeze_ok = bandwidth < 0.05
    ev.phase2 = [
        Check(f"{cfg.TF_GATILLO} · Compresión Bollinger (ancho < 5%)", f"{bandwidth * 100:.2f}%", ok=squeeze_ok)
    ]
    ev.phase2_ok = squeeze_ok

    # Fase 3: Ruptura explosiva con volumen
    vol_ok = bool(t.volume > 1.3 * t.vol_sma20)
    if ev.trend == "LONG":
        break_ok = bool(t.close > t.bb_up)
        target_price = float(t.bb_up)
    else:
        break_ok = bool(t.close < t.bb_low)
        target_price = float(t.bb_low)

    ev.phase3 = [
        Check(f"{cfg.TF_GATILLO} · Ruptura Banda Bollinger", f"Cierre {_f(t.close, 1)} vs Banda {_f(target_price, 1)}", ok=break_ok),
        Check(f"{cfg.TF_GATILLO} · Volumen de expansión (> 1.3x)", f"{_f(t.volume, 1)} vs {_f(t.vol_sma20 * 1.3, 1)}", ok=vol_ok),
    ]
    ev.phase3_ok = bool(break_ok and vol_ok)

    ev.execution_points = {
        "strategy_id": "estrategia_3",
        "strategy_name": strat_info["name"],
        "side": ev.trend,
        "current_price": ev.price,
        "target_price": target_price,
        "current_rsi": float(t.rsi14),
        "target_rsi": "Ruptura BB",
        "dist_price": ev.price - target_price,
        "dist_rsi": 0.0,
        "trigger_tf": cfg.TF_GATILLO,
        "status": "LISTO" if ev.phase3_ok else "ESPERANDO",
        "message": f"Esperando compresión y ruptura de ${target_price:,.1f} con volumen.",
    }

    if ev.phase1_ok and ev.phase2_ok and ev.phase3_ok:
        ev.signal = Signal(
            side=ev.trend, pattern="Ruptura de Squeeze Bollinger con Volumen",
            exhaustion=f"Bandwidth={bandwidth * 100:.1f}%",
            open_time=int(t.open_time), close_time=int(t.close_time),
            open=float(t.open), high=float(t.high), low=float(t.low), close=float(t.close),
            atr=float(t.atr14),
        )
        ev.note = f"¡SEÑAL {ev.trend}! Ruptura de Squeeze"
    else:
        ev.note = "Esperando ruptura de compresión de volatilidad."
    return ev


def evaluate_estrategia_4(frames: dict[str, pd.DataFrame], cfg, cache: dict | None = None, with_sr: bool = False) -> Evaluation:
    """Estrategia 4: Cruce Dorado EMA 20/50 + Re-testeo."""
    strat_info = STRATEGIES["estrategia_4"]
    ev = Evaluation(strategy_id="estrategia_4", strategy_name=strat_info["name"])
    tft = cfg.TF_TENDENCIA
    trig = frames.get(cfg.TF_GATILLO)
    d1 = frames.get(tft)
    if trig is None or d1 is None or len(trig) < 30 or len(d1) < MIN_TREND:
        ev.note = "Faltan datos de temporalidades."
        return ev

    t = trig.iloc[-1]
    ev.close_time = int(t.close_time)
    ev.price = float(t.close)
    d = d1.iloc[-1]

    # Fase 1: Cruce alcista EMA20 > EMA50
    golden_ok = bool(d.ema20 > d.ema50)
    ev.trend = "LONG" if golden_ok else "SHORT"
    ev.phase1_ok = golden_ok
    ev.phase1 = [
        Check(f"{tft} · Cruce Dorado EMA(20) > EMA(50)", f"{_f(d.ema20, 0)} vs {_f(d.ema50, 0)}", ok=golden_ok)
    ]

    # Fase 2: Re-testeo de la EMA50 en gatillo
    retest_ok = bool(t.low <= t.ema50 * 1.003 and t.high >= t.ema50 * 0.997)
    ev.phase2 = [
        Check(f"{cfg.TF_GATILLO} · Re-testeo en EMA(50)", f"Mín {_f(t.low, 1)} vs EMA50 {_f(t.ema50, 1)}", ok=retest_ok)
    ]
    ev.phase2_ok = retest_ok

    # Fase 3: Rebote alcista (cierre verde)
    rebound_ok = bool(t.close > t.open and t.close > t.ema50)
    ev.phase3 = [
        Check(f"{cfg.TF_GATILLO} · Vela de rebote alcista", f"Cierre {_f(t.close, 1)} > Apertura {_f(t.open, 1)}", ok=rebound_ok)
    ]
    ev.phase3_ok = rebound_ok

    target_price = float(t.ema50)
    ev.execution_points = {
        "strategy_id": "estrategia_4",
        "strategy_name": strat_info["name"],
        "side": "LONG",
        "current_price": ev.price,
        "target_price": target_price,
        "current_rsi": float(t.rsi14),
        "target_rsi": "Rebote EMA50",
        "dist_price": ev.price - target_price,
        "dist_rsi": 0.0,
        "trigger_tf": cfg.TF_GATILLO,
        "status": "LISTO" if (ev.phase2_ok and ev.phase3_ok) else "ESPERANDO",
        "message": f"Esperando que el precio re-testee la EMA50 (${target_price:,.1f}) y confirme rebote.",
    }

    if ev.phase1_ok and ev.phase2_ok and ev.phase3_ok:
        ev.signal = Signal(
            side="LONG", pattern="Cruce Dorado + Re-testeo EMA50",
            exhaustion=f"EMA50={target_price:,.1f}",
            open_time=int(t.open_time), close_time=int(t.close_time),
            open=float(t.open), high=float(t.high), low=float(t.low), close=float(t.close),
            atr=float(t.atr14),
        )
        ev.note = "¡SEÑAL LONG! Rebote en Cruce Dorado"
    else:
        ev.note = "Esperando re-testeo y rebote en EMA50."
    return ev


def evaluate_estrategia_5(frames: dict[str, pd.DataFrame], cfg, cache: dict | None = None, with_sr: bool = False) -> Evaluation:
    """Estrategia 5: Cazador de Divergencias Alcistas en Soportes."""
    strat_info = STRATEGIES["estrategia_5"]
    ev = Evaluation(strategy_id="estrategia_5", strategy_name=strat_info["name"])
    tft = cfg.TF_TENDENCIA
    trig = frames.get(cfg.TF_GATILLO)
    d1 = frames.get(tft)
    if trig is None or d1 is None or len(trig) < 60 or len(d1) < MIN_TREND:
        ev.note = "Faltan datos de temporalidades."
        return ev

    t = trig.iloc[-1]
    ev.close_time = int(t.close_time)
    ev.price = float(t.close)
    d = d1.iloc[-1]

    # Fase 1: Tendencia alcista diaria
    trend_up = bool(d.close > d.sma200 if not pd.isna(d.sma200) else d.close > d.ema50)
    ev.trend = "LONG" if trend_up else "SHORT"
    ev.phase1_ok = trend_up
    ev.phase1 = [
        Check(f"{tft} · Tendencia Macro Alcista", f"{_f(d.close, 0)}", ok=trend_up)
    ]

    # Fase 2: Divergencia alcista de precio vs RSI/MACD
    div_ok, div_txt = patterns._divergence(trig, "LONG", cfg)
    ev.phase2 = [
        Check(f"{cfg.TF_GATILLO} · Divergencia Alcista RSI/MACD", div_txt or "Buscando divergencia", ok=div_ok)
    ]
    ev.phase2_ok = div_ok

    # Fase 3: Giro confirmado con RSI saliendo de sobreventa
    rsi_turn = bool(t.rsi14 > 35.0 and trig.iloc[-2].rsi14 <= 35.0)
    ev.phase3 = [
        Check(f"{cfg.TF_GATILLO} · Giro de RSI (> 35)", f"RSI {_f(t.rsi14, 1)}", ok=rsi_turn)
    ]
    ev.phase3_ok = rsi_turn

    ev.execution_points = {
        "strategy_id": "estrategia_5",
        "strategy_name": strat_info["name"],
        "side": "LONG",
        "current_price": ev.price,
        "target_price": None,
        "current_rsi": float(t.rsi14),
        "target_rsi": "> 35.0",
        "dist_price": 0.0,
        "dist_rsi": 0.0,
        "trigger_tf": cfg.TF_GATILLO,
        "status": "LISTO" if (ev.phase2_ok and ev.phase3_ok) else "ESPERANDO",
        "message": "Buscando divergencias regulares alcistas en soportes.",
    }

    if ev.phase1_ok and ev.phase2_ok and ev.phase3_ok:
        ev.signal = Signal(
            side="LONG", pattern=f"Divergencia Alcista ({div_txt})",
            exhaustion=div_txt,
            open_time=int(t.open_time), close_time=int(t.close_time),
            open=float(t.open), high=float(t.high), low=float(t.low), close=float(t.close),
            atr=float(t.atr14),
        )
        ev.note = "¡SEÑAL LONG! Divergencia alcista confirmada"
    else:
        ev.note = "Buscando divergencias regulares alcistas."
    return ev


# ---------------------------------------------------------------------------
# Despachador principal de evaluación
# ---------------------------------------------------------------------------
def evaluate(frames: dict[str, pd.DataFrame], cfg, cache: dict | None = None, with_sr: bool = False) -> Evaluation:
    """Evalúa la estrategia configurada en `cfg.ESTRATEGIA`."""
    strat = getattr(cfg, "ESTRATEGIA", "estrategia_1")
    if strat == "estrategia_1":
        return evaluate_estrategia_1(frames, cfg, cache, with_sr)
    elif strat == "estrategia_3":
        return evaluate_estrategia_3(frames, cfg, cache, with_sr)
    elif strat == "estrategia_4":
        return evaluate_estrategia_4(frames, cfg, cache, with_sr)
    elif strat == "estrategia_5":
        return evaluate_estrategia_5(frames, cfg, cache, with_sr)
    else:
        return evaluate_estrategia_2(frames, cfg, cache, with_sr)

