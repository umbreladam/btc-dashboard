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


def holdings(s, ledger):
    """(strike, cold, other, total) BTC: live Strike / cold-scan balances when cached, else the transaction ledger."""
    strike = s["strike_balance_cache"]["btc"] if s.get("strike_balance_cache") else ledger.get("strike", 0)
    cold = s["cold_balance_cache"]["btc"] if s.get("cold_balance_cache") else ledger.get("cold", 0)
    other = sum(v for k, v in ledger.items() if k not in ("strike", "cold"))
    return strike, cold, other, strike + cold + other


def current_price(c, s, strike_key=""):
    sp = dict(prices.spot(external=not s.get("disable_external_prices"), strike_key=strike_key))
    if not sp.get("gbp"):  # offline / external fetches disabled without Strike: last cached daily close
        r = c.execute("SELECT gbp, usd FROM prices WHERE gbp IS NOT NULL ORDER BY day DESC LIMIT 1").fetchone()
        if r:
            sp.update(gbp=r["gbp"], usd=r["usd"], src="cached daily close")
    return sp


def summary(c, strike_key="", cold_wallets=None):
    s = db.get_settings(c)
    txs = load(c)
    ledger = wallets(txs)
    strike, cold, other, total = holdings(s, ledger)
    ev = events(txs)
    res = cgt.match(ev) if ev else cgt.Result([], 0, 0)
    sp = current_price(c, s, strike_key)
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


MIN_FALLBACK_BUY_GBP = 1.0


def buy_price_usable(t):
    """A buy's implied price (gbp/btc) is only a sane stand-in for a missing market close if the GBP amount isn't
    pennies: dust buys (e.g. Coinbase round-ups of 0.0000002 BTC recorded as £0.00 or £0.01) imply £0 or
    £40k+ per BTC and made the Value line collapse to zero / spike whenever they were the latest buy."""
    return t["btc"] > 0 and (t["gbp"] or 0) >= MIN_FALLBACK_BUY_GBP


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
                if t["kind"] == "buy" and buy_price_usable(t):
                    last_price = t["gbp"] / t["btc"]
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
        p = hist.get(d.isoformat(), (None, None))[0] or last_price   # no cached close: carry the last known price
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


# ---------------- value change over a period ----------------
PERIODS = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "1w": timedelta(days=7), "1m": timedelta(days=30),
           "1y": timedelta(days=365), "4y": timedelta(days=4 * 365 + 1)}


def _ets(t):
    from datetime import timezone
    dt = datetime.fromisoformat(t["ts"])
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def ledger_btc_at(txs, ts):
    """BTC held per the transaction ledger at epoch `ts` (buys - sells - network fees / lost; transfers are neutral)."""
    btc = 0.0
    for t in txs:
        if _ets(t) <= ts:
            btc += t["btc"] if t["kind"] == "buy" else -t["btc"] if t["kind"] == "sell" else -(t["fee_btc"] or 0)
    return btc


def net_invested(txs, t0, t1):
    """GBP put in (buys at cost) minus GBP taken out (sell proceeds) between epochs t0 (excl.) and t1 (incl.)."""
    return sum((t["gbp"] or 0) * (1 if t["kind"] == "buy" else -1)
               for t in txs if t["kind"] in ("buy", "sell") and t0 < _ets(t) <= t1)


def daily_price_at(hist, ts):
    """Daily close nearest to epoch `ts`: candle D closes at D+1 00:00 UTC; walk back up to a week for gaps."""
    from datetime import timezone
    d = (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=12)).date() - timedelta(days=1)
    for i in range(8):
        p = hist.get((d - timedelta(days=i)).isoformat(), (None,))[0]
        if p:
            return p
    return None


def intraday_price_at(points, ts, tolerance_s):
    """Price of the intraday point nearest to epoch `ts` if it is within `tolerance_s`."""
    if not points:
        return None
    t, p = min(points, key=lambda x: abs(x[0] - ts))
    return p if abs(t - ts) <= tolerance_s and p else None


def change(c, period, strike_key="", now=None, spot=None, intraday=None):
    """Market performance of the portfolio over `period` (1h/1d/1w/1m/1y/4y/all), excluding money paid in/out:
    change = value now - (value at start + net GBP invested during the period); pct = change / that base.
    Holdings basis matches the main Value (live balances); the gap between live balances and the ledger is treated
    as held throughout. 'all' = unrealised PnL vs S104 cost basis (same as the PnL card)."""
    from datetime import timezone
    period = (period or "1d").lower()
    if period not in PERIODS and period != "all":
        raise ValueError("unknown period")
    s = db.get_settings(c)
    txs = load(c)
    sp = spot if spot is not None else current_price(c, s, strike_key)
    pg = sp.get("gbp") or 0
    out = {"period": period, "available": False, "change_gbp": None, "pct": None}
    if not pg:
        return {**out, "reason": "No current price"}
    if period == "all":
        ev = events(txs)
        res = cgt.match(ev) if ev else cgt.Result([], 0, 0)
        if not res.pool_cost:
            return {**out, "reason": "No cost basis"}
        pnl = res.pool_btc * pg - res.pool_cost
        return {**out, "available": True, "change_gbp": pnl, "pct": pnl / res.pool_cost * 100,
                "base_gbp": res.pool_cost, "value_now": res.pool_btc * pg}
    now = now or datetime.now(timezone.utc).timestamp()
    t0 = now - PERIODS[period].total_seconds()
    external = not s.get("disable_external_prices")
    p0 = None
    if period in ("1h", "1d", "1w") and external:
        pts = intraday(7 if period == "1w" else 1) if intraday else prices.intraday(7 if period == "1w" else 1)
        p0 = intraday_price_at(pts, t0, {"1h": 900, "1d": 3600, "1w": 3 * 3600}[period])
    if p0 is None and period != "1h":
        p0 = daily_price_at(prices.history(c), t0)
    if not p0:
        return {**out, "reason": "No price for the start of this period"}
    ledger_now = ledger_btc_at(txs, now)
    total = holdings(s, wallets(txs))[3]
    h0 = max(0.0, ledger_btc_at(txs, t0) + (total - ledger_now))
    v0, inv, vnow = h0 * p0, net_invested(txs, t0, now), total * pg
    base = v0 + inv
    chg = vnow - base
    return {**out, "available": True, "change_gbp": chg, "pct": (chg / base * 100) if base > 0 else None,
            "base_gbp": base, "start_value": v0, "net_invested": inv, "value_now": vnow,
            "price_start": p0, "price_now": pg, "start": datetime.fromtimestamp(t0, timezone.utc).isoformat()}
