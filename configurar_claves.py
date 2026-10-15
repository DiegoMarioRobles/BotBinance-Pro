"""Alternativa por consola al panel ⚙ Configuración: guarda las claves en el Administrador de credenciales de Windows.

    python configurar_claves.py

Las claves se escriben sin mostrarse en pantalla. Deja un campo vacío para no cambiar ese valor.
"""
from __future__ import annotations

import getpass

from botbinance import secrets_store as sec

CAMPOS = [
    (sec.TESTNET_KEY, "API Key de TESTNET"),
    (sec.TESTNET_SECRET, "API Secret de TESTNET"),
    (sec.REAL_KEY, "API Key de la cuenta REAL"),
    (sec.REAL_SECRET, "API Secret de la cuenta REAL"),
    (sec.TELEGRAM_TOKEN, "Token del bot de Telegram"),
]


def main() -> None:
    print(f"Las claves se guardan en: {sec.backend_name()}\n(Enter = dejar sin cambios · escribe BORRAR para eliminar la actual)\n")
    for name, label in CAMPOS:
        estado = f"guardada ({sec.mask(sec.get(name))})" if sec.has(name) and "key" in name else ("guardada" if sec.has(name) else "vacía")
        valor = getpass.getpass(f"{label} [{estado}]: ").strip()
        if not valor:
            continue
        if valor.upper() == "BORRAR":
            sec.set(name, "")
            print("   → eliminada")
        elif len(valor) < 20 or " " in valor:
            print("   ✘ no parece válida (¿completa y sin espacios?). No se guardó.")
        else:
            sec.set(name, valor)
            print("   ✔ guardada")
    print("\nListo. Reinicia el bot o pulsa «Probar conexión» en el dashboard.")


if __name__ == "__main__":
    main()
