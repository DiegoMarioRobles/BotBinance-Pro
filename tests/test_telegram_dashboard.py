import time

import pytest

from botbinance import secrets_store
from botbinance.config import ConfigStore
from botbinance.dashboard.server import create_app
from botbinance.engine import Engine
from botbinance.state import BotState
from botbinance import paths


# ---------------------------------------------------------------- Telegram
def make_tg(cfg, handler=None):
    state = BotState()
    eng = Engine(cfg, ConfigStore(paths.CONFIG_FILE), state)
    tg = eng.notifier
    sent = []
    tg._send_to = lambda chat, text: sent.append((chat, text))
    if handler:
        tg.handler = handler
    return tg, state, sent


def upd(text, chat=111):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": chat}}}


def test_pairing_with_correct_code(cfg):
    tg, state, sent = make_tg(cfg)
    code = tg.new_pairing_code()
    tg._handle(upd(f"/start {code}"))
    assert cfg.TELEGRAM_CHAT_ID == "111" and state.telegram["linked"] and state.telegram["pairing_code"] == ""
    assert sent and "vinculado" in sent[-1][1].lower()


def test_pairing_rejects_wrong_code_and_locks_after_5(cfg):
    tg, state, sent = make_tg(cfg)
    code = tg.new_pairing_code()
    for _ in range(5):
        tg._handle(upd("/start 000000" if code != "000000" else "/start 111111"))
    assert cfg.TELEGRAM_CHAT_ID == ""
    sent.clear()
    tg._handle(upd(f"/start {code}"))                      # aunque acierte, ya está bloqueado
    assert cfg.TELEGRAM_CHAT_ID == "" and sent == []


def test_pairing_requires_a_generated_code(cfg):
    tg, _, sent = make_tg(cfg)
    tg._handle(upd("/start 123456"))
    assert cfg.TELEGRAM_CHAT_ID == "" and "dashboard" in sent[-1][1]


def test_expired_code_is_rejected(cfg):
    tg, state, sent = make_tg(cfg)
    code = tg.new_pairing_code()
    state.telegram["pairing_expires"] = time.time() - 1
    tg._handle(upd(f"/start {code}"))
    assert cfg.TELEGRAM_CHAT_ID == ""


def test_unlinked_chat_cannot_run_commands(cfg):
    tg, _, sent = make_tg(cfg, handler=lambda c, a: "SECRETO")
    tg._handle(upd("/estado"))
    assert sent == []


def test_other_chats_are_ignored_once_linked(cfg):
    tg, _, sent = make_tg(cfg, handler=lambda c, a: "OK")
    cfg.TELEGRAM_CHAT_ID = "111"
    tg._handle(upd("/cerrar", chat=999))
    assert sent == []
    tg._handle(upd("/estado", chat=111))
    assert sent == [("111", "OK")]
    tg._handle(upd("/estado@MiBot", chat=111))             # los comandos en grupos llevan @nombre
    assert len(sent) == 2


def test_send_is_noop_when_not_ready(cfg):
    tg, _, _ = make_tg(cfg)
    tg.send("hola")                                         # sin token ni chat: no encola, no falla
    assert tg._q.qsize() == 0


# ---------------------------------------------------------------- Dashboard
@pytest.fixture
def app_env(cfg):
    state = BotState()
    eng = Engine(cfg, ConfigStore(paths.CONFIG_FILE), state)
    app = create_app(eng, state, cfg, eng.store)
    client = app.test_client()
    page = client.get("/", headers={"Host": "127.0.0.1:5000"}).get_data(as_text=True)
    token = page.split('name="token" content="')[1].split('"')[0]
    return client, token, eng, state


H = {"Host": "127.0.0.1:5000"}


def test_index_served_with_token_and_security_headers(app_env):
    client, token, _, _ = app_env
    r = client.get("/", headers=H)
    assert r.status_code == 200 and len(token) > 20
    assert r.headers["X-Frame-Options"] == "DENY" and "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert "inline" not in r.get_data(as_text=True).split("<script")[1][:80]       # sin scripts inline


def test_foreign_host_header_is_rejected(app_env):
    client, _, _, _ = app_env
    assert client.get("/api/state", headers={"Host": "evil.example.com"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "192.168.1.5:5000"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "localhost:5000"}).status_code == 200


def test_post_requires_token_and_local_origin(app_env):
    client, token, eng, state = app_env
    assert client.post("/api/pause", headers=H, json={}).status_code == 403
    assert client.post("/api/pause", headers={**H, "X-Token": "malo"}, json={}).status_code == 403
    assert client.post("/api/pause", headers={**H, "X-Token": token, "Origin": "https://evil.example.com"}, json={}).status_code == 403
    assert not state.paused
    r = client.post("/api/pause", headers={**H, "X-Token": token, "Origin": "http://127.0.0.1:5000"}, json={})
    assert r.status_code == 200 and state.paused


def test_config_endpoint_never_returns_secrets(app_env, isolate_files):
    client, token, eng, _ = app_env
    secret = "SUPERSECRETVALUE" + "x" * 30
    isolate_files[secrets_store.TESTNET_SECRET] = secret
    isolate_files[secrets_store.TESTNET_KEY] = "APIKEY" + "y" * 40
    isolate_files[secrets_store.TELEGRAM_TOKEN] = "123456789:" + "z" * 35
    body = client.get("/api/config", headers=H).get_data(as_text=True)
    assert secret not in body and "z" * 35 not in body and "APIKEY" + "y" * 40 not in body
    assert body.count("••••") >= 1                          # solo se muestran los últimos 4 de la API key
    st = client.get("/api/state", headers=H).get_data(as_text=True)
    assert secret not in st


def test_credentials_endpoint_validates(app_env, monkeypatch):
    client, token, eng, _ = app_env
    monkeypatch.setattr(eng, "rebuild_trade_client", lambda: None)
    hdr = {**H, "X-Token": token}
    assert client.post("/api/credentials", headers=hdr, json={"testnet_api_key": "x"}).status_code == 400
    r = client.post("/api/credentials", headers=hdr, json={"testnet_api_key": "k" * 64, "testnet_api_secret": "s" * 64})
    assert r.status_code == 200 and secrets_store.has(secrets_store.TESTNET_SECRET)


def test_config_endpoint_validation_and_real_confirmation(app_env):
    client, token, eng, _ = app_env
    hdr = {**H, "X-Token": token}
    r = client.post("/api/config", headers=hdr, json={"values": {"APALANCAMIENTO": 500}})
    assert r.status_code == 400 and not r.get_json()["ok"]
    r = client.post("/api/config", headers=hdr, json={"values": {"SANDBOX_MODE": False}})
    assert r.status_code == 400 and "OPERAR REAL" in r.get_json()["errors"][0]
    r = client.post("/api/config", headers=hdr, json={"values": {"SANDBOX_MODE": False}, "confirm_real": "operar real"})
    assert r.status_code == 200 and eng.cfg.SANDBOX_MODE is False


def test_state_and_candles_without_feed(app_env):
    client, _, _, _ = app_env
    st = client.get("/api/state", headers=H).get_json()
    assert st["mode"] == "TESTNET" and st["booted"] is False and st["logs"] == []
    assert client.get("/api/candles?tf=1h", headers=H).get_json()["series"] is None


def test_telegram_pair_endpoint_requires_token(app_env):
    client, token, _, _ = app_env
    r = client.post("/api/telegram/pair", headers={**H, "X-Token": token}, json={})
    assert r.status_code == 400 and "token" in r.get_json()["error"].lower()


def test_resume_refused_with_fatal_config_error(app_env):
    client, token, _, state = app_env
    state.fatal = "Configuración inválida: x"
    r = client.post("/api/resume", headers={**H, "X-Token": token}, json={})
    assert r.status_code == 400
