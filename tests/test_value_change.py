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
