from datetime import date, datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo
from . import cgt, db, prices

LDN = ZoneInfo("Europe/London")


def uk_day(ts: str) -> date:
    return datetime.fromisoformat(ts).astimezone(LDN).date()


def load(c):
    return [dict(r) for r in c.execute("SELECT * FROM tx ORDER BY ts, id")]


def events(txs):
    ev = []
    for t in txs:
        if t["kind"] in ("buy", "sell"):
            ev.append(cgt.Event(uk_day(t["ts"]), t["kind"], t["btc"], t["gbp"], str(t["id"])))
        elif t["kind"] == "transfer" and t["fee_btc"]:
            ev.append(cgt.Event(uk_day(t["ts"]), "fee", t["fee_btc"], 0, str(t["id"])))
    return ev


def wallets(txs):
    w = defaultdict(float)
    for t in txs:
        if t["kind"] == "buy":
            w[t["wallet"]] += t["btc"]
        elif t["kind"] == "sell":
            w[t["wallet"]] -= t["btc"]
        elif t["kind"] == "transfer":
            w[t["wallet"]] -= t["btc"] + (t["fee_btc"] or 0)
            w[t["to_wallet"] or "other"] += t["btc"]
    return {k: round(v, 8) for k, v in w.items()}


def tax_settings(s):
    return cgt.TaxSettings(**{k: float(s[k]) for k in ("other_income", "personal_allowance", "basic_band",
                                                        "basic_rate", "higher_rate", "annual_exempt", "exempt_used")})


def summary(c):
    s = db.get_settings(c)
    txs = load(c)
    ledger = wallets(txs)
    strike = s["strike_balance_cache"]["btc"] if s.get("strike_balance_cache") else ledger.get("strike", 0)
    cold = s["cold_balance_cache"]["btc"] if s.get("cold_balance_cache") else ledger.get("cold", 0)
    other = sum(v for k, v in ledger.items() if k not in ("strike", "cold"))
    total = strike + cold + other
    ev = events(txs)
    res = cgt.match(ev) if ev else cgt.Result([], 0, 0)
    sp = prices.spot()
    pg, pu = sp.get("gbp") or 0, sp.get("usd") or 0
    value_gbp = total * pg
    # Cost basis = Section 104 pool cost (matches what HMRC uses after same-day/30-day matches)
    basis = res.pool_cost
    ledger_btc = res.pool_btc
    pnl = ledger_btc * pg - basis  # PnL on BTC with known cost basis
    today = datetime.now(LDN).date()
    ts = tax_settings(s)
    try:
        hypo = cgt.sell_all_today(ev, today, pg, ledger_btc, ts, float(s["sell_fee_pct"]) / 100)
        hypo["value_after_tax"] = hypo["after_tax"] + (total - ledger_btc) * pg * (1 - float(s["sell_fee_pct"]) / 100)
    except ValueError as e:
        hypo = {"error": str(e)}
    realised_ty = sum(d.gain for d in res.disposals if cgt.tax_year_of(d.day) == cgt.tax_year_of(today))
    buys = [t for t in txs if t["kind"] == "buy"]
    return {
        "price": {"gbp": pg, "usd": pu, "source": sp.get("src")},
        "holdings": {"total": total, "strike": strike, "cold": cold, "other": other, "ledger": ledger,
                     "ledger_btc": ledger_btc,
                     "strike_live": bool(s.get("strike_balance_cache")), "cold_live": bool(s.get("cold_balance_cache")),
                     "mismatch_btc": total - ledger_btc},
        "value": {"gbp": value_gbp, "usd": total * pu},
        "cost_basis_gbp": basis,
        "avg_price_gbp": basis / ledger_btc if ledger_btc else 0,
        "pnl_gbp": pnl, "pnl_pct": (pnl / basis * 100) if basis else 0,
        "cgt": hypo, "realised_this_tax_year": realised_ty,
        "disposals": [{"day": d.day.isoformat(), "btc": d.btc, "proceeds": d.proceeds, "cost": d.cost, "gain": d.gain,
                       "matches": d.matches} for d in res.disposals],
        "counts": {"tx": len(txs), "buys": len(buys), "example": sum(t["example"] for t in txs),
                   "estimates": sum(t["estimate"] for t in txs)},
        "total_invested_gbp": sum(t["gbp"] for t in buys),
    }


def series(c):
    """Daily series: BTC held, pool cost basis, value (GBP)."""
    txs = load(c)
    if not txs:
        return []
    hist = prices.history(c)
    by_day = defaultdict(list)
    for t in txs:
        by_day[uk_day(t["ts"])].append(t)
    start, end = min(by_day), datetime.now(LDN).date()
    out, btc, last_price = [], 0.0, None
    ev_so_far = []
    d = start
    pool_cost = 0.0
    while d <= end:
        changed = False
        for t in by_day.get(d, []):
            if t["kind"] in ("buy", "sell") or (t["kind"] == "transfer" and t["fee_btc"]):
                ev_so_far.append(cgt.Event(d, t["kind"] if t["kind"] != "transfer" else "fee",
                                           t["btc"] if t["kind"] != "transfer" else t["fee_btc"], t["gbp"]))
                changed = True
                if t["kind"] == "buy":
                    last_price = t["gbp"] / t["btc"] if t["btc"] else last_price
            if t["kind"] == "buy":
                btc += t["btc"]
            elif t["kind"] == "sell":
                btc -= t["btc"]
            elif t["kind"] == "transfer":
                btc -= t["fee_btc"] or 0
        if changed:
            try:
                pool_cost = cgt.match(ev_so_far).pool_cost
            except ValueError:
                pass
        p = hist.get(d.isoformat(), (None, None))[0] or last_price
        if p:
            last_price = p
        out.append({"day": d.isoformat(), "btc": round(btc, 8), "cost": round(pool_cost, 2),
                    "value": round(btc * (p or 0), 2), "price": p})
        d += timedelta(days=1)
    return out
