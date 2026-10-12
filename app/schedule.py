"""DCA schedule calculation (pure functions, UK local time)."""
import calendar
from datetime import date, datetime, time, timedelta

FREQUENCIES = ("daily", "weekly", "monthly")


def parse_time(s) -> time:
    hh, mm = map(int, str(s or "15:25").strip().split(":")[:2])
    return time(hh, mm)


def is_due_day(d: date, frequency: str = "daily", weekday: int = 0, monthday: int = 1) -> bool:
    """weekday: 0=Monday..6=Sunday. monthday: 1..31; short months use their last day."""
    if frequency == "weekly":
        return d.weekday() == int(weekday) % 7
    if frequency == "monthly":
        last = calendar.monthrange(d.year, d.month)[1]
        return d.day == min(max(int(monthday), 1), last)
    return True


def check_time(d: date, buy_time, delay_min: int, tz) -> datetime:
    """When the balance check runs on day d: shortly after the configured buy time."""
    return datetime.combine(d, parse_time(buy_time), tz) + timedelta(minutes=int(delay_min))


def next_check(now: datetime, frequency="daily", buy_time="15:25", weekday=0, monthday=1,
               delay_min=10, last_run: str | None = None) -> datetime:
    """Next scheduled balance check at or after `now` (now must be tz-aware)."""
    d = now.date()
    for _ in range(400):
        if is_due_day(d, frequency, weekday, monthday) and d.isoformat() != last_run:
            t = check_time(d, buy_time, delay_min, now.tzinfo)
            if t >= now:
                return t
            if d == now.date():
                return now  # due today and overdue: runs at the next scheduler tick
        d += timedelta(days=1)
    raise ValueError("no due day found")


def should_run(now: datetime, s: dict) -> bool:
    if not s.get("strike_job_enabled", True):
        return False
    d = now.date()
    if not is_due_day(d, s.get("strike_frequency", "daily"), s.get("strike_weekday", 0), s.get("strike_monthday", 1)):
        return False
    if s.get("strike_last_run") == d.isoformat():
        return False
    return now >= check_time(d, s.get("strike_job_time", "15:25"), s.get("strike_check_delay_min", 10), now.tzinfo)
