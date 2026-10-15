from botbinance import risk
from botbinance.strategy import Signal


def sig(side="LONG", price=80_000.0, low=79_700.0, high=80_300.0, atr=400.0):
    return Signal(side=side, pattern="Pin Bar", exhaustion="x", open_time=0, close_time=1, open=price, high=high,
                  low=low, close=price, atr=atr)


def test_liquidation_distance_matches_spec(cfg):
    assert abs(risk.liquidation_distance(cfg) - 0.095) < 1e-12            # 1/10 - 0.4% - 2*0.05%
    assert abs(risk.liquidation_price("LONG", 100_000, cfg) - 90_500) < 1e-6
    assert abs(risk.liquidation_price("SHORT", 100_000, cfg) - 109_500) < 1e-6


def test_loss_formula(cfg):
    # 200 * 1% + 200 * 0.05% * 2 = 2.2
    assert abs(risk.loss_at_sl(200, 0.01, 0.0005) - 2.2) < 1e-12


def test_long_plan_approved(cfg, rules):
    p = risk.build_plan(cfg, sig(), 80_000.0, rules)
    assert p.approved and not p.reduced
    assert abs(p.sl_price - (79_700 - 1.5 * 400)) < 1e-6                   # mecha - 1.5 ATR
    assert abs(p.dist_sl - (80_000 - 79_100) / 80_000) < 1e-12
    assert p.qty == 0.002 and abs(p.notional - 160.0) < 1e-9               # 200/80000 = 0.0025 -> lote 0.002
    assert abs(p.dist_tp - 3 * p.dist_sl) < 1e-12
    assert p.tp_price > 80_000 and p.sl_price < 80_000
    assert p.loss_sl_usdt <= cfg.RIESGO_MAXIMO_PERMITIDO_USDT
    assert abs(p.loss_sl_usdt - (160 * p.dist_sl + 160 * 0.0005 * 2)) < 1e-9


def test_short_plan_is_mirrored(cfg, rules):
    p = risk.build_plan(cfg, sig("SHORT", high=80_300.0, low=79_700.0), 80_000.0, rules)
    assert p.approved
    assert abs(p.sl_price - (80_300 + 600)) < 1e-6
    assert p.sl_price > 80_000 and p.tp_price < 80_000
    assert abs(p.liq_price - 80_000 * (1 + 0.095)) < 1e-6


def test_blocked_when_sl_beyond_liquidation(cfg, rules):
    p = risk.build_plan(cfg, sig(low=70_000.0, atr=500.0), 80_000.0, rules)   # SL a >13%
    assert not p.approved and "liquidación" in p.blocked_reason


def test_margin_reduced_when_risk_too_high(cfg, rules):
    # precio 30k: nominal objetivo 200 -> 0.006 BTC ($180). SL a ~2.2% -> pérdida ~$4.1 > $3 -> reducir a 0.004 BTC
    s = sig(price=30_000.0, low=29_700.0, high=30_100.0, atr=130.0)       # SL = 29_700 - 195 = 29_505
    p = risk.build_plan(cfg, s, 30_000.0, rules)
    assert p.approved and p.reduced
    assert p.qty < 0.006 and p.loss_sl_usdt <= cfg.RIESGO_MAXIMO_PERMITIDO_USDT
    assert p.qty * 30_000 >= 100


def test_blocked_when_min_lot_exceeds_max_risk(cfg, rules):
    # a 80k el lote mínimo operable es 0.002 ($160): con SL a 2.2% la pérdida mínima supera $3 -> bloqueada
    s = sig(low=79_000.0, high=80_200.0, atr=500.0)                       # SL = 79_000 - 750 -> 2.19%
    p = risk.build_plan(cfg, s, 80_000.0, rules)
    assert not p.approved and "riesgo" in p.blocked_reason.lower()


def test_blocked_when_balance_insufficient(cfg, rules):
    p = risk.build_plan(cfg, sig(), 80_000.0, rules, available=5.0)
    assert not p.approved and "saldo" in p.blocked_reason.lower()


def test_blocked_below_min_notional(cfg, rules):
    cfg.MARGEN_EN_USO_POR_OPERACION = 9.0          # nominal 90 < 100
    p = risk.build_plan(cfg, sig(), 80_000.0, rules)
    assert not p.approved


def test_invalid_inputs_blocked(cfg, rules):
    assert not risk.build_plan(cfg, sig(atr=0.0), 80_000.0, rules).approved
    assert not risk.build_plan(cfg, sig(), 0.0, rules).approved


def test_board_contains_spec_message(cfg, rules):
    text = risk.format_board(cfg, 80_000.0, rules)
    assert "📌 Margen en uso: $20.00 USDT | Apalancamiento: 10x | Posición Total: $200.00 USDT" in text
    assert "9.50%" in text and "$80.00 USDT están a salvo" in text
    plan = risk.build_plan(cfg, sig(), 80_000.0, rules)
    assert "Stop Loss" in risk.format_board(cfg, 80_000.0, rules, plan)


def test_settings_validation(cfg):
    assert cfg.validate() == []
    cfg.APALANCAMIENTO = 40
    cfg.TASA_MANTENIMIENTO_BTC = 0.03               # 1/40 - 3% - 0.1% < 0 -> la liquidación llegaría antes que cualquier SL
    assert any("liquidación" in e for e in cfg.validate())
    cfg.APALANCAMIENTO = 10
    cfg.TASA_MANTENIMIENTO_BTC = 0.004
    cfg.MARGEN_EN_USO_POR_OPERACION = 5
    assert any("mínimo" in e for e in cfg.validate())
    cfg.MARGEN_EN_USO_POR_OPERACION = 200
    assert any("capital" in e.lower() for e in cfg.validate())


def test_settings_apply_coerces_types(cfg):
    errs = cfg.apply({"APALANCAMIENTO": "5", "ATR_MULT_SL": "1,8", "ACTIVAR_BREAK_EVEN": "false", "NOPE": 1})
    assert errs == [] and cfg.APALANCAMIENTO == 5 and cfg.ATR_MULT_SL == 1.8 and cfg.ACTIVAR_BREAK_EVEN is False
    assert cfg.apply({"APALANCAMIENTO": "abc"})
    assert cfg.apply({"APALANCAMIENTO": "2.5"})
