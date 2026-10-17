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
    return {k: round(v, 8) for k, v in w.items() if not (k == "lost" and abs(v) < 5e-9)}


def tax_settings(s):
    return cgt.TaxSettings(**{k: float(s[k]) for k in ("other_income", "personal_allowance", "basic_band",
                                                        "basic_rate", "higher_rate", "annual_exempt", "exempt_used")})


def summary(c, strike_key="", cold_wallets=None):
    s = db.get_settings(c)
    txs = load(c)
    ledger = wallets(txs)
    strike = s["strike_balance_cache"]["btc"] if s.get("strike_balance_cache") else ledger.get("strike", 0)
    cold = s["cold_balance_cache"]["btc"] if s.get("cold_balance_cache") else ledger.get("cold", 0)
    other = sum(v for k, v in ledger.items() if k not in ("strike", "cold"))
    total = strike + cold + other
    ev = events(txs)
    res = cgt.match(ev) if ev else cgt.Result([], 0, 0)
    sp = dict(prices.spot(external=not s.get("disable_external_prices"), strike_key=strike_key))
    if not sp.get("gbp"):  # offline / external fetches disabled without Strike: last cached daily close
        r = c.execute("SELECT gbp, usd FROM prices WHERE gbp IS NOT NULL ORDER BY day DESC LIMIT 1").fetchone()
        if r:
            sp.update(gbp=r["gbp"], usd=r["usd"], src="cached daily close")
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
    rows = wallet_rows(s, ledger, cold_wallets or [], strike, total, pg, pu)
    return {
        "wallets": rows,
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


EXCHANGE_NAMES = {"strike": "Strike", "coinbase": "Coinbase", "kraken": "Kraken", "other": "Other", "cold": "Cold storage (ledger)"}


def wallet_rows(s, ledger, cold, strike_btc, total, pg, pu):
    """One row per wallet for the Wallets panel. Rows sum to holdings.total."""
    rows = []
    sc = s.get("strike_balance_cache") or {}
    rows.append({"id": "strike", "name": "Strike", "type": "Exchange", "description": "Strike account" +
                 (" (live API balance)" if sc else " (from transaction ledger)"), "btc": strike_btc,
                 "scanned_at": sc.get("at"), "source": "strike-api" if sc else "ledger"})
    cache = s.get("cold_balance_cache")
    if cache:
        per = {w.get("id"): w for w in cache.get("wallets", [])}
        for w in cold:
            p = per.get(w["id"], {})
            rows.append({"id": w["id"], "name": w["name"], "type": w["type"], "policy": w.get("policy"),
                         "description": w.get("description", ""), "btc": p.get("balance", 0.0),
                         "scanned_at": p.get("at") or cache.get("at"), "source": "electrs"})
        known = sum(r["btc"] for r in rows[1:])
        if abs(cache["btc"] - known) > 1e-9:  # e.g. a wallet removed since the last scan
            rows.append({"id": "cold-other", "name": "Cold storage (unattributed)", "type": "Single-sig",
                         "description": "Last scan total not matched to a current wallet", "btc": round(cache["btc"] - known, 8),
                         "scanned_at": cache.get("at"), "source": "electrs"})
    else:
        for w in cold:
            rows.append({"id": w["id"], "name": w["name"], "type": w["type"], "policy": w.get("policy"),
                         "description": w.get("description", ""), "btc": 0.0, "scanned_at": None,
                         "source": "not scanned", "pending": True})
        if ledger.get("cold"):
            rows.append({"id": "cold", "name": "Cold storage (ledger)", "type": "Cold", "description":
                         "From transfers in your transaction history; scan a wallet for live balances",
                         "btc": ledger["cold"], "scanned_at": None, "source": "ledger"})
    for k, v in sorted(ledger.items()):
        if k in ("strike", "cold") or abs(v) < 1e-12:
            continue
        rows.append({"id": "x-" + k, "name": EXCHANGE_NAMES.get(k, k.title()), "type": "Exchange" if k != "other" else "Other",
                     "description": "Derived from imported/manual transactions", "btc": v, "scanned_at": None,
                     "source": "ledger"})
    for r in rows:
        r["btc"] = round(r["btc"], 8)
        r["gbp"], r["usd"] = r["btc"] * pg, r["btc"] * pu
        r["pct"] = (r["btc"] / total * 100) if total else 0
    return rows


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


def intraday(c, points):
    """Portfolio at each intraday point [(epoch_s, gbp_price)]: BTC held and pool cost as of that instant
    (transactions applied by exact timestamp; the S104 pool is recomputed only when a transaction falls in range)."""
    from datetime import timezone
    txs = load(c)
    out, i, btc, evs, pool = [], 0, 0.0, [], 0.0

    def apply(t):
        nonlocal btc
        d = uk_day(t["ts"])
        if t["kind"] in ("buy", "sell") or (t["kind"] == "transfer" and t["fee_btc"]):
            evs.append(cgt.Event(d, t["kind"] if t["kind"] != "transfer" else "fee",
                                 t["btc"] if t["kind"] != "transfer" else t["fee_btc"], t["gbp"]))
        btc += t["btc"] if t["kind"] == "buy" else -t["btc"] if t["kind"] == "sell" else -(t["fee_btc"] or 0)

    def ets(t):
        dt = datetime.fromisoformat(t["ts"])
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()

    for ts, p in sorted(points):
        n = len(evs)
        while i < len(txs) and ets(txs[i]) <= ts:
            apply(txs[i]); i += 1
        if len(evs) != n:
            try:
                pool = cgt.match(evs).pool_cost
            except ValueError:
                pass
        out.append({"t": ts, "day": datetime.fromtimestamp(ts, LDN).strftime("%Y-%m-%d %H:%M"),
                    "btc": round(btc, 8), "cost": round(pool, 2), "value": round(btc * p, 2), "price": p})
    return out
