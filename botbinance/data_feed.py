"""Alimentación de datos de mercado: velas en tiempo real para cualquier temporalidad.

Arquitectura híbrida:
  * REST al inicio: descarga el historial de cada temporalidad (para calcular EMA200, ADX, etc.).
  * WebSocket (kline streams): Binance empuja cada actualización; el campo `x` = true marca el CIERRE de vela.
  * REST de respaldo: un vigilante comprueba que no falte ninguna vela cerrada y rellena huecos tras
    una desconexión, de modo que los indicadores nunca se corrompen.
Los DataFrames guardan SOLO velas cerradas (la vela en formación se guarda aparte, para el gráfico).

Binance no ofrece 16h, 20h ni 2d: se construyen agrupando velas de 4h (o 1d) alineadas al epoch (UTC).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import pandas as pd
import websocket

from .binance_api import BinanceAPIError, BinanceFutures, NetworkError
from .indicators import enrich
from .util import RESAMPLED, TF_MS, last_closed_open_time, next_open, register_anchor

log = logging.getLogger("botbinance.feed")

# Los streams de velas de Binance Futures viven en /market/stream (la ruta antigua /stream ya no entrega datos).
WS_BASES = ["wss://fstream.binance.com/market/stream", "wss://fstream.binance.com/stream"]
MAXROWS = 1500
WARMUP = {"1m": 1000, "3m": 1000, "5m": 1000, "15m": 1000, "30m": 1000, "1h": 1000, "2h": 1000, "4h": 1000,
          "6h": 1000, "8h": 1000, "12h": 1000, "1d": 1000, "3d": 900, "1w": 400, "1M": 120}
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time"]


def klines_to_df(klines: list) -> pd.DataFrame:
    """Respuesta de /fapi/v1/klines -> DataFrame OHLCV."""
    if not klines:
        return pd.DataFrame(columns=COLS)
    arr = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), int(k[6])] for k in klines]
    return pd.DataFrame(arr, columns=COLS)


def resample(df_base: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Agrupa velas base en la temporalidad derivada `tf` (16h, 20h, 2d). Solo bloques COMPLETOS."""
    base, k = RESAMPLED[tf]
    if df_base.empty:
        return pd.DataFrame(columns=COLS)
    per = TF_MS[tf]
    block = (df_base["open_time"] // per) * per
    g = df_base.groupby(block, sort=True)
    agg = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum"), close_time=("close_time", "last"), n=("open", "size"))
    agg = agg[agg["n"] == k]
    agg.index.name = "open_time"
    return agg.drop(columns="n").reset_index()[COLS].reset_index(drop=True)


def series_of(df: pd.DataFrame, n: int) -> dict:
    d = df.tail(n)
    cols = ["open_time", "open", "high", "low", "close", "volume", "ema20", "ema50", "sma200", "bb_mid", "bb_up", "bb_low", "rsi14"]
    res = {}
    for c in cols:
        if c in d.columns:
            res[c] = [None if pd.isna(v) else float(v) for v in d[c]]
        else:
            res[c] = [None] * len(d)
    return res


class DataFeed:
    def __init__(self, client: BinanceFutures, cfg, on_close: Callable[[str], None]):
        self.client = client            # cliente PÚBLICO de mainnet (datos reales de mercado)
        self.cfg = cfg
        self.on_close = on_close
        self.raw: dict[str, pd.DataFrame] = {}
        self.frames: dict[str, pd.DataFrame] = {}   # velas cerradas + indicadores (solo temporalidades de la estrategia)
        self.forming: dict[str, dict] = {}
        self.last_price: float | None = None
        self.last_price_ts = 0.0
        self.ws_connected = False
        self.last_msg_ts = 0.0
        self.variant = 0
        self._opened_ts = 0.0
        self._ws: websocket.WebSocketApp | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._last_refresh: dict[str, float] = {}
        self._chart_cache: dict[str, tuple[float, dict]] = {}
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ qué temporalidades se usan
    @property
    def timeframes(self) -> list[str]:
        """Temporalidades que usa la estrategia (cada una con su función)."""
        c = self.cfg
        tfs = {c.TF_TENDENCIA, c.TF_ZONA, c.TF_GATILLO, c.TF_FIBONACCI}
        if c.TF_TENDENCIA_SUPERIOR:
            tfs.add(c.TF_TENDENCIA_SUPERIOR)
        return sorted(tfs, key=lambda t: TF_MS[t])

    @property
    def native_tfs(self) -> list[str]:
        """Temporalidades que existen en Binance y hay que descargar/escuchar (incluye las bases de las derivadas)."""
        out = set()
        for tf in self.timeframes:
            out.add(RESAMPLED[tf][0] if tf in RESAMPLED else tf)
        return sorted(out, key=lambda t: TF_MS[t])

    # ------------------------------------------------------------------ descarga
    def _fetch_closed(self, tf: str, limit: int, start: int | None = None) -> pd.DataFrame:
        if tf in RESAMPLED:
            base, k = RESAMPLED[tf]
            return resample(self._fetch_closed(base, min(1500, limit * k + k), start), tf)
        kl = self.client.klines(self.cfg.SYMBOL, tf, limit=limit, start_time=start)
        df = klines_to_df(kl)
        # Quita la vela en formación (la última si aún no cerró).
        df = df[df["close_time"] < self.client.now_ms()].reset_index(drop=True)
        if not df.empty:
            register_anchor(tf, int(df["open_time"].iloc[-1]))
        return df

    def _store(self, tf: str, df: pd.DataFrame) -> None:
        with self._lock:
            self.raw[tf] = df.tail(MAXROWS).reset_index(drop=True)
            if tf in self.timeframes:
                self.frames[tf] = enrich(self.raw[tf], self.cfg.BB_PERIODO, self.cfg.BB_DESVIACION)

    def warmup(self) -> None:
        for tf in self.native_tfs:
            self._store(tf, self._fetch_closed(tf, WARMUP.get(tf, 1000)))
        for tf in self.timeframes:
            if tf in RESAMPLED:
                self._store(tf, resample(self.raw[RESAMPLED[tf][0]], tf))
        for tf in self.timeframes:
            df = self.raw[tf]
            if df.empty:
                raise BinanceAPIError(-1, f"Sin velas para la temporalidad {tf}", None, "klines")
            log.info("Historial %s cargado: %d velas (última cerrada: %s)", tf, len(df),
                     pd.to_datetime(df["open_time"].iloc[-1], unit="ms", utc=True).strftime("%Y-%m-%d %H:%M"))
        self.last_price = float(self.raw[self.cfg.TF_GATILLO]["close"].iloc[-1])
        self.last_price_ts = time.time()

    # ------------------------------------------------------------------ incorporar velas cerradas
    def _merge(self, tf: str, new_df: pd.DataFrame) -> bool:
        with self._lock:
            cur = self.raw.get(tf)
            if cur is None or cur.empty:
                merged, added = new_df, not new_df.empty
            else:
                last = int(cur["open_time"].iloc[-1])
                add = new_df[new_df["open_time"] > last]
                if add.empty:
                    return False
                merged, added = pd.concat([cur, add], ignore_index=True), True
            self._store(tf, merged)
            return added

    def refresh_tf(self, tf: str, limit: int = 60) -> bool:
        """Relee por REST las últimas velas de `tf`. Dispara on_close si encuentra velas cerradas nuevas."""
        self._last_refresh[tf] = time.time()
        try:
            cur = self.raw.get(tf)
            if cur is not None and len(cur):
                gap = (self.client.now_ms() - int(cur["open_time"].iloc[-1])) // TF_MS[tf] + 3
                limit = min(max(limit, gap), 1000)
            df = self._fetch_closed(tf, limit)
        except (BinanceAPIError, NetworkError) as exc:
            log.warning("No se pudo refrescar %s por REST: %s", tf, exc)
            return False
        if self._merge(tf, df):
            self._after_close(tf)
            return True
        return False

    def _fire(self, tf: str) -> None:
        try:
            self.on_close(tf)
        except Exception:  # pragma: no cover
            log.exception("Error en on_close(%s)", tf)

    def _after_close(self, tf: str) -> None:
        """Una vela de `tf` acaba de cerrar: actualiza las derivadas que dependen de ella y avisa al motor."""
        for d_tf, (base, k) in RESAMPLED.items():
            if base == tf and d_tf in self.timeframes:
                with self._lock:
                    new = resample(self.raw[base].tail(k * 4), d_tf)
                if self._merge(d_tf, new):
                    self._fire(d_tf)
        if tf in self.timeframes:
            self._fire(tf)

    def _append_ws(self, tf: str, k: dict) -> None:
        row = {"open_time": int(k["t"]), "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
               "close": float(k["c"]), "volume": float(k["v"]), "close_time": int(k["T"])}
        with self._lock:
            cur = self.raw.get(tf)
            last = int(cur["open_time"].iloc[-1]) if cur is not None and len(cur) else None
        if last is not None:
            if row["open_time"] <= last:
                return  # duplicada
            if row["open_time"] > next_open(tf, last):
                log.warning("Hueco de velas en %s; relleno por REST", tf)
                self.refresh_tf(tf)
                return
        if self._merge(tf, pd.DataFrame([row], columns=COLS)):
            self._after_close(tf)

    # ------------------------------------------------------------------ WebSocket
    def start(self) -> None:
        self._stop.clear()
        for target, name in ((self._run_ws, "feed-ws"), (self._watchdog, "feed-watchdog")):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        for th in self._threads:
            th.join(timeout=3)
        self._threads.clear()

    def _on_message(self, _ws, message: str) -> None:
        try:
            msg = json.loads(message)
            data = msg.get("data", msg)
            if data.get("e") != "kline":
                return
            k = data["k"]
            tf = k["i"]
        except (ValueError, KeyError, AttributeError):
            return
        self.last_msg_ts = time.time()
        self.ws_connected = True
        self.last_price = float(k["c"])
        self.last_price_ts = self.last_msg_ts
        self.forming[tf] = {"open_time": int(k["t"]), "open": float(k["o"]), "high": float(k["h"]),
                            "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"])}
        if k.get("x"):
            self._append_ws(tf, k)

    def _run_ws(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            base = WS_BASES[self.variant % len(WS_BASES)]
            streams = "/".join(f"{self.cfg.SYMBOL.lower()}@kline_{tf}" for tf in self.native_tfs)
            url = f"{base}?streams={streams}"
            self._opened_ts = time.time()
            self._ws = websocket.WebSocketApp(
                url,
                on_open=lambda _w: log.info("WebSocket de velas conectado (%s)", base.split("//")[1]),
                on_message=self._on_message,
                on_error=lambda _w, e: log.warning("WebSocket error: %s", e),
            )
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # pragma: no cover
                log.warning("WebSocket cerrado: %s", exc)
            self.ws_connected = False
            if self._stop.is_set():
                break
            if self.last_msg_ts < self._opened_ts:      # no llegó ningún dato: probar la otra ruta
                self.variant += 1
            lived = time.time() - self._opened_ts
            backoff = 1.0 if lived > 60 else min(backoff * 2, 30.0)
            log.warning("WebSocket desconectado; reintento en %.0fs y relleno por REST", backoff)
            for tf in self.timeframes:
                self.refresh_tf(tf)
            self._stop.wait(backoff)

    def _watchdog(self) -> None:
        """Reconecta si el WS se queda mudo y garantiza por REST que no falte ninguna vela cerrada."""
        while not self._stop.wait(5):
            if self.ws_connected and time.time() - max(self._opened_ts, self.last_msg_ts) > 30:
                log.warning("WebSocket sin datos hace 30s; reconectando")
                try:
                    self._ws.close()  # type: ignore[union-attr]
                except Exception:
                    pass
            now = self.client.now_ms()
            for tf in self.timeframes:
                with self._lock:
                    df = self.raw.get(tf)
                if df is None or df.empty:
                    continue
                expected = last_closed_open_time(tf, now)
                overdue = now - next_open(tf, expected)
                if int(df["open_time"].iloc[-1]) < expected and overdue > 6000 \
                        and time.time() - self._last_refresh.get(tf, 0) > 10:
                    log.info("La vela %s de las %s no llegó por WS; la pido por REST", tf,
                             pd.to_datetime(expected, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M"))
                    self.refresh_tf(tf)

    # ------------------------------------------------------------------ consulta
    def snapshot(self) -> dict[str, pd.DataFrame]:
        with self._lock:
            return dict(self.frames)

    def chart_data(self, tf: str, n: int = 300) -> dict:
        """Velas para el gráfico: de la memoria si la temporalidad está cargada; si no, se piden por REST."""
        with self._lock:
            df = self.frames.get(tf)
            forming = self.forming.get(tf)
            if (df is None or df.empty) and tf in self.raw and not self.raw[tf].empty:
                df = enrich(self.raw[tf], self.cfg.BB_PERIODO, self.cfg.BB_DESVIACION)
                self.frames[tf] = df
        if df is None or df.empty:
            return self._chart_on_demand(tf, n)
        last_open = int(df["open_time"].iloc[-1])
        fm = forming if forming and forming["open_time"] > last_open else None
        return {"series": series_of(df, n), "forming": fm}

    def _chart_on_demand(self, tf: str, n: int) -> dict:
        hit = self._chart_cache.get(tf)
        if hit and time.time() - hit[0] < 10:
            return hit[1]
        try:
            now = self.client.now_ms()
            if tf in RESAMPLED:
                base, k = RESAMPLED[tf]
                kl = self.client.klines(self.cfg.SYMBOL, base, limit=min(1500, (n + 220) * k))
                allb = klines_to_df(kl)
                closed = resample(allb[allb["close_time"] < now], tf)
                forming = None
            else:
                kl = self.client.klines(self.cfg.SYMBOL, tf, limit=min(1000, n + 220))
                df = klines_to_df(kl)
                closed = df[df["close_time"] < now].reset_index(drop=True)
                tail = df[df["close_time"] >= now]
                forming = None if tail.empty else {c: float(tail.iloc[-1][c]) for c in COLS[:6]}
                if forming:
                    forming["open_time"] = int(forming["open_time"])
            if closed.empty:
                return {"series": None, "forming": None}
            data = {"series": series_of(enrich(closed, self.cfg.BB_PERIODO, self.cfg.BB_DESVIACION), n), "forming": forming}
        except Exception as exc:
            log.warning("Gráfico %s: no se pudieron pedir las velas (%s)", tf, exc)
            return hit[1] if hit else {"series": None, "forming": None}
        self._chart_cache[tf] = (time.time(), data)
        return data
