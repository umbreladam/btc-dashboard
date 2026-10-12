from datetime import date
import pytest
from app.cgt import Event, match, cgt_due, TaxSettings, sell_all_today, tax_year_of

D = date


def test_pool_only():
    r = match([Event(D(2024, 1, 1), "buy", 1, 10000), Event(D(2024, 2, 1), "buy", 1, 30000),
               Event(D(2024, 6, 1), "sell", 1, 40000)])
    d = r.disposals[0]
    assert d.cost == pytest.approx(20000) and d.gain == pytest.approx(20000)
    assert d.matches[0][0] == "s104"
    assert r.pool_btc == pytest.approx(1) and r.pool_cost == pytest.approx(20000)


def test_same_day_rule_takes_priority():
    r = match([Event(D(2024, 1, 1), "buy", 1, 10000),
               Event(D(2024, 5, 1), "buy", 1, 50000), Event(D(2024, 5, 1), "sell", 1, 52000)])
    d = r.disposals[0]
    assert d.matches == [("same-day", 1, pytest.approx(50000))]
    assert d.gain == pytest.approx(2000)
    assert r.pool_cost == pytest.approx(10000)


def test_same_day_multiple_buys_aggregated():
    r = match([Event(D(2024, 5, 1), "buy", 0.5, 20000), Event(D(2024, 5, 1), "buy", 0.5, 30000),
               Event(D(2024, 5, 1), "sell", 0.5, 30000)])
    assert r.disposals[0].cost == pytest.approx(25000)


def test_thirty_day_bed_and_breakfast():
    r = match([Event(D(2024, 1, 1), "buy", 1, 10000),
               Event(D(2024, 3, 1), "sell", 1, 30000),
               Event(D(2024, 3, 20), "buy", 1, 28000)])
    d = r.disposals[0]
    assert d.matches[0][0] == "30-day" and d.cost == pytest.approx(28000)
    assert d.gain == pytest.approx(2000)
    assert r.pool_btc == pytest.approx(1) and r.pool_cost == pytest.approx(10000)


def test_thirty_day_window_boundary():
    # Day 30 counts, day 31 does not
    r = match([Event(D(2024, 1, 1), "buy", 2, 20000), Event(D(2024, 3, 1), "sell", 1, 30000),
               Event(D(2024, 3, 31), "buy", 1, 15000)])
    assert r.disposals[0].matches[0][0] == "30-day"
    r = match([Event(D(2024, 1, 1), "buy", 2, 20000), Event(D(2024, 3, 1), "sell", 1, 30000),
               Event(D(2024, 4, 1), "buy", 1, 15000)])
    assert r.disposals[0].matches[0][0] == "s104"


def test_partial_mixed_matching():
    r = match([Event(D(2024, 1, 1), "buy", 1, 10000),
               Event(D(2024, 3, 1), "buy", 0.2, 6000),
               Event(D(2024, 3, 1), "sell", 0.5, 15000),
               Event(D(2024, 3, 10), "buy", 0.1, 3500)])
    d = r.disposals[0]
    rules = [m[0] for m in d.matches]
    assert rules == ["same-day", "30-day", "s104"]
    # 0.2 @ 6000 + 0.1 @ 3500 + 0.2 from pool (1 BTC @ 10000) = 2000
    assert d.cost == pytest.approx(6000 + 3500 + 2000)
    assert r.pool_btc == pytest.approx(0.8) and r.pool_cost == pytest.approx(8000)


def test_same_day_beats_earlier_bnb():
    # Acquisition on 10 Mar is same-day with the 10 Mar disposal, so it can't be used for 1 Mar's B&B
    r = match([Event(D(2024, 1, 1), "buy", 2, 20000),
               Event(D(2024, 3, 1), "sell", 1, 30000),
               Event(D(2024, 3, 10), "buy", 1, 25000), Event(D(2024, 3, 10), "sell", 1, 26000)])
    d1, d2 = r.disposals
    assert d2.matches[0][0] == "same-day"
    assert d1.matches[0][0] == "s104"


def test_transfers_ignored_and_oversell_raises():
    with pytest.raises(ValueError):
        match([Event(D(2024, 1, 1), "buy", 1, 100), Event(D(2024, 1, 2), "sell", 2, 100)])
    r = match([Event(D(2024, 1, 1), "buy", 1, 100), Event(D(2024, 1, 2), "transfer", 1, 0)])
    assert r.pool_btc == 1 and r.pool_cost == 100


def test_tax_rates_and_bands():
    s = TaxSettings(other_income=30000)  # taxable income 17430 -> band left 20270
    t = cgt_due(25000, s)
    assert t["taxable_gain"] == pytest.approx(22000)
    assert t["at_basic"] == pytest.approx(20270) and t["at_higher"] == pytest.approx(1730)
    assert t["tax"] == pytest.approx(20270 * .18 + 1730 * .24)
    assert cgt_due(2500, s)["tax"] == 0
    assert cgt_due(5000, TaxSettings(other_income=80000, exempt_used=1000))["tax"] == pytest.approx(3000 * .24)
    assert cgt_due(-500, s)["tax"] == 0


def test_tax_year_and_sell_all():
    assert tax_year_of(D(2026, 4, 5)) == 2025 and tax_year_of(D(2026, 4, 6)) == 2026
    ev = [Event(D(2025, 1, 1), "buy", 1, 20000)]
    t = sell_all_today(ev, D(2026, 10, 12), 60000, 1, TaxSettings(other_income=100000))
    assert t["net_gain"] == pytest.approx(40000)
    assert t["tax"] == pytest.approx(37000 * .24) and t["after_tax"] == pytest.approx(60000 - 8880)


def test_network_fee_on_own_transfer_keeps_cost():
    r = match([Event(D(2024, 1, 1), "buy", 1, 10000), Event(D(2024, 2, 1), "fee", 0.001, 0)])
    assert r.pool_btc == pytest.approx(0.999) and r.pool_cost == pytest.approx(10000)
