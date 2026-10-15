"""Módulo de diagnóstico de riesgo, apalancamiento y liquidación (pre-ejecución).

Calcula tamaño de posición, Stop Loss técnico, Take Profit, pérdida real si salta el SL y precio de
liquidación en margen aislado. Aplica los filtros de seguridad: reduce el tamaño o BLOQUEA la orden.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .binance_api import SymbolRules


@dataclass
class TradePlan:
    side: str
    ref_price: float
    qty: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
    leverage: int = 0
    sl_price: float = 0.0
    tp_price: float = 0.0
    dist_sl: float = 0.0            # fracción (0.012 = 1.2%)
    dist_tp: float = 0.0
    loss_sl_usdt: float = 0.0       # pérdida real si salta el SL (incluye comisiones)
    profit_tp_usdt: float = 0.0     # ganancia neta si llega al TP
    liq_price: float = 0.0
    liq_dist: float = 0.0
    approved: bool = False
    reduced: bool = False
    blocked_reason: str = ""
    notes: list[str] = field(default_factory=list)
    martingale: bool = False
    levels: list[dict] = field(default_factory=list)      # [{price, qty, offset}] (el nivel 0 es la entrada a mercado)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
def liquidation_distance(cfg) -> float:
    """Fracción de movimiento adverso que liquida el 100% del margen aislado (fórmula de la especificación)."""
    return 1.0 / cfg.APALANCAMIENTO - cfg.TASA_MANTENIMIENTO_BTC - 2.0 * cfg.TASA_COMISION_TAKER


def liquidation_price(side: str, entry: float, cfg) -> float:
    d = liquidation_distance(cfg)
    return entry * (1.0 - d) if side == "LONG" else entry * (1.0 + d)


def loss_at_sl(notional: float, dist_sl: float, fee: float) -> float:
    """Pérdida real = nominal x distancia_SL + comisiones de entrada y salida."""
    return notional * dist_sl + notional * fee * 2.0


def sl_price_for(side: str, signal, cfg) -> float:
    """Extremo (mecha) de la vela gatillo -/+ ATR_MULT_SL x ATR(14)."""
    if side == "LONG":
        return signal.low - cfg.ATR_MULT_SL * signal.atr
    return signal.high + cfg.ATR_MULT_SL * signal.atr


# ---------------------------------------------------------------------------
def build_plan(cfg, signal, ref_price: float, rules: SymbolRules, available: float | None = None) -> TradePlan:
    """Plan de la operación: con martingala (entradas escalonadas) si está activa y cabe en el riesgo/margen;
    si no cabe, se baja a menos niveles y, en último caso, a una única entrada normal."""
    single = _single_plan(cfg, signal, ref_price, rules, available)
    levels = getattr(signal, "levels", None)
    if not (cfg.MARTINGALA_ACTIVA and levels):
        return single
    for n in range(min(int(cfg.MARTINGALA_NIVELES), 1 + len(levels)), 1, -1):
        plan = _levels_plan(cfg, signal, ref_price, rules, available, n)
        if plan is not None:
            return plan
    single.notes.append(
        "Martingala activada pero NO cabe en el riesgo/margen configurados: se entra con un único nivel. "
        "Para permitirla sube el margen por operación y/o el riesgo máximo de la martingala.")
    return single


def _levels_plan(cfg, signal, ref_price: float, rules: SymbolRules, available: float | None, n: int) -> TradePlan | None:
    """Entradas escalonadas: nivel 0 a mercado y niveles 1..n-1 como órdenes límite, cada una = anterior x multiplicador.

    Un Stop Loss común queda detrás del último nivel. El tamaño se calcula para que, si se ejecutan TODOS los niveles y
    salta el Stop Loss, la pérdida total no supere el riesgo de la martingala (por eso la 1ª entrada es más pequeña) ni el
    presupuesto de margen por operación. Devuelve None si no cabe.
    """
    side = signal.side
    sg = 1.0 if side == "LONG" else -1.0
    fee = cfg.TASA_COMISION_TAKER
    lev = int(cfg.APALANCAMIENTO)
    prices = [ref_price] + [float(x) for x in signal.levels[: n - 1]]
    if side == "LONG":
        sl = min(signal.low, prices[-1]) - cfg.ATR_MULT_SL * signal.atr
    else:
        sl = max(signal.high, prices[-1]) + cfg.ATR_MULT_SL * signal.atr
    if any((p - sl) * sg <= 0 for p in prices) or any((prices[i] - prices[i + 1]) * sg <= 0 for i in range(n - 1)):
        return None
    w = [cfg.MARTINGALA_MULTIPLICADOR ** i for i in range(n)]
    risk_cap = cfg.MARTINGALA_RIESGO_MAX_USDT or cfg.RIESGO_MAXIMO_PERMITIDO_USDT
    budget = cfg.MARGEN_EN_USO_POR_OPERACION * lev                      # nominal TOTAL de todos los niveles
    unit_loss = sum(wi * (abs(p - sl) + fee * (p + sl)) for wi, p in zip(w, prices))
    unit_notional = sum(wi * p for wi, p in zip(w, prices))
    unit = rules.floor_qty(min(risk_cap / unit_loss, budget / unit_notional))
    qs = [rules.floor_qty(unit * wi) for wi in w]
    min_qty = max(float(rules.min_qty), cfg.LOTE_MINIMO_BTC)
    min_notional = max(float(rules.min_notional), cfg.NOTIONAL_MINIMO_USDT)
    if any(q < min_qty or q * p < min_notional for q, p in zip(qs, prices)):
        return None
    total_q = sum(qs)
    notional = sum(q * p for q, p in zip(qs, prices))
    avg_all = notional / total_q
    loss = sum(q * (abs(p - sl) + fee * (p + sl)) for q, p in zip(qs, prices))
    if loss > risk_cap * 1.0001 or notional > budget * 1.0001:
        return None
    liq_all = liquidation_price(side, avg_all, cfg)
    if (side == "LONG" and sl <= liq_all) or (side == "SHORT" and sl >= liq_all):
        return None
    if available is not None and available < notional / lev * 1.02 + notional * fee:
        return None

    plan = TradePlan(side=side, ref_price=ref_price, leverage=lev, martingale=True)
    plan.levels = [{"price": p, "qty": q, "offset": p / ref_price - 1.0} for p, q in zip(prices, qs)]
    plan.qty = qs[0]
    plan.notional, plan.margin = notional, notional / lev
    plan.sl_price = sl
    plan.dist_sl = abs(ref_price - sl) / ref_price
    plan.dist_tp = plan.dist_sl * cfg.RATIO_RIESGO_BENEFICIO
    plan.tp_price = ref_price * (1.0 + sg * plan.dist_tp)
    plan.loss_sl_usdt = loss
    plan.profit_tp_usdt = total_q * cfg.RATIO_RIESGO_BENEFICIO * abs(avg_all - sl) - notional * fee - total_q * (
        avg_all + cfg.RATIO_RIESGO_BENEFICIO * abs(avg_all - sl) * sg) * fee
    plan.liq_price, plan.liq_dist = liq_all, liquidation_distance(cfg)
    plan.approved = True
    plan.notes.append(
        f"MARTINGALA de {n} niveles ({getattr(signal, 'levels_src', '') or 'niveles'}): "
        + " · ".join(f"{q:g} BTC @ ${p:,.0f}" for q, p in zip(qs, prices))
        + f". Si se ejecutan todos y salta el SL se pierden ${loss:,.2f} (máx. ${risk_cap:,.2f}).")
    return plan


def _single_plan(cfg, signal, ref_price: float, rules: SymbolRules, available: float | None = None) -> TradePlan:
    """Plan de una única entrada o bloqueado (con el motivo)."""
    side = signal.side
    fee = cfg.TASA_COMISION_TAKER
    lev = int(cfg.APALANCAMIENTO)
    plan = TradePlan(side=side, ref_price=ref_price, leverage=lev)
    plan.liq_dist = liquidation_distance(cfg)
    plan.liq_price = liquidation_price(side, ref_price, cfg)

    min_qty = max(float(rules.min_qty), cfg.LOTE_MINIMO_BTC)
    min_notional = max(float(rules.min_notional), cfg.NOTIONAL_MINIMO_USDT)

    def block(reason: str) -> TradePlan:
        plan.approved = False
        plan.blocked_reason = reason
        return plan

    if not ref_price > 0 or not signal.atr > 0:
        return block("Datos inválidos (precio o ATR no válidos).")

    # --- Stop Loss técnico y distancia
    plan.sl_price = sl_price_for(side, signal, cfg)
    plan.dist_sl = abs(ref_price - plan.sl_price) / ref_price
    wrong_side = (side == "LONG" and plan.sl_price >= ref_price) or (side == "SHORT" and plan.sl_price <= ref_price)
    if wrong_side or plan.dist_sl <= 0:
        return block("El Stop Loss calculado quedó del lado equivocado del precio.")

    # --- Filtro de liquidación: el SL debe saltar ANTES que la liquidación
    if plan.dist_sl >= plan.liq_dist:
        return block(
            f"BLOQUEADA: el Stop Loss ({plan.dist_sl * 100:.2f}%) está más lejos que la liquidación "
            f"({plan.liq_dist * 100:.2f}%). Riesgo de liquidación prematura."
        )

    # --- Tamaño de posición (nominal objetivo = margen x apalancamiento) redondeado al lote de Binance
    target_notional = cfg.MARGEN_EN_USO_POR_OPERACION * lev
    qty = rules.floor_qty(target_notional / ref_price)
    if qty < min_qty or qty * ref_price < min_notional:
        return block(
            f"BLOQUEADA: la cantidad ({qty:g} BTC = ${qty * ref_price:,.2f}) no alcanza el mínimo de Binance "
            f"({min_qty:g} BTC y ${min_notional:,.0f} nominal)."
        )
    notional = qty * ref_price
    loss = loss_at_sl(notional, plan.dist_sl, fee)

    # --- Filtro de riesgo máximo: reducir el margen o bloquear
    if loss > cfg.RIESGO_MAXIMO_PERMITIDO_USDT:
        max_notional = cfg.RIESGO_MAXIMO_PERMITIDO_USDT / (plan.dist_sl + 2.0 * fee)
        qty_r = rules.floor_qty(max_notional / ref_price)
        if qty_r < min_qty or qty_r * ref_price < min_notional:
            return block(
                f"BLOQUEADA: con un SL a {plan.dist_sl * 100:.2f}% la pérdida mínima posible "
                f"(${loss_at_sl(min_notional, plan.dist_sl, fee):.2f} con el lote mínimo) supera el riesgo "
                f"máximo de ${cfg.RIESGO_MAXIMO_PERMITIDO_USDT:.2f}."
            )
        plan.reduced = True
        plan.notes.append(
            f"Margen reducido de ${notional / lev:,.2f} a ${qty_r * ref_price / lev:,.2f} para no superar el "
            f"riesgo máximo de ${cfg.RIESGO_MAXIMO_PERMITIDO_USDT:.2f}."
        )
        qty = qty_r
        notional = qty * ref_price
        loss = loss_at_sl(notional, plan.dist_sl, fee)

    plan.qty = qty
    plan.notional = notional
    plan.margin = notional / lev
    plan.loss_sl_usdt = loss

    # --- Saldo disponible
    if available is not None and available < plan.margin * 1.02 + notional * fee:
        return block(f"BLOQUEADA: saldo disponible ${available:,.2f} insuficiente para el margen ${plan.margin:,.2f}.")

    # --- Take Profit = distancia del SL x ratio R:B
    plan.dist_tp = plan.dist_sl * cfg.RATIO_RIESGO_BENEFICIO
    sign = 1.0 if side == "LONG" else -1.0
    plan.tp_price = ref_price * (1.0 + sign * plan.dist_tp)
    plan.profit_tp_usdt = notional * plan.dist_tp - notional * fee * 2.0
    plan.approved = True
    return plan


# ---------------------------------------------------------------------------
def startup_checks(cfg, price: float, rules: SymbolRules) -> tuple[bool, list[str]]:
    """Validaciones del tablero inicial (lote mínimo y nominal mínimo)."""
    msgs = []
    ok = True
    notional = cfg.MARGEN_EN_USO_POR_OPERACION * cfg.APALANCAMIENTO
    qty = rules.floor_qty(notional / price)
    min_qty = max(float(rules.min_qty), cfg.LOTE_MINIMO_BTC)
    min_notional = max(float(rules.min_notional), cfg.NOTIONAL_MINIMO_USDT)
    if qty < min_qty:
        ok = False
        msgs.append(f"Cantidad {qty:g} BTC < lote mínimo {min_qty:g} BTC")
    if qty * price < min_notional:
        ok = False
        msgs.append(f"Nominal real ${qty * price:,.2f} < mínimo ${min_notional:,.0f}")
    return ok, msgs


def format_board(cfg, price: float, rules: SymbolRules, plan: TradePlan | None = None) -> str:
    """Tablero de Diagnóstico Financiero (consola, logs y dashboard)."""
    lev = cfg.APALANCAMIENTO
    nominal = cfg.MARGEN_EN_USO_POR_OPERACION * lev
    qty_raw = nominal / price
    qty = rules.floor_qty(qty_raw)
    ok, problems = startup_checks(cfg, price, rules)
    liq_d = liquidation_distance(cfg)
    line = "─" * 64
    out = [
        line,
        "  TABLERO DE DIAGNÓSTICO FINANCIERO",
        line,
        f"  Entorno ................ {cfg.mode_label()}   Par: {cfg.SYMBOL}   Precio: ${price:,.2f}",
        f"  Capital total .......... ${cfg.CAPITAL_TOTAL_CUENTA:,.2f} USDT",
        f"  Margen por operación ... ${cfg.MARGEN_EN_USO_POR_OPERACION:,.2f} USDT (ISOLATED)",
        f"  Apalancamiento ......... {lev}x",
        f"  Posición nominal ....... ${nominal:,.2f} USDT",
        f"  Cantidad ............... {qty_raw:.6f} BTC → {qty:g} BTC (lote Binance) = ${qty * price:,.2f}",
        f"  Validaciones ........... lote ≥ {max(float(rules.min_qty), cfg.LOTE_MINIMO_BTC):g} BTC y nominal ≥ "
        f"${max(float(rules.min_notional), cfg.NOTIONAL_MINIMO_USDT):,.0f}: {'OK' if ok else 'FALLA – ' + '; '.join(problems)}",
        f"  Distancia liquidación .. {liq_d * 100:.2f}%  (1/{lev} − {cfg.TASA_MANTENIMIENTO_BTC * 100:.2f}% mant. "
        f"− 2×{cfg.TASA_COMISION_TAKER * 100:.2f}% comisión)",
        f"  Liquidación LONG ....... ${liquidation_price('LONG', price, cfg):,.2f}",
        f"  Liquidación SHORT ...... ${liquidation_price('SHORT', price, cfg):,.2f}",
        f"  Riesgo máx. permitido .. ${cfg.RIESGO_MAXIMO_PERMITIDO_USDT:,.2f} "
        f"({cfg.RIESGO_MAXIMO_PERMITIDO_USDT / cfg.CAPITAL_TOTAL_CUENTA * 100:.1f}% de la cuenta)   R:B = 1:{cfg.RATIO_RIESGO_BENEFICIO:g}",
    ]
    if plan is not None:
        out += [line, f"  OPERACIÓN {plan.side} – referencia ${plan.ref_price:,.2f}"]
        out += [
            f"  Stop Loss .............. ${plan.sl_price:,.2f}  (distancia {plan.dist_sl * 100:.2f}%)",
            f"  Take Profit ............ ${plan.tp_price:,.2f}  (distancia {plan.dist_tp * 100:.2f}%)",
        ]
        if plan.approved:
            out += [
                f"  Cantidad / nominal ..... {plan.qty:g} BTC / ${plan.notional:,.2f}  (margen ${plan.margin:,.2f})",
                f"  Pérdida real si salta SL ${plan.loss_sl_usdt:,.2f}  |  Ganancia neta si llega a TP ${plan.profit_tp_usdt:,.2f}",
                f"  Liquidación ............ ${plan.liq_price:,.2f}  (SL queda a salvo antes)",
            ]
            for n in plan.notes:
                out.append(f"  {'🪜' if n.startswith('MARTINGALA') else '⚠'} {n}")
        else:
            out.append(f"  ⛔ {plan.blocked_reason}")
    margin = plan.margin if (plan is not None and plan.approved) else cfg.MARGEN_EN_USO_POR_OPERACION
    liq_price = plan.liq_price if plan is not None else liquidation_price("LONG", price, cfg)
    free = max(cfg.CAPITAL_TOTAL_CUENTA - margin, 0.0)
    out += [
        line,
        f"  📌 Margen en uso: ${margin:,.2f} USDT | Apalancamiento: {lev}x | Posición Total: ${margin * lev:,.2f} USDT.",
        f"     Para perder el 100% de estos ${margin:,.2f} USDT, BTC tendría que moverse un {liq_d * 100:.2f}% en tu "
        f"contra (llegar a ${liq_price:,.2f}).",
        f"     Tus otros ${free:,.2f} USDT están a salvo.",
        line,
    ]
    return "\n".join(out)
