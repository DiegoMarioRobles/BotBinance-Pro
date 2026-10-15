from types import SimpleNamespace


from botbinance import paths, secrets_store
from botbinance.binance_api import SymbolRules
from botbinance.config import ConfigStore
from botbinance.engine import Engine
from botbinance.state import BotState
from botbinance.strategy import Evaluation, Signal
from botbinance.util import read_json
from fake_exchange import FakeFutures

CLOSE = 1_700_000_000_000


def make_engine(cfg, age_s=1.0):
    state = BotState()
    eng = Engine(cfg, ConfigStore(paths.CONFIG_FILE), state)
    eng.tm.client = eng.trade_client = FakeFutures(80_000.0)
    eng.rules = eng.tm.rules = SymbolRules()
    eng.feed = SimpleNamespace(last_price=80_000.0, snapshot=lambda: {})
    eng.data_client = SimpleNamespace(now_ms=lambda: CLOSE + int(age_s * 1000))
    state.trading_ready, state.paused = True, False
    state.balance = {"wallet": 100.0, "available": 100.0}
    notes = []
    eng.notifier.send = notes.append
    return eng, state, notes


def ev_with(side="LONG", open_time=CLOSE - 3_600_000, low=79_700.0, high=80_300.0, atr=400.0):
    sig = Signal(side=side, pattern="Pin Bar", exhaustion="BB", open_time=open_time, close_time=CLOSE, open=80_000.0,
                 high=high, low=low, close=80_000.0, atr=atr)
    return Evaluation(close_time=CLOSE, trend=side, signal=sig, price=80_000.0)


def test_signal_opens_trade_and_notifies(cfg):
    eng, state, notes = make_engine(cfg)
    eng._handle_signal(ev_with())
    assert eng.tm.trade is not None and eng.tm.client.pos is not None
    assert len(eng.tm.client.orders) == 2
    assert any("ABIERTA" in n for n in notes) and "TABLERO" in state.board and state.last_plan["approved"]


def test_paused_bot_does_not_trade_but_reports(cfg):
    eng, state, notes = make_engine(cfg)
    state.paused = True
    eng._handle_signal(ev_with())
    assert eng.tm.trade is None and eng.tm.client.pos is None
    assert any("pausa" in n for n in notes)


def test_not_ready_bot_does_not_trade(cfg):
    eng, state, notes = make_engine(cfg)
    state.trading_ready, state.trading_note = False, "Faltan claves"
    eng._handle_signal(ev_with())
    assert eng.tm.client.pos is None and any("Faltan claves" in n for n in notes)


def test_duplicate_signal_is_ignored_and_persisted(cfg):
    eng, state, notes = make_engine(cfg)
    ev = ev_with()
    eng._handle_signal(ev)
    n_before = len(eng.tm.client.calls)
    eng._handle_signal(ev)                                   # misma vela: nada nuevo
    assert len(eng.tm.client.calls) == n_before
    assert read_json(paths.RUNTIME_FILE)["last_signal_time"] == ev.signal.open_time


def test_stale_signal_is_discarded(cfg):
    eng, _, notes = make_engine(cfg, age_s=cfg.MAX_RETRASO_SENAL_SEG + 60)
    eng._handle_signal(ev_with())
    assert eng.tm.client.pos is None and notes == []


def test_risk_block_prevents_order_and_informs(cfg):
    eng, state, notes = make_engine(cfg)
    eng._handle_signal(ev_with(low=70_000.0, atr=500.0))     # SL más lejos que la liquidación
    assert eng.tm.client.pos is None and state.last_plan["approved"] is False
    assert any("BLOQUEADA" in n or "liquidación" in n for n in notes)
    cfg.TELEGRAM_AVISAR_SENALES_BLOQUEADAS = False
    eng2, _, notes2 = make_engine(cfg)
    eng2._handle_signal(ev_with(low=70_000.0, atr=500.0))
    assert notes2 == []


def test_open_position_blocks_new_signals(cfg):
    eng, _, notes = make_engine(cfg)
    eng._handle_signal(ev_with(open_time=CLOSE - 7_200_000))
    calls = len(eng.tm.client.calls)
    eng._handle_signal(ev_with(open_time=CLOSE - 3_600_000))
    assert len(eng.tm.client.calls) == calls and any("ya hay una posición" in n for n in notes)


def test_apply_config_validation_and_real_mode_confirmation(cfg):
    eng, state, _ = make_engine(cfg)
    errs, _ = eng.apply_config({"MARGEN_EN_USO_POR_OPERACION": 500})
    assert errs and cfg.MARGEN_EN_USO_POR_OPERACION == 20.0                 # no se aplicó
    errs, _ = eng.apply_config({"SANDBOX_MODE": False})
    assert errs and "OPERAR REAL" in errs[0] and cfg.SANDBOX_MODE is True
    errs, _ = eng.apply_config({"SANDBOX_MODE": False}, confirm_real=True)
    assert not errs and cfg.SANDBOX_MODE is False and state.paused          # al cambiar a REAL queda en pausa
    assert read_json(paths.CONFIG_FILE)["SANDBOX_MODE"] is False
    errs, _ = eng.apply_config({"RATIO_RIESGO_BENEFICIO": "2.5"})
    assert not errs and cfg.RATIO_RIESGO_BENEFICIO == 2.5


def test_cannot_change_mode_or_keys_with_open_trade(cfg):
    eng, state, _ = make_engine(cfg)
    eng._handle_signal(ev_with())
    assert eng.tm.trade
    errs, _ = eng.apply_config({"SANDBOX_MODE": False}, confirm_real=True)
    assert errs and cfg.SANDBOX_MODE is True
    errs = eng.save_credentials({"testnet_api_key": "K" * 40})
    assert errs and not secrets_store.has(secrets_store.TESTNET_KEY)


def test_save_credentials_validates_and_stores(cfg, monkeypatch):
    eng, _, _ = make_engine(cfg)
    monkeypatch.setattr(eng, "rebuild_trade_client", lambda: None)
    assert eng.save_credentials({"testnet_api_key": "corta"})
    assert not eng.save_credentials({"testnet_api_key": "A" * 64, "testnet_api_secret": "B" * 64, "telegram_token": "123456789:" + "C" * 35})
    assert secrets_store.get(secrets_store.TESTNET_KEY) == "A" * 64
    assert secrets_store.get(secrets_store.TELEGRAM_TOKEN).startswith("123456789:")
    assert not eng.save_credentials({"clear": ["testnet_api_key"]})
    assert not secrets_store.has(secrets_store.TESTNET_KEY)
    # los campos vacíos no borran nada
    eng.save_credentials({"testnet_api_secret": ""})
    assert secrets_store.has(secrets_store.TESTNET_SECRET)


def test_credentials_are_kept_separate_per_environment(cfg):
    secrets_store.set(secrets_store.TESTNET_KEY, "T" * 40)
    secrets_store.set(secrets_store.REAL_KEY, "R" * 40)
    assert secrets_store.credentials_for(True)[0] == "T" * 40
    assert secrets_store.credentials_for(False)[0] == "R" * 40
    assert secrets_store.mask("R" * 40) == "•" * 8 + "RRRR"


def test_test_connection_without_keys(cfg):
    eng, _, _ = make_engine(cfg)
    res = eng.test_connection("testnet")
    assert res["ok"] is False and "No hay claves" in res["error"]


def test_pause_resume_persist_and_telegram_commands(cfg):
    eng, state, notes = make_engine(cfg)
    assert "PAUSA" in eng.handle_command("/estado", []) or "operando" in eng.handle_command("/estado", [])
    eng.handle_command("/pausar", [])
    assert state.paused and read_json(paths.RUNTIME_FILE)["paused"] is True
    eng.handle_command("/reanudar", [])
    assert not state.paused
    assert eng.handle_command("/posicion", []) == "Sin operación abierta."
    assert "Saldo" in eng.handle_command("/balance", [])
    assert "desconocido" in eng.handle_command("/foo", []).lower() or "no reconocido" in eng.handle_command("/foo", []).lower()


def test_sync_frames_waits_for_simultaneous_higher_timeframe_close(cfg):
    """A las 16:00 UTC cierran a la vez 1h y 4h: el análisis debe esperar a tener la vela de 4h."""
    import pandas as pd
    from botbinance.util import last_closed_open_time
    eng, _, _ = make_engine(cfg)
    now = 1_791_993_600_500                                     # justo después de un cierre múltiple
    tfs = ["1h", "4h", "1d", "1w"]
    exp = {tf: last_closed_open_time(tf, now) for tf in tfs}
    raw = {tf: pd.DataFrame({"open_time": [exp[tf]]}) for tf in tfs}
    raw["4h"] = pd.DataFrame({"open_time": [exp["4h"] - 14_400_000]})        # la vela de 4h aún no llegó por WebSocket
    refreshed = []

    def refresh(tf):
        refreshed.append(tf)
        raw[tf] = pd.DataFrame({"open_time": [exp[tf]]})

    eng.feed = SimpleNamespace(raw=raw, timeframes=tfs, refresh_tf=refresh, last_price=1.0, snapshot=lambda: {})
    eng.data_client = SimpleNamespace(now_ms=lambda: now)
    assert eng._sync_frames(timeout=5) is True and refreshed == ["4h"]
    # si la vela no aparece, no se queda bloqueado: devuelve False al agotar el tiempo
    raw["4h"] = pd.DataFrame({"open_time": [exp["4h"] - 14_400_000]})
    eng.feed.refresh_tf = lambda tf: None
    assert eng._sync_frames(timeout=0.5) is False


def test_telegram_close_requires_confirmation(cfg):
    eng, _, _ = make_engine(cfg)
    eng._handle_signal(ev_with())
    assert eng.tm.client.pos is not None
    assert "/cerrar SI" in eng.handle_command("/cerrar", [])
    assert eng.tm.client.pos is not None                        # aún abierta
    assert "cerrada" in eng.handle_command("/cerrar", ["SI"]).lower()
    assert eng.tm.client.pos is None


def test_identical_testnet_and_real_keys_are_rejected(cfg, monkeypatch):
    eng, _, _ = make_engine(cfg)
    monkeypatch.setattr(eng, "rebuild_trade_client", lambda: None)
    same = {"testnet_api_key": "K" * 64, "real_api_key": "K" * 64}
    assert "idénticas" in eng.save_credentials(same)[0] and not secrets_store.has(secrets_store.TESTNET_KEY)
    assert not eng.save_credentials({"testnet_api_key": "K" * 64})
    assert eng.save_credentials({"real_api_key": "K" * 64})            # coincide con la de Testnet ya guardada
    assert not eng.save_credentials({"real_api_key": "R" * 64})
