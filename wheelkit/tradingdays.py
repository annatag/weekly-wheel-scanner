"""The US equity-option trading calendar.

Every deadline this toolkit produces is a date you have to act on, and you
cannot act on a Sunday. "Close or roll at 21 DTE" resolved by subtracting 21
calendar days from expiry, which lands on a weekend two times in seven and on
a holiday a few times a year. The plan then read like a real instruction with
a date nobody could follow, and in practice the position was managed a day or
three late - always late, never early, because the drift is one-directional.

So deadlines resolve backwards to the nearest prior session. Acting early is
always possible; acting on a closed day is not.

The holiday set is generated from the NYSE rules rather than listed, so it
stays correct in future years without maintenance. Half days are ignored:
the market is open, and an order can be worked.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

SATURDAY = 5


def easter(year: int) -> date:
    """Gregorian Easter Sunday - the anchor for Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """The nth given weekday of a month; nth=-1 means the last one."""
    if nth < 0:
        day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 \
            else date(year, 12, 31)
        while day.weekday() != weekday:
            day -= timedelta(days=1)
        return day
    day = date(year, month, 1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day + timedelta(weeks=nth - 1)


def _observed(day: date) -> date:
    """Weekend holidays are observed Friday before or Monday after."""
    if day.weekday() == SATURDAY:
        return day - timedelta(days=1)
    if day.weekday() == SATURDAY + 1:
        return day + timedelta(days=1)
    return day


def _new_year(year: int) -> date | None:
    """New Year's Day, or None in the years the exchange does not close.

    The Saturday case is the exception to the observation rule: the market
    stays open the preceding Friday rather than shutting the last session of
    the year. It was open on 31 December 2021 for exactly this reason.
    """
    day = date(year, 1, 1)
    if day.weekday() == SATURDAY:
        return None
    return _observed(day)


@lru_cache(maxsize=None)
def market_holidays(year: int) -> frozenset[date]:
    """Full-day NYSE/Nasdaq closures for one calendar year."""
    days = {
        _nth_weekday(year, 1, 0, 3),                    # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),                    # Washington's Birthday
        easter(year) - timedelta(days=2),               # Good Friday
        _nth_weekday(year, 5, 0, -1),                   # Memorial Day
        _observed(date(year, 6, 19)),                   # Juneteenth
        _observed(date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, 0, 1),                    # Labor Day
        _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
        _observed(date(year, 12, 25)),                  # Christmas
    }
    new_year = _new_year(year)
    if new_year is not None:
        days.add(new_year)
    return frozenset(days)


def is_trading_day(day: date) -> bool:
    return day.weekday() < SATURDAY and day not in market_holidays(day.year)


def previous_trading_day(day: date, *, inclusive: bool = True) -> date:
    """The given day if the market is open, else the session before it."""
    if not inclusive:
        day -= timedelta(days=1)
    # Ten steps clears the longest run of closures the calendar can produce.
    for _ in range(10):
        if is_trading_day(day):
            return day
        day -= timedelta(days=1)
    return day


def next_trading_day(day: date, *, inclusive: bool = True) -> date:
    if not inclusive:
        day += timedelta(days=1)
    for _ in range(10):
        if is_trading_day(day):
            return day
        day += timedelta(days=1)
    return day


def deadline_for_dte(expiration: date, dte: int) -> date:
    """The last session on or before ``dte`` calendar days from expiry.

    This is the one function the rest of the toolkit should use to turn an
    "at N DTE" rule into a date. Resolving backwards rather than forwards
    keeps the deadline inside the intended window: a Monday deadline for a
    Sunday date is already a day later than the rule asked for.
    """
    return previous_trading_day(expiration - timedelta(days=max(dte, 0)))


def trading_days_between(start: date, end: date) -> int:
    """Sessions strictly after ``start`` and up to and including ``end``."""
    if end <= start:
        return 0
    count, day = 0, start + timedelta(days=1)
    while day <= end:
        if is_trading_day(day):
            count += 1
        day += timedelta(days=1)
    return count
