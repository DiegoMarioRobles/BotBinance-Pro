"""Backtest con datos históricos reales de Binance Futures (mainnet, endpoints públicos).

Usa EXACTAMENTE las mismas funciones que el bot en vivo (`strategy.evaluate` y `risk.build_plan`), de modo
que lo que se prueba es lo que se ejecuta. Reglas del simulador (conservadoras):
  * Señal al CIERRE de la vela gatillo -> entrada a mercado en la APERTURA de la vela siguiente (+ slippage).
  * SL/TP calculados como en vivo (distancia % desde el precio de entrada real).
  * Si una vela toca SL y TP a la vez, se asume que salta primero el SL.
  * Break-even: al tocar 1R se mueve el SL a entrada +0.1% a partir de la vela siguiente.
  * Comisión taker en entrada y salida. No incluye funding.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd

from . import paths, risk, strategy
from .binance_api import BinanceFutures, SymbolRules
from .data_feed import COLS, klines_to_df, resample
from .indicators import enrich
from .util import RESAMPLED, TF_MS, next_open, now_ms

log = logging.getLogger("botbinance.backtest")


# ---------------------------------------------------------------------------
# Descarga de historial (con caché en disco)
# ---------------------------------------------------------------------------
def _download(client: BinanceFutures, symbol: str, tf: str, start_ms: int, end_ms: int,
              cancel: threading.Event | None = None) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    cur = start_ms
    while cur < end_ms:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("cancelado")
        kl = client.klines(symbol, tf, limit=1500, start_time=cur)
        if not kl:
            break
        df = klines_to_df(kl)
        parts.append(df)
        nxt = next_open(tf, int(df["open_time"].iloc[-1]))
        if nxt <= cur:
            break
        cur = nxt
        if len(df) < 1500:
            break
        time.sleep(0.05)
    if not parts:
        return pd.DataFrame(columns=COLS)
    out = pd.concat(parts, ignore_index=True).drop_duplicates("open_time")
    return out[out["close_time"] < client.now_ms()].reset_index(drop=True)


def load_history(client: BinanceFutures, symbol: str, tf: str, start_ms: int, end_ms: int,
                 cancel: threading.Event | None = None) -> pd.DataFrame:
    """Velas cerradas de [start_ms, end_ms]; usa/actualiza la caché data/hist/<par>_<tf>.csv."""
    if tf in RESAMPLED:                       # 16h, 20h, 2d: se construyen a partir de velas reales de otra temporalidad
        return resample(load_history(client, symbol, RESAMPLED[tf][0], start_ms, end_ms, cancel), tf)
    iv = TF_MS[tf]
    csv = paths.HIST_DIR / f"{symbol}_{tf}.csv"
    meta_f = paths.HIST_DIR / f"{symbol}_{tf}.meta.json"
    df = None
    if csv.exists() and meta_f.exists():
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
            if meta.get("requested_start", 1 << 62) <= start_ms:
                df = pd.read_csv(csv)
        except (OSError, ValueError):
            df = None
    if df is None or df.empty:
        df = _download(client, symbol, tf, start_ms, end_ms, cancel)
        meta_f.write_text(json.dumps({"requested_start": start_ms}), encoding="utf-8")
    else:
        last = int(df["open_time"].iloc[-1])
        if last + 2 * iv < min(end_ms, client.now_ms()):
            new = _download(client, symbol, tf, last + iv, end_ms, cancel)
            if not new.empty:
                df = pd.concat([df, new], ignore_index=True).drop_duplicates("open_time")
    df = df.sort_values("open_time").reset_index(drop=True)
    df.to_csv(csv, index=False)
    return df[(df["open_time"] >= start_ms) & (df["open_time"] <= end_ms)].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Simulación
# ---------------------------------------------------------------------------
@dataclass
class SimTrade:
    side: str
    pattern: str
    signal_time: int
    entry_time: int
    entry: float
    exit_time: int
    exit: float
    qty: float
    margin: float
    sl: float
    tp: float
    dist_sl: float
    reason: str
    gross: float
    fees: float
    net: float
    r: float
    reduced: bool
    be: bool
    levels_filled: int = 1


def _simulate(trig: pd.DataFrame, i: int, plan, cfg, slip: float, o, h, lo, c, ot, ct) -> tuple[SimTrade, int]:
    """Simula una operación abierta tras la señal de la vela i (con entradas escalonadas si el plan las incluye).

    Si en una misma vela se ejecuta un nivel y salta el SL se asume el peor orden (primero el movimiento adverso).
    Devuelve (operación, índice de la vela de salida)."""
    side = plan.side
    sg = 1.0 if side == "LONG" else -1.0
    e_idx = i + 1
    entry = o[e_idx] * (1.0 + sg * slip)               # compra más cara / venta más barata
    sl0 = entry * (1.0 - sg * plan.dist_sl)
    sl = sl0
    rr = plan.dist_tp / plan.dist_sl
    pending = [(entry * (1.0 + lv["offset"]), lv["qty"]) for lv in plan.levels[1:]] if plan.martingale else []
    fills = [(entry, plan.qty)]
    qty, cost = plan.qty, entry * plan.qty
    avg = entry
    tp = entry * (1.0 + sg * plan.dist_tp)
    fee = cfg.TASA_COMISION_TAKER
    be_done = False
    be_buf = cfg.BREAK_EVEN_BUFFER_PCT
    n = len(o)
    exit_px, reason, x_idx = None, "FIN DE DATOS", n - 1
    for j in range(e_idx, n):
        while pending and ((lo[j] <= pending[0][0]) if side == "LONG" else (h[j] >= pending[0][0])):
            pr, q = pending.pop(0)
            pr = min(pr, o[j]) if side == "LONG" else max(pr, o[j])          # si abre más allá del nivel, rellena a la apertura
            fills.append((pr, q))
            qty += q
            cost += pr * q
            avg = cost / qty
            tp = avg + sg * rr * abs(avg - sl0)
        sl_hit = (lo[j] <= sl) if side == "LONG" else (h[j] >= sl)
        tp_hit = (h[j] >= tp) if side == "LONG" else (lo[j] <= tp)
        gap_sl = (o[j] <= sl) if side == "LONG" else (o[j] >= sl)
        gap_tp = (o[j] >= tp) if side == "LONG" else (o[j] <= tp)
        if gap_sl:
            exit_px, reason, x_idx = o[j], ("BREAK-EVEN" if be_done else "STOP LOSS"), j
            break
        if sl_hit:                                     # si toca SL y TP en la misma vela: gana el SL
            exit_px, reason, x_idx = sl * (1.0 - sg * slip), ("BREAK-EVEN" if be_done else "STOP LOSS"), j
            break
        if gap_tp:
            exit_px, reason, x_idx = o[j], "TAKE PROFIT", j
            break
        if tp_hit:
            exit_px, reason, x_idx = tp * (1.0 - sg * slip), "TAKE PROFIT", j
            break
        if cfg.ACTIVAR_BREAK_EVEN and not be_done:
            one_r = avg + sg * abs(avg - sl0)
            if (h[j] >= one_r) if side == "LONG" else (lo[j] <= one_r):
                be_done = True
                sl = avg * (1.0 + sg * be_buf)         # vigente desde la vela siguiente
                pending.clear()                        # con riesgo cero ya no se añaden niveles
    if exit_px is None:
        exit_px = c[n - 1]
    gross = sum(q * (exit_px - p) * sg for p, q in fills)
    fees = sum(q * p * fee for p, q in fills) + qty * exit_px * fee
    net = gross - fees
    st = SimTrade(
        side=side, pattern="", signal_time=int(ot[i]), entry_time=int(ot[e_idx]),
        entry=float(avg), exit_time=int(ct[x_idx]), exit=float(exit_px), qty=qty,
        margin=plan.margin, sl=float(sl0), tp=float(tp), dist_sl=plan.dist_sl,
        reason=reason, gross=float(gross), fees=float(fees), net=float(net),
        r=float(net / plan.loss_sl_usdt) if plan.loss_sl_usdt else 0.0, reduced=plan.reduced, be=be_done,
        levels_filled=len(fills),
    )
    return st, x_idx


def run_backtest(cfg, client: BinanceFutures, start_ms: int, end_ms: int, slippage: float = 0.0002,
                 progress: Callable[[float, str], None] | None = None,
                 cancel: threading.Event | None = None, rules: SymbolRules | None = None) -> dict:
    rules = rules or SymbolRules(symbol=cfg.SYMBOL)
    say = progress or (lambda p, m: None)
    tfs = {cfg.TF_TENDENCIA, cfg.TF_ZONA, cfg.TF_GATILLO, cfg.TF_FIBONACCI}
    if cfg.TF_TENDENCIA_SUPERIOR:
        tfs.add(cfg.TF_TENDENCIA_SUPERIOR)
    tfs = sorted(tfs, key=lambda t: TF_MS[t])
    if (end_ms - start_ms) / TF_MS[cfg.TF_GATILLO] > 350_000:
        raise ValueError(f"Rango demasiado grande para velas de {cfg.TF_GATILLO} (más de 350 000 velas). "
                         "Acorta el rango de fechas o usa un gatillo mayor.")
    frames: dict[str, pd.DataFrame] = {}
    for n, tf in enumerate(tfs):
        say(0.02 + 0.18 * n / len(tfs), f"Descargando historial {tf}…")
        warm = 320 * TF_MS[tf]                     # 320 velas de calentamiento para los indicadores
        df = load_history(client, cfg.SYMBOL, tf, max(start_ms - warm, 0), end_ms, cancel)
        if df.empty:
            raise RuntimeError(f"Sin datos históricos para {tf}")
        frames[tf] = enrich(df, cfg.BB_PERIODO, cfg.BB_DESVIACION)
    say(0.2, "Simulando la estrategia vela a vela…")

    trig = frames[cfg.TF_GATILLO]
    o, h, lo, c = (trig[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    ot = trig["open_time"].to_numpy()
    ct = trig["close_time"].to_numpy()
    kidx = {tf: np.searchsorted(frames[tf]["close_time"].to_numpy(), ct, side="right") - 1 for tf in tfs}

    first = int(np.searchsorted(ot, start_ms, side="left"))
    last = len(trig) - 2                                 # necesitamos la vela siguiente para entrar
    cache: dict = {}
    equity = cfg.CAPITAL_TOTAL_CUENTA
    peak = equity
    max_dd = 0.0
    trades: list[SimTrade] = []
    curve = [(int(ot[first]), equity)]
    funnel = {"velas": 0, "tendencia_larga": 0, "tendencia_corta": 0, "standby_lateral": 0, "sin_tendencia": 0,
              "fase2_ok": 0, "fase3_ok_senales": 0, "bloqueadas_riesgo": 0, "operadas": 0, "sin_capital": 0}
    blocked: dict[str, int] = {}
    i = first
    total = max(last - first, 1)
    while i <= last:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("cancelado")
        if (i - first) % 500 == 0:
            say(0.2 + 0.78 * (i - first) / total, f"Simulando… {pd.to_datetime(ot[i], unit='ms', utc=True):%Y-%m-%d}")
        view = {tf: frames[tf].iloc[: kidx[tf][i] + 1] for tf in tfs}
        ev = strategy.evaluate(view, cfg, cache)
        funnel["velas"] += 1
        if ev.trend == "LONG":
            funnel["tendencia_larga"] += 1
        elif ev.trend == "SHORT":
            funnel["tendencia_corta"] += 1
        elif ev.trend == "STANDBY":
            funnel["standby_lateral"] += 1
        else:
            funnel["sin_tendencia"] += 1
        if ev.phase2_ok:
            funnel["fase2_ok"] += 1
        sig = ev.signal
        if sig is None:
            i += 1
            continue
        funnel["fase3_ok_senales"] += 1
        plan = risk.build_plan(cfg, sig, float(c[i]), rules, available=equity)
        if not plan.approved:
            funnel["bloqueadas_riesgo"] += 1
            key = plan.blocked_reason.split(":")[0][:60] if plan.blocked_reason else "bloqueada"
            reason_short = ("SL más lejos que liquidación" if "liquidación" in plan.blocked_reason else
                            "Riesgo mínimo > máximo permitido" if "pérdida mínima" in plan.blocked_reason else
                            "Saldo insuficiente" if "saldo" in plan.blocked_reason else
                            "Lote/nominal mínimo" if "mínimo de Binance" in plan.blocked_reason else key)
            blocked[reason_short] = blocked.get(reason_short, 0) + 1
            i += 1
            continue
        st, x_idx = _simulate(trig, i, plan, cfg, slippage, o, h, lo, c, ot, ct)
        st.pattern = sig.pattern
        trades.append(st)
        funnel["operadas"] += 1
        equity += st.net
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append((st.exit_time, equity))
        if equity < cfg.MARGEN_EN_USO_POR_OPERACION:
            funnel["sin_capital"] += 1
            break
        i = x_idx + 1

    say(0.99, "Calculando métricas…")
    result = _summarize(cfg, trades, curve, funnel, blocked, start_ms, end_ms, frames, max_dd, slippage)
    say(1.0, "Listo")
    return result


# ---------------------------------------------------------------------------
def _summarize(cfg, trades: list[SimTrade], curve, funnel, blocked, start_ms, end_ms, frames, max_dd, slip) -> dict:
    cap0 = cfg.CAPITAL_TOTAL_CUENTA
    n = len(trades)
    nets = np.array([t.net for t in trades]) if n else np.array([])
    wins = nets[nets > 0.05] if n else nets
    losses = nets[nets < -0.05] if n else nets
    flat = n - len(wins) - len(losses)
    gross_win, gross_loss = float(wins.sum()) if len(wins) else 0.0, float(-losses.sum()) if len(losses) else 0.0
    rs = np.array([t.r for t in trades]) if n else np.array([])
    trig = frames[cfg.TF_GATILLO]
    seg = trig[(trig["open_time"] >= start_ms) & (trig["open_time"] <= end_ms)]
    bh = None
    if len(seg) > 1:
        bh = float(seg["close"].iloc[-1] / seg["open"].iloc[0] - 1.0)
    by_year: dict[str, dict] = {}
    for t in trades:
        y = str(datetime.fromtimestamp(t.exit_time / 1000, tz=timezone.utc).year)
        d = by_year.setdefault(y, {"operaciones": 0, "neto": 0.0, "ganadoras": 0})
        d["operaciones"] += 1
        d["neto"] += t.net
        d["ganadoras"] += 1 if t.net > 0.05 else 0
    final_eq = curve[-1][1]
    metrics = {
        "operaciones": n,
        "ganadoras": int(len(wins)), "perdedoras": int(len(losses)), "en_cero_be": int(flat),
        "winrate_pct": round(100.0 * len(wins) / n, 1) if n else None,
        "neto_total_usdt": round(float(nets.sum()), 2) if n else 0.0,
        "retorno_pct": round(100.0 * (final_eq - cap0) / cap0, 2),
        "capital_final": round(final_eq, 2),
        "ganancia_media_usdt": round(float(wins.mean()), 2) if len(wins) else None,
        "perdida_media_usdt": round(float(losses.mean()), 2) if len(losses) else None,
        "expectativa_usdt": round(float(nets.mean()), 2) if n else None,
        "expectativa_R": round(float(rs.mean()), 2) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not gross_win else float("inf")),
        "max_drawdown_usdt": round(float(max_dd), 2),
        "max_drawdown_pct": round(100.0 * max_dd / cap0, 2),
        "duracion_media_horas": round(float(np.mean([(t.exit_time - t.entry_time) / 3_600_000 for t in trades])), 1) if n else None,
        "largos": sum(1 for t in trades if t.side == "LONG"), "cortos": sum(1 for t in trades if t.side == "SHORT"),
        "reducidas_por_riesgo": sum(1 for t in trades if t.reduced),
        "break_even_activado": sum(1 for t in trades if t.be),
        "con_martingala": sum(1 for t in trades if t.levels_filled > 1),
        "niveles_medios": round(float(np.mean([t.levels_filled for t in trades])), 2) if n else None,
        "buy_hold_pct": round(100.0 * bh, 2) if bh is not None else None,
    }
    notes = []
    if n < 30:
        notes.append(f"Muestra insuficiente: {n} operaciones. Con tan pocas, ninguna métrica es estadísticamente "
                     "fiable (un par de operaciones cambian todo el resultado).")
    if funnel["bloqueadas_riesgo"]:
        notes.append(f"{funnel['bloqueadas_riesgo']} señales fueron bloqueadas por el módulo de riesgo: {blocked}.")
    notes.append("No incluye funding rates ni liquidez real; el slippage es una estimación.")
    return {
        "params": {
            "desde": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "hasta": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "par": cfg.SYMBOL, "tf_tendencia": cfg.TF_TENDENCIA, "tf_superior": cfg.TF_TENDENCIA_SUPERIOR or "—",
            "tf_zona": cfg.TF_ZONA, "tf_gatillo": cfg.TF_GATILLO,
            "martingala": (f"{cfg.MARTINGALA_NIVELES} niveles x{cfg.MARTINGALA_MULTIPLICADOR:g}" if cfg.MARTINGALA_ACTIVA else "no"),
            "capital": cap0, "margen": cfg.MARGEN_EN_USO_POR_OPERACION, "apalancamiento": cfg.APALANCAMIENTO,
            "riesgo_max": cfg.RIESGO_MAXIMO_PERMITIDO_USDT, "rr": cfg.RATIO_RIESGO_BENEFICIO,
            "break_even": cfg.ACTIVAR_BREAK_EVEN, "comision": cfg.TASA_COMISION_TAKER, "slippage": slip,
            "ventana_agotamiento": cfg.VENTANA_AGOTAMIENTO,
        },
        "metrics": metrics,
        "funnel": funnel,
        "blocked": blocked,
        "by_year": {k: {**v, "neto": round(v["neto"], 2)} for k, v in sorted(by_year.items())},
        "equity": [[int(t), round(float(e), 2)] for t, e in curve],
        "trades": [t.__dict__ for t in trades],
        "notes": notes,
        "generated": now_ms(),
    }


def save_result(result: dict) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    f = paths.BT_DIR / f"backtest_{stamp}.json"
    f.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if result["trades"]:
        pd.DataFrame(result["trades"]).to_csv(paths.BT_DIR / f"backtest_{stamp}_operaciones.csv", index=False)
    return str(f)


def parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
