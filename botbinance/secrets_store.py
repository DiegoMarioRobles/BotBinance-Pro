"""Almacenamiento de claves.

Las API keys y el token de Telegram se guardan en el Administrador de credenciales de Windows
(librería `keyring`): cifradas y ligadas a tu usuario de Windows. No se escriben en el proyecto.
Si `keyring` no estuviera disponible se usa un archivo local de respaldo (con aviso).
"""
from __future__ import annotations

import logging
import threading

from . import paths
from .util import read_json, write_json

log = logging.getLogger("botbinance.secrets")

SERVICE = "BotBinanceBTC"

# Nombres de claves permitidas
TESTNET_KEY = "testnet_api_key"
TESTNET_SECRET = "testnet_api_secret"
REAL_KEY = "real_api_key"
REAL_SECRET = "real_api_secret"
TELEGRAM_TOKEN = "telegram_token"
ALLOWED = {TESTNET_KEY, TESTNET_SECRET, REAL_KEY, REAL_SECRET, TELEGRAM_TOKEN}

_lock = threading.Lock()
_keyring = None
_backend_name = "archivo local (sin cifrar)"

try:  # pragma: no cover - depende del sistema
    import keyring as _kr

    _backend = _kr.get_keyring()
    name = type(_backend).__name__
    if "fail" in name.lower() or "null" in name.lower():
        raise RuntimeError(f"backend keyring no usable: {name}")
    _keyring = _kr
    _backend_name = "Administrador de credenciales de Windows" if "WinVault" in name else name
except Exception as exc:  # pragma: no cover
    log.warning("keyring no disponible (%s). Se usará un archivo local de respaldo.", exc)
    _keyring = None


def backend_name() -> str:
    return _backend_name


def _check(name: str) -> None:
    if name not in ALLOWED:
        raise KeyError(f"clave desconocida: {name}")


def get(name: str) -> str:
    _check(name)
    with _lock:
        if _keyring is not None:
            try:
                return _keyring.get_password(SERVICE, name) or ""
            except Exception as exc:  # pragma: no cover
                log.error("No se pudo leer %s de keyring: %s", name, exc)
        return (read_json(paths.SECRETS_FALLBACK_FILE, {}) or {}).get(name, "")


def set(name: str, value: str) -> None:  # noqa: A001 - nombre claro para el uso
    _check(name)
    value = (value or "").strip()
    with _lock:
        if _keyring is not None:
            try:
                if value:
                    _keyring.set_password(SERVICE, name, value)
                else:
                    try:
                        _keyring.delete_password(SERVICE, name)
                    except Exception:
                        pass
                return
            except Exception as exc:  # pragma: no cover
                log.error("No se pudo escribir %s en keyring (%s). Uso archivo local.", name, exc)
        data = read_json(paths.SECRETS_FALLBACK_FILE, {}) or {}
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        write_json(paths.SECRETS_FALLBACK_FILE, data)


def mask(value: str) -> str:
    """Muestra solo los últimos 4 caracteres."""
    if not value:
        return ""
    return "•" * 8 + value[-4:]


def has(name: str) -> bool:
    return bool(get(name))


def credentials_for(sandbox: bool) -> tuple[str, str]:
    """(api_key, api_secret) del entorno activo. Las de testnet y real se guardan por separado."""
    if sandbox:
        return get(TESTNET_KEY), get(TESTNET_SECRET)
    return get(REAL_KEY), get(REAL_SECRET)
