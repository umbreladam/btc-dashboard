"""UK Capital Gains Tax for cryptoassets using HMRC share-matching rules.

Order of matching for each disposal (HMRC Cryptoassets Manual CRYPTO22200-22260):
  1. Same-day rule: acquisitions on the same day as the disposal.
  2. 30-day ("bed & breakfast") rule: acquisitions in the 30 days AFTER the disposal (earliest first).
  3. Section 104 pool: average cost of everything else.
Transfers between your own wallets are not disposals and are ignored here; only the BTC network
fee they burn ("fee" events) reduces the pool quantity while keeping its cost.
All amounts in GBP; fees are included in acquisition cost and deducted from proceeds.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from collections import defaultdict

SATS = 100_000_000


@dataclass
class Event:
    day: date
    kind: str          # "buy" | "sell"
    btc: float
    gbp: float         # cost incl. fees (buy) or net proceeds after fees (sell)
    ref: str = ""


@dataclass
class Disposal:
    day: date
    btc: float
    proceeds: float
    cost: float = 0.0
    matches: list = field(default_factory=list)  # (rule, btc, cost)

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost


@dataclass
class Result:
    disposals: list
    pool_btc: float
    pool_cost: float


def _s(x: float) -> int:
    return int(round(x * SATS))


def match(events: list[Event]) -> Result:
    # Aggregate per day (HMRC treats all same-day acquisitions/disposals as single transactions).
    buys = defaultdict(lambda: [0, 0.0])   # day -> [sats, cost]
    sells = defaultdict(lambda: [0, 0.0])  # day -> [sats, proceeds]
    fees = defaultdict(int)                # day -> sats of on-chain network fee paid moving own coins
    for e in events:
        if e.kind == "fee":
            fees[e.day] += _s(e.btc)
            continue
        tgt = buys if e.kind == "buy" else sells if e.kind == "sell" else None
        if tgt is None:
            continue
        tgt[e.day][0] += _s(e.btc)
        tgt[e.day][1] += e.gbp
    b_rem = {d: v[0] for d, v in buys.items()}       # unmatched sats per acquisition day
    s_rem = {d: v[0] for d, v in sells.items()}
    disp = {d: Disposal(d, v[0] / SATS, v[1]) for d, v in sells.items()}

    def take(bday, sday, rule):
        q = min(b_rem.get(bday, 0), s_rem[sday])
        if q <= 0:
            return
        unit = buys[bday][1] / buys[bday][0]
        cost = unit * q
        b_rem[bday] -= q
        s_rem[sday] -= q
        disp[sday].cost += cost
        disp[sday].matches.append((rule, q / SATS, cost))

    # 1. same day (all days first: takes priority over B&B of earlier disposals)
    for d in sorted(sells):
        if d in buys:
            take(d, d, "same-day")
    # 2. 30-day rule, disposals in chronological order, acquisitions earliest first
    for d in sorted(sells):
        for k in range(1, 31):
            bd = d + timedelta(days=k)
            if s_rem[d] <= 0:
                break
            if bd in buys:
                take(bd, d, "30-day")
    # 3. Section 104 pool, chronological
    pool_s, pool_c = 0, 0.0
    for d in sorted(set(buys) | set(sells) | set(fees)):
        if d in buys and b_rem[d] > 0:
            pool_c += buys[d][1] / buys[d][0] * b_rem[d]
            pool_s += b_rem[d]
        if d in fees:
            # Network fee on a transfer between own wallets: the coins are gone but their cost stays
            # in the pool (simplified, conservative treatment; see README).
            pool_s = max(0, pool_s - fees[d])
        if d in sells and s_rem[d] > 0:
            q = s_rem[d]
            if q > pool_s:
                raise ValueError(f"Disposal on {d} exceeds holdings by {(q - pool_s) / SATS} BTC")
            cost = pool_c * q / pool_s
            pool_c -= cost
            pool_s -= q
            s_rem[d] = 0
            disp[d].cost += cost
            disp[d].matches.append(("s104", q / SATS, cost))
    return Result(sorted(disp.values(), key=lambda x: x.day), pool_s / SATS, pool_c)


def tax_year_of(d: date) -> int:
    """Returns starting year of the UK tax year (6 April - 5 April)."""
    return d.year if (d.month, d.day) >= (4, 6) else d.year - 1


@dataclass
class TaxSettings:
    other_income: float = 0.0
    personal_allowance: float = 12570
    basic_band: float = 37700
    basic_rate: float = 0.18
    higher_rate: float = 0.24
    annual_exempt: float = 3000
    exempt_used: float = 0.0
    taper_pa: bool = True   # PA reduced £1 per £2 of income over £100k


def cgt_due(net_gain: float, s: TaxSettings) -> dict:
    pa = s.personal_allowance
    if s.taper_pa and s.other_income > 100_000:
        pa = max(0.0, pa - (s.other_income - 100_000) / 2)
    taxable_income = max(0.0, s.other_income - pa)
    band_left = max(0.0, s.basic_band - taxable_income)
    aea_left = max(0.0, s.annual_exempt - s.exempt_used)
    taxable = max(0.0, net_gain - aea_left)
    at_basic = min(taxable, band_left)
    at_higher = taxable - at_basic
    tax = at_basic * s.basic_rate + at_higher * s.higher_rate
    return dict(net_gain=net_gain, exempt_available=aea_left, taxable_gain=taxable,
                basic_band_left=band_left, at_basic=at_basic, at_higher=at_higher, tax=tax)


def sell_all_today(events: list[Event], today: date, price_gbp: float, holdings_btc: float,
                   s: TaxSettings, fee_pct: float = 0.0) -> dict:
    """Hypothetical: dispose of all holdings today. Includes this tax year's realised gains."""
    proceeds = holdings_btc * price_gbp * (1 - fee_pct)
    evs = list(events)
    if holdings_btc > 0:
        evs.append(Event(today, "sell", holdings_btc, proceeds, "hypothetical"))
    res = match(evs)
    ty = tax_year_of(today)
    net = sum(d.gain for d in res.disposals if tax_year_of(d.day) == ty)
    realised = sum(d.gain for d in res.disposals if tax_year_of(d.day) == ty and d.day != today) if holdings_btc else net
    t = cgt_due(net, s)
    t.update(proceeds=proceeds, after_tax=proceeds - t["tax"], realised_this_year=realised,
             tax_year=f"{ty}/{str(ty + 1)[2:]}")
    return t
