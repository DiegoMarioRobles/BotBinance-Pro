from datetime import datetime, timezone
from types import SimpleNamespace


from botbinance.data_feed import DataFeed, klines_to_df
from botbinance.util import last_closed_open_time

H = 3_600_000


def ms(y, m, d, hh=0, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def test_last_closed_open_time_all_timeframes():
    now = ms(2026, 10, 14, 14, 30)                       # miércoles 14:30 UTC
    assert last_closed_open_time("1h", now) == ms(2026, 10, 14, 13)
    assert last_closed_open_time("2h", now) == ms(2026, 10, 14, 12)
    assert last_closed_open_time("4h", now) == ms(2026, 10, 14, 8)
    assert last_closed_open_time("1d", now) == ms(2026, 10, 13)
    # la semana de Binance abre el lunes 00:00 UTC: la última cerrada empezó el lunes 5 de octubre
    assert last_closed_open_time("1w", now) == ms(2026, 10, 5)
    assert datetime.fromtimestamp(ms(2026, 10, 5) / 1000, tz=timezone.utc).weekday() == 0
    # justo después del cierre del lunes 00:00 la semana anterior ya cerró
    assert last_closed_open_time("1w", ms(2026, 10, 12, 0, 0) + 1) == ms(2026, 10, 5)
    assert last_closed_open_time("1w", ms(2026, 10, 12) - 1) == ms(2026, 9, 28)


def test_klines_to_df():
    kl = [[1000, "1", "2", "0.5", "1.5", "10", 1999, "x", 1, "y", "z", "0"]]
    df = klines_to_df(kl)
    assert list(df.columns)[:5] == ["open_time", "open", "high", "low", "close"] and df.loc[0, "high"] == 2.0
    assert klines_to_df([]).empty


class FakeClient:
    def __init__(self, rows):
        self.rows, self.fetches = rows, 0

    def now_ms(self):
        return self.rows[-1][6] + 10_000

    def klines(self, symbol, tf, limit=500, start_time=None, end_time=None):
        self.fetches += 1
        return [r for r in self.rows if start_time is None or r[0] >= start_time][-limit:]


def kline(i, t0=1_700_000_000_000 // H * H, price=100.0):
    o = t0 + i * H
    return [o, str(price + i), str(price + i + 1), str(price + i - 1), str(price + i + 0.5), "10", o + H - 1, "0", 1, "0", "0", "0"]


def make_feed(n=80):
    rows = [kline(i) for i in range(n)]
    cfg = SimpleNamespace(SYMBOL="BTCUSDT", TF_TENDENCIA="1d", TF_TENDENCIA_SUPERIOR="", TF_ZONA="4h", TF_GATILLO="1h", TF_FIBONACCI="1d", BB_PERIODO=20, BB_DESVIACION=2.0)
    fired = []
    feed = DataFeed(FakeClient(rows), cfg, fired.append)
    feed.raw["1h"] = klines_to_df(rows[:60])
    from botbinance.indicators import enrich
    feed.frames["1h"] = enrich(feed.raw["1h"])
    return feed, fired, rows


def ws_k(row, closed=True):
    return {"t": row[0], "T": row[6], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5], "x": closed, "i": "1h"}


def test_closed_ws_candle_is_appended_and_fires_once():
    feed, fired, rows = make_feed()
    feed._append_ws("1h", ws_k(rows[60]))
    assert fired == ["1h"] and len(feed.raw["1h"]) == 61 and feed.frames["1h"]["ema20"].iloc[-1] > 0
    feed._append_ws("1h", ws_k(rows[60]))            # duplicado: se ignora
    assert fired == ["1h"] and len(feed.raw["1h"]) == 61


def test_forming_candle_is_not_in_closed_frames():
    feed, fired, rows = make_feed()
    feed._on_message(None, '{"stream":"btcusdt@kline_1h","data":{"e":"kline","k":%s}}' % str(ws_k(rows[60], closed=False)).replace("'", '"').replace("False", "false"))
    assert fired == [] and len(feed.raw["1h"]) == 60
    assert feed.forming["1h"]["open_time"] == rows[60][0] and feed.last_price == float(rows[60][4])


def test_gap_triggers_rest_backfill():
    feed, fired, rows = make_feed()
    feed._append_ws("1h", ws_k(rows[65]))            # faltan las velas 60..64
    assert len(feed.raw["1h"]) >= 65 and feed.client.fetches >= 1
    assert fired == ["1h"]
    assert (feed.raw["1h"]["open_time"].diff().dropna() == H).all()     # sin huecos ni duplicados


def test_merge_keeps_order_and_caps_length():
    feed, _, rows = make_feed()
    new = klines_to_df(rows[55:75])                  # solapa con lo existente
    assert feed._merge("1h", new)
    ot = feed.raw["1h"]["open_time"]
    assert ot.is_monotonic_increasing and ot.is_unique and len(ot) == 75
    assert not feed._merge("1h", new)                # nada nuevo


def test_series_of_includes_bb_mid_and_rsi14():
    from botbinance.data_feed import series_of
    feed, _, _ = make_feed(80)
    df = feed.frames["1h"]
    series = series_of(df, 30)
    assert "bb_mid" in series
    assert "rsi14" in series
    assert "bb_up" in series
    assert "bb_low" in series
    assert len(series["rsi14"]) == 30
    assert len(series["bb_mid"]) == 30


def test_re_enrich_all_updates_indicators_with_new_config():
    feed, _, _ = make_feed(80)
    old_rsi = feed.frames["1h"]["rsi14"].copy()
    old_bb = feed.frames["1h"]["bb_up"].copy()
    feed.cfg.RSI_PERIODO = 7
    feed.cfg.BB_PERIODO = 10
    feed.cfg.BB_DESVIACION = 2.5
    feed.re_enrich_all()
    new_rsi = feed.frames["1h"]["rsi14"]
    new_bb = feed.frames["1h"]["bb_up"]
    assert len(new_rsi.dropna()) > len(old_rsi.dropna())
    assert not (old_bb.dropna() == new_bb.dropna().loc[old_bb.dropna().index]).all()


