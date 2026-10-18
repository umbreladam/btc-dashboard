"""Value change indicator (1.0.12): market change excluding money paid in/out. Synthetic data only, no network."""
import json, os, re, shutil, subprocess
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from app import main, db, prices, portfolio

STATIC = os.path.join(os.path.dirname(main.__file__), "static")
PROXY = "10.21.0.1"
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
SPOT = {"gbp": 33000.0, "usd": 42000.0, "src": "test"}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(main, "TRUSTED_PEERS", {PROXY})
    monkeypatch.setattr(prices, "spot", lambda external=True, strike_key="": dict(SPOT))
    monkeypatch.setattr(prices, "intraday", lambda days: [])
    def offline(*a, **k): raise RuntimeError("offline")
    monkeypatch.setattr(prices, "_coinbase_daily", offline)   # never touch the network in tests
    main.init_storage()


def tx(kind, when, btc, gbp=0.0, fee_btc=0.0, wallet="cold"):
    with db.conn() as c:
        db.insert_tx(c, dict(ts=when.isoformat(), kind=kind, source="manual", wallet=wallet, btc=btc, gbp=gbp,
                             fee_btc=fee_btc, to_wallet="cold" if kind == "transfer" else None, ext_id=f"{kind}{when}{btc}"))


def price(day, gbp):
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO prices(day,gbp) VALUES(?,?)", (day, gbp))


def chg(period, **kw):
    with db.conn() as c:
        return portfolio.change(c, period, now=NOW.timestamp(), spot=kw.pop("spot", SPOT), **kw)


def seed_week():
    tx("buy", NOW - timedelta(days=30), 1.0, 20000.0)
    price("2026-06-07", 30000.0)   # candle closing 2026-06-08 00:00 UTC = nearest close to NOW - 7d


def test_week_change_is_pure_market_move():
    seed_week()
    r = chg("1w")
    assert r["available"] and r["price_start"] == 30000.0
    assert r["change_gbp"] == pytest.approx(3000.0) and r["pct"] == pytest.approx(10.0)


def test_buys_during_period_are_not_gains_and_sells_are_not_losses():
    seed_week()
    tx("buy", NOW - timedelta(days=2), 0.5, 16000.0)        # paid £16k for 0.5 BTC
    r = chg("1w")
    assert r["net_invested"] == pytest.approx(16000.0)
    assert r["change_gbp"] == pytest.approx(1.5 * 33000 - (30000 + 16000))
    assert r["pct"] == pytest.approx(3500 / 46000 * 100)
    tx("sell", NOW - timedelta(days=1), 0.5, 16500.0)       # took £16.5k out
    r = chg("1w")
    assert r["net_invested"] == pytest.approx(-500.0)
    assert r["change_gbp"] == pytest.approx(1.0 * 33000 - (30000 - 500))


def test_transfers_are_neutral_but_network_fees_count():
    seed_week()
    tx("transfer", NOW - timedelta(days=3), 0.5, fee_btc=0.001, wallet="coinbase")
    r = chg("1w")
    assert r["net_invested"] == 0 and r["change_gbp"] == pytest.approx(0.999 * 33000 - 30000)


def test_live_balance_gap_counts_as_held_throughout():
    seed_week()
    with db.conn() as c:
        db.set_settings(c, {"cold_balance_cache": {"btc": 1.2, "wallets": [], "at": NOW.isoformat()}})
    r = chg("1w")
    assert r["value_now"] == pytest.approx(1.2 * 33000) and r["start_value"] == pytest.approx(1.2 * 30000)
    assert r["pct"] == pytest.approx(10.0)


def test_negative_change_and_missing_price():
    seed_week()
    r = chg("1w", spot={"gbp": 27000.0})
    assert r["change_gbp"] == pytest.approx(-3000.0) and r["pct"] == pytest.approx(-10.0)
    assert chg("1y")["available"] is False                    # no price history a year back
    assert chg("1w", spot={"gbp": None})["available"] is False


def test_one_hour_needs_intraday_prices_and_respects_disable_external():
    seed_week()
    t0 = NOW.timestamp() - 3600
    pts = [(t0 - 300, 32000.0), (t0 + 120, 32500.0), (NOW.timestamp() - 60, 33000.0)]
    r = chg("1h", intraday=lambda days: pts)
    assert r["available"] and r["price_start"] == 32500.0 and r["change_gbp"] == pytest.approx(500.0)
    assert chg("1h", intraday=lambda days: [])["available"] is False           # feed down -> "—"
    assert chg("1h", intraday=lambda days: [(t0 - 7200, 1.0)])["available"] is False   # too far from start
    with db.conn() as c:
        db.set_settings(c, {"disable_external_prices": True})
    called = []
    assert chg("1h", intraday=lambda days: called.append(days) or pts)["available"] is False and not called


def test_one_day_falls_back_to_daily_close_without_intraday():
    tx("buy", NOW - timedelta(days=30), 1.0, 20000.0)
    price("2026-06-13", 31000.0)   # NOW - 1d = 2026-06-14 12:00 -> nearest close is candle 2026-06-14, falls back a day
    r = chg("1d")
    assert r["available"] and r["price_start"] == 31000.0


def test_all_matches_unrealised_pnl_card():
    tx("buy", NOW - timedelta(days=400), 1.0, 20000.0)
    tx("buy", NOW - timedelta(days=100), 0.5, 15000.0)
    r = chg("all")
    with db.conn() as c:
        s = portfolio.summary(c)
    assert r["change_gbp"] == pytest.approx(s["pnl_gbp"]) and r["pct"] == pytest.approx(s["pnl_pct"])


def test_api_endpoint():
    seed_week()
    cl = TestClient(main.app, client=(PROXY, 50000))
    assert cl.get("/api/change?period=5x").status_code == 400
    r = cl.get("/api/change?period=all").json()
    assert r["available"] and r["period"] == "all"
    assert cl.get("/api/change").json()["period"] == "1d"


# ---------- front end ----------
JS = open(os.path.join(STATIC, "app.js")).read()
HTML = open(os.path.join(STATIC, "index.html")).read()


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_change_text_sign_colour_and_privacy():
    fmt = JS[JS.index("const MASK"):JS.index("function setPrivacy")]
    cases = [{"available": True, "change_gbp": 1650.4, "pct": 1.5}, {"available": True, "change_gbp": -2345.6, "pct": -3.217},
             {"available": True, "change_gbp": 0.2, "pct": 0.0001}, {"available": False}, None,
             {"available": True, "change_gbp": 100, "pct": None}]
    prog = ("const document={documentElement:{classList:{contains:()=>false}}};const esc=t=>String(t);\n" + fmt.replace("let PRIV", "var PRIV") +
            "\nconst C=" + json.dumps(cases) + ";const off=C.map(chgFmt); PRIV=true; console.log(JSON.stringify([off, C.map(chgFmt)]));")
    off, on = json.loads(subprocess.check_output(["node", "-e", prog], text=True))
    assert off == [{"text": "+1.50% (+£1,650)", "cls": "up"}, {"text": "-3.22% (-£2,346)", "cls": "down"},
                   {"text": "0.00% (£0)", "cls": "flat"}, {"text": "—", "cls": "flat"}, {"text": "—", "cls": "flat"},
                   {"text": "+£100", "cls": "up"}]
    assert on == [{"text": "+1.50% (£****)", "cls": "up"}, {"text": "-3.22% (£****)", "cls": "down"},
                  {"text": "0.00% (£****)", "cls": "flat"}, {"text": "—", "cls": "flat"}, {"text": "—", "cls": "flat"},
                  {"text": "£****", "cls": "up"}]


def test_period_pills_default_and_persistence():
    seg = re.search(r'<div class="seg mini" id="chg-seg"[^>]*>(.*?)</div>', HTML).group(1)
    assert re.findall(r'data-p="(\w+)">(\w+)<', seg) == [("1h", "1H"), ("1d", "1D"), ("1w", "1W"), ("1m", "1M"),
                                                          ("1y", "1Y"), ("4y", "4Y"), ("all", "All")]
    assert '"satoshi-change-period"' in JS and ': "1d"' in JS
    assert 'id="c-chg"' in HTML and HTML.index('id="c-gbp"') < HTML.index('id="c-chg"')
    # chart range buttons are scoped so the change pills don't drive the chart
    assert 'querySelectorAll(".seg button' not in JS


# ---------- 1.0.13: 4Y showed "—" when the daily history didn't reach the period start ----------
def test_4y_backfills_missing_start_window_then_returns_a_number(monkeypatch):
    tx("buy", NOW - timedelta(days=2000), 1.0, 5000.0)
    tx("buy", NOW - timedelta(days=100), 0.2, 6000.0)
    for i in range(1, 700):   # like an instance updated from <=1.0.9: Kraken's ~720 days only
        price((NOW.date() - timedelta(days=i)).isoformat(), 30000.0)
    with db.conn() as c:
        db.set_settings(c, {"cold_balance_cache": {"btc": 1.18, "wallets": [], "at": NOW.isoformat()}})  # live < ledger
    calls = []

    def fake_cb(product, a, b):
        calls.append((a, b))
        out, d = {}, a
        while d <= b:
            out[d.isoformat()] = 15000.0
            d += timedelta(days=1)
        return out
    monkeypatch.setattr(prices, "_coinbase_daily", fake_cb)
    with db.conn() as c:
        assert prices.needs_backfill(c, date_of(NOW - timedelta(days=2001)))
    r = chg("4y")
    assert r["available"] and r["change_gbp"] is not None and r["pct"] is not None and r["price_start"] == 15000.0
    assert len(calls) == 1 and (calls[0][1] - calls[0][0]).days <= 10        # only the start window was fetched
    calls.clear()
    assert chg("4y")["available"] and calls == []                             # now cached


def test_4y_without_external_prices_stays_unavailable_without_fetching(monkeypatch):
    tx("buy", NOW - timedelta(days=2000), 1.0, 5000.0)
    price((NOW.date() - timedelta(days=1)).isoformat(), 30000.0)
    monkeypatch.setattr(prices, "_coinbase_daily", lambda *a: pytest.fail("must not fetch"))
    with db.conn() as c:
        db.set_settings(c, {"disable_external_prices": True})
    assert chg("4y")["available"] is False


def test_daily_price_uses_nearest_stored_day_within_slack():
    ts = datetime(2022, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()   # nearest close: candle 2022-10-18
    assert portfolio.daily_price_at({"2022-10-20": (3.0, None), "2022-10-15": (1.0, None)}, ts) == 3.0
    assert portfolio.daily_price_at({"2022-10-14": (1.0, None), "2022-10-22": (2.0, None)}, ts) == 1.0   # tie: earlier
    assert portfolio.daily_price_at({"2022-10-10": (1.0, None), "2022-10-27": (2.0, None)}, ts) is None


def test_needs_backfill():
    with db.conn() as c:
        assert prices.needs_backfill(c, NOW.date())          # empty cache
        price("2018-01-05", 10000.0)
        assert not prices.needs_backfill(c, date_of(datetime(2018, 1, 1)))
        assert prices.needs_backfill(c, date_of(datetime(2017, 12, 1)))


def date_of(dt):
    return dt.date()


def test_scheduler_price_refresh_runs_in_worker_thread_with_own_connection(monkeypatch):
    """Regression: the scheduler passed its SQLite connection into asyncio.to_thread, so every daily refresh died
    with sqlite3.ProgrammingError and the price history (incl. the Coinbase backfill) never updated."""
    import asyncio
    tx("buy", NOW - timedelta(days=2000), 1.0, 5000.0)
    seen = []

    def fake_refresh(c, start):
        c.execute("INSERT OR REPLACE INTO prices(day,gbp) VALUES('2021-01-01', 25000)")   # must not raise
        seen.append(start)
        return 1
    monkeypatch.setattr(prices, "refresh_history", fake_refresh)
    assert asyncio.run(asyncio.to_thread(main.refresh_prices_job, "2026-06-15")) == 1
    with db.conn() as c:
        s = db.get_settings(c)
        assert s["prices_day"] == "2026-06-15" and s["prices_attempt_at"] and seen
        assert prices.history(c)["2021-01-01"][0] == 25000


def test_price_refresh_due_logic(monkeypatch):
    import time as _t
    day = datetime(2026, 6, 15, 12, 0)
    assert main.prices_refresh_due({}, day)                                                   # never run
    assert not main.prices_refresh_due({"prices_attempt_at": _t.time() - 60}, day)            # just tried: back off
    assert main.prices_refresh_due({"prices_day": "2026-06-14", "prices_attempt_at": _t.time() - 3600}, day)
    done = {"prices_day": "2026-06-15", "prices_attempt_at": _t.time() - 7 * 3600}
    tx("buy", NOW - timedelta(days=2000), 1.0, 5000.0)
    price("2026-06-14", 30000.0)
    assert main.prices_refresh_due(done, day)            # history doesn't reach the first transaction: backfill
    price((NOW - timedelta(days=2002)).date().isoformat(), 5000.0)
    assert not main.prices_refresh_due(done, day)        # covered: once a day is enough
