"""Tolerant CSV importers for Coinbase, Kraken (ledgers + trades) and Strike exports.

Each parser returns (rows, errors). A row is a dict compatible with db.insert_tx.
Column names are matched case-insensitively against several aliases because these exchanges
have changed their export formats over the years. GBP values are used where present; if a
trade was in another fiat currency the row is flagged with a warning so you can fix it.
"""
import csv, io, re
from datetime import datetime, timezone

BTC_ASSETS = {"BTC", "XBT", "XXBT", "XBT.M", "XBT.F", "BTC.M"}


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _num(v):
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "").replace("£", "").replace("$", "").replace("€", "")
    s = re.sub(r"[A-Za-z ]", "", s)
    if s in ("", "-", "."):
        return 0.0
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s)


DATE_FMTS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f",
             "%Y-%m-%d %H:%M", "%b %d %Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S",
             "%Y-%m-%d", "%b %d %Y", "%d %b %Y %H:%M:%S", "%d/%m/%Y"]


def _date(v):
    s = str(v).strip().replace(" UTC", "").replace("Z", "").strip()
    s = re.sub(r"([+-]\d\d:?\d\d)$", "", s).strip()
    for f in DATE_FMTS:
        try:
            return datetime.strptime(s, f).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()


def _find_header(text, must):
    """Skip preamble lines (Coinbase adds some) until a line containing all `must` columns."""
    lines = text.splitlines()
    for i, line in enumerate(lines[:50]):
        n = {_norm(c) for c in next(csv.reader([line]), [])}
        if all(any(m in c for c in n) for m in must):
            return "\n".join(lines[i:])
    return None


def _get(row, *aliases):
    nm = {_norm(k): v for k, v in row.items() if k}
    for a in aliases:
        if _norm(a) in nm and nm[_norm(a)] not in (None, ""):
            return nm[_norm(a)]
    for a in aliases:  # prefix match, e.g. "Total (inclusive of fees and/or spread)"
        for k, v in nm.items():
            if k.startswith(_norm(a)) and v not in (None, ""):
                return v
    return None


# ---------------- Coinbase ----------------
def parse_coinbase(text):
    body = _find_header(text, ["timestamp", "transactiontype", "asset"])
    if body is None:
        return [], ["Could not find Coinbase header (Timestamp, Transaction Type, Asset...)"]
    rows, errs = [], []
    for i, r in enumerate(csv.DictReader(io.StringIO(body)), start=2):
        try:
            asset = (_get(r, "Asset") or "").upper()
            if asset not in BTC_ASSETS:
                continue
            typ = (_get(r, "Transaction Type") or "").lower()
            qty = abs(_num(_get(r, "Quantity Transacted")))
            ccy = (_get(r, "Price Currency", "Spot Price Currency") or "GBP").upper()
            total = abs(_num(_get(r, "Total (inclusive of fees", "Total")))
            sub = abs(_num(_get(r, "Subtotal")))
            fee = abs(_num(_get(r, "Fees and/or Spread", "Fees")))
            t = dict(ts=_date(_get(r, "Timestamp")), source="coinbase", wallet="coinbase", btc=qty,
                     ext_id=_get(r, "ID"), note=_get(r, "Notes"))
            if "buy" in typ or typ in ("receive", "rewards income", "learning reward", "staking income") or "convert" in typ:
                if typ in ("receive",):
                    t.update(kind="transfer", wallet="other", to_wallet="coinbase", note=(t["note"] or "") + " (incoming)")
                else:
                    t.update(kind="buy", gbp=total or sub + fee, fee_gbp=fee)
                    if "convert" in typ:
                        t["note"] = (t["note"] or "") + " [convert: check]"
                    if "reward" in typ or "income" in typ:
                        t["note"] = (t["note"] or "") + " [income: cost = market value]"
            elif "sell" in typ:
                t.update(kind="sell", gbp=(sub - fee) if sub else total, fee_gbp=fee)
            elif typ in ("send", "withdrawal"):
                t.update(kind="transfer", to_wallet="cold")
            else:
                errs.append(f"line {i}: skipped unknown type '{typ}'")
                continue
            if ccy != "GBP" and t["kind"] in ("buy", "sell"):
                t["note"] = (t.get("note") or "") + f" [WARNING: priced in {ccy}, not GBP]"
            rows.append(t)
        except Exception as e:
            errs.append(f"line {i}: {e}")
    return rows, errs


# ---------------- Kraken ----------------
def _kr_asset(a):
    a = (a or "").upper()
    if a in BTC_ASSETS:
        return "BTC"
    if a in ("ZGBP", "GBP", "GBP.HOLD", "ZGBP.HOLD"):
        return "GBP"
    return a


def parse_kraken(text):
    if _find_header(text, ["refid", "asset", "amount"]):
        return _kraken_ledgers(_find_header(text, ["refid", "asset", "amount"]))
    body = _find_header(text, ["pair", "vol", "cost"])
    if body:
        return _kraken_trades(body)
    return [], ["Not a Kraken ledgers.csv (txid,refid,time,type,asset,amount,fee,balance) or trades.csv export"]


def _kraken_ledgers(body):
    rows, errs, groups = [], [], {}
    for i, r in enumerate(csv.DictReader(io.StringIO(body)), start=2):
        if not _get(r, "txid"):   # Kraken puts an empty-txid duplicate line for some entries
            continue
        groups.setdefault(_get(r, "refid") or f"row{i}", []).append(r)
    for refid, legs in groups.items():
        try:
            btc = [l for l in legs if _kr_asset(_get(l, "asset")) == "BTC"]
            gbp = [l for l in legs if _kr_asset(_get(l, "asset")) == "GBP"]
            if not btc:
                continue
            typ = (_get(btc[0], "type") or "").lower()
            amt = sum(_num(_get(l, "amount")) for l in btc)
            fee_btc = sum(_num(_get(l, "fee")) for l in btc)
            ts = _date(_get(btc[0], "time"))
            base = dict(ts=ts, source="kraken", wallet="kraken", ext_id=refid)
            if typ in ("trade", "spend", "receive") and gbp:
                g = sum(_num(_get(l, "amount")) for l in gbp)
                gfee = sum(_num(_get(l, "fee")) for l in gbp)
                if amt > 0:
                    rows.append({**base, "kind": "buy", "btc": amt - fee_btc, "gbp": abs(g) + gfee, "fee_gbp": gfee})
                else:
                    rows.append({**base, "kind": "sell", "btc": abs(amt) + fee_btc, "gbp": g - gfee, "fee_gbp": gfee})
            elif typ in ("trade", "spend", "receive"):
                others = {_kr_asset(_get(l, "asset")) for l in legs} - {"BTC"}
                errs.append(f"refid {refid}: BTC trade against {others or 'unknown'} (not GBP) skipped - add manually")
            elif typ == "withdrawal":
                rows.append({**base, "kind": "transfer", "to_wallet": "cold", "btc": abs(amt), "fee_btc": fee_btc})
            elif typ == "deposit":
                rows.append({**base, "kind": "transfer", "wallet": "other", "to_wallet": "kraken", "btc": abs(amt)})
            elif typ in ("staking", "earn"):
                continue
            else:
                errs.append(f"refid {refid}: skipped type '{typ}'")
        except Exception as e:
            errs.append(f"refid {refid}: {e}")
    return rows, errs


def _kraken_trades(body):
    rows, errs = [], []
    for i, r in enumerate(csv.DictReader(io.StringIO(body)), start=2):
        try:
            pair = (_get(r, "pair") or "").upper().replace("/", "")
            if not (pair.startswith("XXBT") or pair.startswith("XBT") or pair.startswith("BTC")):
                continue
            if not pair.endswith("GBP"):
                errs.append(f"line {i}: pair {pair} is not GBP - skipped"); continue
            typ = (_get(r, "type") or "").lower()
            vol, cost, fee = _num(_get(r, "vol")), _num(_get(r, "cost")), _num(_get(r, "fee"))
            t = dict(ts=_date(_get(r, "time")), source="kraken", wallet="kraken", btc=vol,
                     ext_id=_get(r, "txid"), fee_gbp=fee)
            t.update(kind="buy", gbp=cost + fee) if typ == "buy" else t.update(kind="sell", gbp=cost - fee)
            rows.append(t)
        except Exception as e:
            errs.append(f"line {i}: {e}")
    return rows, errs


# ---------------- Strike ----------------
def parse_strike(text):
    body = _find_header(text, ["amountbtc"]) or _find_header(text, ["btc", "transactiontype"])
    if body is None:
        return [], ["Could not find Strike header (needs an 'Amount BTC' column)"]
    rows, errs = [], []
    for i, r in enumerate(csv.DictReader(io.StringIO(body)), start=2):
        try:
            status = (_get(r, "State", "Status") or "completed").lower()
            if status not in ("completed", "complete", "success", "succeeded", "settled"):
                continue
            typ = (_get(r, "Transaction Type", "Type") or "").lower()
            btc = _num(_get(r, "Amount BTC"))
            fee_btc = abs(_num(_get(r, "Fee BTC")))
            gbp = _num(_get(r, "Amount GBP"))
            fee_gbp = abs(_num(_get(r, "Fee GBP")))
            ts_raw = _get(r, "Date & Time (UTC)", "Time (UTC)", "Completed Date (UTC)", "Initiated Date (UTC)", "Date", "Time")
            tm = _get(r, "Completed Time (UTC)", "Initiated Time (UTC)")
            if tm and ts_raw and ":" not in ts_raw:
                ts_raw = f"{ts_raw} {tm}"
            t = dict(ts=_date(ts_raw), source="strike", wallet="strike",
                     ext_id=_get(r, "Reference", "Transaction ID", "ID"), note=_get(r, "Description"))
            if btc == 0:
                continue
            if btc > 0 and ("trade" in typ or "purchase" in typ or "buy" in typ or "exchange" in typ or gbp):
                if not gbp:
                    usd = _num(_get(r, "Amount USD"))
                    t["note"] = (t["note"] or "") + f" [WARNING: no GBP amount (USD {usd}) - fix cost]"
                    gbp = abs(usd)
                t.update(kind="buy", btc=btc, gbp=abs(gbp) + fee_gbp, fee_gbp=fee_gbp)
            elif btc < 0 and ("trade" in typ or "sell" in typ or "exchange" in typ) and gbp:
                t.update(kind="sell", btc=abs(btc), gbp=abs(gbp) - fee_gbp, fee_gbp=fee_gbp)
            elif btc < 0:
                t.update(kind="transfer", to_wallet="cold", btc=abs(btc), fee_btc=fee_btc,
                         note=(t["note"] or "") + f" {_get(r, 'Destination') or ''}".strip())
            else:
                t.update(kind="transfer", wallet="other", to_wallet="strike", btc=btc)
            rows.append(t)
        except Exception as e:
            errs.append(f"line {i}: {e}")
    return rows, errs


PARSERS = {"coinbase": parse_coinbase, "kraken": parse_kraken, "strike": parse_strike}


def detect(text):
    h = _norm(text[:3000])
    if "refid" in h or ("pair" in h and "ordertxid" in h):
        return "kraken"
    if "quantitytransacted" in h:
        return "coinbase"
    if "amountbtc" in h:
        return "strike"
    return None
