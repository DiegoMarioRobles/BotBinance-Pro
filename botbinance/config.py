"""Panel de configuración de capital, riesgo y estrategia.

Los nombres en MAYÚSCULAS coinciden con las variables del documento de especificación.
Los valores se guardan en data/config.json (nunca contiene claves: esas van en secrets_store).
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, fields

from . import paths
from .util import PULLBACK_TFS, TF_MS, TREND_TFS, TRIGGER_TFS, read_json, write_json

URL_TESTNET = "https://testnet.binancefuture.com"
URL_DEMO = "https://demo-fapi.binance.com"
URL_REAL = "https://fapi.binance.com"


@dataclass
class Settings:
    # ---------------- Estrategia activa y modelo de ejecución ----------------
    ESTRATEGIA: str = "estrategia_1"             # estrategia_1 ... estrategia_5

    # ---------------- Capital y riesgo (variables de usuario) ----------------
    SANDBOX_MODE: bool = True                    # True = pruebas (testnet), False = cuenta real
    ENTORNO_PRUEBAS_URL: str = URL_TESTNET       # testnet clásico o demo-fapi
    SYMBOL: str = "BTCUSDT"
    CAPITAL_TOTAL_CUENTA: float = 100.0          # USDT
    MARGEN_EN_USO_POR_OPERACION: float = 20.0    # USDT bloqueados como margen aislado
    APALANCAMIENTO: int = 10
    RIESGO_MAXIMO_PERMITIDO_USDT: float = 3.0    # pérdida máxima real si salta el SL
    RATIO_RIESGO_BENEFICIO: float = 3.0          # TP = 3 x distancia del SL
    ACTIVAR_BREAK_EVEN: bool = True
    BREAK_EVEN_BUFFER_PCT: float = 0.001         # SL a entrada +0.1% (cubre comisiones)
    TASA_COMISION_TAKER: float = 0.0005          # 0.05% por orden a mercado
    TASA_MANTENIMIENTO_BTC: float = 0.004        # 0.4%
    ATR_MULT_SL: float = 1.5                     # SL = extremo de la vela gatillo -/+ 1.5 x ATR
    NOTIONAL_MINIMO_USDT: float = 100.0          # mínimo de Binance para la posición nominal
    LOTE_MINIMO_BTC: float = 0.001

    # ---------------- Temporalidades (cada función con la suya) ----------------
    TF_TENDENCIA: str = "1d"                     # ¿alcista o bajista?: 4h 8h 12h 16h 20h 1d 2d 3d 1w 1M
    TF_TENDENCIA_SUPERIOR: str = "1w"            # filtro opcional de una temporalidad mayor ("" = ninguno)
    TF_ZONA: str = "4h"                          # retrocesos: zona de valor EMA20/EMA50 (5m ... 1d)
    TF_FIBONACCI: str = "1d"                     # velas usadas para detectar el último impulso (Fibonacci / S-R)
    TF_GATILLO: str = "1h"                       # gatillo de entrada: patrón de velas + agotamiento (1m ... 4h)

    # ---------------- Fase 1: tendencia macro ----------------
    ADX_TENDENCIA_MIN: float = 25.0              # ADX(1d) > 25 habilita operar
    ADX_LATERAL_MAX: float = 22.0                # ADX(1d) < 22 = STANDBY

    # ---------------- Fase 2: retroceso a zona de valor ----------------
    ZONA_TOLERANCIA_ATR: float = 0.5             # distancia máx. a la zona EMA20-EMA50
    FIB_MIN: float = 0.5
    FIB_MAX: float = 0.618
    FIB_ZIGZAG_ATR: float = 2.0                  # reversión mínima (x ATR diario) para confirmar un pivote
    FIB_IMPULSO_MIN_ATR: float = 4.0             # tamaño mínimo del impulso (x ATR diario) para ser "mayor"
    LONG_RSI_DIARIO_MIN: float = 40.0
    LONG_RSI_DIARIO_MAX: float = 55.0
    SHORT_RSI_DIARIO_MIN: float = 45.0
    SHORT_RSI_DIARIO_MAX: float = 60.0

    # ---------------- Fase 3: gatillo ----------------
    BB_PERIODO: int = 20
    BB_DESVIACION: float = 2.0
    RSI_SOBREVENTA: float = 35.0
    RSI_SOBRECOMPRA: float = 65.0
    STOCH_SOBREVENTA: float = 20.0
    STOCH_SOBRECOMPRA: float = 80.0
    VENTANA_AGOTAMIENTO: int = 1                 # 1 = el agotamiento debe darse en la vela gatillo
    PIN_MECHA_CUERPO: float = 2.0
    PIN_MECHA_OPUESTA_MAX: float = 0.25
    ENGULFING_VOL_MULT: float = 1.2
    DIV_LOOKBACK: int = 60
    DIV_PIVOTE_N: int = 3
    DIV_SEPARACION_MIN: int = 5

    # ---------------- Martingala (entradas escalonadas en soportes/resistencias) ----------------
    MARTINGALA_ACTIVA: bool = False
    MARTINGALA_NIVELES: int = 2                  # entradas totales (la 1ª a mercado, el resto órdenes límite)
    MARTINGALA_MULTIPLICADOR: float = 2.0        # cada nivel = anterior x multiplicador
    MARTINGALA_FUENTE: str = "fibonacci"         # "fibonacci" o "soportes" (pivotes de soporte/resistencia)
    MARTINGALA_RIESGO_MAX_USDT: float = 0.0      # 0 = usar RIESGO_MAXIMO_PERMITIDO_USDT; súbelo para permitir más niveles

    # ---------------- Ejecución ----------------
    WORKING_TYPE: str = "MARK_PRICE"             # precio que dispara SL/TP: MARK_PRICE o CONTRACT_PRICE
    MAX_RETRASO_SENAL_SEG: int = 120             # no operar señales "viejas"
    MONITOR_INTERVALO_SEG: float = 2.0           # frecuencia del bucle de break-even

    # ---------------- Servicios ----------------
    DASHBOARD_PORT: int = 5000
    TELEGRAM_ACTIVO: bool = True
    TELEGRAM_CHAT_ID: str = ""                   # se rellena solo al vincular con /start CODIGO
    TELEGRAM_AVISAR_SENALES_BLOQUEADAS: bool = True

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    def trade_base_url(self) -> str:
        return URL_REAL if not self.SANDBOX_MODE else self.ENTORNO_PRUEBAS_URL

    def mode_label(self) -> str:
        return "TESTNET" if self.SANDBOX_MODE else "REAL"

    def apply(self, data: dict) -> list[str]:
        """Aplica un diccionario con conversión de tipos. Devuelve la lista de errores de conversión."""
        errors: list[str] = []
        known = {f.name: f for f in fields(self)}
        for key, value in data.items():
            if key not in known:
                continue
            current = getattr(self, key)
            try:
                setattr(self, key, _coerce(value, current))
            except (ValueError, TypeError):
                errors.append(f"{key}: valor inválido ({value!r})")
        return errors

    def validate(self) -> list[str]:
        """Validación estricta al inicio (lista de errores; vacía si todo está bien)."""
        e: list[str] = []
        if not self.SYMBOL.endswith("USDT"):
            e.append("SYMBOL debe ser un par USDT (p. ej. BTCUSDT).")
        if self.CAPITAL_TOTAL_CUENTA <= 0:
            e.append("CAPITAL_TOTAL_CUENTA debe ser > 0.")
        if self.MARGEN_EN_USO_POR_OPERACION <= 0:
            e.append("MARGEN_EN_USO_POR_OPERACION debe ser > 0.")
        if self.MARGEN_EN_USO_POR_OPERACION > self.CAPITAL_TOTAL_CUENTA:
            e.append("MARGEN_EN_USO_POR_OPERACION no puede superar CAPITAL_TOTAL_CUENTA.")
        if not (1 <= int(self.APALANCAMIENTO) <= 125):
            e.append("APALANCAMIENTO debe estar entre 1 y 125.")
        nominal = self.MARGEN_EN_USO_POR_OPERACION * max(self.APALANCAMIENTO, 1)
        if nominal < self.NOTIONAL_MINIMO_USDT:
            e.append(
                f"La posición nominal (${nominal:,.2f}) es menor al mínimo de Binance "
                f"(${self.NOTIONAL_MINIMO_USDT:,.2f}). Sube el margen o el apalancamiento."
            )
        if self.RIESGO_MAXIMO_PERMITIDO_USDT <= 0:
            e.append("RIESGO_MAXIMO_PERMITIDO_USDT debe ser > 0.")
        if self.RIESGO_MAXIMO_PERMITIDO_USDT > self.CAPITAL_TOTAL_CUENTA:
            e.append("RIESGO_MAXIMO_PERMITIDO_USDT no puede superar el capital total.")
        if self.RATIO_RIESGO_BENEFICIO < 1:
            e.append("RATIO_RIESGO_BENEFICIO debe ser >= 1.")
        if not (0 < self.TASA_COMISION_TAKER < 0.01):
            e.append("TASA_COMISION_TAKER fuera de rango (0 - 1%).")
        if not (0 <= self.TASA_MANTENIMIENTO_BTC < 0.05):
            e.append("TASA_MANTENIMIENTO_BTC fuera de rango (0 - 5%).")
        liq = 1 / max(self.APALANCAMIENTO, 1) - self.TASA_MANTENIMIENTO_BTC - 2 * self.TASA_COMISION_TAKER
        if liq <= 0:
            e.append("Con ese apalancamiento la distancia de liquidación sería <= 0. Baja el apalancamiento.")
        if self.ATR_MULT_SL <= 0:
            e.append("ATR_MULT_SL debe ser > 0.")
        if self.TF_TENDENCIA not in TREND_TFS:
            e.append("TF_TENDENCIA debe ser una de: " + ", ".join(TREND_TFS) + ".")
        if self.TF_TENDENCIA_SUPERIOR and self.TF_TENDENCIA_SUPERIOR not in TREND_TFS:
            e.append("TF_TENDENCIA_SUPERIOR debe ser vacía o una de: " + ", ".join(TREND_TFS) + ".")
        if self.TF_ZONA not in PULLBACK_TFS:
            e.append("TF_ZONA (retrocesos) debe ser una de: " + ", ".join(PULLBACK_TFS) + ".")
        if self.TF_FIBONACCI not in PULLBACK_TFS:
            e.append("TF_FIBONACCI debe ser una de: " + ", ".join(PULLBACK_TFS) + ".")
        if self.TF_GATILLO not in TRIGGER_TFS:
            e.append("TF_GATILLO debe ser una de: " + ", ".join(TRIGGER_TFS) + ".")
        if not e:
            if TF_MS[self.TF_GATILLO] > TF_MS[self.TF_ZONA]:
                e.append("El gatillo no puede ser de una temporalidad mayor que la de retrocesos.")
            if TF_MS[self.TF_ZONA] > TF_MS[self.TF_TENDENCIA]:
                e.append("La temporalidad de retrocesos no puede ser mayor que la de tendencia.")
            if self.TF_TENDENCIA_SUPERIOR and TF_MS[self.TF_TENDENCIA_SUPERIOR] <= TF_MS[self.TF_TENDENCIA]:
                e.append("El filtro superior debe ser de una temporalidad mayor que la de tendencia (o déjalo vacío).")
        if not (2 <= int(self.MARTINGALA_NIVELES) <= 4):
            e.append("MARTINGALA_NIVELES debe estar entre 2 y 4.")
        if self.MARTINGALA_MULTIPLICADOR < 1:
            e.append("MARTINGALA_MULTIPLICADOR debe ser >= 1.")
        if self.MARTINGALA_FUENTE not in ("fibonacci", "soportes"):
            e.append("MARTINGALA_FUENTE debe ser 'fibonacci' o 'soportes'.")
        if self.MARTINGALA_RIESGO_MAX_USDT < 0 or self.MARTINGALA_RIESGO_MAX_USDT > self.CAPITAL_TOTAL_CUENTA:
            e.append("MARTINGALA_RIESGO_MAX_USDT debe estar entre 0 y el capital total.")
        if self.WORKING_TYPE not in ("MARK_PRICE", "CONTRACT_PRICE"):
            e.append("WORKING_TYPE debe ser MARK_PRICE o CONTRACT_PRICE.")
        if self.ENTORNO_PRUEBAS_URL not in (URL_TESTNET, URL_DEMO):
            e.append("ENTORNO_PRUEBAS_URL no reconocido.")
        if self.ADX_LATERAL_MAX > self.ADX_TENDENCIA_MIN:
            e.append("ADX_LATERAL_MAX no puede ser mayor que ADX_TENDENCIA_MIN.")
        if not (0 < self.FIB_MIN < self.FIB_MAX < 1):
            e.append("Fibonacci: se requiere 0 < FIB_MIN < FIB_MAX < 1.")
        if self.VENTANA_AGOTAMIENTO < 1:
            e.append("VENTANA_AGOTAMIENTO debe ser >= 1.")
        if not (1024 <= int(self.DASHBOARD_PORT) <= 65535):
            e.append("DASHBOARD_PORT debe estar entre 1024 y 65535.")
        if self.ESTRATEGIA not in ("estrategia_1", "estrategia_2", "estrategia_3", "estrategia_4", "estrategia_5"):
            e.append("ESTRATEGIA debe ser una de: estrategia_1, estrategia_2, estrategia_3, estrategia_4, estrategia_5.")
        return e

    def strategy_info(self) -> dict:
        from .strategy import STRATEGIES
        return STRATEGIES.get(self.ESTRATEGIA, STRATEGIES.get("estrategia_1", {"name": self.ESTRATEGIA}))

    def strategy_label(self) -> str:
        return self.strategy_info().get("name", self.ESTRATEGIA)


def _coerce(value, current):
    if isinstance(current, bool):
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "si", "sí", "yes", "on"):
                return True
            if v in ("false", "0", "no", "off"):
                return False
            raise ValueError(value)
        return bool(value)
    if isinstance(current, int):
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        f = float(value)
        if f != int(f):
            raise ValueError(value)
        return int(f)
    if isinstance(current, float):
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return float(value)
    return str(value).strip()


class ConfigStore:
    """Carga/guarda Settings en disco (con candado: el dashboard y el bot lo usan a la vez)."""

    def __init__(self, path=paths.CONFIG_FILE):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Settings:
        s = Settings()
        data = read_json(self.path, {}) or {}
        s.apply(data)
        return s

    def save(self, s: Settings) -> None:
        with self._lock:
            write_json(self.path, s.to_dict())
