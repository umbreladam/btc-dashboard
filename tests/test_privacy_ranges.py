"""Privacy mode (front-end masking) and 1D/1W intraday ranges."""
import json, re, shutil, subprocess, os
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app import main, db, prices, portfolio

STATIC = os.path.join(os.path.dirname(main.__file__), "static")
PROXY = "10.21.0.1"
H = {"X-Satoshi-Request": "1"}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(main, "TRUSTED_PEERS", {PROXY})
    monkeypatch.setattr(prices, "spot", lambda external=True, strike_key="": {"gbp": 50000.0, "usd": 65000.0, "src": "test"})
    main.init_storage()


def cl():
    return TestClient(main.app, client=(PROXY, 50000))


def ep(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def seed():
    with db.conn() as c:
        for ts, kind, b, g in (("2026-10-10T09:00:00", "buy", 0.1, 4000), ("2026-10-12T10:30:00", "buy", 0.05, 2500),
                               ("2026-10-12T12:00:00", "sell", 0.02, 1100)):
            c.execute("INSERT INTO tx(ts,kind,source,wallet,btc,gbp,fee_gbp,fee_btc,dedupe) VALUES(?,?,?,?,?,?,0,0,?)",
                      (ts + "+00:00", kind, "manual", "cold", b, g, ts + kind))


# ---------- intraday ----------
def test_intraday_holdings_follow_exact_timestamps():
    seed()
    pts = [(ep("2026-10-12T10:00:00"), 50000.0), (ep("2026-10-12T11:00:00"), 51000.0), (ep("2026-10-12T13:00:00"), 52000.0)]
    with db.conn() as c:
        out = portfolio.intraday(c, pts)
    assert [p["btc"] for p in out] == [0.1, 0.15, 0.13]
    assert [p["value"] for p in out] == [5000.0, round(0.15 * 51000, 2), round(0.13 * 52000, 2)]
    assert out[0]["cost"] == 4000 and out[1]["cost"] == 6500
    assert out[2]["cost"] == 5500   # same-day rule: the sale matches that day's buy first, rest goes to the pool
    assert out[0]["day"].endswith("11:00")   # UK local time (BST)


def test_intraday_endpoint_uses_feed(monkeypatch):
    seed()
    monkeypatch.setattr(prices, "intraday", lambda days: [(ep("2026-10-12T13:00:00"), 52000.0)])
    r = cl().get("/api/series/intraday?days=1").json()
    assert r["available"] and r["points"][0]["btc"] == 0.13


def test_intraday_respects_disable_external_prices(monkeypatch):
    seed()
    called = []
    monkeypatch.setattr(prices, "intraday", lambda days: called.append(days) or [(1, 1.0)])
    with db.conn() as c:
        db.set_settings(c, {"disable_external_prices": True})
    r = cl().get("/api/series/intraday?days=7").json()
    assert r == {"available": False, "reason": "External price fetches are disabled", "points": []} and not called


def test_intraday_feed_failure_is_graceful(monkeypatch):
    monkeypatch.setattr(prices, "intraday", lambda days: [])
    assert cl().get("/api/series/intraday?days=1").json()["available"] is False


# ---------- privacy mode ----------
def html():
    return open(os.path.join(STATIC, "index.html")).read()


def test_privacy_init_runs_in_head_before_render():
    h = html()
    head = h.split("</head>")[0]
    assert head.index('src="static/privacy-init.js"') < head.index("style.css")
    assert h.index("privacy-init.js") < h.index("static/app.js")
    js = open(os.path.join(STATIC, "privacy-init.js")).read()
    assert "satoshi-privacy" in js and 'classList.add("priv")' in js
    assert 'id="btn-priv"' in h


def test_all_money_rendering_goes_through_masking_formatters():
    js = open(os.path.join(STATIC, "app.js")).read()
    # raw formatters are only used for the public market price (ticker / Strike rate)
    raw = [l for l in js.splitlines() if re.search(r"\b(gbpRaw|usdRaw)\(", l) and "const " not in l]
    assert all("ticker" in l or "rate_gbp" in l for l in raw), raw
    assert ".toFixed(8)" not in js.replace("(v || 0).toFixed(8)", "")
    assert "options: opts(btc4)" in js and "opts(v => gbp(v))" in js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_formatters_mask_figures_but_keep_percentages():
    js = open(os.path.join(STATIC, "app.js")).read()
    fmt = js[js.index("const MASK"):js.index("function setPrivacy")]
    prog = ("const document={documentElement:{classList:{contains:()=>globalThis.P}}};" + "\n" + fmt.replace("let PRIV", "var PRIV") +
            "\nconst run=()=>[gbp(1234.5,2),usd(99),btc(0.12345678),btc4(0.5),signed(-12),gbpRaw(60000)];"
            "\nconst a=run(); PRIV=true; console.log(JSON.stringify([a, run()]));")
    off, on = json.loads(subprocess.check_output(["node", "-e", prog], text=True))
    assert off == ["£1,234.50", "$99", "0.12345678", "0.5000 ₿", "-£12", "£60,000"]
    assert on == ["£****", "$****", "****", "**** ₿", "£****", "£60,000"]
