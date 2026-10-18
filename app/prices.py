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


def _coinbase_daily(product, start: date, end: date):
    """Daily closes from Coinbase Exchange's public candles API (no key; full history; max 300 candles per request)."""
    out, d = {}, start
    while d <= end:
        e = min(d + timedelta(days=299), end)
        r = httpx.get(f"https://api.exchange.coinbase.com/products/{product}/candles", timeout=20, headers=UA,
                      params={"granularity": 86400, "start": f"{d.isoformat()}T00:00:00Z", "end": f"{e.isoformat()}T23:59:59Z"})
        r.raise_for_status()
        for cdl in r.json():   # [time, low, high, open, close, volume]
            out[datetime.fromtimestamp(int(cdl[0]), timezone.utc).date().isoformat()] = float(cdl[4])
        d = e + timedelta(days=1)
    return out


def missing_days(c, start: date, end: date):
    have = {r[0] for r in c.execute("SELECT day FROM prices WHERE gbp IS NOT NULL AND gbp > 0 AND day >= ?",
                                    (start.isoformat(),))}
    out, d = [], start
    while d <= end:
        if d.isoformat() not in have:
            out.append(d)
        d += timedelta(days=1)
    return out


def backfill_history(c, start: date, end: date = None):
    """Kraken/CoinGecko only reach back ~720/365 days. Fill older gaps (back to the first transaction) from
    Coinbase Exchange daily candles. Only missing days are requested, so after the first run this is a no-op."""
    yday = datetime.now(timezone.utc).date() - timedelta(days=1)
    gaps = missing_days(c, start, min(end or yday, yday))
    if not gaps:
        return 0
    runs = [[gaps[0], gaps[0]]]   # contiguous gap runs, so one stray missing day doesn't refetch years of candles
    for d in gaps[1:]:
        if d - runs[-1][1] == timedelta(days=1):
            runs[-1][1] = d
        else:
            runs.append([d, d])
    g = {}
    for a, b in runs:
        try:
            g.update(_coinbase_daily("BTC-GBP", a, b))
        except Exception:
            break
    got = 0
    for d in gaps:
        p = g.get(d.isoformat())
        if p:
            c.execute("INSERT INTO prices(day,gbp,usd,synthetic) VALUES(?,?,NULL,0) ON CONFLICT(day) DO UPDATE SET "
                      "gbp=excluded.gbp, synthetic=0 WHERE prices.gbp IS NULL OR prices.gbp <= 0", (d.isoformat(), p))
            got += 1
    return got


def needs_backfill(c, start: date, slack_days=7):
    """True if the cached daily history doesn't reach back to `start` (e.g. an instance updated from <=1.0.9,
    whose daily refresh had already run that day, so the Coinbase backfill never ran)."""
    r = c.execute("SELECT min(day) FROM prices WHERE gbp IS NOT NULL AND gbp > 0").fetchone()
    return not r or not r[0] or date.fromisoformat(r[0]) > start + timedelta(days=slack_days)


def history_start(c, default=date(2021, 1, 1)):
    """First day the daily price cache should cover: the earliest transaction (UK day; a day early for safety)."""
    r = c.execute("SELECT min(ts) FROM tx").fetchone()
    try:
        return min(default, datetime.fromisoformat(r[0]).date() - timedelta(days=1)) if r and r[0] else default
    except ValueError:
        return default


def refresh_history(c, start: date):
    """Fill the price cache. CoinGecko free tier gives ~365 days, Kraken OHLC ~720 days; older days back to
    `start` come from Coinbase Exchange daily candles. Days still missing fall back (in the chart) to the last
    known market price or a non-dust buy price recorded in your transactions."""
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
    return got + backfill_history(c, start)


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
