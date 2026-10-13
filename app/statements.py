"""Exchange statements: PDF (via pdfplumber) and the matching CSV exports.

Supported:
  * Coinbase "Transaction report" PDF (same rows as the Coinbase transaction-history CSV)
  * Coinbase Pro "Fills Statement" PDF / fills.csv           (trades; BTC-GBP pairs imported)
  * Coinbase Pro "Account Statement" PDF / account.csv       (Match+Fee legs grouped per trade id, BTC deposits/withdrawals)
  * CoinCorner "GBP Account Statement" PDF / CSV             (Bought/Sold ... BTC @ £price trades)
Only BTC rows are returned; altcoin rows are ignored. BTC traded against something other than GBP is skipped with an
explanation (no reliable GBP value). Coinbase Pro fills and account statement share ext_ids (pair:trade id), so
importing both does not double count.
"""
import base64, csv, io, re
from collections import OrderedDict
from datetime import datetime, timezone
from .importers import coinbase_row, _date, _num, _find_header, _get

FIAT = {"GBP", "USD", "EUR"}
STABLE = {"USDT", "USDC", "DAI", "BUSD"}


# ---------------- PDF text ----------------
def pdf_text(data: bytes) -> str:
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if len(pdf.pages) > 500:
            raise ValueError("PDF has too many pages")
        for page in pdf.pages:
            out.append(page.extract_text(layout=True) or "")
    return "\n".join(out)


def lines(text):
    return [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]


def detect_pdf(text):
    h = re.sub(r"\s+", " ", text[:6000]).lower()
    if "coincorner" in h or "coincorner" in text.lower()[-3000:]:
        return "coincorner"
    if "trade id product" in h or "fills statement" in h or re.search(r"\bfills\b", h) and "trade id" in h:
        return "coinbase-pro-fills"
    if "account history" in h and ("portfolio type time" in h or "coinbase pro" in h):
        return "coinbase-pro-account"
    if "transaction report for" in h or "quantity transacted" in h:
        return "coinbase-report"
    return None


# ---------------- Coinbase transaction report (PDF) ----------------
_CB1 = re.compile(r"^(\d{4}-\d{2}-)\s+(\S+)\s+([A-Z0-9]+)\s+([\d.]+(?:e-?\d+)?)\s+([A-Z]{3})\s+([\d.,]+)\s*(.*)$", re.I)
_CB2 = re.compile(r"^(\d{2}T\d{2}:\d{2}:\d{2}Z)\s*(.*)$")
_AMT = re.compile(r"^(-?[£$€]-?[\d,]+(?:\.\d+)?)\s*")


def parse_coinbase_report(text):
    rows, errs, ls = [], [], lines(text)
    for i, l in enumerate(ls):
        m = _CB1.match(l)
        if not m:
            continue
        nxt = next((x for x in ls[i + 1:i + 4] if x), "")
        m2 = _CB2.match(nxt)
        if not m2:
            errs.append(f"report line {i + 1}: could not read timestamp - skipped"); continue
        ym, typ, asset, qty, ccy, spot, rest = m.groups()
        if asset.upper() != "BTC":
            continue
        cont = re.match(r"(Trade (?:Buy|Sell))\b", m2.group(2))
        if cont:
            typ = f"{typ} {cont.group(1)}"
        elif typ.lower() == "advanced":   # wrapped type column lost: infer from the notes
            typ += " trade " + ("buy" if re.search(r"\bBought\b", rest) else "sell" if re.search(r"\bSold\b", rest) else "?")
        amts = []
        while (a := _AMT.match(rest)):
            amts.append(abs(_num(a.group(1)))); rest = rest[a.end():]
        sub, total = (amts + [0.0, 0.0])[:2] if len(amts) >= 2 else (0.0, amts[0] if amts else 0.0)
        try:
            ts = _date(ym + m2.group(1))
            t, err = coinbase_row(typ, abs(float(qty)), ccy.upper(), sub, total, rest.strip(), ts)
        except Exception as e:
            errs.append(f"report line {i + 1}: {e}"); continue
        if err:
            errs.append(f"report {ts[:19]}: {err}"); continue
        rows.append(t)
    return rows, errs


# ---------------- Coinbase Pro fills ----------------
def _pro_trade(product, side, tid, ts, size, fee, total, fee_ccy, errs):
    base, _, quote = product.upper().partition("-")
    if base != "BTC":
        if quote == "BTC":
            errs.append(f"fill {product} {tid} {ts[:19]}: {base} traded against BTC ({side}) - BTC moved but not imported (altcoin trade, add manually if needed)")
        return None
    if quote not in FIAT:
        errs.append(f"fill {product} {tid} {ts[:19]}: BTC {side.lower()} against {quote} - no GBP value, skipped (add manually with the GBP value)")
        return None
    t = dict(ts=ts, source="coinbase-pro", wallet="coinbase", btc=abs(size), ext_id=f"{product.upper()}:{tid}", fee_gbp=abs(fee))
    if side.upper() == "BUY":
        t.update(kind="buy", gbp=abs(total))
    else:
        t.update(kind="sell", gbp=abs(total))
    if quote != "GBP":
        t["note"] = f"[WARNING: priced in {quote}, not GBP]"
    return t


_FILL = re.compile(r"^(\S+)\s+(\d+)\s+([A-Z0-9]+-[A-Z0-9]+)\s+(BUY|SELL)\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\S+\s+"
                   r"([\d.]+(?:e-?\d+)?)\s+([\d.]+(?:e-?\d+)?)\s+([\d.]+(?:e-?\d+)?)\s+([A-Z]+)\s+([+-])\s*([\d.]+(?:e-?\d+)?)\s+([A-Z]+)$")


def parse_pro_fills_pdf(text):
    rows, errs = [], []
    for l in lines(text):
        m = _FILL.match(l)
        if not m:
            continue
        _, tid, product, side, ts, size, price, fee, fccy, sign, total, tccy = m.groups()
        t = _pro_trade(product, side, tid, _date(ts), float(size), float(fee), float(total), fccy, errs)
        if t:
            rows.append(t)
    return rows, errs


def parse_pro_fills_csv(text):
    body = _find_header(text, ["tradeid", "product", "side", "size"])
    if body is None:
        return [], ["Not a Coinbase Pro fills.csv (portfolio,trade id,product,side,created at,size,...)"]
    rows, errs = [], []
    for i, r in enumerate(csv.DictReader(io.StringIO(body)), start=2):
        try:
            t = _pro_trade(_get(r, "product") or "", _get(r, "side") or "", _get(r, "trade id"), _date(_get(r, "created at")),
                           _num(_get(r, "size")), _num(_get(r, "fee")), _num(_get(r, "total")), "", errs)
            if t:
                rows.append(t)
        except Exception as e:
            errs.append(f"line {i}: {e}")
    return rows, errs


# ---------------- Coinbase Pro account statement ----------------
_ACC = re.compile(r"^(\S+)\s+(Deposit|Withdrawal|Match|Fee|Conversion|Rebate)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?: UTC)?\s+"
                  r"(-?[\d.]+(?:e-?\d+)?)\s+([A-Z0-9]+)\s+(-?[\d.]+(?:e-?\d+)?)\s+([A-Z0-9]+)\s+(\S+)$")


def _pro_account(entries):
    """entries: dicts type, ts, amount, unit, id (trade id for match/fee, transfer id for deposit/withdrawal)."""
    rows, errs, groups = [], [], OrderedDict()
    for e in entries:
        typ = e["type"].lower()
        if typ in ("match", "fee"):
            groups.setdefault(e["id"], []).append(e)
        elif e["unit"] == "BTC" and typ in ("deposit", "withdrawal"):
            amt = e["amount"]
            t = dict(ts=e["ts"], source="coinbase-pro", btc=abs(amt), ext_id=f"transfer:{e['id']}", kind="transfer")
            if amt > 0:
                t.update(wallet="other", to_wallet="coinbase", note="Coinbase Pro deposit (incoming)")
            else:
                t.update(wallet="coinbase", to_wallet="other", note="Coinbase Pro withdrawal")
            rows.append(t)
    for tid, legs in groups.items():
        btc = [l for l in legs if l["unit"] == "BTC" and l["type"].lower() == "match"]
        if not btc:
            continue
        others = {l["unit"] for l in legs if l["type"].lower() == "match"} - {"BTC"}
        q = sum(l["amount"] for l in btc)
        if len(others) != 1 or not others <= FIAT:
            errs.append(f"account trade {tid} {btc[0]['ts'][:19]}: BTC {'buy' if q > 0 else 'sell'} against {'/'.join(sorted(others)) or '?'} - "
                        "no GBP value, skipped (add manually with the GBP value)")
            continue
        quote = others.pop()
        fiat = sum(l["amount"] for l in legs if l["unit"] == quote and l["type"].lower() == "match")
        fee = sum(abs(l["amount"]) for l in legs if l["type"].lower() == "fee" and l["unit"] == quote)
        fee_btc = sum(abs(l["amount"]) for l in legs if l["type"].lower() == "fee" and l["unit"] == "BTC")
        t = dict(ts=btc[0]["ts"], source="coinbase-pro", wallet="coinbase", ext_id=f"BTC-{quote}:{tid}", fee_gbp=fee)
        if q > 0:
            t.update(kind="buy", btc=q - fee_btc, gbp=abs(fiat) + fee)
        else:
            t.update(kind="sell", btc=abs(q) + fee_btc, gbp=fiat - fee)
        if quote != "GBP":
            t["note"] = f"[WARNING: priced in {quote}, not GBP]"
        rows.append(t)
    return rows, errs


def parse_pro_account_pdf(text):
    ents = []
    for l in lines(text):
        m = _ACC.match(l)
        if m:
            _, typ, ts, amt, unit, bal, bunit, xid = m.groups()
            ents.append(dict(type=typ, ts=_date(ts), amount=float(amt), unit=unit.upper(), id=xid))
    return _pro_account(ents)


def parse_pro_account_csv(text):
    body = _find_header(text, ["type", "time", "amount", "balance", "unit"])
    if body is None:
        return [], ["Not a Coinbase Pro account.csv (portfolio,type,time,amount,balance,amount/balance unit,...)"]
    ents = []
    for r in csv.DictReader(io.StringIO(body)):
        typ = (_get(r, "type") or "").strip()
        xid = _get(r, "trade id") if typ.lower() in ("match", "fee") else _get(r, "transfer id")
        ents.append(dict(type=typ, ts=_date(_get(r, "time")), amount=_num(_get(r, "amount")),
                         unit=(_get(r, "amount/balance unit", "unit") or "").upper(), id=xid or _get(r, "order id") or ""))
    return _pro_account(ents)


# ---------------- CoinCorner ----------------
_CC_DET = re.compile(r"(Bought|Sold)\s*([\d.]+)\s*BTC\s*@\s*[£$€]?\s*([\d,.]+)", re.I)


def _cc_row(date_s, ref, typ, details, amount, errs):
    m = _CC_DET.search(details or "")
    if not (typ or "").lower().startswith("trade"):
        return None
    if not m:
        if re.search(r"\bBTC\b", details or "", re.I):
            errs.append(f"CoinCorner {ref}: unrecognised BTC trade '{details}' - skipped")
        return None
    side, qty, price = m.groups()
    d = datetime.strptime(re.sub(r"\s+", " ", date_s.strip()), "%d %b %Y")
    ts = d.replace(hour=12, tzinfo=timezone.utc).isoformat()
    gbp = abs(_num(amount))
    return dict(ts=ts, kind="buy" if side.lower() == "bought" else "sell", source="coincorner", wallet="coincorner",
                btc=float(qty), gbp=gbp, fee_gbp=0.0, ext_id=ref,
                note=f"{side} {qty} BTC @ £{price} (statement has no time: 12:00 UTC used; fee included in price)")


_CC = re.compile(r"^(\d{1,2} ?[A-Za-z]{3} ?\d{4})\s+(CC\d+)\s+(Trade|Bank ?deposit|Bank ?withdrawal|Withdrawal|Deposit|\S+)\s*(.*?)\s+(-?£[\d,]+\.\d\d)\s+(-?£[\d,]+\.\d\d)$")


def parse_coincorner_pdf(text):
    rows, errs = [], []
    for l in lines(text):
        m = _CC.match(l)
        if not m:
            continue
        ds, ref, typ, det, amt, bal = m.groups()
        ds = re.sub(r"^(\d{1,2}) ?([A-Za-z]{3}) ?(\d{4})$", r"\1 \2 \3", ds)
        try:
            t = _cc_row(ds, ref, typ, det, amt, errs)
        except Exception as e:
            errs.append(f"CoinCorner {ref}: {e}"); continue
        if t:
            rows.append(t)
    return rows, errs


def parse_coincorner_csv(text):
    body = _find_header(text, ["reference", "type", "details", "amount"])
    if body is None:
        return [], ["Not a CoinCorner statement CSV (Date, Reference, Type, Details, Amount, Balance)"]
    rows, errs = [], []
    for i, r in enumerate(csv.DictReader(io.StringIO(body)), start=2):
        try:
            t = _cc_row(_get(r, "date") or "", _get(r, "reference") or f"line{i}", _get(r, "type"), _get(r, "details"), _get(r, "amount"), errs)
            if t:
                rows.append(t)
        except Exception as e:
            errs.append(f"line {i}: {e}")
    return rows, errs


PDF_PARSERS = {"coinbase-report": parse_coinbase_report, "coinbase-pro-fills": parse_pro_fills_pdf,
               "coinbase-pro-account": parse_pro_account_pdf, "coincorner": parse_coincorner_pdf}
CSV_PARSERS = {"coinbase-pro-fills": parse_pro_fills_csv, "coinbase-pro-account": parse_pro_account_csv,
               "coincorner": parse_coincorner_csv}


def detect_csv(text):
    h = re.sub(r"[^a-z0-9,]", "", text[:3000].lower())
    if "tradeid" in h and "product" in h and "side" in h:
        return "coinbase-pro-fills"
    if "amountbalanceunit" in h or ("transferid" in h and "tradeid" in h):
        return "coinbase-pro-account"
    if "reference" in h and "details" in h and ("coincorner" in h or ",type," in h):
        return "coincorner"
    return None


def parse_pdf_b64(b64: str, source: str = "auto"):
    data = base64.b64decode(b64, validate=True)
    if not data.startswith(b"%PDF"):
        raise ValueError("Not a PDF file")
    text = pdf_text(data)
    src = detect_pdf(text) if source in ("auto", "", None) else source
    if src == "coinbase":
        src = "coinbase-report"
    if src not in PDF_PARSERS:
        raise ValueError("Unrecognised PDF statement (supported: Coinbase transaction report, Coinbase Pro fills / account statement, CoinCorner statement)")
    return src, *PDF_PARSERS[src](text)
