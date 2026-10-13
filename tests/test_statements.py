"""Statement importers (PDF + CSV). All fixtures are SYNTHETIC: invented names, ids and amounts that only mimic
the layout of the real exports. Never put real statement data here."""
import base64
import pytest
from fastapi.testclient import TestClient
from app import statements, importers, main, db, prices


def make_pdf(lines):
    """Tiny single-page PDF with one text line per entry (Courier, so column spacing survives extraction)."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = ["BT /F1 7 Tf 9 TL 20 820 Td"] + [f"({esc(l)}) Tj T*" for l in lines] + ["ET"]
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 900 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    return bytes(out)


def parse(lines, source="auto"):
    return statements.parse_pdf_b64(base64.b64encode(make_pdf(lines)).decode(), source)


CB_REPORT = [
    "Transaction report for Test User                     Date range",
    "Timestamp        Transaction    Asset Quantity           Spot Price   Spot Price at    Subtot Total (inclusive of fees Notes",
    "2019-02-         Buy            BTC    0.01000000        GBP          3000.00          \xa329.00 \xa330.00   Bought 0.01000000 BTC for \xa330.00 GBP",
    "05T10:00:00Z",
    "2019-03-         Sell           BTC    0.00500000        GBP          3200.00          \xa316.00       \xa315.50   Sold 0.00500000 BTC for \xa315.50 GBP",
    "06T11:00:00Z",
    "2019-04-         Receive        BTC    0.00100000        GBP          4000.00                                Received 0.00100000 BTC from SomePool",
    "07T12:00:00Z",
    "2019-05-         Send           BTC    0.00200000        GBP          5000.00                                Sent 0.00200000 BTC to",
    "08T13:00:00Z                                                                    bc1qexampleexampleexample",
    "2019-06-         Convert        BTC    0.00100000        GBP          6000.00          \xa35.90       \xa36.00    Converted 0.00100000 BTC to 1.5 FAKECOIN",
    "09T14:00:00Z",
    "2022-08-         Advanced       BTC    0.00300000        GBP          18000.00         \xa353.70 \xa354.00   Bought 0.00300000 BTC for \xa354.00 GBP on BTC-GBP",
    "10T15:00:00Z   Trade Buy 4",
    "2022-08-         Advanced       BTC    2.5e-07           GBP          18000.00         \xa30.00 \xa30.00     Bought 0.00000025 BTC for \xa30.00 GBP on BTC-USDT",
    "10T15:00:01Z   Trade Buy",
    "2022-09-         Advanced       ETH    0.10000000        GBP          1300.00          \xa313.00 \xa313.10   Bought 0.1 ETH for \xa313.10 GBP on ETH-GBP",
    "11T16:00:00Z   Trade Buy",
]


def test_coinbase_report_pdf():
    src, rows, errs = parse(CB_REPORT)
    assert src == "coinbase-report" and errs == []
    k = [(r["kind"], r["btc"]) for r in rows]
    assert k == [("buy", 0.01), ("sell", 0.005), ("transfer", 0.001), ("transfer", 0.002), ("sell", 0.001), ("buy", 0.003), ("buy", 2.5e-07)]
    buy, sell, rec, send, conv, adv = rows[:6]
    assert buy["gbp"] == 30.0 and buy["fee_gbp"] == pytest.approx(1.0) and buy["ts"].startswith("2019-02-05T10:00:00")
    assert sell["gbp"] == 15.5 and sell["fee_gbp"] == pytest.approx(0.5)
    assert rec["to_wallet"] == "coinbase" and send["to_wallet"] == "cold"      # transfers: not disposals
    assert "disposal" in conv["note"] and conv["gbp"] == 6.0                  # BTC -> altcoin convert is a disposal
    assert adv["gbp"] == 54.0 and "BTC-USDT" in rows[6]["note"]


PRO_FILLS = [
    "Coinbase Pro - Fills Statement",
    "FILLS",
    " Portfolio Trade Id Product     Side Date                  Displayed              Qty.         Price          Fee                Total",
    " default   111       BTC-GBP    BUY 2021-01-02 03:04:05    Y                 0.01000000   20000.00    1.00 GBP    - 201.00 GBP",
    " default   112       BTC-GBP    SELL 2021-01-03 03:04:05   Y                 0.00500000   22000.00    0.55 GBP    + 109.45 GBP",
    " default   113       BTC-USDT   BUY 2021-01-04 03:04:05    Y                 0.00100000   30000.00    0.15 USDT   - 30.15 USDT",
    " default   114       FAKE-BTC   SELL 2021-01-05 03:04:05   Y                 5.00000000   0.0001      1e-06 BTC   + 0.000499 BTC",
    " default   115       FAKE-GBP   BUY 2021-01-06 03:04:05    Y                 5.00000000   2.00        0.05 GBP    - 10.05 GBP",
]

PRO_ACCOUNT = [
    "Coinbase Pro - Account Statement",
    "ACCOUNT HISTORY",
    " Portfolio          Type         Time                                 Amount                  Balance                 ID",
    " default            Deposit      2021-01-01 00:00:00 UTC    0.0200000000000000 BTC     0.0200000000000000 BTC         aaaa1111",
    " default            Match        2021-01-02 03:04:05 UTC    -200.0000000000000000 GBP  0.0000000000000000 GBP         111",
    " default            Match        2021-01-02 03:04:05 UTC    0.0100000000000000 BTC     0.0300000000000000 BTC         111",
    " default            Fee          2021-01-02 03:04:05 UTC    -1.0000000000000000 GBP    -1.0000000000000000 GBP        111",
    " default            Match        2021-01-03 03:04:06 UTC    -0.0050000000000000 BTC    0.0250000000000000 BTC         112",
    " default            Match        2021-01-03 03:04:06 UTC    110.0000000000000000 GBP   109.0000000000000000 GBP       112",
    " default            Fee          2021-01-03 03:04:06 UTC    -0.5500000000000000 GBP    108.4500000000000000 GBP       112",
    " default            Match        2021-01-04 03:04:05 UTC    0.0010000000000000 BTC     0.0260000000000000 BTC         113",
    " default            Match        2021-01-04 03:04:05 UTC    -30.0000000000000000 USDT  0.0000000000000000 USDT        113",
    " default            Withdrawal   2021-01-09 00:00:00 UTC    -0.0260000000000000 BTC    0.0000000000000000 BTC         bbbb2222",
]


def test_pro_fills_pdf():
    src, rows, errs = parse(PRO_FILLS)
    assert src == "coinbase-pro-fills"
    assert [(r["kind"], r["btc"], r["gbp"], r["fee_gbp"]) for r in rows] == [("buy", 0.01, 201.0, 1.0), ("sell", 0.005, 109.45, 0.55)]
    assert rows[0]["ext_id"] == "BTC-GBP:111"
    assert len(errs) == 2 and "USDT" in errs[0] and "FAKE traded against BTC" in errs[1]   # FAKE-GBP silently ignored


def test_pro_account_pdf_matches_fills():
    src, rows, errs = parse(PRO_ACCOUNT)
    assert src == "coinbase-pro-account"
    tr = [r for r in rows if r["kind"] == "transfer"]
    assert [(r["wallet"], r["to_wallet"], r["btc"]) for r in tr] == [("other", "coinbase", 0.02), ("coinbase", "other", 0.026)]
    trades = {r["ext_id"]: r for r in rows if r["kind"] != "transfer"}
    _, fills, _ = parse(PRO_FILLS)
    for f in fills:   # same ext_id + same amounts as the fills statement -> deduped when both are imported
        a = trades[f["ext_id"]]
        assert (a["kind"], a["btc"], a["fee_gbp"]) == (f["kind"], f["btc"], f["fee_gbp"]) and a["gbp"] == pytest.approx(f["gbp"])
    assert len(errs) == 1 and "USDT" in errs[0]


CC = [
    "Support: support@coincorner.com",
    "GBP Account Statement",
    "  Date              Reference         Type              Details                                Amount      Balance",
    "  05 Nov 2021       CC1000002         Trade             Bought 0.00100000 BTC @ \xa330,000.00       -\xa330.00      \xa30.00",
    "  05 Nov 2021       CC1000001         Bank deposit                                                \xa330.00     \xa330.00",
    "  06 Nov 2021       CC1000003         Trade             Sold 0.00050000 BTC @ \xa331,000.00         \xa315.50      \xa315.50",
]


def test_coincorner_pdf():
    src, rows, errs = parse(CC)
    assert src == "coincorner" and errs == []
    assert [(r["kind"], r["btc"], r["gbp"], r["ext_id"]) for r in rows] == [("buy", 0.001, 30.0, "CC1000002"), ("sell", 0.0005, 15.5, "CC1000003")]
    assert rows[0]["ts"].startswith("2021-11-05T12:00:00")


def test_csv_variants():
    fills = "portfolio,trade id,product,side,created at,size,size unit,price,fee,total,price/fee/total unit\n" \
            "default,111,BTC-GBP,BUY,2021-01-02T03:04:05.000Z,0.01,BTC,20000,1,-201,GBP\n" \
            "default,9,FAKE-GBP,BUY,2021-01-02T03:04:05.000Z,1,FAKE,2,0.01,-2.01,GBP\n"
    assert statements.detect_csv(fills) == "coinbase-pro-fills"
    r, e = statements.parse_pro_fills_csv(fills)
    assert [(x["kind"], x["gbp"], x["ext_id"]) for x in r] == [("buy", 201.0, "BTC-GBP:111")] and not e
    acct = "portfolio,type,time,amount,balance,amount/balance unit,transfer id,trade id,order id\n" \
           "default,match,2021-01-02T03:04:05Z,-200,0,GBP,,111,o1\ndefault,match,2021-01-02T03:04:05Z,0.01,0.01,BTC,,111,o1\n" \
           "default,fee,2021-01-02T03:04:05Z,-1,-1,GBP,,111,o1\ndefault,withdrawal,2021-01-03T00:00:00Z,-0.01,0,BTC,t9,,\n"
    assert statements.detect_csv(acct) == "coinbase-pro-account"
    r, e = statements.parse_pro_account_csv(acct)
    assert [(x["kind"], x["btc"]) for x in r] == [("transfer", 0.01), ("buy", 0.01)] and r[1]["gbp"] == 201.0
    cc = "Date,Reference,Type,Details,Amount,Balance\n05 Nov 2021,CC1,Trade,Bought 0.001 BTC @ \xa330000.00,-\xa330.00,\xa30.00\n"
    assert statements.detect_csv(cc) == "coincorner"
    assert statements.parse_coincorner_csv(cc)[0][0]["gbp"] == 30.0


def test_unknown_pdf_rejected():
    with pytest.raises(ValueError):
        parse(["Some other bank statement", "nothing to see"])
    with pytest.raises(ValueError):
        statements.parse_pdf_b64(base64.b64encode(b"not a pdf").decode())


# ---------- API: preview + dedupe on re-import (and across PDF/CSV) ----------
@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setattr(main, "TRUSTED_PEERS", {"10.21.0.1"})
    monkeypatch.setattr(prices, "spot", lambda external=True, strike_key="": {"gbp": 50000.0, "usd": 65000.0, "src": "test"})
    main.init_storage()
    return TestClient(main.app, client=("10.21.0.1", 50000))


H = {"X-Satoshi-Request": "1"}


def test_preview_commit_and_dedupe(client):
    pdf = base64.b64encode(make_pdf(PRO_FILLS)).decode()
    p = client.post("/api/import/preview", json={"pdf_b64": pdf, "filename": "x.pdf"}, headers=H).json()
    assert p["source"] == "coinbase-pro-fills" and p["stats"]["buys"] == 1 and p["stats"]["duplicates"] == 0
    assert client.post("/api/import/commit", json={"rows": p["rows"]}, headers=H).json()["inserted"] == 2
    again = client.post("/api/import/preview", json={"pdf_b64": pdf}, headers=H).json()
    assert again["stats"]["duplicates"] == 2
    acct = client.post("/api/import/preview", json={"pdf_b64": base64.b64encode(make_pdf(PRO_ACCOUNT)).decode()}, headers=H).json()
    assert acct["stats"]["duplicates"] == 2 and acct["stats"]["transfers"] == 2    # fills already imported -> trades skipped
    # same Coinbase trade from the PDF report and from the CSV export (which has an ID column) is a duplicate
    rep = client.post("/api/import/preview", json={"pdf_b64": base64.b64encode(make_pdf(CB_REPORT[:4])).decode()}, headers=H).json()
    client.post("/api/import/commit", json={"rows": rep["rows"]}, headers=H)
    csvt = "Timestamp,Transaction Type,Asset,Quantity Transacted,Spot Price Currency,Spot Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),Fees and/or Spread,Notes,ID\n" \
           "2019-02-05T10:00:00Z,Buy,BTC,0.01,GBP,3000,29,30,1,x,abc123\n"
    assert client.post("/api/import/preview", json={"text": csvt}, headers=H).json()["stats"]["duplicates"] == 1


def test_bad_pdf_is_400(client):
    r = client.post("/api/import/preview", json={"pdf_b64": base64.b64encode(b"%PDF-1.4 garbage").decode()}, headers=H)
    assert r.status_code == 400


def test_identical_rows_in_one_file_are_not_collapsed(client):
    twin = CB_REPORT[:2] + CB_REPORT[2:4] * 2   # two genuinely separate identical buys in the same second
    p = client.post("/api/import/preview", json={"pdf_b64": base64.b64encode(make_pdf(twin)).decode()}, headers=H).json()
    assert p["stats"]["rows"] == 2 and p["stats"]["duplicates"] == 0
    assert client.post("/api/import/commit", json={"rows": p["rows"]}, headers=H).json()["inserted"] == 2
    again = client.post("/api/import/preview", json={"pdf_b64": base64.b64encode(make_pdf(twin)).decode()}, headers=H).json()
    assert again["stats"]["duplicates"] == 2
