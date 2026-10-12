"""EXAMPLE data generator - every row is flagged example=1 and labelled in the UI.
Remove with Settings -> 'Delete example data' once you import real records."""
import math, random
from datetime import datetime, timedelta, date, time, timezone
from zoneinfo import ZoneInfo
from . import db

LDN = ZoneInfo("Europe/London")


def _price(d: date) -> float:
    """Rough synthetic GBP price curve used ONLY when no cached market price exists for that day."""
    t = (d - date(2021, 1, 1)).days
    return 25000 + 20000 * math.sin(t / 260) + t * 32 + 2500 * math.sin(t / 23)


def generate(c, price_lookup=None, today=None):
    rnd = random.Random(42)
    today = today or datetime.now(LDN).date()
    pl = price_lookup or {}
    P = lambda d: (pl.get(d.isoformat()) or _price(d)) * (1 + rnd.uniform(-0.01, 0.01))
    rows = []

    def at(d, hh, mm):
        return datetime.combine(d, time(hh, mm), LDN).astimezone(timezone.utc).isoformat()

    # Coinbase: monthly-ish buys 2021-2023, then sent to cold storage
    d = date(2021, 3, 4)
    while d < date(2023, 12, 1):
        gbp = rnd.choice([100, 150, 250, 500]); p = P(d); fee = round(gbp * 0.0149, 2)
        rows.append(dict(ts=at(d, 19, 12), kind="buy", source="coinbase", wallet="coinbase",
                         btc=round((gbp - fee) / p, 8), gbp=gbp, fee_gbp=fee))
        d += timedelta(days=rnd.randint(25, 40))
    cb = sum(r["btc"] for r in rows)
    # one example sale on Coinbase (exercises same-day / 30-day / pool rules)
    sd = date(2023, 6, 15); sb = 0.02
    rows.append(dict(ts=at(sd, 12, 0), kind="sell", source="coinbase", wallet="coinbase", btc=sb,
                     gbp=round(sb * P(sd) * 0.985, 2), fee_gbp=round(sb * P(sd) * 0.015, 2), note="Example sale"))
    rows.append(dict(ts=at(date(2023, 12, 10), 10, 0), kind="transfer", source="coinbase", wallet="coinbase",
                     to_wallet="cold", btc=round(cb - sb - 0.0001, 8), fee_btc=0.0001, note="To cold storage"))
    # Kraken: fortnightly buys in 2024
    d, kr = date(2024, 1, 8), 0
    while d < date(2025, 1, 1):
        gbp = 200; p = P(d); fee = round(gbp * 0.004, 2); b = round((gbp - fee) / p, 8); kr += b
        rows.append(dict(ts=at(d, 8, 30), kind="buy", source="kraken", wallet="kraken", btc=b, gbp=gbp, fee_gbp=fee))
        d += timedelta(days=14)
    rows.append(dict(ts=at(date(2025, 1, 6), 9, 0), kind="transfer", source="kraken", wallet="kraken",
                     to_wallet="cold", btc=round(kr - 0.00002, 8), fee_btc=0.00002, note="To cold storage"))
    # Strike: £20 daily at 15:18, sweep to cold at ~0.01 BTC
    d, bal = date(2025, 6, 1), 0.0
    while d <= today:
        if d == today and datetime.now(LDN).time() < time(15, 18):
            break
        b = round(20 / P(d), 8); bal += b
        rows.append(dict(ts=at(d, 15, 18), kind="buy", source="strike", wallet="strike", btc=b, gbp=20))
        if bal >= 0.01:
            rows.append(dict(ts=at(d, 18, 0), kind="transfer", source="strike", wallet="strike", to_wallet="cold",
                             btc=round(bal - 0.000002, 8), fee_btc=0.000002, note="Sweep to cold storage"))
            bal = 0.0
        d += timedelta(days=1)
    n = 0
    for r in rows:
        r["example"] = 1
        r["note"] = ("EXAMPLE " + (r.get("note") or "")).strip()
        n += db.insert_tx(c, r)
    return n
