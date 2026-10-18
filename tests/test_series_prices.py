"""Daily Value series vs missing price history (1.0.10). Synthetic data only, no network."""
import sqlite3
from datetime import date, timedelta
import pytest
from app import db, portfolio, prices


@pytest.fixture
def c():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    yield con
    con.close()


def tx(c, ts, kind, btc, gbp, ext):
    db.insert_tx(c, dict(ts=ts, kind=kind, source="coinbase", wallet="coinbase", btc=btc, gbp=gbp, ext_id=ext))


def by_day(c):
    return {r["day"]: r for r in portfolio.series(c)}


def test_dust_buys_do_not_zero_or_spike_value_without_cached_prices(c):
    tx(c, "2023-01-01T10:00:00+00:00", "buy", 1.0, 20000.0, "b1")
    tx(c, "2023-01-05T10:00:00+00:00", "buy", 0.0000002, 0.0, "dust0")    # round-up recorded as £0.00
    tx(c, "2023-01-10T10:00:00+00:00", "buy", 0.00000024, 0.01, "dust1")  # £0.01 -> ~£41,667/BTC
    tx(c, "2023-01-20T10:00:00+00:00", "buy", 0.1, 2100.0, "b2")
    s = by_day(c)
    assert s["2023-01-07"]["price"] == 20000.0 and s["2023-01-07"]["value"] > 19000
    assert s["2023-01-12"]["price"] == 20000.0
    assert s["2023-01-25"]["price"] == 21000.0
    assert all(r["value"] > 0 for r in s.values())


def test_cached_close_wins_and_is_carried_over_gaps(c):
    tx(c, "2023-06-01T10:00:00+00:00", "buy", 1.0, 20000.0, "b1")
    c.execute("INSERT INTO prices(day,gbp) VALUES('2023-06-02', 21000)")
    c.execute("INSERT INTO prices(day,gbp) VALUES('2023-06-04', 0)")     # bad/zero row is ignored
    tx(c, "2023-06-05T10:00:00+00:00", "buy", 0.0000001, 0.0, "dust")
    s = by_day(c)
    assert s["2023-06-02"]["value"] == 21000.0
    assert s["2023-06-03"]["price"] == 21000.0 and s["2023-06-04"]["price"] == 21000.0
    assert s["2023-06-06"]["price"] == 21000.0


def test_history_start_follows_first_transaction(c):
    assert prices.history_start(c) == date(2021, 1, 1)
    tx(c, "2018-01-11T06:59:29+00:00", "buy", 0.01, 50.0, "b1")
    assert prices.history_start(c) == date(2018, 1, 10)


class FakeResp:
    def __init__(self, data): self.data = data
    def raise_for_status(self): pass
    def json(self): return self.data


def test_backfill_fills_only_missing_days_in_chunks(c, monkeypatch):
    calls = []

    def fake_get(url, params=None, **kw):
        assert url == "https://api.exchange.coinbase.com/products/BTC-GBP/candles"
        s, e = date.fromisoformat(params["start"][:10]), date.fromisoformat(params["end"][:10])
        assert (e - s).days < 300 and params["granularity"] == 86400
        calls.append((s, e))
        out, d = [], s
        while d <= e:
            ts = int((d - date(1970, 1, 1)).total_seconds())
            out.append([ts, 1, 2, 3, 10000.0 + (d - date(2020, 1, 1)).days, 9])
            d += timedelta(days=1)
        return FakeResp(out)

    monkeypatch.setattr(prices.httpx, "get", fake_get)
    today = date.today()
    # recent days already cached (as Kraken would); one kept value must not be overwritten
    d = today - timedelta(days=100)
    while d < today:
        c.execute("INSERT INTO prices(day,gbp,usd) VALUES(?,?,?)", (d.isoformat(), 50000.0, 60000.0))
        d += timedelta(days=1)
    start = today - timedelta(days=800)
    n = prices.backfill_history(c, start)
    assert n == 700 and len(calls) == 3
    h = prices.history(c)
    assert h[start.isoformat()][0] == 10000.0 + (start - date(2020, 1, 1)).days
    assert h[(today - timedelta(days=50)).isoformat()] == (50000.0, 60000.0)
    assert not prices.missing_days(c, start, today - timedelta(days=1))
    calls.clear()
    assert prices.backfill_history(c, start) == 0 and calls == []   # complete cache: no requests


def test_backfill_network_failure_is_harmless(c, monkeypatch):
    def boom(*a, **k): raise RuntimeError("offline")
    monkeypatch.setattr(prices.httpx, "get", boom)
    assert prices.backfill_history(c, date.today() - timedelta(days=10)) == 0
