"""Estado compartido entre el motor, el dashboard y Telegram (thread-safe)."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque


class BotState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.logs: deque = deque(maxlen=800)
        self._log_seq = 0
        self.started_at = time.time()

        self.mode = "TESTNET"
        self.paused = False
        self.pause_reason = ""
        self.trading_ready = False
        self.trading_note = "Iniciando…"
        self.fatal = ""

        self.price: float | None = None
        self.price_ts = 0.0
        self.evaluation: dict | None = None
        self.board = ""
        self.last_plan: dict | None = None
        self.position: dict | None = None
        self.trade: dict | None = None
        self.foreign_position: dict | None = None
        self.balance: dict | None = None
        self.history: list[dict] = []

        self.feed = {"ws": False, "last_msg": 0.0, "variant": ""}
        self.api = {"ok": None, "error": "", "checked": 0.0}
        self.telegram = {"token": False, "linked": False, "ok": None, "pairing_code": "", "pairing_expires": 0.0}
        self.public_ip = ""
        self.alerts: dict[str, dict] = {}
        self.backtest: dict = {"running": False, "progress": 0.0, "message": "", "result": None, "error": ""}

    # ------------------------------------------------------------------
    def add_log(self, level: str, msg: str, created: float | None = None) -> None:
        with self.lock:
            self._log_seq += 1
            self.logs.append({"id": self._log_seq, "t": created or time.time(), "level": level, "msg": msg})

    def set_alert(self, key: str, text: str | None, level: str = "warn") -> None:
        with self.lock:
            if text:
                self.alerts[key] = {"text": text, "level": level}
            else:
                self.alerts.pop(key, None)

    def recent_logs(self, since_id: int = 0, limit: int = 120) -> list[dict]:
        with self.lock:
            items = [x for x in self.logs if x["id"] > since_id]
        return items[-limit:]


class StateLogHandler(logging.Handler):
    """Copia los logs al estado para mostrarlos en el dashboard."""

    def __init__(self, state: BotState):
        super().__init__(level=logging.INFO)
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.state.add_log(record.levelname, record.getMessage(), record.created)
        except Exception:  # pragma: no cover
            pass
