"""Satoshi native CSV format (1.0.9) and Strike fee fix. Synthetic data only."""
from datetime import date
from app import importers, portfolio, cgt

NATIVE = """timestamp_utc,type,btc,gbp,fee_gbp,fee_btc,wallet,to_wallet,source,ext_id,note,estimate
2024-01-01T10:00:00Z,buy,1.00000000,20000.00,10.00,,coinbase,,coinbase,b1,,
2024-01-02T10:00:00Z,income,0.10000000,2000.00,,,strike,,strike,i1,,
2024-01-03T10:00:00Z,acquisition,0.10000000,2100.00,,,kraken,,kraken,a1,,1
2024-03-01T10:00:00Z,transfer,0.50000000,,,0.00010000,coinbase,cold,coinbase,t1,,
2024-04-01T10:00:00Z,sell,0.10000000,3000.00,5.00,,coinbase,,coinbase,s1,,
2024-04-02T10:00:00Z,spend,0.01000000,300.00,,,strike,,strike,sp1,,
2024-04-03T10:00:00Z,gift,0.01000000,310.00,,,strike,,strike,g1,,
2024-04-04T10:00:00Z,disposal,0.01000000,320.00,,,kraken,,kraken,d1,,
2024-04-05T10:00:00Z,fee,0.00100000,30.00,,,kraken,,kraken,f1,,
2024-06-21T10:00:00Z,lost,0.20000000,,,,cold,,manual,l1,boating accident,
"""


def _rows():
    assert importers.detect(NATIVE) == "native"
    rows, errs = importers.parse_native(NATIVE)
    assert not errs and len(rows) == 10
    for i, r in enumerate(rows, 1):
        r["id"] = i
    return rows


def test_native_types_map():
    r = _rows()
    assert [x["kind"] for x in r] == ["buy"] * 3 + ["transfer"] + ["sell"] * 5 + ["transfer"]
    assert [x["note"].split("[")[-1] for x in r][:3] == ["buy]", "income]", "acquisition]"]
    assert r[2]["estimate"] == 1 and r[0]["estimate"] == 0
    assert r[3]["to_wallet"] == "cold" and r[3]["fee_btc"] == 0.0001
    lost = r[9]
    assert lost["to_wallet"] == "lost" and lost["btc"] == 0 and lost["fee_btc"] == 0.2 and "[lost]" in lost["note"]


def test_native_pool_and_wallets():
    r = _rows()
    res = cgt.match(portfolio.events(r))
    # 1.2 acquired, 0.131 disposed, 0.0001 network fee + 0.2 lost leave the pool with cost kept
    assert res.pool_btc == __import__("pytest").approx(1.2 - 0.131 - 0.0001 - 0.2)
    assert res.pool_cost == __import__("pytest").approx(24100 * (1 - 0.131 / 1.1999))
    assert len(res.disposals) == 5   # lost and transfer are not disposals
    assert sum(d.proceeds for d in res.disposals) == __import__("pytest").approx(3960)
    w = portfolio.wallets(r)
    assert w == {"coinbase": 0.3999, "cold": 0.3, "strike": 0.08, "kraken": 0.089}
    assert "lost" not in w


def test_native_errors():
    bad = NATIVE.splitlines()[0] + "\n2024-01-01T00:00:00Z,swap,1,1,,,a,,b,x,,\n2024-01-01T00:00:00Z,buy,0,1,,,a,,b,y,,\n2024-01-01T00:00:00Z,buy,1,1,,,A!,,b,z,,\n"
    rows, errs = importers.parse_native(bad)
    assert not rows and len(errs) == 3


def test_strike_buy_fee_not_added_twice():
    t = ("Reference,Date & Time (UTC),Transaction Type,Amount GBP,Fee GBP,Amount BTC,Fee BTC\n"
         "r1,Jan 02 2026 15:18:47,Purchase,-20.00,0.30,0.00040000,\n"
         "r2,Jan 03 2026 15:18:47,Sell,50.00,0.50,-0.00100000,\n")
    rows, errs = importers.parse_strike(t)
    assert not errs
    assert rows[0]["kind"] == "buy" and rows[0]["gbp"] == 20.0 and rows[0]["fee_gbp"] == 0.3
    assert rows[1]["kind"] == "sell" and rows[1]["gbp"] == 49.5
