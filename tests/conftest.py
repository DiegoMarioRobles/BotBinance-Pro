import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botbinance import paths, secrets_store  # noqa: E402
from botbinance.binance_api import SymbolRules  # noqa: E402
from botbinance.config import Settings  # noqa: E402
from botbinance.indicators import enrich  # noqa: E402


@pytest.fixture
def cfg() -> Settings:
    return Settings()


@pytest.fixture
def rules() -> SymbolRules:
    return SymbolRules()


@pytest.fixture(autouse=True)
def isolate_files(tmp_path, monkeypatch):
    """Ningún test toca data/ real ni el Administrador de credenciales de Windows."""
    for name in ("TRADE_FILE", "HISTORY_FILE", "RUNTIME_FILE", "CONFIG_FILE", "SECRETS_FALLBACK_FILE"):
        monkeypatch.setattr(paths, name, tmp_path / f"{name.lower()}.json")
    store: dict[str, str] = {}
    monkeypatch.setattr(secrets_store, "get", lambda name: store.get(name, ""))
    monkeypatch.setattr(secrets_store, "has", lambda name: bool(store.get(name)))

    def _set(name, value):
        secrets_store._check(name)
        value = (value or "").strip()
        if value:
            store[name] = value
        else:
            store.pop(name, None)

    monkeypatch.setattr(secrets_store, "set", _set)
    return store


def candles(closes, spread=0.5, vol=100.0, tf_ms=3_600_000, start=1_700_000_000_000):
    """DataFrame OHLCV sintético a partir de una lista de cierres (open = cierre previo)."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    n = len(closes)
    ot = start + np.arange(n) * tf_ms
    return pd.DataFrame({
        "open_time": ot, "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, vol), "close_time": ot + tf_ms - 1,
    })


def enriched(closes, **kw):
    return enrich(candles(closes, **kw))
