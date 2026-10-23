"""Satoshi Demo (DEMO_MODE=1): generate a realistic but entirely FAKE stacking history from the first-launch wizard.

Buys use REAL historical BTC/GBP daily closes (the public price cache) when available, otherwise the synthetic curve.
Exchanges and cold wallets are made up; nothing here talks to Strike, Electrum or any wallet."""
import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from . import db, example

LDN = ZoneInfo("Europe/London")
FREQS = ("daily", "weekly", "monthly")
COLD_EVERY = {"monthly": 1, "quarterly": 3, "6-monthly": 6, "yearly": 12, "never": 0}
# made-up exchanges: (ledger key, display name, fee %)
EXCHANGES = [("satsbank", "SatsBank", 0.0149), ("orangex", "OrangeX", 0.005), ("stackly", "Stackly", 0.0099)]
WALLETS = {
    "lighthouse": {"name": "Lighthouse Vault", "type": "Multisig", "policy": "2-of-3",
                   "description": "Demo 2-of-3 multisig: 3 made-up hardware signers"},
    "acorn": {"name": "Acorn Signer", "type": "Single-sig", "policy": "single-sig",
              "description": "Demo single-sig hardware wallet (made up)"},
}
DEMO_KEYS = ("demo_configured", "demo_params", "demo_wallets")


def validate(p: dict) -> dict:
    try:
        start = date.fromisoformat(str(p.get("start", ""))[:10])
    except ValueError:
        raise ValueError("Start date must be YYYY-MM-DD")
    today = datetime.now(LDN).date()
    if start < date(2015, 1, 1) or start > today - timedelta(days=14):
        raise ValueError("Start date must be between 2015-01-01 and two weeks ago")
    freq = str(p.get("frequency", "weekly")).lower()
    if freq not in FREQS:
        raise ValueError("Frequency must be daily, weekly or monthly")
    cold = str(p.get("cold_every", "quarterly")).lower()
    if cold not in COLD_EVERY:
        raise ValueError("Cold storage frequency must be one of " + ", ".join(COLD_EVERY))
    amount, salary, lump = float(p.get("amount") or 0), float(p.get("salary") or 0), float(p.get("lump_sum") or 0)
    if not 1 <= amount <= 100000:
        raise ValueError("DCA amount must be between £1 and £100,000")
    if not 0 <= salary <= 10_000_000 or not 0 <= lump <= 10_000_000:
        raise ValueError("Salary and lump sum must be between £0 and £10m")
    n = int(p.get("exchanges") or 1)
    if not 1 <= n <= len(EXCHANGES):
        raise ValueError(f"Number of exchanges must be 1-{len(EXCHANGES)}")
    return {"start_date": start, "start": start.isoformat(), "amount": amount, "frequency": freq, "salary": salary,
            "lump_sum": lump, "cold_every": cold, "exchanges": n}


def wipe(c):
    """Delete every transaction, demo settings and the job log. The public price cache is kept (it's not user data)."""
    c.execute("DELETE FROM tx")
    c.execute("DELETE FROM joblog")
    c.execute("DELETE FROM settings")


def _dates(start, end, freq):
    d, i = start, 0
    while d < end:
        yield d
        i += 1
        if freq == "daily":
            d = start + timedelta(days=i)
        elif freq == "weekly":
            d = start + timedelta(weeks=i)
        else:
            m = start.month - 1 + i
            y, m = start.year + m // 12, m % 12 + 1
            d = date(y, m, min(start.day, 28))


def _month_index(d, start):
    return (d.year - start.year) * 12 + d.month - start.month


def generate(c, params: dict, price_lookup=None, today=None) -> dict:
    """Insert the fake history (all rows source=demo exchange keys). Returns counts."""
    p = params if "start_date" in params else validate(params)
    today = today or datetime.now(LDN).date()
    pl = price_lookup or {}
    rnd = random.Random(f"{p['start']}|{p['amount']}|{p['frequency']}|{p['cold_every']}|{p['exchanges']}")
    known = sorted(pl)

    def price(d):
        v = pl.get(d.isoformat())
        if not v and known:   # nearest earlier cached close
            import bisect
            i = bisect.bisect_right(known, d.isoformat()) - 1
            v = pl[known[i]] if i >= 0 else None
        return (v or example._price(d)) * (1 + rnd.uniform(-0.004, 0.004))

    def at(d, hh, mm):
        return datetime.combine(d, time(hh, mm), LDN).astimezone(timezone.utc).isoformat()

    exs = EXCHANGES[:p["exchanges"]]
    start, rows, bal = p["start_date"], [], {k: 0.0 for k, _, _ in exs}
    span_m = max(1, _month_index(today, start))
    seg = span_m / len(exs)   # move to the next (cheaper) exchange part-way through, like real stackers do
    cold_m = COLD_EVERY[p["cold_every"]]
    sweeps, last_sweep_m, sells_done, spend_done = 0, 0, 0, False
    sell_at = {int(span_m * 0.45), int(span_m * 0.8)} if span_m >= 6 else set()

    def buy(d, key, fee_pct, gbp, note=None, hh=None):
        px = price(d)
        fee = round(gbp * fee_pct, 2)
        b = round((gbp - fee) / px, 8)
        bal[key] += b
        rows.append(dict(ts=at(d, hh if hh is not None else rnd.randint(7, 21), rnd.randint(0, 59)), kind="buy",
                         source=key, wallet=key, btc=b, gbp=round(gbp, 2), fee_gbp=fee, note=note))

    if p["lump_sum"] > 0:
        buy(start, exs[0][0], exs[0][2], p["lump_sum"], "Starting lump sum", 10)
    for d in _dates(start, today, p["frequency"]):
        mi = _month_index(d, start)
        key, _, fee_pct = exs[min(len(exs) - 1, int(mi / seg))]
        buy(d, key, fee_pct, p["amount"], "Recurring buy")
        # now and then move some coins back from the vault to the exchange and sell them (realised gain for CGT)
        if mi in sell_at and sells_done < 2:
            vault = sum(r["btc"] for r in rows if r.get("to_wallet") == "lighthouse")
            src, sb = ("lighthouse", round(vault * rnd.uniform(0.12, 0.25), 8)) if vault > 0.002 else (key, round(bal[key] * 0.5, 8))
            if sb > 0.0001:
                sell_at.discard(mi)
                sells_done += 1
                sd = d
                if src == "lighthouse":
                    fee_btc = round(rnd.uniform(0.00002, 0.00008), 8)
                    rows.append(dict(ts=at(d, 9, rnd.randint(0, 59)), kind="transfer", source=key, wallet="lighthouse",
                                     to_wallet=key, btc=sb, fee_btc=fee_btc, note="Deposit from Lighthouse Vault to sell"))
                    rows[-1]["source"] = "lighthouse"
                    sd = d + timedelta(days=1)
                    bal[key] += sb
                px = price(sd)
                gross = sb * px
                fee = round(gross * fee_pct, 2)
                bal[key] -= sb
                rows.append(dict(ts=at(sd, 16, rnd.randint(0, 59)), kind="sell", source=key, wallet=key, btc=sb,
                                 gbp=round(gross - fee, 2), fee_gbp=fee,
                                 note=["Sold some for a house deposit", "Took some profit"][sells_done % 2]))
        # periodic sweep to cold storage: mostly the multisig vault, every third one to the single-sig
        if cold_m and mi - last_sweep_m >= cold_m:
            last_sweep_m = mi
            dest = "acorn" if sweeps % 3 == 2 else "lighthouse"
            for k, _, _ in exs:
                if bal[k] > 0.0002:
                    fee_btc = round(rnd.uniform(0.00001, 0.00006), 8)
                    amt = round(bal[k] - fee_btc, 8)
                    rows.append(dict(ts=at(d, 22, rnd.randint(0, 59)), kind="transfer", source=k, wallet=k,
                                     to_wallet=dest, btc=amt, fee_btc=fee_btc,
                                     note=f"Withdrawal to {WALLETS[dest]['name']}"))
                    bal[k] = 0.0
            sweeps += 1
            if dest == "acorn" and not spend_done and mi > span_m * 0.5:
                spend_done = True
                pocket = sum(r["btc"] for r in rows if r.get("to_wallet") == "acorn")
                sb = round(min(pocket * 0.1, 400 / price(d)), 8)
                if sb > 0:
                    rows.append(dict(ts=at(d + timedelta(days=3), 13, 7), kind="sell", source="acorn", wallet="acorn",
                                     btc=sb, gbp=round(sb * price(d + timedelta(days=3)), 2),
                                     note="Paid for a laptop with bitcoin [spend]"))
    n = sum(db.insert_tx(c, dict(r, example=0, ext_id=f"demo-{i}")) for i, r in enumerate(rows))
    meta = {k: {"name": name, "type": "Exchange", "description": f"Demo exchange (made up), {fee * 100:.2f}% fee"}
            for k, name, fee in exs}
    meta.update(WALLETS)
    db.set_settings(c, {"demo_configured": True, "demo_wallets": meta, "other_income": p["salary"],
                        "demo_params": {k: v for k, v in p.items() if k != "start_date"},
                        "strike_job_enabled": False})
    return {"rows": n, "buys": sum(r["kind"] == "buy" for r in rows), "sells": sum(r["kind"] == "sell" for r in rows),
            "transfers": sum(r["kind"] == "transfer" for r in rows)}


def wallet_list(s):
    """Settings list for demo mode: made-up wallets only (no key material exists)."""
    return [{"id": "d-" + k, "name": m["name"], "type": m["type"], "policy": m.get("policy"), "description": m["description"],
             "input": "demo", "demo": True} for k, m in (s.get("demo_wallets") or {}).items()]
