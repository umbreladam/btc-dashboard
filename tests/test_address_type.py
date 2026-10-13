"""Single-sig address-type auto-detection (1.0.8). Keys are the public BIP44/49/84/86 test vectors
(mnemonic "abandon abandon ... about"), never real wallets. Electrum is an in-process fake server."""
import asyncio, json
import pytest
from fastapi.testclient import TestClient
from app import electrum, wallets, main, db, prices
from app.electrum import derive, scan

X44 = "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj"
Y49 = "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrArf3zbeJJJUZPf663zsP"
X84 = "xpub6CatWdiZiodmUeTDp8LT5or8nmbKNcuyvz7WyksVFkKB4RHwCD3XyuvPEbvqAQY3rAPshWcMLoP2fMFMKHPJ4ZeZXYVUhLv1VMrjPC7PW6V"  # BIP84 account key, plain xpub prefix
X86 = "xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9QyrLprQ3VCECoY49yfdDEHGCtMMj92pReUsQ"


def test_bip_vectors_per_type():
    assert derive(X44, 0, 0, 1, "p2pkh")[0][1] == "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"            # BIP44
    assert derive(Y49, 0, 0, 1, "auto")[0][1] == "37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf"             # BIP49 (ypub -> nested)
    assert derive(X84, 0, 0, 1, "p2wpkh")[0][1] == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"   # BIP84
    assert derive(X86, 0, 0, 1, "p2tr")[0][1] == "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"  # BIP86
    # plain xpub with nothing detected yet defaults to native SegWit, not legacy
    assert derive(X84, 0, 0, 1, "auto")[0][1] == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    with pytest.raises(ValueError):
        derive(X84, 0, 0, 1, "p2bogus")


def fake_server(funded, seen=None):
    async def handle(r, w):
        while line := await r.readline():
            m = json.loads(line)
            p = m["params"]
            if seen is not None and p:
                seen.append(p[0])
            res = ["fake", "1.4"] if m["method"] == "server.version" else \
                ([{"tx_hash": "00", "height": 1}] if p[0] in funded else []) if m["method"].endswith("get_history") else \
                {"confirmed": funded.get(p[0], 0), "unconfirmed": 0}
            w.write((json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}) + "\n").encode())
            await w.drain()
    return handle


def run_scan(xpub, funded, override="auto", seen=None):
    async def main_():
        srv = await asyncio.start_server(fake_server(funded, seen), "127.0.0.1", 0)
        try:
            return await asyncio.wait_for(scan(xpub, "127.0.0.1", srv.sockets[0].getsockname()[1], gap=20, override=override), 30)
        finally:
            srv.close()
    return asyncio.run(main_())


def sh(x, st, chain=0, i=0):
    return derive(x, chain, i, 1, st)[0][2]


def test_auto_finds_legacy_history_for_plain_xpub():
    r = run_scan(X44, {sh(X44, "p2pkh", 0, 2): 120000, sh(X44, "p2pkh", 1, 0): 5000})
    assert r["script_types"] == ["p2pkh"] and round(r["balance"], 8) == 0.00125
    assert all(a["script"] == "p2pkh" for a in r["used_addresses"])


def test_auto_finds_segwit_and_taproot():
    assert run_scan(X84, {sh(X84, "p2wpkh", 0, 0): 10000})["script_types"] == ["p2wpkh"]
    r = run_scan(X86, {sh(X86, "p2tr", 0, 5): 7000})
    assert r["script_types"] == ["p2tr"] and round(r["balance"], 8) == 0.00007


def test_multiple_types_are_summed_primary_is_largest():
    funded = {sh(X84, "p2pkh", 0, 0): 1000, sh(X84, "p2wpkh", 0, 1): 90000, sh(X84, "p2sh-p2wpkh", 1, 3): 9000}
    r = run_scan(X84, funded)
    assert r["script_types"] == ["p2wpkh", "p2sh-p2wpkh", "p2pkh"] and round(r["balance"], 8) == 0.001


def test_nothing_found_reports_no_types_and_zero():
    r = run_scan(X84, {})
    assert r["script_types"] == [] and r["balance"] == 0
    w = {"input": "xpub", "spec": X84, "script": "auto", "detected_scripts": []}
    assert wallets.effective_script(w) == "p2wpkh" and wallets.first_receive(w).startswith("bc1q")


def test_explicit_type_scans_only_that_type():
    seen = []
    r = run_scan(X84, {sh(X84, "p2wpkh", 0, 0): 10000}, override="p2pkh", seen=seen)
    assert r["balance"] == 0 and r["script_types"] == ["p2pkh"]
    assert sh(X84, "p2wpkh", 0, 0) not in seen


# ---------- API: migration, scan persistence, edit ----------
PROXY = "10.21.0.1"
H = {"X-Satoshi-Request": "1"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(main, "TRUSTED_PEERS", {PROXY})
    monkeypatch.setattr(prices, "spot", lambda external=True, strike_key="": {"gbp": 50000.0, "usd": 65000.0, "src": "test"})
    main.init_storage()
    return TestClient(main.app, client=(PROXY, 50000))


def test_schema2_plain_xpub_wallet_migrates_to_auto_and_fixes_itself(client, monkeypatch):
    # a 1.0.2-1.0.7 wallet: plain xpub, no explicit type, scanned as legacy p2pkh -> 0 balance
    old = {"id": "w1", "name": "Savings", "description": "", "script": "auto", "type_override": None, "schema": 2,
           "input": "xpub", "spec": X84, "detected_type": "Single-sig", "policy": "single-sig", "fingerprints": [],
           "script_type": "p2pkh", "network": "mainnet"}
    legacy_choice = dict(old, id="w2", spec=X44, script="p2pkh")   # user explicitly chose Legacy: kept
    with db.conn() as c:
        db.set_settings(c, {"cold_wallets": [old, legacy_choice]})
    main.init_storage()
    ws = {w["id"]: w for w in client.get("/api/cold/wallets").json()}
    assert ws["w1"]["address_type"] == "auto" and ws["w1"]["first_receive"] == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    assert ws["w2"]["address_type"] == "p2pkh" and ws["w2"]["first_receive"] == "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"

    funded = {sh(X84, "p2wpkh", 0, 0): 250000}

    # run the real scanner against a fake server instead of Electrs
    async def real_against_fake(spec, host, port, use_ssl, gap, override, cert_fp=""):
        srv = await asyncio.start_server(fake_server(funded), "127.0.0.1", 0)
        try:
            return await scan(spec, "127.0.0.1", srv.sockets[0].getsockname()[1], False, gap, override)
        finally:
            srv.close()
    monkeypatch.setattr(electrum, "scan", real_against_fake)
    monkeypatch.setattr(main, "electrum_target", lambda s: ("electrs.local", 50001, False, ""))
    r = client.post("/api/cold/scan", json={}, headers=H).json()
    assert {w["id"]: w["balance"] for w in r["wallets"]} == {"w1": 0.0025, "w2": 0.0}
    ws = {w["id"]: w for w in client.get("/api/cold/wallets").json()}
    assert ws["w1"]["detected_scripts"] == ["p2wpkh"] and "auto-detected" in ws["w1"]["script_type"]
    log = json.dumps(client.get("/api/log").json())
    assert "bc1q" not in log and "xpub" not in log and "1Lq" not in log      # no addresses/keys in logs

    # edit address type: saved, cache cleared so the next refresh rescans with it
    e = client.put("/api/cold/wallets/w1", json={"address_type": "p2tr"}, headers=H).json()["wallet"]
    assert e["address_type"] == "p2tr" and e["first_receive"].startswith("bc1p")
    with db.conn() as c:
        assert db.get_settings(c)["cold_balance_cache"] is None
    assert client.put("/api/cold/wallets/w1", json={"address_type": "p2bogus"}, headers=H).status_code == 400
    assert client.put("/api/cold/wallets/w1", json={"address_type": "auto"}, headers=H).json()["wallet"]["address_type"] == "auto"
