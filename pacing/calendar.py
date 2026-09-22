"""Flight-window arithmetic shared by all three pacing types."""
from __future__ import annotations

import calendar
import datetime as dt


def inclusive_days(start: dt.date, end: dt.date) -> int:
    """Length of a flight in days, counting both endpoints."""
    if end < start:
        return 0
    return (end - start).days + 1


def month_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


def month_window(
    as_of: dt.date, start: dt.date, end: dt.date
) -> tuple[dt.date, dt.date] | None:
    """The part of `as_of`'s calendar month that falls inside the flight.

    A flight that starts on the 17th only has 14 days of September to deliver
    its September impressions in, so the daily target for that month is higher
    than monthly/30. Returns None when the flight does not touch the month.
    """
    m_start, m_end = month_bounds(as_of)
    win_start = max(m_start, start)
    win_end = min(m_end, end)
    if win_end < win_start:
        return None
    return win_start, win_end


def elapsed_days(as_of: dt.date, start: dt.date, end: dt.date) -> int:
    """Days of the flight that have run as of `as_of`, capped at its length.

    `as_of` is the last date the delivery feed covers, and it counts as a full
    day: on day one of a flight, one day's worth of delivery is expected.
    """
    if as_of < start:
        return 0
    return min(inclusive_days(start, as_of), inclusive_days(start, end))


def months_between(start: dt.date, end: dt.date) -> int:
    """How many calendar months a flight touches, counting both ends.

    1 May to 31 Dec is eight months, not seven: a flight that touches any
    part of a month sells that month's impressions. This is the figure the
    orders export calls `months_running`, and what the hand-kept sheet
    multiplies a monthly goal by to reach a total.
    """
    if end < start:
        return 0
    return (end.year - start.year) * 12 + (end.month - start.month) + 1
