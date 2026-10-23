"""Satoshi Demo (DEMO_MODE): fake history generator + integrations disabled. No network: prices are stubbed."""
from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient
from app import main, db, prices, demo, portfolio, strike, electrum

PROXY = "10.21.0.1"
H = {"X-Satoshi-Request": "1"}
TODAY = date(2026, 10, 20)
PL = {(date(2019, 1, 1) + timedelta(days=i)).isoformat(): 20000 + i * 25 for i in range(3000)}
P = {"start": "2021-03-01", "amount": 50, "frequency": "weekly", "salary": 60000, "lump_sum": 1000,
     "cold_every": "quarterly", "exchanges": 3}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(main, "TRUSTED_PEERS", {PROXY})
    monkeypatch.setattr(prices, "spot", lambda external=True, strike_key="": {"gbp": 80000.0, "usd": 105000.0, "src": "test"})
    monkeypatch.setattr(prices, "refresh_history", lambda c, start: 0)
    def boom(*a, **k):
        raise AssertionError("demo mode must not contact Strike / Electrum")
    for mod, fn in ((strike, "balance_btc"), (strike, "rate_btc_gbp"), (electrum, "scan")):
        monkeypatch.setattr(mod, fn, boom)
    main.init_storage()
    yield


def gen(c, **kw):
    return demo.generate(c, demo.validate({**P, **kw}), PL, today=TODAY)


def test_generator_realistic(env):
    with db.conn() as c:
        r = gen(c)
        s = db.get_settings(c)
        txs = portfolio.load(c)
    assert r["buys"] > 250 and r["sells"] >= 2 and r["transfers"] >= 10
    srcs = {t["source"] for t in txs}
    assert {"satsbank", "orangex", "stackly"} <= srcs and "strike" not in srcs
    led = portfolio.wallets(txs)
    assert led["lighthouse"] > 0 and led["acorn"] > 0 and all(v > -1e-8 for v in led.values())
    assert s["other_income"] == 60000 and s["demo_wallets"]["lighthouse"]["type"] == "Multisig"
    buys = [t for t in txs if t["kind"] == "buy" and t["note"] == "Recurring buy"]
    assert all(t["gbp"] == 50 and t["fee_gbp"] > 0 for t in buys)
    # real (here: stubbed cache) prices used, within jitter
    t = buys[10]
    day = portfolio.uk_day(t["ts"]).isoformat()
    assert abs((t["gbp"] - t["fee_gbp"]) / t["btc"] / PL[day] - 1) < 0.01
    assert any("[spend]" in (t["note"] or "") for t in txs)
    assert all(t["ts"] < "2026-10-20" for t in txs)


@pytest.mark.parametrize("freq,cold,n", [("daily", "monthly", 1), ("monthly", "never", 2), ("weekly", "yearly", 2)])
def test_generator_variants(env, freq, cold, n):
    with db.conn() as c:
        r = gen(c, frequency=freq, cold_every=cold, exchanges=n, start="2023-06-15", lump_sum=0)
        led = portfolio.wallets(portfolio.load(c))
    assert r["buys"] > 10
    assert (r["transfers"] == 0) == (cold == "never")
    assert all(v > -1e-8 for v in led.values())


def test_validate_rejects():
    for bad in ({"start": "nope"}, {"frequency": "hourly"}, {"amount": 0}, {"exchanges": 9}, {"cold_every": "x"},
                {"start": TODAY.isoformat()}):
        with pytest.raises(ValueError):
            demo.validate({**P, **bad})


def test_demo_api_flow(env, monkeypatch):
    monkeypatch.setattr(main, "DEMO", True)
    cl = TestClient(main.app, client=(PROXY, 5000))
    assert cl.get("/api/demo/status").json() == {"demo": True, "configured": False, "params": None}
    assert cl.post("/api/demo/setup", json=P, headers=H).json()["rows"] > 100
    st = cl.get("/api/demo/status").json()
    assert st["configured"] and st["params"]["salary"] == 60000
    s = cl.get("/api/summary").json()
    assert s["demo"] and s["holdings"]["cold"] > 0 and s["cgt"].get("value_after_tax")
    names = {w["name"] for w in s["wallets"]}
    assert {"Lighthouse Vault", "Acorn Signer", "Stackly"} <= names and "Strike" not in names
    assert abs(sum(w["btc"] for w in s["wallets"]) - s["holdings"]["total"]) < 1e-6
    # integrations are off
    for path in ("/api/strike/test", "/api/cold/scan", "/api/cold/preview", "/api/cold/wallets", "/api/example/load"):
        assert cl.post(path, json={"spec": "x"}, headers=H).status_code == 403, path
    assert "error" in cl.post("/api/strike/run", json={}, headers=H).json()
    cl.post("/api/settings", json={"strike_api_key": "SECRET", "electrum_override": True, "other_income": 1}, headers=H)
    with db.conn() as c:
        s2 = db.get_settings(c)
    assert s2["strike_api_key"] == "" and not s2["electrum_override"] and s2["other_income"] == 1
    assert cl.get("/api/settings").json()["demo"] is True
    assert [w["name"] for w in cl.get("/api/cold/wallets").json()][:2] == ["SatsBank", "OrangeX"]
    # reset wipes everything and re-arms the wizard
    assert cl.post("/api/demo/reset", json={}, headers=H).json()["ok"]
    assert cl.get("/api/demo/status").json()["configured"] is False
    assert cl.get("/api/tx").json() == []


def test_demo_endpoints_absent_in_real_app(env):
    cl = TestClient(main.app, client=(PROXY, 5000))
    assert cl.get("/api/demo/status").json() == {"demo": False}
    assert cl.post("/api/demo/setup", json=P, headers=H).status_code == 404
    assert cl.post("/api/demo/reset", json={}, headers=H).status_code == 404
    assert cl.get("/api/summary").json()["demo"] is False
