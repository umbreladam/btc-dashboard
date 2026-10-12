"""Strike API (read-only key). Docs: https://docs.strike.me  Base: https://api.strike.me/v1"""
import httpx

BASE = "https://api.strike.me/v1"


def _h(key):
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def balance_btc(key) -> float:
    r = httpx.get(f"{BASE}/balances", headers=_h(key), timeout=20)
    r.raise_for_status()
    for b in r.json():
        if b.get("currency") == "BTC":
            return float(b.get("total") or b.get("current") or 0)
    return 0.0


def rate_btc(key, currency="GBP") -> float:
    r = httpx.get(f"{BASE}/rates/ticker", headers=_h(key), timeout=20)
    r.raise_for_status()
    for t in r.json():
        if t.get("sourceCurrency") == "BTC" and t.get("targetCurrency") == currency:
            return float(t["amount"])
    raise RuntimeError(f"BTC/{currency} rate not in Strike ticker")


def rate_btc_gbp(key) -> float:
    return rate_btc(key, "GBP")


def daily_record(prev: float | None, new: float, rate: float, dca_gbp: float):
    """Pure decision logic for the daily job. Returns list of tx dicts (without ts/source)."""
    if prev is None:
        return []  # first run: baseline only
    delta = round(new - prev, 8)
    if delta > 0:
        return [dict(kind="buy", wallet="strike", btc=delta, gbp=dca_gbp, estimate=0,
                     note=f"Strike DCA buy (balance +{delta:.8f})")]
    if delta == 0:
        return []
    est = round(dca_gbp / rate, 8)
    out = round(prev + est - new, 8)
    txs = [dict(kind="buy", wallet="strike", btc=est, gbp=dca_gbp, estimate=1,
                note=f"ESTIMATE: balance dropped; assumed £{dca_gbp} buy at £{rate:,.0f}/BTC")]
    if out > 0:
        txs.append(dict(kind="transfer", wallet="strike", to_wallet="cold", btc=out, estimate=1,
                        note="ESTIMATE: assumed withdrawal to cold storage"))
    return txs
