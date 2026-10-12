from datetime import date, datetime
from zoneinfo import ZoneInfo
from app.schedule import is_due_day, next_check, should_run
from app.strike import daily_record

L = ZoneInfo("Europe/London")


def test_daily_weekly_monthly_due():
    assert is_due_day(date(2026, 10, 12), "daily")
    assert is_due_day(date(2026, 10, 12), "weekly", 0)        # Monday
    assert not is_due_day(date(2026, 10, 13), "weekly", 0)
    assert is_due_day(date(2026, 10, 18), "weekly", 6)        # Sunday
    assert is_due_day(date(2026, 10, 15), "monthly", monthday=15)
    assert not is_due_day(date(2026, 10, 14), "monthly", monthday=15)


def test_monthly_short_months_use_last_day():
    assert is_due_day(date(2026, 2, 28), "monthly", monthday=31)
    assert is_due_day(date(2028, 2, 29), "monthly", monthday=30)   # leap year
    assert not is_due_day(date(2028, 2, 28), "monthly", monthday=30)
    assert is_due_day(date(2026, 4, 30), "monthly", monthday=31)
    assert is_due_day(date(2026, 1, 31), "monthly", monthday=31)
    assert not is_due_day(date(2026, 1, 30), "monthly", monthday=31)


def test_next_check_after_buy_time():
    now = datetime(2026, 10, 12, 9, 0, tzinfo=L)
    assert next_check(now, "daily", "15:25", delay_min=10) == datetime(2026, 10, 12, 15, 35, tzinfo=L)
    # already ran today -> tomorrow
    assert next_check(now, "daily", "15:25", delay_min=10, last_run="2026-10-12").date() == date(2026, 10, 13)
    # weekly Friday
    assert next_check(now, "weekly", "08:00", weekday=4, delay_min=5) == datetime(2026, 10, 16, 8, 5, tzinfo=L)
    # monthly 31st from Nov -> Nov 30
    n = datetime(2026, 11, 2, 12, 0, tzinfo=L)
    assert next_check(n, "monthly", "10:00", monthday=31, delay_min=0) == datetime(2026, 11, 30, 10, 0, tzinfo=L)


def test_should_run():
    s = {"strike_job_enabled": True, "strike_frequency": "weekly", "strike_weekday": 0,
         "strike_job_time": "15:25", "strike_check_delay_min": 10}
    assert not should_run(datetime(2026, 10, 12, 15, 30, tzinfo=L), s)   # before buy + delay
    assert should_run(datetime(2026, 10, 12, 15, 36, tzinfo=L), s)
    assert not should_run(datetime(2026, 10, 13, 15, 36, tzinfo=L), s)   # not scheduled day
    assert not should_run(datetime(2026, 10, 12, 15, 36, tzinfo=L), {**s, "strike_last_run": "2026-10-12"})
    assert not should_run(datetime(2026, 10, 12, 15, 36, tzinfo=L), {**s, "strike_job_enabled": False})


def test_drop_uses_configured_amount():
    txs = daily_record(0.01, 0.0, 50000.0, 50.0)
    assert txs[0]["gbp"] == 50.0 and txs[0]["btc"] == 0.001 and txs[1]["btc"] == 0.011
