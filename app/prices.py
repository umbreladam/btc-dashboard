"""BTC price (GBP/USD) from public APIs, cached in SQLite. No account data is ever sent."""
import time, math, httpx
from datetime import date, datetime, timezone, timedelta

_spot = {"t": 0, "gbp": None, "usd": None, "src": None}
UA = {"User-Agent": "satoshi-btc-dashboard/1.0"}


def spot(external=True, strike_key=""):
    """external=False: never contact CoinGecko/Kraken; use Strike's ticker if a key is configured."""
    if time.time() - _spot["t"] < 60 and _spot["gbp"] and (external or _spot["src"] in ("strike", None)):
        return _spot
    if not external:
        if strike_key:
            try:
                from . import strike
                _spot.update(t=time.time(), gbp=strike.rate_btc(strike_key, "GBP"),
                             usd=strike.rate_btc(strike_key, "USD"), src="strike")
            except Exception:
                pass
        else:
            _spot.update(t=time.time(), gbp=None, usd=None, src=None)
        return _spot
    try:
        r = httpx.get("https://api.coingecko.com/api/v3/simple/price",
                      params={"ids": "bitcoin", "vs_currencies": "gbp,usd"}, timeout=10, headers=UA)
        j = r.json()["bitcoin"]
        _spot.update(t=time.time(), gbp=float(j["gbp"]), usd=float(j["usd"]), src="coingecko")
    except Exception:
        try:
            r = httpx.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTGBP,XBTUSD"}, timeout=10, headers=UA)
            res = r.json()["result"]
            _spot.update(t=time.time(), gbp=float(res["XXBTZGBP"]["c"][0]), usd=float(res["XXBTZUSD"]["c"][0]), src="kraken")
        except Exception:
            pass
    return _spot


def _kraken_daily(pair):
    r = httpx.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 1440}, timeout=20, headers=UA)
    res = r.json()["result"]
    key = [k for k in res if k != "last"][0]
    return {datetime.fromtimestamp(int(c[0]), timezone.utc).date().isoformat(): float(c[4]) for c in res[key]}


def _coingecko_daily(ccy, days=365):
    r = httpx.get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
                  params={"vs_currency": ccy, "days": days, "interval": "daily"}, timeout=20, headers=UA)
    return {datetime.fromtimestamp(p[0] / 1000, timezone.utc).date().isoformat(): float(p[1]) for p in r.json()["prices"]}


def refresh_history(c, start: date):
    """Fill the price cache. CoinGecko free tier gives ~365 days, Kraken OHLC ~720 days.
    Older days (e.g. early Coinbase buys) fall back to the buy prices recorded in your transactions."""
    got = 0
    for fetch in (lambda: (_kraken_daily("XBTGBP"), _kraken_daily("XBTUSD")),
                  lambda: (_coingecko_daily("gbp"), _coingecko_daily("usd"))):
        try:
            g, u = fetch()
            for d, p in g.items():
                c.execute("INSERT INTO prices(day,gbp,usd,synthetic) VALUES(?,?,?,0) ON CONFLICT(day) DO UPDATE SET "
                          "gbp=excluded.gbp, usd=COALESCE(excluded.usd, prices.usd), synthetic=0", (d, p, u.get(d)))
                got += 1
        except Exception:
            continue
    return got


def history(c):
    return {r["day"]: (r["gbp"], r["usd"]) for r in c.execute("SELECT * FROM prices ORDER BY day")}


_intra = {}


def intraday(days: int):
    """Intraday GBP prices [(epoch_s, price)] for the last `days` (1 or 7): Kraken OHLC (5-min / 1-hour candles),
    CoinGecko market_chart as fallback. Cached 2 min. Callers must check the 'disable external prices' setting."""
    key, now = days, time.time()
    if key in _intra and now - _intra[key][0] < 120:
        return _intra[key][1]
    since, pts = now - days * 86400, None
    try:
        r = httpx.get("https://api.kraken.com/0/public/OHLC", timeout=20, headers=UA,
                      params={"pair": "XBTGBP", "interval": 5 if days <= 1 else 60, "since": int(since)})
        res = r.json()["result"]
        rows = res[[k for k in res if k != "last"][0]]
        pts = [(int(c[0]), float(c[4])) for c in rows if int(c[0]) >= since]
    except Exception:
        try:
            r = httpx.get("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart", timeout=20, headers=UA,
                          params={"vs_currency": "gbp", "days": days})
            pts = [(int(p[0] / 1000), float(p[1])) for p in r.json()["prices"] if p[0] / 1000 >= since]
        except Exception:
            pts = None
    if pts:
        _intra[key] = (now, pts)
    return pts or []
