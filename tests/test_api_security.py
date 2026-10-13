"""API security: proxy-only access, CSRF, no key material in responses, electrum lock, wallet types, migration.
All keys are public BIP test vectors (see test_descriptor.py), not real wallets."""
import json, os, re, stat, sqlite3
import pytest
from fastapi.testclient import TestClient
from app import main, db, prices, wallets, electrum, security

ZPUB = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
A = "xpub6ERApfZwUNrhLCkDtcHTcxd75RbzS1ed54G1LkBUHQVHQKqhMkhgbmJbZRkrgZw4koxb5JaHWkY4ALHY2grBGRjaDMzQLcgJvLJuZZvRcEL"
B = "xpub68NZiKmJWnxxS6aaHmn81bvJeTESw724CRDs6HbuccFQN9Ku14VQrADWgqbhhTHBaohPX4CjNLf9fq9MYo6oDaPPLPxSb7gwQN3ih19Zm4Y"
C = "xpub661MyMwAqRbcGDZQUKLqmWodYLcoBQnQH33yYkkF3jjxeLvY8qr2wWGEWkiKFaaQfJCoi3HeEq3Dc5DptfbCyjD38fNhSqtKc1UHaP4ba3t"
MULTI = f"wsh(sortedmulti(2,[deadbeef/48h/0h/0h/2h]{A}/<0;1>/*,[cafebabe/48h/0h/0h/2h]{B}/<0;1>/*,[0badf00d/48h/0h/0h/2h]{C}/<0;1>/*))"
FAKE_STRIKE = "TESTSTRIKEKEY-0123456789abcdef-NOTREAL"
KEY_RE = re.compile(r"[xyztuvYZUV](?:pub|prv)[1-9A-HJ-NP-Za-km-z]{20,}")
PROXY = "10.21.0.1"
H = {"X-Satoshi-Request": "1"}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(main, "TRUSTED_PEERS", {PROXY, "127.0.0.1"})
    monkeypatch.setattr(prices, "spot", lambda external=True, strike_key="": {"gbp": 50000.0, "usd": 65000.0, "src": "test"})
    main.init_storage()
    yield


def client(peer=PROXY):
    return TestClient(main.app, client=(peer, 50000))


def add(c, spec, name="W", **kw):
    r = c.post("/api/cold/wallets", json={"name": name, "spec": spec, **kw}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["wallet"]


def leaks(text):
    return KEY_RE.search(text) or FAKE_STRIKE in text or A[4:30] in text or ZPUB[4:30] in text or "sortedmulti(" in text


# ---------- proxy guard ----------
def test_non_proxy_requests_rejected():
    for peer in ("10.21.21.10", "10.21.0.7", "172.17.0.2"):
        c = client(peer)
        assert c.get("/api/summary").status_code == 403
        assert c.get("/").status_code == 403
        assert c.post("/api/settings", json={}, headers=H).status_code == 403
    assert client().get("/api/health").status_code == 200


def test_docs_disabled():
    c = client()
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(p).status_code == 404


def test_default_gateway_trusted(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setattr(security, "default_gateway", lambda: "10.21.0.1")
    assert "10.21.0.1" in security.trusted_peers()
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.21.0.5")
    assert security.trusted_peers() == {"10.21.0.5"}


# ---------- CSRF ----------
@pytest.mark.parametrize("method,path", [("post", "/api/settings"), ("post", "/api/tx"), ("delete", "/api/tx/1"),
                                         ("post", "/api/import/preview"), ("post", "/api/cold/wallets"),
                                         ("put", "/api/cold/wallets/x"), ("delete", "/api/example"), ("post", "/api/cold/scan")])
def test_csrf_rejected(method, path):
    c = client()
    f = getattr(c, method)
    # no custom header
    r = f(path, json={}) if method != "delete" else f(path, headers={"Content-Type": "application/json"})
    assert r.status_code == 403
    # header but form content type (what a cross-site <form> can send)
    r = c.request(method.upper(), path, content=b"a=1", headers={**H, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403
    r = c.request(method.upper(), path, files={"file": ("a.csv", b"x")}, headers=H)
    assert r.status_code == 403
    # cross-origin Origin
    r = c.request(method.upper(), path, json={}, headers={**H, "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_csrf_accepts_same_origin():
    c = client()
    r = c.post("/api/settings", json={"sell_fee_pct": 1}, headers={**H, "Origin": "http://testserver"})
    assert r.status_code == 200


# ---------- no key material ----------
def test_no_key_material_in_any_get():
    c = client()
    add(c, MULTI, "Vault", description="2-of-3 test vault")
    w2 = add(c, ZPUB, "Spending")
    r = c.post("/api/settings", json={"strike_api_key": FAKE_STRIKE}, headers=H)
    assert r.status_code == 200
    with db.conn() as cn:  # a legacy 1.0.0 xpub left in settings must not leak either
        db.set_settings(cn, {"xpub": ZPUB})
        raw = dict(cn.execute("SELECT v FROM settings WHERE k='strike_api_key'").fetchone())["v"]
    assert FAKE_STRIKE not in raw  # encrypted at rest
    checked = 0
    for route in main.app.routes:
        if "GET" not in getattr(route, "methods", set()) or not route.path.startswith("/api"):
            continue
        path = route.path.replace("{wid}", w2["id"])
        r = c.get(path)
        assert r.status_code < 500, path
        assert not leaks(r.text), f"key material in {path}"
        checked += 1
    assert checked >= 8
    # mutating responses too
    r = c.post("/api/cold/preview", json={"spec": MULTI}, headers=H)
    assert r.status_code == 200 and "descriptor" not in r.json() and not leaks(r.text)
    assert r.json()["fingerprints"] == ["deadbeef", "cafebabe", "0badf00d"] and r.json()["type"] == "Multisig"
    r = c.put(f"/api/cold/wallets/{w2['id']}", json={"name": "Renamed"}, headers=H)
    assert not leaks(r.text)


def test_errors_are_generic():
    c = client()
    bad = A[:-3] + "zzz"
    for p in ("/api/cold/preview", "/api/cold/wallets"):
        r = c.post(p, json={"spec": bad}, headers=H)
        assert r.status_code == 400 and bad[:20] not in r.text and not leaks(r.text)
    r = c.post("/api/cold/wallets", json={"spec": MULTI.replace("2,", "5,", 1)}, headers=H)
    assert r.status_code == 400 and not leaks(r.text)


def test_private_key_refused():
    xprv = "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
    r = client().post("/api/cold/wallets", json={"spec": xprv}, headers=H)
    assert r.status_code == 400 and xprv[:20] not in r.text


# ---------- wallet types / edit ----------
def test_type_autodetect_and_override():
    c = client()
    m = add(c, MULTI, "Vault")
    s = add(c, ZPUB, "Hot")
    assert (m["type"], m["policy"]) == ("Multisig", "2-of-3")
    assert (s["type"], s["policy"]) == ("Single-sig", "single-sig")
    single_desc = f"wpkh([deadbeef/84h/0h/0h]{A}/<0;1>/*)"
    assert add(c, single_desc, "Desc")["type"] == "Single-sig"
    assert wallets.detect(MULTI.replace("sortedmulti", "multi"))["detected_type"] == "Multisig"
    r = c.put(f"/api/cold/wallets/{s['id']}", json={"type": "Multisig", "description": " my  note ", "name": "Hot 2"}, headers=H)
    w = r.json()["wallet"]
    assert (w["type"], w["type_overridden"], w["description"], w["name"]) == ("Multisig", True, "my note", "Hot 2")
    assert c.put(f"/api/cold/wallets/{s['id']}", json={"type": "auto"}, headers=H).json()["wallet"]["type"] == "Single-sig"
    assert c.put(f"/api/cold/wallets/{s['id']}", json={"type": "Bogus"}, headers=H).status_code == 400
    # key material unchanged by edit: addresses still derive
    assert len(c.get(f"/api/cold/wallets/{s['id']}/addresses?n=2").json()) == 4
    assert c.post("/api/cold/wallets", json={"spec": ZPUB}, headers=H).status_code == 409


def test_wallets_panel_rows():
    c = client()
    m = add(c, MULTI, "Vault")
    with db.conn() as cn:
        db.set_settings(cn, {"strike_balance_cache": {"btc": 0.1, "at": "2026-10-12T10:00:00+00:00"},
                             "cold_balance_cache": {"btc": 0.5, "at": "2026-10-12T11:00:00+00:00",
                                                    "wallets": [{"id": m["id"], "balance": 0.5, "at": "2026-10-12T11:00:00+00:00"}]}})
        db.insert_tx(cn, {"ts": "2024-01-01T00:00:00+00:00", "kind": "buy", "source": "kraken", "wallet": "kraken", "btc": 0.4, "gbp": 10000})
    s = c.get("/api/summary").json()
    rows = {r["name"]: r for r in s["wallets"]}
    assert rows["Strike"]["type"] == "Exchange" and rows["Vault"]["type"] == "Multisig" and rows["Kraken"]["btc"] == 0.4
    assert abs(sum(r["btc"] for r in s["wallets"]) - s["holdings"]["total"]) < 1e-9
    assert abs(sum(r["pct"] for r in s["wallets"]) - 100) < 1e-6
    assert rows["Vault"]["gbp"] == 0.5 * 50000 and rows["Vault"]["scanned_at"]


# ---------- migration ----------
def test_migrate_101_and_100():
    s = {"cold_wallets": [{"id": "ab12", "label": "2-of-3 vault", "spec": MULTI, "script": "auto"},
                          {"id": "cd34", "label": "Old", "spec": ZPUB, "script": "auto"}], "xpub": A, "xpub_script": "p2wpkh"}
    ws, changed = wallets.migrate(s)
    assert changed and [w["name"] for w in ws] == ["2-of-3 vault", "Old", "Cold storage"]
    assert [w["type"] for w in map(wallets.public_view, ws)] == ["Multisig", "Single-sig", "Single-sig"]
    assert ws[0]["id"] == "ab12" and all(w["schema"] == wallets.SCHEMA for w in ws)
    again, changed2 = wallets.migrate({"cold_wallets": ws})
    assert not changed2 and again == ws


def test_migration_on_startup_and_file_modes(tmp_path):
    with db.conn() as cn:
        db.set_settings(cn, {"cold_wallets": [], "xpub": ZPUB, "strike_api_key": FAKE_STRIKE})
    main.init_storage()
    with db.conn() as cn:
        s = db.get_settings(cn)
    assert s["xpub"] == "" and s["cold_wallets"][0]["name"] == "Cold storage" and s["cold_wallets"][0]["schema"] == wallets.SCHEMA
    assert s["strike_api_key"].startswith("enc:") and main.strike_key(s) == FAKE_STRIKE
    assert stat.S_IMODE(os.stat(db.DB_PATH).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.join(db.DATA_DIR, "app-secret.key")).st_mode) == 0o600


# ---------- electrum host ----------
def test_electrum_locked_to_umbrel():
    c = client()
    s = c.get("/api/settings").json()
    assert s["electrum_effective"]["host"] == "10.21.21.10" and s["electrum_effective"]["port"] == 50001
    # old-style host field is ignored
    c.post("/api/settings", json={"electrum_host": "evil.example"}, headers=H)
    assert c.get("/api/settings").json()["electrum_effective"]["host"] == "10.21.21.10"
    # override without confirmation refused
    r = c.post("/api/settings", json={"electrum_override": True, "electrum_host_override": "electrum.example.org",
                                      "electrum_port_override": 50002}, headers=H)
    assert r.status_code == 400
    assert c.get("/api/settings").json()["electrum_effective"]["host"] == "10.21.21.10"
    r = c.post("/api/settings", json={"electrum_override": True, "electrum_host_override": "electrum.example.org",
                                      "electrum_port_override": 50002, "electrum_override_confirm": "This reveals my addresses"},
               headers=H)
    assert r.status_code == 200
    e = c.get("/api/settings").json()["electrum_effective"]
    assert (e["host"], e["port"], e["overridden"]) == ("electrum.example.org", 50002, True)
    c.post("/api/settings", json={"electrum_override": False}, headers=H)
    assert c.get("/api/settings").json()["electrum_effective"]["host"] == "10.21.21.10"


@pytest.mark.parametrize("host,port,ok", [("10.21.21.10", 50001, True), ("electrum.example.org", "50002", True),
                                          ("[::1]", 1, True), ("http://x", 1, False), ("a b", 1, False),
                                          ("host/path", 1, False), ("user@host", 1, False), ("", 1, False),
                                          ("h", 0, False), ("h", 70000, False), ("h", "x", False), ("-bad.com", 1, False)])
def test_validate_host(host, port, ok):
    if ok:
        electrum.validate_host(host, port)
    else:
        with pytest.raises(ValueError):
            electrum.validate_host(host, port)


def test_cert_fingerprint():
    fp = "AB:" * 31 + "AB"
    assert electrum.norm_fingerprint(fp) == "ab" * 32
    with pytest.raises(ValueError):
        electrum.norm_fingerprint("abcd")
    c = client()
    assert c.post("/api/settings", json={"electrum_cert_fp": "nothex"}, headers=H).status_code == 400
    assert c.post("/api/settings", json={"electrum_cert_fp": fp, "electrum_ssl": True}, headers=H).status_code == 200
    assert c.get("/api/settings").json()["electrum_effective"]["pinned"] is True


def test_tls_verification_default():
    import ssl, asyncio
    cl = electrum.Client("127.0.0.1", 1, use_ssl=True)
    assert cl.cert_fp == ""  # default: CA + hostname verification (create_default_context)
    ctx = ssl.create_default_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_disable_external_prices(monkeypatch):
    import importlib
    calls = []
    real = importlib.reload(prices)
    monkeypatch.setattr(real.httpx, "get", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(RuntimeError()))
    from app import strike
    monkeypatch.setattr(strike, "rate_btc", lambda key, cur="GBP": 40000.0 if cur == "GBP" else 52000.0)
    sp = real.spot(external=False, strike_key="k")
    assert sp["gbp"] == 40000.0 and sp["src"] == "strike" and not calls
    c = client()
    c.post("/api/settings", json={"disable_external_prices": True}, headers=H)
    assert c.post("/api/prices/refresh", json={}, headers=H).status_code == 400
