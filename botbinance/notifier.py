"""Telegram: avisos de operaciones y comandos remotos.

Seguridad: el bot solo habla con el chat vinculado. Para vincular, el dashboard muestra un código de
6 dígitos (válido 10 min) que debes enviar al bot como `/start 123456`; así nadie que encuentre el
nombre de tu bot puede quedarse con el control.
"""
from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from typing import Callable

import requests

from . import secrets_store

log = logging.getLogger("botbinance.telegram")

API = "https://api.telegram.org"
HELP = (
    "Comandos:\n"
    "/estado – fases de la estrategia y estado del bot\n"
    "/posicion – operación abierta\n"
    "/balance – saldo de la cuenta\n"
    "/pausar – no abrir nuevas operaciones\n"
    "/reanudar – volver a operar\n"
    "/cerrar – cierra la posición a mercado (pide confirmación)\n"
    "/ayuda – esta lista"
)


class Telegram:
    def __init__(self, cfg, state, config_store, command_handler: Callable[[str, list[str]], str]):
        self.cfg = cfg
        self.state = state
        self.store = config_store
        self.handler = command_handler
        self.token = ""
        self._q: queue.Queue = queue.Queue(maxsize=300)
        self._stop = threading.Event()
        self._offset = 0
        self._bad_attempts: list[float] = []
        self._threads: list[threading.Thread] = []
        self.reload_token()

    # ------------------------------------------------------------------ configuración
    def reload_token(self) -> None:
        self.token = secrets_store.get(secrets_store.TELEGRAM_TOKEN)
        self.state.telegram["token"] = bool(self.token)
        self.state.telegram["linked"] = bool(self.cfg.TELEGRAM_CHAT_ID)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.TELEGRAM_ACTIVO and self.token)

    def new_pairing_code(self) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.state.telegram["pairing_code"] = code
        self.state.telegram["pairing_expires"] = time.time() + 600
        self._bad_attempts.clear()
        return code

    def unlink(self) -> None:
        self.cfg.TELEGRAM_CHAT_ID = ""
        self.store.save(self.cfg)
        self.state.telegram["linked"] = False

    # ------------------------------------------------------------------ API
    def _api(self, method: str, payload: dict | None = None, timeout: float = 15.0):
        if not self.token:
            raise RuntimeError("sin token")
        try:
            r = requests.post(f"{API}/bot{self.token}/{method}", json=payload or {}, timeout=timeout)
        except requests.RequestException as exc:
            # No se incluye el texto de la excepción: contiene la URL con el token.
            raise ConnectionError(type(exc).__name__) from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {r.status_code}: {data.get('description', 'error')}")
        return data["result"]

    def test_token(self) -> dict:
        try:
            me = self._api("getMe")
            return {"ok": True, "username": me.get("username", "")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------ envío
    def send(self, text: str) -> None:
        """Encola un aviso (no bloquea al motor). Si Telegram no está listo, solo queda en el log."""
        if not (self.enabled and self.cfg.TELEGRAM_CHAT_ID):
            return
        try:
            self._q.put_nowait(text[:4000])
        except queue.Full:
            log.warning("Cola de Telegram llena; descarto un aviso")

    def _send_to(self, chat_id: str, text: str) -> None:
        self._api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})

    def _sender(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._q.get(timeout=1)
            except queue.Empty:
                continue
            for attempt in range(3):
                try:
                    self._send_to(self.cfg.TELEGRAM_CHAT_ID, text)
                    self.state.telegram["ok"] = True
                    break
                except Exception as exc:
                    self.state.telegram["ok"] = False
                    log.warning("No se pudo enviar a Telegram (%d/3): %s", attempt + 1, exc)
                    time.sleep(2 * (attempt + 1))

    # ------------------------------------------------------------------ recepción
    def _poller(self) -> None:
        while not self._stop.is_set():
            if not self.enabled:
                self._stop.wait(5)
                continue
            try:
                r = requests.get(f"{API}/bot{self.token}/getUpdates",
                                 params={"timeout": 25, "offset": self._offset, "allowed_updates": '["message"]'},
                                 timeout=35)
                data = r.json()
                if not data.get("ok"):
                    self.state.telegram["ok"] = False
                    log.warning("Telegram getUpdates: %s", data.get("description"))
                    self._stop.wait(30)
                    continue
                self.state.telegram["ok"] = True
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    try:
                        self._handle(upd)
                    except Exception:  # pragma: no cover
                        log.exception("Error procesando un mensaje de Telegram")
            except (requests.RequestException, ValueError):
                self.state.telegram["ok"] = False
                self._stop.wait(10)

    def _handle(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text or not chat_id:
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        linked = self.cfg.TELEGRAM_CHAT_ID

        if linked and chat_id != linked:
            log.warning("Mensaje de Telegram ignorado de un chat no vinculado")
            return

        if not linked:
            if cmd == "/start":
                self._try_pair(chat_id, args)
            return

        if cmd == "/start":
            self._send_to(chat_id, "✅ Bot de trading activo.\n" + HELP)
        elif cmd in ("/ayuda", "/help"):
            self._send_to(chat_id, HELP)
        else:
            reply = self.handler(cmd, args)
            if reply:
                self._send_to(chat_id, reply)

    def _try_pair(self, chat_id: str, args: list[str]) -> None:
        now = time.time()
        self._bad_attempts = [t for t in self._bad_attempts if now - t < 600]
        code = self.state.telegram.get("pairing_code", "")
        if len(self._bad_attempts) >= 5:
            return
        if not code or now > self.state.telegram.get("pairing_expires", 0):
            self._send_to(chat_id, "Para vincular este chat, genera un código en el dashboard "
                                   "(Configuración → Telegram) y envía: /start CÓDIGO")
            return
        if not args or not secrets.compare_digest(args[0], code):
            self._bad_attempts.append(now)
            self._send_to(chat_id, "Código incorrecto.")
            return
        self.cfg.TELEGRAM_CHAT_ID = chat_id
        self.store.save(self.cfg)
        self.state.telegram.update(linked=True, pairing_code="", pairing_expires=0.0)
        log.info("Telegram vinculado correctamente")
        self._send_to(chat_id, "✅ Chat vinculado. Recibirás aquí los avisos de operaciones.\n" + HELP)

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        self._stop.clear()
        for target, name in ((self._sender, "tg-sender"), (self._poller, "tg-poller")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
