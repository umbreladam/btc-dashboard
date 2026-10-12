import pathlib, pytest
from app import importers
from app.strike import daily_record
from app.electrum import derive

EX = pathlib.Path(__file__).parent.parent / "examples"


def test_coinbase():
    t = (EX / "coinbase_example.csv").read_text()
    assert importers.detect(t) == "coinbase"
    rows, errs = importers.parse_coinbase(t)
    assert [r["kind"] for r in rows] == ["buy", "buy", "sell", "transfer"]
    assert rows[0]["gbp"] == pytest.approx(162.40) and rows[0]["btc"] == pytest.approx(0.005)
    assert rows[2]["btc"] == pytest.approx(0.003) and rows[2]["gbp"] == pytest.approx(88.65)
    assert rows[3]["to_wallet"] == "cold"


def test_coinbase_legacy_format():
    t = ("Timestamp,Transaction Type,Asset,Quantity Transacted,Spot Price Currency,Spot Price at Transaction,Subtotal,"
         "Total (inclusive of fees and/or spread),Fees and/or Spread,Notes\n"
         "2021-05-01T10:00:00Z,Buy,BTC,0.001,GBP,40000,40,41.50,1.50,\n")
    rows, _ = importers.parse_coinbase(t)
    assert rows[0]["gbp"] == pytest.approx(41.5)


def test_kraken_ledgers():
    t = (EX / "kraken_ledgers_example.csv").read_text()
    assert importers.detect(t) == "kraken"
    rows, errs = importers.parse_kraken(t)
    assert [r["kind"] for r in rows] == ["buy", "buy", "transfer"]
    assert rows[0]["gbp"] == pytest.approx(200.8) and rows[0]["btc"] == pytest.approx(0.0055)
    assert rows[2]["btc"] == pytest.approx(0.0104) and rows[2]["fee_btc"] == pytest.approx(0.0001)


def test_kraken_trades():
    t = ('"txid","ordertxid","pair","time","type","ordertype","price","cost","fee","vol","margin","misc","ledgers"\n'
         '"T1","O1","XXBTZGBP","2024-03-01 10:00:00.1234","buy","market",50000,100,0.4,0.002,0,"","L1,L2"\n')
    rows, _ = importers.parse_kraken(t)
    assert rows[0]["kind"] == "buy" and rows[0]["gbp"] == pytest.approx(100.4)


def test_strike():
    t = (EX / "strike_example.csv").read_text()
    assert importers.detect(t) == "strike"
    rows, errs = importers.parse_strike(t)
    assert [r["kind"] for r in rows] == ["buy", "buy", "transfer"], errs
    assert rows[0]["gbp"] == pytest.approx(20) and rows[2]["fee_btc"] == pytest.approx(0.000005)


def test_strike_daily_logic():
    assert daily_record(None, 0.001, 60000, 20) == []
    up = daily_record(0.001, 0.00133, 60000, 20)
    assert up[0]["btc"] == pytest.approx(0.00033) and up[0]["gbp"] == 20 and up[0]["estimate"] == 0
    down = daily_record(0.0099, 0.0001, 50000, 20)
    assert down[0]["estimate"] == 1 and down[0]["btc"] == pytest.approx(0.0004)
    assert down[1]["kind"] == "transfer" and down[1]["btc"] == pytest.approx(0.0099 + 0.0004 - 0.0001)


def test_xpub_derivation_bip84_vector():
    zpub = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
    a = derive(zpub, 0, 0, 2)
    assert a[0][1] == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    assert a[1][1] == "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"
    assert derive(zpub, 1, 0, 1)[0][1] == "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
