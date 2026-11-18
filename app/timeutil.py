"""UTC minute / day conversion helpers.

Time is stored everywhere as *Unix epoch minutes* (integer). A UTC day boundary
is therefore ``day * 1440``; a day is the half-open interval
``[day*1440, (day+1)*1440)`` in minute space.
"""
from __future__ import annotations

from datetime import datetime, timezone

MINUTES_PER_DAY = 1440


class TimeFormatError(ValueError):
    """Raised when a time string cannot be parsed as an epoch minute."""


def parse_utc_minute(value: object) -> int:
    """Parse ``value`` into an integer Unix epoch minute.

    Accepts:
      * integers / numeric strings -> already epoch minutes
      * ISO-8601 strings ending in ``Z`` or with an explicit ``+00:00``
        offset; the resulting timestamp must land on a whole minute.
    Naive timestamps are rejected on purpose: the contract requires UTC.
    """
    if isinstance(value, bool):  # bool is a subclass of int, reject it
        raise TimeFormatError(f"unsupported time value: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise TimeFormatError(f"epoch minute must be whole number: {value!r}")
        return int(value)

    text = str(value).strip()
    if not text:
        raise TimeFormatError("empty time value")

    # Numeric string -> epoch minute.
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        if not number.is_integer():
            raise TimeFormatError(f"epoch minute must be whole number: {text!r}")
        return int(number)

    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise TimeFormatError(f"unparseable UTC timestamp: {text!r}") from exc
    if dt.tzinfo is None:
        raise TimeFormatError(
            f"timestamp must be UTC (use 'Z' or '+00:00'): {text!r}"
        )
    dt = dt.astimezone(timezone.utc)
    if dt.second != 0 or dt.microsecond != 0:
        raise TimeFormatError(f"timestamp must be on a whole minute: {text!r}")
    return int(dt.timestamp()) // 60


def minute_to_iso(minute: int) -> str:
    """Render an epoch minute as an ISO-8601 UTC string ``YYYY-MM-DDTHH:MMZ``."""
    dt = datetime.fromtimestamp(minute * 60, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def day_of_minute(minute: int) -> int:
    """Return the UTC day index (floor division by 1440)."""
    return minute // MINUTES_PER_DAY


def day_to_iso(day: int) -> str:
    """Render a UTC day index as ``YYYY-MM-DD``."""
    dt = datetime.fromtimestamp(day * MINUTES_PER_DAY * 60, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def parse_day(value: object) -> int:
    """Parse a ``YYYY-MM-DD`` UTC date string (or numeric day index)."""
    if isinstance(value, bool):
        raise TimeFormatError(f"unsupported day value: {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        if not number.is_integer():
            raise TimeFormatError(f"day index must be whole number: {text!r}")
        return int(number)
    try:
        dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise TimeFormatError(f"day must be YYYY-MM-DD (UTC): {text!r}") from exc
    # day index = epoch minutes / 1440; timestamp() is in seconds.
    return (int(dt.timestamp()) // 60) // MINUTES_PER_DAY


def day_bounds(day: int) -> tuple[int, int]:
    """Return the half-open ``[start_minute, end_minute)`` bounds of a day."""
    start = day * MINUTES_PER_DAY
    return start, start + MINUTES_PER_DAY


def days_in_span(start_minute: int, end_minute: int) -> list[int]:
    """All UTC day indices overlapped by a half-open minute interval."""
    if end_minute <= start_minute:
        return []
    first = day_of_minute(start_minute)
    last = day_of_minute(end_minute - 1)
    return list(range(first, last + 1))
