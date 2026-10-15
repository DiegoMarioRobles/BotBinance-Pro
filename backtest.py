"""Backtest por consola:  python backtest.py --desde 2021-01-01 --hasta 2026-10-01

Usa datos reales públicos de Binance (no necesita API keys) y la misma estrategia y gestión de riesgo
que el bot en vivo, con los parámetros de data/config.json.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from botbinance import logging_setup
from botbinance.backtester import parse_date, run_backtest, save_result
from botbinance.binance_api import MAINNET_URL, BinanceFutures
from botbinance.config import ConfigStore
from botbinance.util import PULLBACK_TFS, TREND_TFS, TRIGGER_TFS, now_ms


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest de la estrategia Top-Down Pullback")
    ap.add_argument("--desde", default="2021-01-01", help="fecha inicial YYYY-MM-DD")
    ap.add_argument("--hasta", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"), help="fecha final YYYY-MM-DD")
    ap.add_argument("--tendencia", choices=TREND_TFS, help="temporalidad de tendencia (¿alcista o bajista?)")
    ap.add_argument("--superior", choices=TREND_TFS + ["ninguno"], help="filtro de tendencia superior ('ninguno' = sin filtro)")
    ap.add_argument("--zona", choices=PULLBACK_TFS, help="temporalidad de los retrocesos (zona EMA20/EMA50)")
    ap.add_argument("--gatillo", choices=TRIGGER_TFS, help="temporalidad del gatillo de entrada")
    ap.add_argument("--martingala", choices=["si", "no"], help="activar/desactivar las entradas escalonadas")
    ap.add_argument("--niveles", type=int, help="niveles de la martingala (2-4)")
    ap.add_argument("--margen", type=float, help="margen total por operación en USDT (presupuesto de todos los niveles)")
    ap.add_argument("--riesgo-mart", type=float, help="riesgo máximo con martingala en USDT (0 = el normal)")
    ap.add_argument("--ventana", type=int, help="velas en las que puede darse el agotamiento (1 = estricto)")
    ap.add_argument("--slippage", type=float, default=0.0002, help="slippage por operación (0.0002 = 0.02%%)")
    args = ap.parse_args()

    logging_setup.setup(None)
    cfg = ConfigStore().load()
    for key, val in (("TF_TENDENCIA", args.tendencia), ("TF_ZONA", args.zona), ("TF_GATILLO", args.gatillo),
                     ("MARTINGALA_NIVELES", args.niveles), ("MARGEN_EN_USO_POR_OPERACION", args.margen),
                     ("MARTINGALA_RIESGO_MAX_USDT", args.riesgo_mart)):
        if val is not None:
            setattr(cfg, key, val)
    if args.superior:
        cfg.TF_TENDENCIA_SUPERIOR = "" if args.superior == "ninguno" else args.superior
    if args.martingala:
        cfg.MARTINGALA_ACTIVA = args.martingala == "si"
    if args.ventana:
        cfg.VENTANA_AGOTAMIENTO = args.ventana
    errs = cfg.validate()
    if errs:
        print("Configuración inválida:", *errs, sep="\n  - ")
        return 2

    client = BinanceFutures(MAINNET_URL)
    client.sync_time()
    rules = client.symbol_rules(cfg.SYMBOL)

    def progress(p: float, msg: str) -> None:
        print(f"\r[{p * 100:5.1f}%] {msg:<60}", end="", flush=True)

    start, end = parse_date(args.desde), min(parse_date(args.hasta) + 86_400_000 - 1, now_ms())
    res = run_backtest(cfg, client, start, end, args.slippage, progress, rules=rules)
    print()
    m, f = res["metrics"], res["funnel"]
    print("=" * 66)
    pr = res["params"]
    print(f" BACKTEST {cfg.SYMBOL} · {pr['desde']} → {pr['hasta']}")
    print(f" tendencia {pr['tf_tendencia']} (+ {pr['tf_superior']}) · retrocesos {pr['tf_zona']} · gatillo {pr['tf_gatillo']} · martingala: {pr['martingala']}")
    print("=" * 66)
    print(f" Velas evaluadas ........ {f['velas']:,}")
    print(f"   con tendencia LONG/SHORT: {f['tendencia_larga']:,} / {f['tendencia_corta']:,}   lateral(STANDBY): {f['standby_lateral']:,}")
    print(f"   Fase 2 cumplida ...... {f['fase2_ok']:,}")
    print(f"   Señales (Fase 3) ..... {f['fase3_ok_senales']:,}   bloqueadas por riesgo: {f['bloqueadas_riesgo']}   operadas: {f['operadas']}")
    print("-" * 66)
    for k in ("operaciones", "ganadoras", "perdedoras", "en_cero_be", "winrate_pct", "neto_total_usdt", "retorno_pct",
              "capital_final", "expectativa_usdt", "expectativa_R", "profit_factor", "ganancia_media_usdt",
              "perdida_media_usdt", "max_drawdown_usdt", "max_drawdown_pct", "duracion_media_horas",
              "largos", "cortos", "con_martingala", "niveles_medios", "buy_hold_pct"):
        print(f" {k:<24} {m[k]}")
    if res["by_year"]:
        print("-" * 66)
        for y, d in res["by_year"].items():
            print(f" {y}: {d['operaciones']} operaciones · neto {d['neto']:+.2f} USDT · ganadoras {d['ganadoras']}")
    print("-" * 66)
    for n in res["notes"]:
        print(" •", n)
    print(" Resultado guardado en:", save_result(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
